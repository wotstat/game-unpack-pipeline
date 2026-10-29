from __future__ import annotations

import base64
import hashlib
import multiprocessing
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from game_downloader.acquisition import ArtifactCorruptError
from game_downloader.delivery import ArtifactDownloader, DownloadPolicy
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    AcquisitionPlan,
    DiskSpaceEstimate,
    PartAcquisition,
    PartName,
    SourceHash,
    TorrentDescriptorRecord,
)
from game_downloader.torrent import bytes_path_from_text, parse_torrent
from game_downloader.workspace import Workspace


class _Source(ThreadingHTTPServer):
    data: dict[str, bytes]
    slow_paths: set[str]
    wait_for: tuple[str, ...]
    started: dict[str, threading.Event]
    ended: dict[str, threading.Event]
    sent: dict[str, int]
    requests: list[tuple[str, str | None]]
    disconnect: bool
    disconnected: bool
    lock: threading.Lock


class _Handler(BaseHTTPRequestHandler):
    server: _Source

    def do_HEAD(self) -> None:
        self.send_response(200)
        self._headers(len(self.server.data[self.path]))

    def _headers(self, size: int) -> None:
        self.send_header("Content-Length", str(size))
        self.send_header("ETag", '"stable"')
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:
        payload = self.server.data[self.path]
        raw_range = self.headers.get("Range")
        start, end = 0, len(payload) - 1
        if raw_range is not None:
            left, right = raw_range.removeprefix("bytes=").split("-")
            start = int(left)
            end = int(right) if right else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        else:
            self.send_response(200)
        self._headers(end - start + 1)
        with self.server.lock:
            self.server.requests.append((self.path, raw_range))
            disconnect = (
                self.server.disconnect
                and self.path == "/client.bin"
                and not self.server.disconnected
            )
            if disconnect:
                self.server.disconnected = True
        self.server.started[self.path].set()
        if self.path == "/locale.bin":
            for path in self.server.wait_for:
                assert self.server.started[path].wait(10)
        try:
            if disconnect:
                prefix = max(1, (end - start + 1) // 2)
                self.wfile.write(payload[start : start + prefix])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if self.path in self.server.slow_paths:
                for offset in range(start, end + 1, 4096):
                    chunk = payload[offset : min(end + 1, offset + 4096)]
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    with self.server.lock:
                        self.server.sent[self.path] += len(chunk)
                    time.sleep(0.01)
            else:
                self.wfile.write(payload[start : end + 1])
        except (OSError, ssl.SSLError):
            pass
        finally:
            self.server.ended[self.path].set()

    def log_message(self, _format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("http-process-tls")
    certificate, key = root / "certificate.pem", root / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    return certificate, key


@contextmanager
def source(
    tls_files: tuple[Path, Path],
    data: dict[str, bytes],
    *,
    disconnect: bool = False,
    slow_paths: set[str] | None = None,
    wait_for: tuple[str, ...] = (),
) -> Iterator[_Source]:
    server = _Source(("127.0.0.1", 0), _Handler)
    server.data = data
    server.slow_paths = slow_paths or set()
    server.wait_for = wait_for
    server.started = {path: threading.Event() for path in data}
    server.ended = {path: threading.Event() for path in data}
    server.sent = dict.fromkeys(data, 0)
    server.requests = []
    server.disconnect = disconnect
    server.disconnected = False
    server.lock = threading.Lock()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*tls_files)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def plan(workspace: Workspace, server: _Source, *, corrupt_locale: bool = False) -> AcquisitionPlan:
    raw = base64.b64decode(
        (Path(__file__).parent / "fixtures/torrent/reference-multifile.torrent.b64").read_text()
    )
    commit = workspace.blobs.put_bytes(raw)
    descriptor = TorrentDescriptorRecord(
        descriptor_sha256=commit.sha256,
        source_urls=("https://fixture.invalid/data.torrent",),
        fetched_url="https://fixture.invalid/data.torrent",
        final_url="https://fixture.invalid/data.torrent",
        blob_sha256=commit.sha256,
        blob_size=len(raw),
        blob_path=commit.relative_path,
        metainfo=parse_torrent(raw),
    )
    parts = []
    for name, language, path in (
        (PartName.CLIENT, None, "/client.bin"),
        (PartName.SD_CONTENT, None, "/sd.bin"),
        (PartName.LOCALE, "EN", "/locale.bin"),
    ):
        payload = server.data[path]
        digest = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
        artifact = AcquisitionArtifact(
            artifact_id="sha256:" + hashlib.sha256(path.encode()).hexdigest(),
            role="client-file",
            part=name,
            language=language,
            part_version="1",
            acquisition_mode=AcquisitionMode.REFERENCE,
            path=bytes_path_from_text(path[1:]),
            size=len(payload),
            source_hash=SourceHash(
                algorithm="sha1",
                value="0" * 40 if corrupt_locale and name is PartName.LOCALE else digest,
            ),
            source_urls=(f"https://127.0.0.1:{server.server_port}{path}",),
            torrent_descriptor_sha256=commit.sha256,
        )
        parts.append(
            PartAcquisition(
                part=name,
                language=language,
                version="1",
                acquisition_mode=AcquisitionMode.REFERENCE,
                artifacts=(artifact,),
                torrent_descriptor_sha256s=(commit.sha256,),
            )
        )
    total = sum(len(body) for body in server.data.values())
    return AcquisitionPlan(
        resolve_result_sha256="sha256:" + "0" * 64,
        parts=tuple(parts),
        descriptors=(descriptor,),
        disk_space=DiskSpaceEstimate(
            descriptor_bytes=len(raw),
            download_bytes=total,
            assembled_bytes=total,
            reserve_bytes=0,
            required_free_bytes=len(raw) + 2 * total,
        ),
    )


@pytest.mark.parametrize("ranged", [False, True], ids=["http-resume", "parallel-range"])
@pytest.mark.parametrize("duplicate_payloads", [False, True], ids=["distinct-CAS", "shared-CAS"])
def test_process_download_preserves_https_hashes_resume_cache_and_global_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tls_files: tuple[Path, Path],
    ranged: bool,
    duplicate_payloads: bool,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files[0]))
    data = {
        "/client.bin": b"client" * 65536,
        "/sd.bin": b"sd" * 32768,
        "/locale.bin": b"locale" * 1024,
    }
    if duplicate_payloads:
        data["/sd.bin"] = data["/client.bin"]
    messages: list[str] = []
    workspace = Workspace(tmp_path)
    workspace.initialize()
    before = {child.pid for child in multiprocessing.active_children()}
    with source(tls_files, data, disconnect=True) as server:
        pinned = plan(workspace, server)
        downloader = ArtifactDownloader(
            DownloadPolicy(
                max_workers=4,
                process_workers=2,
                parallel_range_minimum_bytes=64 * 1024 if ranged else 512 * 1024 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=2,
            ),
            messages.append,
        )
        result = downloader.download(pinned, workspace)
        assert all(item.source_hash_verified for item in result.artifacts)
        assert len({item.blob_sha256 for item in result.artifacts}) == (
            2 if duplicate_payloads else 3
        )
        assert result.downloaded_bytes == sum(map(len, data.values()))
        for item in result.artifacts:
            assert (
                workspace.blobs.path_for(item.blob_sha256).read_bytes()
                == data["/" + str(item.artifact.path.utf8)]
            )
        assert any(
            path == "/client.bin" and requested is not None for path, requested in server.requests
        )
        requests = list(server.requests)
        cached = downloader.download(pinned, workspace)
        assert cached.reused_artifacts == 3
        assert server.requests == requests
    assert any("100.0%" in message for message in messages)
    assert {child.pid for child in multiprocessing.active_children()} <= before


def test_process_failure_cancels_other_process_and_keeps_original_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tls_files: tuple[Path, Path],
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files[0]))
    data = {
        "/client.bin": b"c" * 256 * 1024,
        "/sd.bin": b"s" * 8 * 1024 * 1024,
        "/locale.bin": b"l" * 32768,
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    before = {child.pid for child in multiprocessing.active_children()}
    with source(
        tls_files, data, slow_paths={"/client.bin", "/sd.bin"}, wait_for=("/client.bin", "/sd.bin")
    ) as server:
        pinned = plan(workspace, server, corrupt_locale=True)
        with pytest.raises(ArtifactCorruptError, match="all complete sources were corrupt"):
            ArtifactDownloader(
                DownloadPolicy(
                    max_workers=4, process_workers=2, http_attempts_per_url=1, attempts_per_url=1
                )
            ).download(pinned, workspace)
        assert server.started["/client.bin"].is_set()
        assert server.started["/sd.bin"].is_set()
        assert server.sent["/sd.bin"] < len(data["/sd.bin"])
        assert server.sent["/client.bin"] < len(data["/client.bin"])
    assert {child.pid for child in multiprocessing.active_children()} <= before


def test_process_progress_observer_failure_stops_and_joins_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tls_files: tuple[Path, Path],
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files[0]))
    data = {
        "/client.bin": b"c" * 256 * 1024,
        "/sd.bin": b"s" * 128 * 1024,
        "/locale.bin": b"l" * 32768,
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    before = {child.pid for child in multiprocessing.active_children()}

    def observer(message: str) -> None:
        if message.startswith("Download progress:"):
            raise ValueError("fixture observer failure")

    with source(tls_files, data) as server:
        pinned = plan(workspace, server)
        with pytest.raises(ValueError, match="fixture observer failure"):
            ArtifactDownloader(DownloadPolicy(max_workers=4, process_workers=2), observer).download(
                pinned, workspace
            )
    assert {child.pid for child in multiprocessing.active_children()} <= before

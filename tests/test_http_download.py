from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
from game_downloader.pipeline import StageExecutionError
from game_downloader.torrent import bytes_path_from_text, parse_torrent
from game_downloader.workspace import Workspace


class Source(ThreadingHTTPServer):
    data: dict[str, bytes]
    etag: str | None
    disconnect: bool
    disconnected: bool
    stall: bool
    invalid_ranges: bool
    advertise_ranges: bool
    requests: list[tuple[str, str | None, str | None]]
    lock: threading.Lock
    chunk_delay: float
    chunk_bytes: int
    last_modified: str | None
    range_trickle: bool
    no_head: bool
    get_error: bool
    release: threading.Event


class Handler(BaseHTTPRequestHandler):
    server: Source

    def do_HEAD(self) -> None:
        if self.server.no_head:
            self.send_response(405)
            self.headers_for(0)
            return
        self.send_response(200)
        self.headers_for(len(self.server.data[self.path.split("?", 1)[0]]))

    def headers_for(self, length: int) -> None:
        self.send_header("Content-Length", str(length))
        if self.server.etag is not None:
            self.send_header("ETag", self.server.etag)
        if self.server.last_modified is not None:
            self.send_header("Last-Modified", self.server.last_modified)
        if self.server.advertise_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        data = self.server.data[path]
        raw_range = self.headers.get("Range")
        validator = (
            self.headers.get("If-Match")
            or self.headers.get("If-Range")
            or self.headers.get("If-Unmodified-Since")
        )
        with self.server.lock:
            self.server.requests.append((path, raw_range, validator))
            disconnect = (
                path == "/client.bin" and self.server.disconnect and not self.server.disconnected
            )
            if disconnect:
                self.server.disconnected = True
        if self.server.get_error:
            self.send_response(503)
            self.headers_for(0)
            return
        if self.headers.get("If-Match") is not None and validator != self.server.etag:
            self.send_response(412)
            self.headers_for(0)
            return
        if (
            self.headers.get("If-Unmodified-Since") is not None
            and validator != self.server.last_modified
        ):
            self.send_response(412)
            self.headers_for(0)
            return
        start, end = 0, len(data) - 1
        if raw_range is not None:
            left, right = raw_range.removeprefix("bytes=").split("-", 1)
            start = int(left)
            end = min(int(right), end) if right else end
            if not 0 <= start <= end < len(data):
                self.send_response(416)
                self.headers_for(0)
                return
            self.send_response(206)
            declared = start + 1 if self.server.invalid_ranges else start
            self.send_header("Content-Range", f"bytes {declared}-{end}/{len(data)}")
        else:
            self.send_response(200)
        self.headers_for(end - start + 1)
        if self.server.stall and path == "/client.bin":
            self.server.release.wait(10)
            return
        with suppress(OSError):
            if self.server.range_trickle and path == "/client.bin" and raw_range is not None:
                for offset in range(start, end + 1, 16):
                    self.wfile.write(data[offset : min(offset + 16, end + 1)])
                    self.wfile.flush()
                    time.sleep(0.01)
                return
            if disconnect:
                self.wfile.write(data[start : start + max(1, (end - start + 1) // 2)])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if self.server.chunk_delay and path == "/client.bin":
                width = self.server.chunk_bytes
                for offset in range(start, end + 1, width):
                    self.wfile.write(data[offset : min(offset + width, end + 1)])
                    self.wfile.flush()
                    time.sleep(self.server.chunk_delay)
            else:
                self.wfile.write(data[start : end + 1])

    def log_message(self, _format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("native-http-tls")
    cert, key = root / "certificate.pem", root / "key.pem"
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
            str(cert),
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
    return cert, key


@contextmanager
def source(certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[Source]:
    server = Source(("127.0.0.1", 0), Handler)
    server.data = {
        "/client.bin": b"client-" * 300_000,
        "/sd.bin": b"sd-content",
        "/locale.bin": b"locale",
    }
    server.etag = '"version-1"'
    server.disconnect = server.disconnected = server.stall = server.invalid_ranges = False
    server.advertise_ranges = True
    server.chunk_delay = 0
    server.chunk_bytes = 16384
    server.last_modified = None
    server.range_trickle = False
    server.no_head = server.get_error = False
    server.release = threading.Event()
    server.requests = []
    server.lock = threading.Lock()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*certificate)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate[0]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def plan(workspace: Workspace, server: Source, *, hashes: bool = True) -> AcquisitionPlan:
    raw = base64.b64decode(
        (Path(__file__).parent / "fixtures/torrent/reference-multifile.torrent.b64").read_text()
    )
    commit = workspace.blobs.put_bytes(raw)
    descriptor = TorrentDescriptorRecord(
        descriptor_sha256=commit.sha256,
        source_urls=("https://fixture.invalid/source.torrent",),
        fetched_url="https://fixture.invalid/source.torrent",
        final_url="https://fixture.invalid/source.torrent",
        blob_sha256=commit.sha256,
        blob_size=len(raw),
        blob_path=commit.relative_path,
        metainfo=parse_torrent(raw),
    )
    parts = []
    for part, language, path in (
        (PartName.CLIENT, None, "/client.bin"),
        (PartName.SD_CONTENT, None, "/sd.bin"),
        (PartName.LOCALE, "EN", "/locale.bin"),
    ):
        payload = server.data[path]
        artifact = AcquisitionArtifact(
            artifact_id="sha256:" + hashlib.sha256(path.encode()).hexdigest(),
            role="client-file",
            part=part,
            language=language,
            part_version="1",
            acquisition_mode=AcquisitionMode.REFERENCE,
            path=bytes_path_from_text(path[1:]),
            size=len(payload),
            source_hash=SourceHash(
                algorithm="sha1", value=hashlib.sha1(payload, usedforsecurity=False).hexdigest()
            )
            if hashes
            else None,
            source_urls=(f"https://127.0.0.1:{server.server_port}{path}",),
            torrent_descriptor_sha256=commit.sha256,
        )
        parts.append(
            PartAcquisition(
                part=part,
                language=language,
                version="1",
                acquisition_mode=AcquisitionMode.REFERENCE,
                artifacts=(artifact,),
                torrent_descriptor_sha256s=(commit.sha256,),
            )
        )
    total = sum(len(x) for x in server.data.values())
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


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(*args, **kwargs)
        if Path(args[0][0]).name == "aria2c":
            children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    return children


def policy(**values: Any) -> DownloadPolicy:
    return DownloadPolicy(
        max_workers=3,
        http_attempts_per_url=2,
        http_retry_backoff_seconds=0,
        http_retry_max_backoff_seconds=0,
        **values,
    )


def assert_bytes(result: Any, workspace: Workspace, server: Source) -> None:
    for item in result.artifacts:
        expected = server.data["/" + item.artifact.path.utf8]
        assert workspace.blobs.path_for(item.blob_sha256).read_bytes() == expected
        assert item.blob_sha256 == hashlib.sha256(expected).hexdigest()


def test_http_uses_one_native_process_and_revalidates_cached_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    messages: list[str] = []
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server)
        result = ArtifactDownloader(policy(), messages.append).download(selected, workspace)
        assert_bytes(result, workspace, server)
        before = len(server.requests)
        assert ArtifactDownloader(policy()).download(selected, workspace).reused_artifacts == 3
        assert len(server.requests) == before
    assert len(processes) == 1, "HTTP payload must go through one shared aria2 process"
    assert all(child.poll() is not None for child in processes)
    assert "(100.0%)" in messages[-1]


def test_http_rejects_a_tampered_cached_cas_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server)
        result = ArtifactDownloader(policy()).download(selected, workspace)
        path = workspace.blobs.path_for(result.artifacts[0].blob_sha256)
        path.chmod(0o644)
        with path.open("r+b") as output:
            output.write(b"X")
        path.chmod(0o444)
        before = len(server.requests)
        with pytest.raises(ArtifactCorruptError, match="SHA-256"):
            ArtifactDownloader(policy()).download(selected, workspace)
        assert len(server.requests) == before
    assert len(processes) == 1


@pytest.mark.parametrize("segmented", [False, True], ids=["sequential", "sparse-ranges"])
def test_http_resumes_native_control_file_after_interrupted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
    segmented: bool,
) -> None:
    workspace = Workspace(tmp_path)
    selected_policy = DownloadPolicy(
        max_workers=1,
        http_attempts_per_url=1,
        http_retry_backoff_seconds=0,
        parallel_range_minimum_bytes=1024 * 1024 if segmented else 512 * 1024 * 1024,
        parallel_range_target_bytes=1024 * 1024,
        parallel_range_max_segments=4,
    )
    with source(certificate, monkeypatch) as server:
        if segmented:
            server.data["/client.bin"] = b"segmented-" * 900_000
            server.chunk_delay = 0.02
        else:
            server.disconnect = True
        selected = plan(workspace, server)

        def interrupt(message: str) -> None:
            if segmented and message.startswith("Download progress:"):
                raise RuntimeError("simulated cancellation")

        with pytest.raises(RuntimeError if segmented else StageExecutionError):
            ArtifactDownloader(selected_policy, interrupt).download(selected, workspace)
        assert list(workspace.partial_root.rglob("payload.part.aria2"))
        before = len(server.requests)
        result = ArtifactDownloader(selected_policy).download(selected, workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.resumed_from > 0
        ranges = [r for p, r, _ in server.requests[before:] if p == "/client.bin"]
        assert None not in ranges, "A native resume must not restart a fresh whole-file GET"
        assert any(r is not None and not r.startswith("bytes=0-") for r in ranges)
    assert all(child.poll() is not None for child in processes)


@pytest.mark.parametrize(
    "validator", ['"changed"', None], ids=["changed-validator", "no-validator"]
)
def test_http_restarts_partial_when_source_cannot_be_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    validator: str | None,
) -> None:
    workspace = Workspace(tmp_path)
    selected_policy = DownloadPolicy(max_workers=1, http_attempts_per_url=1)
    with source(certificate, monkeypatch) as server:
        if validator is None:
            server.etag = None
        server.disconnect = True
        selected = plan(workspace, server, hashes=False)
        with pytest.raises(StageExecutionError):
            ArtifactDownloader(selected_policy).download(selected, workspace)
        server.etag = validator
        server.data["/client.bin"] = b"N" * len(server.data["/client.bin"])
        before = len(server.requests)
        result = ArtifactDownloader(selected_policy).download(selected, workspace)
        assert_bytes(result, workspace, server)
        assert all(
            r is None or r.startswith("bytes=0-")
            for p, r, _ in server.requests[before:]
            if p == "/client.bin"
        )


def test_http_never_adopts_sparse_partial_without_its_native_control_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path]
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server, hashes=False)
        artifact = selected.parts[0].artifacts[0]
        partial = workspace.partial_root / artifact.artifact_id.removeprefix("sha256:")
        partial.mkdir(parents=True)
        with (partial / "payload.part").open("wb") as output:
            output.write(b"bad-prefix")
            output.truncate(artifact.size)
        (partial / "state.json").write_text(
            json.dumps(
                {
                    "artifact_id": artifact.artifact_id,
                    "url": artifact.source_urls[0],
                    "bytes_written": 64,
                    "etag": server.etag,
                    "native_control": True,
                    "confirmed_complete": False,
                }
            )
        )
        result = ArtifactDownloader(policy()).download(selected, workspace)
        assert_bytes(result, workspace, server)
        assert any(p == "/client.bin" for p, _, _ in server.requests)


def test_http_restarts_partial_from_a_previous_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path]
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server)
        artifact = selected.parts[0].artifacts[0]
        partial = workspace.partial_root / artifact.artifact_id.removeprefix("sha256:")
        partial.mkdir(parents=True)
        prefix = server.data["/client.bin"][: 512 * 1024]
        (partial / "payload.part").write_bytes(prefix)
        (partial / "state.json").write_text(
            json.dumps(
                {
                    "artifact_id": artifact.artifact_id,
                    "url": artifact.source_urls[0],
                    "bytes_written": len(prefix),
                    "etag": server.etag,
                }
            )
        )
        result = ArtifactDownloader(policy()).download(selected, workspace)
        assert_bytes(result, workspace, server)
        assert all(
            r is None or r.startswith("bytes=0-")
            for p, r, _ in server.requests
            if p == "/client.bin"
        )


def test_http_rejects_corruption_and_stops_its_native_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server)
        server.data["/client.bin"] = b"X" * len(server.data["/client.bin"])
        with pytest.raises(ArtifactCorruptError):
            ArtifactDownloader(policy()).download(selected, workspace)
    assert processes and all(child.poll() is not None for child in processes)
    assert not list(workspace.partial_root.rglob("payload.part.aria2"))


def test_http_healthy_slow_stream_warns_without_being_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path]
) -> None:
    workspace = Workspace(tmp_path)
    messages: list[str] = []
    with source(certificate, monkeypatch) as server:
        server.chunk_delay = 0.085
        selected = plan(workspace, server)
        result = ArtifactDownloader(
            policy(
                http_throughput_window_seconds=10,
                http_minimum_throughput_bytes_per_second=1024 * 1024,
            ),
            messages.append,
        ).download(selected, workspace)
        assert_bytes(result, workspace, server)
    assert any("HTTP forward progress averaged" in message for message in messages)


def test_http_trickle_bytes_keep_useful_progress_deadline_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path]
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.data["/client.bin"] = bytes(range(256))
        server.chunk_delay = 0.05
        server.chunk_bytes = 16
        selected = plan(workspace, server)
        result = ArtifactDownloader(policy(http_no_progress_timeout_seconds=0.4)).download(
            selected, workspace
        )
        assert_bytes(result, workspace, server)


def test_http_sub_block_range_progress_falls_back_without_a_false_idle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path]
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.data["/client.bin"] = b"slow-ranges-" * 200_000
        server.range_trickle = True
        selected = plan(workspace, server)
        result = ArtifactDownloader(
            policy(
                http_no_progress_timeout_seconds=0.8,
                parallel_range_minimum_bytes=1024 * 1024,
                parallel_range_target_bytes=1024 * 1024,
            )
        ).download(selected, workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.parallel_segments == 1
        assert client.transport.parallel_range_fallbacks


@pytest.mark.parametrize("mode", ["no-ranges", "no-validator", "no-head"])
def test_http_handles_missing_server_metadata_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path], mode: str
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.no_head = mode == "no-head"
        server.advertise_ranges = mode != "no-ranges"
        server.etag = None if mode == "no-validator" else server.etag
        selected = plan(workspace, server)
        result = ArtifactDownloader(
            policy(
                parallel_range_minimum_bytes=1024 * 1024, parallel_range_target_bytes=1024 * 1024
            )
        ).download(selected, workspace)
        assert_bytes(result, workspace, server)
        if mode != "no-head":
            client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
            assert client.transport.parallel_segments == 1
            assert client.transport.parallel_range_fallbacks


def test_http_failure_never_logs_uri_credentials_or_provider_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    messages: list[str] = []
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server)
        parts = tuple(
            p.model_copy(
                update={
                    "artifacts": tuple(
                        a.model_copy(
                            update={"source_urls": (a.source_urls[0] + "?token=unit-url-secret",)}
                        )
                        for a in p.artifacts
                    )
                }
            )
            for p in selected.parts
        )
        selected = selected.model_copy(update={"parts": parts})
        server.get_error = True
        with pytest.raises(StageExecutionError) as caught:
            ArtifactDownloader(policy(), messages.append).download(selected, workspace)
    assert "unit-url-secret" not in str(caught.value) + "\n".join(messages)
    assert all("unit-url-secret" not in str(child.args) for child in processes)
    assert all(child.poll() is not None for child in processes)


def test_http_resumes_with_last_modified_when_strong_etag_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certificate: tuple[Path, Path]
) -> None:
    workspace = Workspace(tmp_path)
    selected_policy = DownloadPolicy(max_workers=1, http_attempts_per_url=1)
    with source(certificate, monkeypatch) as server:
        server.etag = None
        server.last_modified = "Sun, 01 Jan 2023 00:00:00 GMT"
        server.disconnect = True
        selected = plan(workspace, server)
        with pytest.raises(StageExecutionError):
            ArtifactDownloader(selected_policy).download(selected, workspace)
        result = ArtifactDownloader(selected_policy).download(selected, workspace)
        assert_bytes(result, workspace, server)
        assert any(
            r is not None and not r.startswith("bytes=0-") and v == server.last_modified
            for p, r, v in server.requests
            if p == "/client.bin"
        )


def test_http_read_timeout_is_bounded_separately_from_useful_progress_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.stall = True
        selected = plan(workspace, server)
        started = time.monotonic()
        with pytest.raises(StageExecutionError):
            ArtifactDownloader(
                DownloadPolicy(
                    max_workers=1,
                    http_attempts_per_url=1,
                    read_timeout_seconds=1,
                    http_no_progress_timeout_seconds=60,
                )
            ).download(selected, workspace)
        assert time.monotonic() - started < 4
    assert processes and all(child.poll() is not None for child in processes)


def test_http_callback_failure_stops_all_native_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)

    def observer(message: str) -> None:
        if message.startswith("Download progress:"):
            raise RuntimeError("observer failure")

    with source(certificate, monkeypatch) as server:
        server.chunk_delay = 0.05
        selected = plan(workspace, server)
        with pytest.raises(RuntimeError, match="observer failure"):
            ArtifactDownloader(policy(progress_interval_seconds=0.1), observer).download(
                selected, workspace
            )
    assert processes and all(child.poll() is not None for child in processes)


def test_http_useful_progress_deadline_stops_a_stalled_native_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.stall = True
        selected = plan(workspace, server)
        started = time.monotonic()
        with pytest.raises(StageExecutionError, match="no forward progress"):
            ArtifactDownloader(policy(http_no_progress_timeout_seconds=0.4)).download(
                selected, workspace
            )
        assert time.monotonic() - started < 2.5
    assert processes and all(child.poll() is not None for child in processes)


@pytest.mark.parametrize("bad_ranges", [False, True], ids=["valid-ranges", "invalid-content-range"])
def test_http_parallel_ranges_preserve_bytes_and_fall_back_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    bad_ranges: bool,
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.data["/client.bin"] = b"range-content" * 700_000
        server.invalid_ranges = bad_ranges
        selected = plan(workspace, server)
        result = ArtifactDownloader(
            policy(
                parallel_range_minimum_bytes=1024 * 1024,
                parallel_range_target_bytes=1024 * 1024,
                parallel_range_max_segments=4,
            )
        ).download(selected, workspace)
        assert_bytes(result, workspace, server)
        assert any(r is not None for p, r, _ in server.requests if p == "/client.bin")

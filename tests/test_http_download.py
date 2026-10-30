from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from game_downloader.acquisition import ArtifactCorruptError
from game_downloader.aria2_rpc import Aria2Session
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
    disconnect_after_bytes: int
    stall: bool
    stall_metadata: bool
    head_delay: float
    invalid_ranges: bool
    advertise_ranges: bool
    requests: list[tuple[str, str | None, str | None]]
    body_writes: list[tuple[str, str | None, int]]
    lock: threading.Lock
    chunk_delay: float
    chunk_bytes: int
    last_modified: str | None
    head_last_modified: str | None
    range_trickle: bool
    no_head: bool
    get_error: bool
    transient_errors: int
    get_status_error: int
    reject_tiny_range: bool
    release: threading.Event


class Handler(BaseHTTPRequestHandler):
    server: Source

    def do_HEAD(self) -> None:
        if self.server.head_delay:
            self.server.release.wait(self.server.head_delay)
        if (
            self.server.stall_metadata
            and not self.server.no_head
            and self.path.split("?", 1)[0] == "/client.bin"
        ):
            self.server.release.wait(10)
            return
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
        modified = self.server.last_modified
        if self.command == "HEAD" and self.server.head_last_modified is not None:
            modified = self.server.head_last_modified
        if modified is not None:
            self.send_header("Last-Modified", modified)
        if self.server.advertise_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        data = self.server.data[path]
        raw_range = self.headers.get("Range")
        if self.server.stall_metadata and raw_range == "bytes=0-0":
            self.server.release.wait(10)
            return
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
            retry_error = path == "/client.bin" and self.server.transient_errors > 0
            if retry_error:
                self.server.transient_errors -= 1
        if self.server.reject_tiny_range and raw_range == "bytes=0-0":
            self.send_response(416)
            self.headers_for(0)
            return
        if self.server.get_error or retry_error or self.server.get_status_error:
            self.send_response(self.server.get_status_error or 503)
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
            if (
                path == "/client.bin"
                and self.server.disconnect_after_bytes
                and end - start + 1 > self.server.disconnect_after_bytes
            ):
                self.wfile.write(data[start : start + self.server.disconnect_after_bytes])
                self.wfile.flush()
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                return
            if self.server.chunk_delay and path == "/client.bin":
                width = self.server.chunk_bytes
                for offset in range(start, end + 1, width):
                    chunk = data[offset : min(offset + width, end + 1)]
                    self.wfile.write(chunk)
                    with self.server.lock:
                        self.server.body_writes.append((path, raw_range, len(chunk)))
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
    server.disconnect_after_bytes = 0
    server.stall_metadata = False
    server.head_delay = 0
    server.advertise_ranges = True
    server.chunk_delay = 0
    server.chunk_bytes = 16384
    server.last_modified = None
    server.head_last_modified = None
    server.range_trickle = False
    server.no_head = server.get_error = False
    server.transient_errors = 0
    server.get_status_error = 0
    server.reject_tiny_range = False
    server.release = threading.Event()
    server.requests = []
    server.body_writes = []
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


@pytest.mark.parametrize("request_timeout", [None, 0.1], ids=["busy-process", "caller-deadline"])
def test_http_control_tolerates_a_short_native_process_stall(
    tmp_path: Path, processes: list[subprocess.Popen[bytes]], request_timeout: float | None
) -> None:
    with Aria2Session(
        tmp_path, executable="aria2c", workers=16, connect_timeout=2, read_timeout=6
    ) as session:
        child = processes[-1]

        def resume() -> None:
            with suppress(ProcessLookupError):
                os.kill(child.pid, signal.SIGCONT)

        os.kill(child.pid, signal.SIGSTOP)
        timer = threading.Timer(2.5, resume)
        timer.start()
        try:
            if request_timeout is not None:
                with pytest.raises(StageExecutionError, match="getVersion: ReadTimeout"):
                    session.call("getVersion", timeout=request_timeout)
            else:
                with ThreadPoolExecutor(max_workers=16) as executor:
                    results = list(executor.map(lambda _: session.call("getVersion"), range(16)))
                assert all("version" in result for result in results)
        finally:
            resume()
            timer.cancel()
            timer.join()
    assert child.poll() is not None


@pytest.mark.parametrize("workers", [1, 16])
@pytest.mark.parametrize("operation", ["addUri", "tellStatus"])
def test_http_download_survives_a_busy_control_loop_within_its_progress_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
    workers: int,
    operation: str,
) -> None:
    original = Aria2Session.call
    submitted = 0
    ready = threading.Event()
    paused = threading.Event()
    lock = threading.Lock()
    timers: list[threading.Timer] = []

    def resume() -> None:
        for child in processes:
            if child.poll() is None:
                with suppress(ProcessLookupError):
                    os.kill(child.pid, signal.SIGCONT)

    def call(
        session: Aria2Session, method: str, *parameters: Any, timeout: float | None = None
    ) -> Any:
        nonlocal submitted
        if method == operation and not paused.is_set():
            if operation == "tellStatus":
                assert ready.wait(3)
            with lock:
                if not paused.is_set():
                    paused.set()
                    os.kill(processes[-1].pid, signal.SIGSTOP)
                    timer = threading.Timer(0.75, resume)
                    timers.append(timer)
                    timer.start()
        result = original(session, method, *parameters, timeout=timeout)
        if method == "addUri":
            with lock:
                submitted += 1
                if submitted >= min(workers, 3):
                    ready.set()
        return result

    monkeypatch.setattr(Aria2Session, "call", call)
    workspace = Workspace(tmp_path)
    try:
        with source(certificate, monkeypatch) as server:
            selected = plan(workspace, server)
            result = ArtifactDownloader(
                DownloadPolicy(
                    max_workers=workers,
                    http_attempts_per_url=1,
                    read_timeout_seconds=0.25,
                    http_no_progress_timeout_seconds=4,
                )
            ).download(selected, workspace)
            assert_bytes(result, workspace, server)
    finally:
        resume()
        for timer in timers:
            timer.cancel()
            timer.join()
    assert paused.is_set()
    assert submitted == 3, "A busy RPC must not replay addUri"
    assert processes and all(child.poll() is not None for child in processes)


@pytest.mark.parametrize(
    "fault", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError]
)
@pytest.mark.parametrize("workers", [1, 16])
def test_http_retries_a_lost_status_response_without_restarting_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
    fault: type[httpx.TransportError],
    workers: int,
) -> None:
    original = httpx.Client.post
    submissions = 0
    lost_responses = 0
    lock = threading.Lock()

    def post(client: httpx.Client, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal submissions, lost_responses
        request = kwargs.get("json", {})
        method = request.get("method")
        with lock:
            if method == "aria2.addUri":
                submissions += 1
            if method == "aria2.tellStatus" and lost_responses < 2:
                lost_responses += 1
                raise fault("temporary lost status response: unit-control-secret")
        return original(client, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", post)
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        result = ArtifactDownloader(
            DownloadPolicy(max_workers=workers, http_no_progress_timeout_seconds=2)
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
    assert lost_responses == 2
    assert submissions == 3, "A lost read-only response must not replay addUri"
    assert processes and all(child.poll() is not None for child in processes)


def test_http_repeated_lost_status_responses_exhaust_the_original_progress_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    original = httpx.Client.post
    status_requests = 0
    submissions = 0

    def post(client: httpx.Client, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal status_requests, submissions
        method = kwargs.get("json", {}).get("method")
        if method == "aria2.addUri":
            submissions += 1
        if method == "aria2.tellStatus":
            status_requests += 1
            raise httpx.ReadTimeout("unit-control-secret")
        return original(client, url, *args, **kwargs)

    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.stall = True
        monkeypatch.setattr(httpx.Client, "post", post)
        started = time.monotonic()
        with pytest.raises(StageExecutionError, match="no forward progress") as failure:
            ArtifactDownloader(
                DownloadPolicy(max_workers=1, http_no_progress_timeout_seconds=0.4)
            ).download(plan(workspace, server), workspace)
        assert time.monotonic() - started < 2.5
        assert "unit-control-secret" not in str(failure.value)
    assert status_requests > 2
    assert submissions == 1
    assert processes and all(child.poll() is not None for child in processes)


@pytest.mark.parametrize("status", [400, 403, 412, 429])
def test_http_failures_report_the_numeric_status_without_native_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    status: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.get_status_error = status
        with pytest.raises(StageExecutionError, match=f"HTTP {status}") as failure:
            ArtifactDownloader(DownloadPolicy(max_workers=1, http_attempts_per_url=1)).download(
                plan(workspace, server), workspace
            )
        assert "127.0.0.1" not in str(failure.value)
        assert "[DEBUG-" not in capsys.readouterr().out


@pytest.mark.parametrize("hashes", [True, False], ids=["source-hash", "validator-only"])
def test_fresh_pinned_content_survives_inconsistent_cdn_last_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    hashes: bool,
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.etag = None
        server.head_last_modified = "Tue, 20 Oct 2026 12:00:00 GMT"
        server.last_modified = "Wed, 21 Oct 2026 12:00:00 GMT"
        selected = plan(workspace, server, hashes=hashes)
        downloader = ArtifactDownloader(policy())
        if hashes:
            assert_bytes(downloader.download(selected, workspace), workspace, server)
            assert all(validator is None for _, _, validator in server.requests)
        else:
            with pytest.raises(StageExecutionError):
                downloader.download(selected, workspace)
            digest = hashlib.sha256(server.data["/client.bin"]).hexdigest()
            assert not workspace.blobs.path_for(digest).exists()


@pytest.mark.parametrize("mode", ["resume", "parallel"])
def test_inconsistent_cdn_validator_restarts_pinned_content_without_reusing_partial_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    mode: str,
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.etag = None
        server.head_last_modified = "Tue, 20 Oct 2026 12:00:00 GMT"
        server.last_modified = "Wed, 21 Oct 2026 12:00:00 GMT"
        server.disconnect = mode == "resume"
        messages: list[str] = []
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                http_attempts_per_url=3,
                http_retry_backoff_seconds=0,
                http_retry_max_backoff_seconds=0,
                parallel_range_minimum_bytes=1024 * 1024
                if mode == "parallel"
                else 512 * 1024 * 1024,
                parallel_range_target_bytes=1024 * 1024,
            ),
            messages.append,
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.attempts == (1 if mode == "parallel" else 2)
        assert client.transport.resumed_from == 0
        assert client.transport.parallel_segments == (2 if mode == "parallel" else 1)
        if mode == "parallel":
            assert not any("code 22" in message for message in messages)
            assert not client.transport.parallel_range_fallbacks
            assert any(
                p == "/client.bin" and v == server.last_modified for p, _, v in server.requests
            )
            assert all(
                v is None or v == server.last_modified
                for p, _, v in server.requests
                if p == "/client.bin"
            )
        else:
            assert not client.transport.parallel_range_fallbacks
            assert any(p == "/client.bin" and r == "bytes=0-0" for p, r, _ in server.requests)
            assert all(v is None for p, _, v in server.requests if p == "/client.bin")
        assert any(p == "/client.bin" and validator is None for p, _, validator in server.requests)


def test_fresh_parallel_download_uses_the_get_validator_without_losing_parallelism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.etag = None
        server.head_last_modified = "Tue, 20 Oct 2026 12:00:00 GMT"
        server.last_modified = "Wed, 21 Oct 2026 12:00:00 GMT"
        result = ArtifactDownloader(
            policy(
                parallel_range_minimum_bytes=1024 * 1024, parallel_range_target_bytes=1024 * 1024
            )
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.attempts == 1
        assert client.transport.parallel_segments == 2
        assert not client.transport.parallel_range_fallbacks


def test_rejected_tiny_range_probe_uses_whole_get_headers_without_failing_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.reject_tiny_range = True
        result = ArtifactDownloader(
            policy(
                parallel_range_minimum_bytes=1024 * 1024, parallel_range_target_bytes=1024 * 1024
            )
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.attempts == 1
        assert client.transport.parallel_segments == 1
        assert (
            client.transport.parallel_range_fallbacks[0].reason.value
            == "range-response-not-partial"
        )


def test_conditional_response_rejected_after_get_probe_still_falls_back_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
) -> None:
    original_get = Handler.do_GET
    rejected = False

    def get(handler: Handler) -> None:
        nonlocal rejected
        if handler.path == "/client.bin" and handler.headers.get("If-Match") and not rejected:
            rejected = True
            handler.send_response(412)
            handler.headers_for(0)
            return
        original_get(handler)

    monkeypatch.setattr(Handler, "do_GET", get)
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        result = ArtifactDownloader(
            policy(
                parallel_range_minimum_bytes=1024 * 1024, parallel_range_target_bytes=1024 * 1024
            )
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert rejected
        assert client.transport.attempts == 2
        assert client.transport.parallel_segments == 1
        assert client.transport.parallel_range_fallbacks[0].reason.value in {
            "validator-changed",
            "range-request-failed",
        }


@pytest.mark.parametrize("reject_tiny_range", [False, True], ids=["range-probe", "whole-get-probe"])
def test_repeated_interruptions_resume_with_the_get_representation_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    reject_tiny_range: bool,
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.etag = None
        server.head_last_modified = "Tue, 20 Oct 2026 12:00:00 GMT"
        server.last_modified = "Wed, 21 Oct 2026 12:00:00 GMT"
        server.disconnect_after_bytes = 256 * 1024
        server.reject_tiny_range = reject_tiny_range
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                http_attempts_per_url=20,
                http_retry_backoff_seconds=0,
                http_retry_max_backoff_seconds=0,
            )
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.resumed_from > 0
        assert 1 < client.transport.attempts < 20
        assert not client.transport.parallel_range_fallbacks
        positions = [
            int(raw_range.split("=", 1)[1].split("-", 1)[0])
            for path, raw_range, _ in server.requests
            if path == "/client.bin" and raw_range and raw_range != "bytes=0-0"
        ]
        assert max(positions) > 1024 * 1024


def test_rejected_resume_validator_without_source_hash_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
) -> None:
    workspace = Workspace(tmp_path)
    original_head = httpx.Client.head
    with source(certificate, monkeypatch) as server:
        server.etag = None
        server.head_last_modified = server.last_modified = "Tue, 20 Oct 2026 12:00:00 GMT"
        server.disconnect = True
        probes = 0

        def head(client: httpx.Client, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal probes
            if str(url).endswith("/client.bin"):
                probes += 1
                if probes > 1:
                    server.last_modified = "Wed, 21 Oct 2026 12:00:00 GMT"
            return original_head(client, url, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "head", head)
        with pytest.raises(StageExecutionError):
            ArtifactDownloader(policy()).download(plan(workspace, server, hashes=False), workspace)
        assert probes == 2
        assert all(
            validator is not None for p, _, validator in server.requests if p == "/client.bin"
        )
        digest = hashlib.sha256(server.data["/client.bin"]).hexdigest()
        assert not workspace.blobs.path_for(digest).exists()


def test_http_transient_cdn_errors_can_recover_after_the_old_eight_attempt_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.transient_errors = 9
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                http_retry_backoff_seconds=0,
                http_retry_max_backoff_seconds=0,
                http_no_progress_timeout_seconds=5,
            )
        ).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.attempts == 10


def test_http_interrupted_stream_retries_and_resumes_within_the_same_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.disconnect = True
        result = ArtifactDownloader(policy()).download(plan(workspace, server), workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.attempts == 2
        assert client.transport.resumed_from > 0
        assert any(
            p == "/client.bin" and r is not None and not r.startswith("bytes=0-")
            for p, r, _ in server.requests
        )
    assert len(processes) == 1
    assert processes[0].poll() is not None


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


@pytest.mark.parametrize("hashes", [True, False], ids=["source-hash", "validator-only"])
def test_http_completed_content_uses_its_source_hash_when_cdn_validator_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    hashes: bool,
) -> None:
    original = httpx.Client.head
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        probes = 0

        def head(client: httpx.Client, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal probes
            if str(url).endswith("/client.bin"):
                probes += 1
                if probes == 2:
                    server.etag = '"different-cdn-edge"'
            return original(client, url, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "head", head)
        result = ArtifactDownloader(policy()).download(
            plan(workspace, server, hashes=hashes), workspace
        )
        assert_bytes(result, workspace, server)
        downloads = [r for p, r, _ in server.requests if p == "/client.bin" and r != "bytes=0-0"]
        assert len(downloads) == (1 if hashes else 2)
        assert probes == (1 if hashes else 4)


def test_http_completed_payload_survives_a_stalled_result_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    original = Aria2Session.call
    completed: set[str] = set()
    lock = threading.Lock()
    timers: list[threading.Timer] = []

    def resume() -> None:
        for child in processes:
            if child.poll() is None:
                with suppress(ProcessLookupError):
                    os.kill(child.pid, signal.SIGCONT)

    def call(
        session: Aria2Session, method: str, *parameters: Any, timeout: float | None = None
    ) -> Any:
        result = original(session, method, *parameters, timeout=timeout)
        if method == "tellStatus" and result["status"] == "complete":
            with lock:
                completed.add(parameters[0])
                if len(completed) == 3 and not timers:
                    os.kill(processes[-1].pid, signal.SIGSTOP)
                    timer = threading.Timer(5.5, resume)
                    timers.append(timer)
                    timer.start()
        return result

    monkeypatch.setattr(Aria2Session, "call", call)
    workspace = Workspace(tmp_path)
    try:
        with source(certificate, monkeypatch) as server:
            result = ArtifactDownloader(policy()).download(plan(workspace, server), workspace)
            assert_bytes(result, workspace, server)
    finally:
        resume()
        for timer in timers:
            timer.cancel()
            timer.join()
    assert timers, "The process must stall after all payload writers complete"
    assert all(child.poll() is not None for child in processes)


def test_http_sibling_hash_failure_stops_a_worker_waiting_for_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    original = Aria2Session.call
    waiting = threading.Event()
    large_gid: str | None = None
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.chunk_delay = 0.05
        selected = plan(workspace, server)
        client_key = selected.parts[0].artifacts[0].artifact_id.removeprefix("sha256:")
        part = selected.parts[1]
        broken = part.artifacts[0].model_copy(
            update={"source_hash": SourceHash(algorithm="sha1", value="0" * 40)}
        )
        selected = selected.model_copy(
            update={
                "parts": (
                    selected.parts[0],
                    part.model_copy(update={"artifacts": (broken,)}),
                    selected.parts[2],
                )
            }
        )

        def call(
            session: Aria2Session, method: str, *parameters: Any, timeout: float | None = None
        ) -> Any:
            nonlocal large_gid
            if method == "addUri":
                result = original(session, method, *parameters, timeout=timeout)
                if Path(parameters[1]["dir"]).name == client_key:
                    large_gid = result
                return result
            if method == "tellStatus":
                if parameters[0] == large_gid:
                    waiting.set()
                    # Model an outstanding RPC: stopping the native session must
                    # unblock it, without waiting for the first scheduled file.
                    deadline = time.monotonic() + 2
                    while processes[-1].poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                else:
                    assert waiting.wait(3)
            return original(session, method, *parameters, timeout=timeout)

        monkeypatch.setattr(Aria2Session, "call", call)
        started = time.monotonic()
        with pytest.raises(ArtifactCorruptError, match="source hash"):
            ArtifactDownloader(DownloadPolicy(max_workers=3, http_attempts_per_url=1)).download(
                selected, workspace
            )
        assert time.monotonic() - started < 1.5
    assert waiting.is_set()
    assert all(child.poll() is not None for child in processes)


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
@pytest.mark.parametrize("change", ["unchanged", "changed", "removed"])
def test_http_completed_partial_revalidates_its_source_before_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    validator: str,
    change: str,
) -> None:
    from game_downloader import delivery

    original = delivery._hash_file
    interrupted = False
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        if validator == "last-modified":
            server.etag = None
            server.last_modified = "Sun, 01 Jan 2023 00:00:00 GMT"
        selected = plan(workspace, server, hashes=False)
        client_key = selected.parts[0].artifacts[0].artifact_id.removeprefix("sha256:")

        def interrupt_hash(path: Path, source_hash: SourceHash | None) -> tuple[str, bool]:
            nonlocal interrupted
            if path.name == "payload.part" and path.parent.name == client_key and not interrupted:
                interrupted = True
                raise RuntimeError("interrupted before hashing a completed payload")
            return original(path, source_hash)

        monkeypatch.setattr(delivery, "_hash_file", interrupt_hash)
        downloader = ArtifactDownloader(DownloadPolicy(max_workers=1, http_attempts_per_url=1))
        with pytest.raises(RuntimeError, match="interrupted before hashing"):
            downloader.download(selected, workspace)
        partial = workspace.partial_root / client_key
        assert json.loads((partial / "state.json").read_text())["confirmed_complete"]
        assert not (partial / "payload.part.aria2").exists()
        if change != "unchanged":
            server.data["/client.bin"] = b"N" * len(server.data["/client.bin"])
            if validator == "etag":
                server.etag = '"version-2"' if change == "changed" else None
            else:
                server.last_modified = (
                    "Mon, 02 Jan 2023 00:00:00 GMT" if change == "changed" else None
                )
        before = sum(path == "/client.bin" for path, _, _ in server.requests)
        result = downloader.download(selected, workspace)
        assert_bytes(result, workspace, server)
        after = sum(path == "/client.bin" for path, _, _ in server.requests)
        assert (after == before) == (change == "unchanged")


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
        written_before = len(server.body_writes)
        server.chunk_delay = max(server.chunk_delay, 0.002)
        result = ArtifactDownloader(selected_policy).download(selected, workspace)
        assert_bytes(result, workspace, server)
        client = next(x for x in result.artifacts if x.artifact.path.utf8 == "client.bin")
        assert client.transport.resumed_from > 0
        ranges = [r for p, r, _ in server.requests[before:] if p == "/client.bin"]
        # With HEAD disabled, aria2 can use one GET only to inspect headers.
        # It must stop that response before re-reading the saved payload prefix.
        assert ranges.count(None) <= 1
        unrequested_body = sum(
            count
            for path, raw_range, count in server.body_writes[written_before:]
            if path == "/client.bin" and raw_range is None
        )
        assert unrequested_body < client.transport.resumed_from
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


@pytest.mark.parametrize("fallback", [False, True], ids=["head", "range-probe"])
def test_http_metadata_wait_is_bounded_by_the_remaining_progress_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
    fallback: bool,
) -> None:
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        server.stall_metadata = True
        server.no_head = fallback
        server.head_delay = 0.9 if fallback else 0
        selected = plan(workspace, server)
        started = time.monotonic()
        with pytest.raises(StageExecutionError):
            ArtifactDownloader(
                DownloadPolicy(
                    max_workers=1,
                    http_attempts_per_url=1,
                    read_timeout_seconds=5,
                    http_no_progress_timeout_seconds=1.2,
                )
            ).download(selected, workspace)
        assert time.monotonic() - started < 2.5
    assert processes and all(child.poll() is not None for child in processes)


def test_http_unknown_add_uri_acknowledgement_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate: tuple[Path, Path],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    original = httpx.Client.post
    submitted: list[str] = []
    workspace = Workspace(tmp_path)
    with source(certificate, monkeypatch) as server:
        selected = plan(workspace, server)
        client_key = selected.parts[0].artifacts[0].artifact_id.removeprefix("sha256:")

        def post(client: httpx.Client, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            request = kwargs.get("json")
            if isinstance(request, dict) and request.get("method") == "aria2.addUri":
                key = Path(request["params"][2]["dir"]).name
                submitted.append(key)
                response = original(client, url, *args, **kwargs)
                if key == client_key:
                    raise httpx.ReadTimeout("lost acknowledgement: unit-control-secret")
                return response
            return original(client, url, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "post", post)
        with pytest.raises(StageExecutionError, match="addUri: ReadTimeout") as failure:
            ArtifactDownloader(policy()).download(selected, workspace)
        assert "unit-control-secret" not in str(failure.value)
        assert submitted.count(client_key) == 1
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

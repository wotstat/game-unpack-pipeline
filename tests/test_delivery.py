from __future__ import annotations

import base64
import hashlib
import io
import socket
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import SplitResult
from urllib.parse import urlsplit as standard_urlsplit

import pytest

from game_downloader.acquisition import ArtifactCorruptError, UnsafeArchiveError
from game_downloader.delivery import (
    MINIMUM_DOWNLOAD_THROUGHPUT_BYTES_PER_SECOND,
    ArtifactDownloader,
    ArtifactVerifier,
    DownloadPolicy,
    DownloadTooSlowError,
    VerificationPolicy,
    _DownloadProgress,
)
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    AcquisitionPlan,
    DiskSpaceEstimate,
    DownloadedArtifact,
    DownloadMethod,
    DownloadResult,
    DownloadTrace,
    PartAcquisition,
    PartName,
    SourceHash,
    SplitSegment,
    TorrentDescriptorRecord,
)
from game_downloader.torrent import bytes_path_from_text, parse_torrent
from game_downloader.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures/torrent"


def test_download_policy_detects_near_stalls_over_two_minutes() -> None:
    policy = DownloadPolicy()

    assert MINIMUM_DOWNLOAD_THROUGHPUT_BYTES_PER_SECOND == 5 * 1024 * 1024
    assert policy.minimum_throughput_bytes_per_second == 5 * 1024 * 1024
    assert policy.minimum_throughput_window_seconds == 120.0


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _torrent_fixture() -> bytes:
    return base64.b64decode(
        (FIXTURES / "reference-multifile.torrent.b64").read_text(encoding="ascii")
    )


class _FixtureServer(ThreadingHTTPServer):
    data: dict[str, bytes]
    requests: list[tuple[str, str | None]]
    disconnect_path: str | None
    did_disconnect: bool
    etag: str | None
    replacement_data: dict[str, bytes]
    invalid_content_range: bool
    ignore_range_requests: bool
    range_etag: str | None
    advertise_ranges: bool
    range_data: dict[str, bytes]
    ignore_range_paths: set[str]


class _RangeHandler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def do_HEAD(self) -> None:
        data = self.server.data[self.path]
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        if self.server.advertise_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if self.server.etag is not None:
            self.send_header("ETag", self.server.etag)
        self.end_headers()

    def do_GET(self) -> None:
        raw_range = self.headers.get("Range")
        data = (
            self.server.range_data.get(self.path, self.server.data[self.path])
            if raw_range is not None
            else self.server.data[self.path]
        )
        self.server.requests.append((self.path, raw_range))
        start = 0
        end = len(data) - 1
        ignore_range = (
            self.server.ignore_range_requests or self.path in self.server.ignore_range_paths
        )
        if raw_range is not None and not ignore_range:
            assert raw_range.startswith("bytes=")
            raw_start, raw_end = raw_range.removeprefix("bytes=").split("-", maxsplit=1)
            start = int(raw_start)
            if raw_end:
                end = int(raw_end)
            assert 0 <= start <= end < len(data)
            self.send_response(206)
            declared_start = start + 1 if self.server.invalid_content_range else start
            self.send_header("Content-Range", f"bytes {declared_start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        response_etag = self.server.range_etag if raw_range is not None else self.server.etag
        if response_etag is not None:
            self.send_header("ETag", response_etag)
        self.end_headers()
        if self.path == self.server.disconnect_path and not self.server.did_disconnect:
            self.server.did_disconnect = True
            midpoint = max(1, (end - start + 1) // 2)
            self.wfile.write(data[start : start + midpoint])
            self.wfile.flush()
            self.server.data.update(self.server.replacement_data)
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.wfile.write(data[start : end + 1])

    def log_message(self, _format: str, *args: object) -> None:
        pass


@contextmanager
def _http_fixture(
    data: dict[str, bytes],
    disconnect_path: str | None,
    *,
    etag: str | None = '"fixture-v1"',
    replacement_data: dict[str, bytes] | None = None,
    invalid_content_range: bool = False,
    ignore_range_requests: bool = False,
    range_etag: str | None = None,
    advertise_ranges: bool = True,
    range_data: dict[str, bytes] | None = None,
    ignore_range_paths: set[str] | None = None,
) -> Iterator[_FixtureServer]:
    server = _FixtureServer(("127.0.0.1", 0), _RangeHandler)
    server.data = data
    server.requests = []
    server.disconnect_path = disconnect_path
    server.did_disconnect = False
    server.etag = etag
    server.replacement_data = replacement_data or {}
    server.invalid_content_range = invalid_content_range
    server.ignore_range_requests = ignore_range_requests
    server.range_etag = etag if range_etag is None else range_etag
    server.advertise_ranges = advertise_ranges
    server.range_data = range_data or {}
    server.ignore_range_paths = ignore_range_paths or set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _plan(
    workspace: Workspace,
    server: _FixtureServer,
    *,
    corrupt_client_hash: bool = False,
    source_hashes: bool = True,
) -> AcquisitionPlan:
    raw_torrent = _torrent_fixture()
    descriptor_sha256 = hashlib.sha256(raw_torrent).hexdigest()
    commit = workspace.blobs.put_bytes(raw_torrent, expected_sha256=descriptor_sha256)
    descriptor = TorrentDescriptorRecord(
        descriptor_sha256=descriptor_sha256,
        source_urls=("https://fixture.invalid/reference.torrent",),
        fetched_url="https://fixture.invalid/reference.torrent",
        final_url="https://fixture.invalid/reference.torrent",
        blob_sha256=descriptor_sha256,
        blob_size=len(raw_torrent),
        blob_path=commit.relative_path,
        metainfo=parse_torrent(raw_torrent),
    )
    address = f"http://127.0.0.1:{server.server_port}"
    definitions = (
        (PartName.CLIENT, None, "/client.bin"),
        (PartName.SD_CONTENT, None, "/sd.bin"),
        (PartName.LOCALE, "EN", "/locale.bin"),
    )
    parts: list[PartAcquisition] = []
    for part, language, url_path in definitions:
        payload = server.data[url_path]
        sha1 = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
        if corrupt_client_hash and part is PartName.CLIENT:
            sha1 = "0" * 40
        artifact = AcquisitionArtifact(
            artifact_id=_digest(f"{part.value}:{language}"),
            role="client-file",
            part=part,
            language=language,
            part_version="1",
            acquisition_mode=AcquisitionMode.REFERENCE,
            path=bytes_path_from_text(url_path.removeprefix("/")),
            size=len(payload),
            source_hash=SourceHash(algorithm="sha1", value=sha1) if source_hashes else None,
            source_urls=(f"{address}{url_path}",),
            torrent_descriptor_sha256=descriptor_sha256,
        )
        parts.append(
            PartAcquisition(
                part=part,
                language=language,
                version="1",
                acquisition_mode=AcquisitionMode.REFERENCE,
                artifacts=(artifact,),
                torrent_descriptor_sha256s=(descriptor_sha256,),
            )
        )
    download_bytes = sum(len(value) for value in server.data.values())
    return AcquisitionPlan(
        resolve_result_sha256=_digest("resolve"),
        parts=tuple(parts),
        descriptors=(descriptor,),
        disk_space=DiskSpaceEstimate(
            descriptor_bytes=len(raw_torrent),
            download_bytes=download_bytes,
            assembled_bytes=download_bytes,
            reserve_bytes=0,
            required_free_bytes=len(raw_torrent) + 2 * download_bytes,
        ),
    )


def test_download_progress_reports_by_time_and_percentage() -> None:
    now = [0.0]
    messages: list[str] = []
    progress = _DownloadProgress(
        {"artifact": 1000},
        {},
        messages.append,
        interval_seconds=60,
        percent_step=10,
        clock=lambda: now[0],
    )

    progress.started(1)
    progress.update("artifact", 50, 50)
    now[0] = 60
    progress.update("artifact", 60, 10)
    now[0] = 70
    progress.update("artifact", 100, 40)
    now[0] = 170
    progress.update("artifact", 1000, 900)
    progress.completed()

    assert messages == [
        "Downloading 1 artifacts: 0 B / 1000 B available",
        "Download progress: 60 B / 1000 B (6.0%); interval: 60 B in 60.0s (1 B/s)",
        "Download progress: 100 B / 1000 B (10.0%); interval: 40 B in 10.0s (4 B/s)",
        "Download progress: 1000 B / 1000 B (100.0%); interval: 900 B in 100.0s (9 B/s)",
    ]


def test_download_progress_aborts_below_minimum_aggregate_throughput() -> None:
    now = [0.0]
    messages: list[str] = []
    progress = _DownloadProgress(
        {"artifact": 100_000},
        {},
        messages.append,
        interval_seconds=60,
        percent_step=10,
        minimum_throughput_bytes_per_second=50,
        minimum_throughput_window_seconds=120,
        clock=lambda: now[0],
    )

    progress.started(1)
    now[0] = 60
    progress.update("artifact", 2_400, 2_400, source_host="cdn.example.test")
    now[0] = 120
    with pytest.raises(DownloadTooSlowError) as caught:
        progress.update("artifact", 4_800, 2_400, source_host="cdn.example.test")

    assert caught.value.error.code == "download_too_slow"
    assert "40.000 B/s" in caught.value.error.message
    assert "50.000 B/s minimum" in caught.value.error.message
    assert "93.0 KiB remain" in caught.value.error.message
    assert "current Artifact artifact from cdn.example.test" in caught.value.error.message
    assert messages[-1].endswith("(40 B/s)")


def test_download_progress_aborts_when_throughput_drops_later() -> None:
    now = [0.0]
    progress = _DownloadProgress(
        {"artifact": 100_000},
        {},
        lambda _message: None,
        interval_seconds=60,
        percent_step=10,
        minimum_throughput_bytes_per_second=50,
        minimum_throughput_window_seconds=120,
        clock=lambda: now[0],
    )

    now[0] = 60
    progress.update("artifact", 6_000, 6_000)
    now[0] = 120
    progress.update("artifact", 12_000, 6_000)
    now[0] = 180
    progress.update("artifact", 12_600, 600)
    now[0] = 240
    with pytest.raises(DownloadTooSlowError):
        progress.update("artifact", 13_200, 600)


def test_download_progress_does_not_abort_at_or_above_minimum_throughput() -> None:
    now = [0.0]
    progress = _DownloadProgress(
        {"artifact": 100_000},
        {},
        lambda _message: None,
        interval_seconds=60,
        percent_step=10,
        minimum_throughput_bytes_per_second=50,
        minimum_throughput_window_seconds=120,
        clock=lambda: now[0],
    )

    now[0] = 60
    progress.update("artifact", 3_000, 3_000)
    now[0] = 120
    progress.update("artifact", 6_000, 3_000)


def test_download_resumes_range_after_disconnect_and_reuses_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 1000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(data, disconnect_path="/client.bin") as server:
        plan = _plan(workspace, server)
        # The URL scheme guard is independently covered by Acquisition Plan tests; the local
        # Range server intentionally has no TLS endpoint.
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        policy = DownloadPolicy(max_workers=1, attempts_per_url=3, chunk_bytes=64 * 1024)
        progress_messages: list[str] = []
        first = ArtifactDownloader(policy, progress_messages.append).download(plan, workspace)
        request_count = len(server.requests)
        remaining_required = plan.disk_space.required_free_bytes - plan.disk_space.download_bytes
        monkeypatch.setattr(
            "game_downloader.delivery.shutil.disk_usage",
            lambda _path: SimpleNamespace(free=remaining_required),
        )
        second = ArtifactDownloader(policy).download(plan, workspace)

    assert first.downloaded_bytes == sum(len(value) for value in data.values())
    assert first.reused_artifacts == 0
    assert progress_messages[0].startswith("Downloading 3 artifacts: 0 B / ")
    assert "(100.0%)" in progress_messages[-1]
    assert second.reused_artifacts == 3
    assert len(server.requests) == request_count
    client_ranges = [header for path, header in server.requests if path == "/client.bin"]
    assert client_ranges[0] is None
    assert client_ranges[1] == f"bytes={len(data['/client.bin']) // 2}-"
    assert all(workspace.blobs.path_for(item.blob_sha256).exists() for item in first.artifacts)


def test_download_stripes_large_artifact_across_validated_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(data, disconnect_path=None) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                chunk_bytes=64 * 1024,
                parallel_range_minimum_bytes=256 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=4,
            )
        ).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    client_ranges = sorted(
        header for path, header in server.requests if path == "/client.bin" and header is not None
    )
    assert client.transport.parallel_segments == 4
    assert client_ranges == [
        "bytes=0-149999",
        "bytes=150000-299999",
        "bytes=300000-449999",
        "bytes=450000-599999",
    ]
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]
    assert not tuple(workspace.partial_root.rglob("range-state.json"))


def test_parallel_range_falls_back_when_server_ignores_bounded_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    messages: list[str] = []
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(
        data,
        disconnect_path=None,
        ignore_range_requests=True,
    ) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                chunk_bytes=64 * 1024,
                parallel_range_minimum_bytes=256 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=4,
            ),
            messages.append,
        ).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]
    assert client.transport.parallel_segments == 1
    assert [item.model_dump(mode="json") for item in client.transport.parallel_range_fallbacks] == [
        {
            "reason": "range-response-not-partial",
            "source_host": "127.0.0.1",
            "response_status": 200,
            "range_index": 0,
            "attempts": 1,
            "discarded_bytes": 0,
        }
    ]
    client_requests = [header for path, header in server.requests if path == "/client.bin"]
    assert any(header is not None for header in client_requests)
    assert client_requests[-1] is None
    assert any(
        client.artifact.artifact_id in message
        and "127.0.0.1" in message
        and "range-response-not-partial" in message
        and "single-stream HTTP" in message
        for message in messages
    )
    assert not tuple(workspace.partial_root.rglob("range-state.json"))


def test_parallel_range_fallback_does_not_disable_other_artifacts_on_same_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload-" * 40_000,
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(
        data,
        disconnect_path=None,
        ignore_range_paths={"/client.bin"},
    ) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                parallel_range_minimum_bytes=256 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=4,
            )
        ).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    sd_content = next(item for item in result.artifacts if item.artifact.path.utf8 == "sd.bin")
    assert client.transport.parallel_segments == 1
    assert len(client.transport.parallel_range_fallbacks) == 1
    assert sd_content.transport.parallel_segments == 4
    assert sd_content.transport.parallel_range_fallbacks == ()


def test_parallel_range_falls_back_when_validator_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(
        data,
        disconnect_path=None,
        range_etag='"fixture-v2"',
    ) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                chunk_bytes=64 * 1024,
                parallel_range_minimum_bytes=256 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=4,
            )
        ).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]
    assert [item.reason.value for item in client.transport.parallel_range_fallbacks] == [
        "validator-changed"
    ]
    assert client.transport.parallel_range_fallbacks[0].source_host == "127.0.0.1"
    client_requests = [header for path, header in server.requests if path == "/client.bin"]
    assert any(header is not None for header in client_requests)
    assert client_requests[-1] is None
    assert not tuple(workspace.partial_root.rglob("range-state.json"))


def test_parallel_range_records_when_probe_does_not_advertise_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(
        data,
        disconnect_path=None,
        advertise_ranges=False,
    ) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                parallel_range_minimum_bytes=256 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=4,
            )
        ).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    assert [item.reason.value for item in client.transport.parallel_range_fallbacks] == [
        "range-not-advertised"
    ]
    assert [header for path, header in server.requests if path == "/client.bin"] == [None]
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]


def test_parallel_range_hash_mismatch_retries_with_single_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    corrupt_ranges = {"/client.bin": b"broken-payload-" * 40_000}
    assert len(corrupt_ranges["/client.bin"]) == len(data["/client.bin"])
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(
        data,
        disconnect_path=None,
        range_data=corrupt_ranges,
    ) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(
            DownloadPolicy(
                max_workers=1,
                parallel_range_minimum_bytes=256 * 1024,
                parallel_range_target_bytes=128 * 1024,
                parallel_range_max_segments=4,
            )
        ).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    assert [item.reason.value for item in client.transport.parallel_range_fallbacks] == [
        "payload-hash-mismatch"
    ]
    assert client.transport.parallel_range_fallbacks[0].discarded_bytes == len(data["/client.bin"])
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]


def test_parallel_range_falls_back_after_range_retries_are_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(data, disconnect_path="/client.bin") as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        policy = DownloadPolicy(
            max_workers=1,
            attempts_per_url=1,
            chunk_bytes=64 * 1024,
            parallel_range_minimum_bytes=256 * 1024,
            parallel_range_target_bytes=128 * 1024,
            parallel_range_max_segments=4,
        )
        result = ArtifactDownloader(policy).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    assert client.transport.parallel_segments == 1
    assert [item.reason.value for item in client.transport.parallel_range_fallbacks] == [
        "range-request-failed"
    ]
    assert client.transport.parallel_range_fallbacks[0].discarded_bytes > 0
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]
    assert not tuple(workspace.partial_root.rglob("range-state.json"))


def test_parallel_range_falls_back_from_incorrect_content_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "/client.bin": b"client-payload-" * 40_000,
        "/sd.bin": b"sd-payload",
        "/locale.bin": b"locale-payload",
    }
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(data, disconnect_path=None, invalid_content_range=True) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        policy = DownloadPolicy(
            max_workers=1,
            parallel_range_minimum_bytes=256 * 1024,
            parallel_range_target_bytes=128 * 1024,
            parallel_range_max_segments=4,
        )
        result = ArtifactDownloader(policy).download(plan, workspace)

    client = next(item for item in result.artifacts if item.artifact.path.utf8 == "client.bin")
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == data["/client.bin"]
    assert [item.reason.value for item in client.transport.parallel_range_fallbacks] == [
        "invalid-content-range"
    ]
    client_requests = [header for path, header in server.requests if path == "/client.bin"]
    assert any(header is not None for header in client_requests)
    assert client_requests[-1] is None


def test_download_restarts_partial_without_a_source_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"old-client-payload"
    replacement = b"new-client-payload"
    data = {"/client.bin": original, "/sd.bin": b"sd", "/locale.bin": b"locale"}
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(
        data,
        disconnect_path="/client.bin",
        etag=None,
        replacement_data={"/client.bin": replacement},
    ) as server:
        plan = _plan(workspace, server, source_hashes=False)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        result = ArtifactDownloader(DownloadPolicy(max_workers=1, attempts_per_url=3)).download(
            plan, workspace
        )

    client = result.artifacts[0]
    assert workspace.blobs.path_for(client.blob_sha256).read_bytes() == replacement
    assert [header for path, header in server.requests if path == "/client.bin"] == [None, None]


def test_fresh_web_download_hashes_while_streaming_and_adopts_without_a_second_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {"/client.bin": b"client", "/sd.bin": b"sd", "/locale.bin": b"locale"}
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(data, disconnect_path=None) as server:
        plan = _plan(workspace, server)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        monkeypatch.setattr(
            "game_downloader.delivery._hash_file",
            lambda *_args: pytest.fail("fresh web payload must not be read again after streaming"),
        )

        result = ArtifactDownloader(DownloadPolicy(max_workers=1)).download(plan, workspace)

    assert result.downloaded_bytes == sum(len(value) for value in data.values())
    assert all(workspace.blobs.path_for(item.blob_sha256).exists() for item in result.artifacts)


def urlsplit_https(value: str) -> SplitResult:
    parsed = standard_urlsplit(value)
    return parsed._replace(scheme="https")


def test_download_rejects_source_hash_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {"/client.bin": b"bad", "/sd.bin": b"sd", "/locale.bin": b"locale"}
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with _http_fixture(data, disconnect_path=None) as server:
        plan = _plan(workspace, server, corrupt_client_hash=True)
        monkeypatch.setattr(
            "game_downloader.delivery.urlsplit", lambda value: urlsplit_https(value)
        )
        with pytest.raises(ArtifactCorruptError, match="complete sources were corrupt"):
            ArtifactDownloader(DownloadPolicy(max_workers=1, attempts_per_url=2)).download(
                plan, workspace
            )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


def _downloaded(
    workspace: Workspace,
    data: bytes,
    *,
    name: str,
    index: int | None = None,
    count: int | None = None,
) -> DownloadedArtifact:
    commit = workspace.blobs.put_bytes(data)
    group_id = _digest("split") if index is not None else None
    artifact = AcquisitionArtifact(
        artifact_id=_digest(name),
        role="delivery-bundle" if index is not None else "client-file",
        part=PartName.LOCALE if index is not None else PartName.CLIENT,
        language="EN" if index is not None else None,
        part_version="1",
        acquisition_mode=(
            AcquisitionMode.INSTALL_BUNDLE if index is not None else AcquisitionMode.REFERENCE
        ),
        path=bytes_path_from_text(name),
        size=len(data),
        torrent_descriptor_sha256="1" * 64,
        transition_from="0" if index is not None else None,
        transition_to="1" if index is not None else None,
        split_segment=(
            SplitSegment(group_id=group_id, index=index, count=count)
            if group_id is not None and index is not None and count is not None
            else None
        ),
    )
    return DownloadedArtifact(
        artifact=artifact,
        blob_sha256=commit.sha256,
        blob_size=commit.size,
        blob_path=commit.relative_path,
        source_hash_verified=False,
        reused=False,
        transport=DownloadTrace(
            method=DownloadMethod.WEB_SEED,
            requested_url="https://fixture.invalid/artifact",
            final_url="https://fixture.invalid/artifact",
        ),
    )


def test_verify_checks_zip_paths_crc_and_split_assembly(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    package = _zip_bytes({"scripts/example.pyc": b"compiled", "readme.txt": b"ok"})
    regular = _downloaded(workspace, package, name="res/packages/scripts.pkg")
    midpoint = len(package) // 2
    first = _downloaded(workspace, package[:midpoint], name="locale.wgpkg.001", index=1, count=2)
    second = _downloaded(workspace, package[midpoint:], name="locale.wgpkg.002", index=2, count=2)
    downloaded = DownloadResult(
        acquisition_plan_sha256=_digest("plan"),
        artifacts=(regular, first, second),
        downloaded_bytes=sum(item.blob_size for item in (regular, first, second)),
        reused_artifacts=0,
    )

    verified = ArtifactVerifier(VerificationPolicy()).verify(downloaded, workspace)

    assert verified.artifacts[0].entries == 2
    assert verified.artifacts[0].container.value == "zip"
    assert len(verified.split_assemblies) == 1
    assert verified.split_assemblies[0].entries == 2
    assert (
        workspace.blobs.path_for(verified.split_assemblies[0].blob_sha256).read_bytes() == package
    )


def test_verify_rejects_archive_traversal(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    malicious = _downloaded(
        workspace,
        _zip_bytes({"../escape": b"bad"}),
        name="res/packages/malicious.pkg",
    )
    downloaded = DownloadResult(
        acquisition_plan_sha256=_digest("plan"),
        artifacts=(malicious,),
        downloaded_bytes=malicious.blob_size,
        reused_artifacts=0,
    )

    with pytest.raises(UnsafeArchiveError, match="unsafe path"):
        ArtifactVerifier().verify(downloaded, workspace)

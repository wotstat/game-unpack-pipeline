from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_downloader.acquisition import UnsafeArchiveError
from game_downloader.delivery import (
    MINIMUM_DOWNLOAD_THROUGHPUT_BYTES_PER_SECOND,
    ArtifactVerifier,
    DownloadPolicy,
    VerificationPolicy,
    _DownloadProgress,
    _HttpTransferProgress,
)
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    DownloadedArtifact,
    DownloadMethod,
    DownloadResult,
    DownloadTrace,
    PartName,
    SplitSegment,
)
from game_downloader.pipeline import StageExecutionError
from game_downloader.torrent import bytes_path_from_text
from game_downloader.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures/torrent"


def test_download_policy_detects_near_stalls_over_five_minutes() -> None:
    policy = DownloadPolicy()

    assert MINIMUM_DOWNLOAD_THROUGHPUT_BYTES_PER_SECOND == 1024 * 1024
    assert policy.minimum_throughput_bytes_per_second == 1024 * 1024
    assert policy.minimum_throughput_window_seconds == 300.0
    assert policy.minimum_throughput_tail_percent == 5.0
    assert policy.minimum_throughput_tail_grace_seconds == 20 * 60


def test_default_progress_tolerates_five_mib_tail_for_five_minutes() -> None:
    now = [0.0]
    policy = DownloadPolicy()
    progress = _DownloadProgress(
        {"artifact": 2 * 1024 * 1024 * 1024},
        {},
        lambda _message: None,
        interval_seconds=60,
        percent_step=10,
        minimum_throughput_bytes_per_second=(policy.minimum_throughput_bytes_per_second),
        minimum_throughput_window_seconds=policy.minimum_throughput_window_seconds,
        clock=lambda: now[0],
    )
    bytes_per_second = int(4.986 * 1024 * 1024)

    for minute in range(1, 6):
        now[0] = minute * 60
        transferred = bytes_per_second * 60
        progress.update(
            "artifact",
            minute * transferred,
            transferred,
            source_host="cdn.example.test",
        )


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


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


def test_download_progress_warns_once_below_minimum_aggregate_throughput() -> None:
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
    progress.update("artifact", 4_800, 2_400, source_host="cdn.example.test")

    warning = messages[-1]
    assert warning.startswith("Warning: aggregate download throughput")
    assert "40.000 B/s" in warning
    assert "50.000 B/s warning threshold" in warning
    assert "93.0 KiB remain" in warning
    assert "current Artifact artifact from cdn.example.test" in warning
    assert warning.endswith("continuing download")
    now[0] = 180
    progress.update("artifact", 7_200, 2_400, source_host="cdn.example.test")
    assert sum("aggregate download throughput" in message for message in messages) == 1
    assert messages[-1].endswith("(40 B/s)")
    now[0] = 240
    progress.update("artifact", 100_000, 92_800)
    progress.completed()


def test_download_progress_warns_again_after_throughput_recovers() -> None:
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

    now[0] = 60
    progress.update("artifact", 6_000, 6_000)
    now[0] = 120
    progress.update("artifact", 12_000, 6_000)
    now[0] = 180
    progress.update("artifact", 12_600, 600)
    now[0] = 240
    progress.update("artifact", 13_200, 600)
    assert sum("aggregate download throughput" in message for message in messages) == 1
    now[0] = 300
    progress.update("artifact", 25_200, 12_000)
    now[0] = 360
    progress.update("artifact", 25_800, 600)
    now[0] = 420
    progress.update("artifact", 26_400, 600)
    assert sum("aggregate download throughput" in message for message in messages) == 2


def test_download_progress_does_not_abort_at_or_above_minimum_throughput() -> None:
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

    now[0] = 60
    progress.update("artifact", 3_000, 3_000)
    now[0] = 120
    progress.update("artifact", 6_000, 3_000)
    assert not any("aggregate download throughput" in message for message in messages)


def test_download_progress_warns_after_twenty_slow_minutes_in_final_five_percent() -> None:
    now = [0.0]
    messages: list[str] = []
    progress = _DownloadProgress(
        {"artifact": 1_000_000},
        {"artifact": 950_000},
        messages.append,
        interval_seconds=60,
        percent_step=10,
        minimum_throughput_bytes_per_second=50,
        minimum_throughput_window_seconds=120,
        minimum_throughput_tail_percent=5,
        minimum_throughput_tail_grace_seconds=20 * 60,
        clock=lambda: now[0],
    )

    for minute in range(2, 21, 2):
        now[0] = minute * 60
        progress.update("artifact", 950_000 + minute * 5 * 60, 600)
    assert not any("aggregate download throughput" in message for message in messages)

    now[0] = 22 * 60
    progress.update("artifact", 950_000 + 22 * 5 * 60, 600)
    assert sum("aggregate download throughput" in message for message in messages) == 1
    now[0] = 24 * 60
    progress.update("artifact", 1_000_000, 43_400)
    progress.completed()


def test_download_progress_starts_tail_grace_when_final_five_percent_is_reached() -> None:
    now = [0.0]
    messages: list[str] = []
    progress = _DownloadProgress(
        {"artifact": 1_000_000},
        {},
        messages.append,
        interval_seconds=60,
        percent_step=10,
        minimum_throughput_bytes_per_second=50,
        minimum_throughput_window_seconds=120,
        minimum_throughput_tail_percent=5,
        minimum_throughput_tail_grace_seconds=20 * 60,
        clock=lambda: now[0],
    )

    now[0] = 10 * 60
    progress.update("artifact", 950_000, 950_000)
    now[0] = 30 * 60
    progress.update("artifact", 950_600, 600)
    assert not any("aggregate download throughput" in message for message in messages)

    now[0] = 32 * 60
    progress.update("artifact", 951_200, 600)
    assert sum("aggregate download throughput" in message for message in messages) == 1


def test_download_progress_stops_tail_grace_after_progress_rolls_back() -> None:
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
        minimum_throughput_tail_percent=5,
        minimum_throughput_tail_grace_seconds=20 * 60,
        clock=lambda: now[0],
    )

    now[0] = 120
    progress.update("artifact", 95_000, 95_000)
    assert not any("aggregate download throughput" in message for message in messages)
    now[0] = 240
    progress.update("artifact", 0)
    assert sum("aggregate download throughput" in message for message in messages) == 1


def test_http_transfer_progress_does_not_renew_idle_timeout_for_replayed_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game_downloader.delivery as delivery_module

    now = [0.0]
    monkeypatch.setattr(delivery_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    transfer = _HttpTransferProgress(
        "artifact",
        1000,
        0,
        DownloadPolicy(
            http_no_progress_timeout_seconds=10,
            http_minimum_throughput_bytes_per_second=0,
        ),
    )
    for at, position in ((1, 500), (4, 0), (5, 500), (8, 0)):
        now[0] = at
        transfer.update(position)

    now[0] = 11
    with pytest.raises(StageExecutionError, match="no forward progress for 10s"):
        transfer.update(500)


def test_http_transfer_progress_warns_once_per_slowdown_after_fast_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game_downloader.delivery as delivery_module

    now = [0.0]
    messages: list[str] = []
    monkeypatch.setattr(delivery_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    transfer = _HttpTransferProgress(
        "artifact",
        100_000,
        0,
        DownloadPolicy(
            http_throughput_window_seconds=10,
            http_minimum_throughput_bytes_per_second=100,
        ),
        observer=messages.append,
    )
    now[0] = 10
    transfer.update(10_000)
    for elapsed in range(1, 10):
        now[0] = 10 + elapsed
        transfer.update(10_000 + elapsed)

    now[0] = 20
    transfer.update(10_010)
    assert len(messages) == 1
    assert "HTTP forward progress averaged 1.000 B/s" in messages[0]
    assert "continuing current stream" in messages[0]
    for elapsed in range(21, 31):
        now[0] = elapsed
        transfer.update(10_010 + elapsed - 20)
        transfer.remaining()
    assert len(messages) == 1

    now[0] = 31
    transfer.update(20_000)
    for elapsed in range(32, 42):
        now[0] = elapsed
        transfer.update(20_000 + elapsed - 31)
    assert len(messages) == 2


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

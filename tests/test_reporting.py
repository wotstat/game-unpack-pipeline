from __future__ import annotations

import hashlib
from typing import cast

from game_downloader._json import JsonObject
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    DownloadedArtifact,
    DownloadMethod,
    DownloadResult,
    DownloadTrace,
    PartName,
    Stage,
)
from game_downloader.reporting import stage_statistics
from game_downloader.torrent import bytes_path_from_text


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _downloaded_artifact(
    name: str,
    *,
    size: int,
    reused: bool,
    resumed_from: int = 0,
    attempts: int = 1,
    parallel_segments: int = 1,
) -> DownloadedArtifact:
    blob_sha256 = hashlib.sha256(f"blob:{name}".encode()).hexdigest()
    artifact = AcquisitionArtifact(
        artifact_id=_digest(f"artifact:{name}"),
        role="client-file",
        part=PartName.CLIENT,
        part_version="1",
        acquisition_mode=AcquisitionMode.REFERENCE,
        path=bytes_path_from_text(name),
        size=size,
        source_urls=(f"https://cdn.invalid/{name}",),
        torrent_descriptor_sha256=hashlib.sha256(b"torrent").hexdigest(),
    )
    return DownloadedArtifact(
        artifact=artifact,
        blob_sha256=blob_sha256,
        blob_size=size,
        blob_path=f"cache/blobs/sha256/{blob_sha256[:2]}/{blob_sha256}",
        source_hash_verified=True,
        reused=reused,
        transport=DownloadTrace(
            method=DownloadMethod.WEB_SEED,
            requested_url=f"https://cdn.invalid/{name}",
            final_url=f"https://cdn.invalid/{name}",
            resumed_from=resumed_from,
            attempts=attempts,
            parallel_segments=parallel_segments,
        ),
    )


def test_download_statistics_separate_network_cache_and_resume_bytes() -> None:
    fetched = _downloaded_artifact(
        "fetched.bin",
        size=100,
        reused=False,
        resumed_from=20,
        attempts=2,
        parallel_segments=4,
    )
    reused = _downloaded_artifact("reused.bin", size=200, reused=True)
    result = DownloadResult(
        acquisition_plan_sha256=_digest("plan"),
        artifacts=(fetched, reused),
        downloaded_bytes=300,
        reused_artifacts=1,
    )

    statistics = stage_statistics(
        Stage.DOWNLOAD,
        cast(JsonObject, result.model_dump(mode="json")),
        duration_seconds=10,
    )

    assert statistics == {
        "artifacts": 2,
        "payload_bytes": 300,
        "fetched_artifacts": 1,
        "network_bytes_estimate": 80,
        "reused_artifacts": 1,
        "reused_bytes": 200,
        "resumed_bytes": 20,
        "download_attempts": 2,
        "parallel_range_artifacts": 1,
        "parallel_range_segments": 4,
        "web_seed_artifacts": 1,
        "torrent_artifacts": 0,
        "network_bytes_per_second": 8.0,
    }


def test_custom_stage_payload_does_not_break_reporting() -> None:
    assert stage_statistics(Stage.RESOLVE, {"custom": "payload"}, 1.0) == {}


def test_snapshot_statistics_include_phase_timings() -> None:
    statistics = stage_statistics(
        Stage.SNAPSHOT,
        {
            "file_records": 10,
            "actionscript_records": 2,
            "stub_records": 3,
            "package_records": 4,
            "conflict_records": 5,
            "timings": {
                "populate_seconds": 1.1,
                "seal_seconds": 0.2,
                "verify_descriptor_seconds": 0.3,
                "verify_manifests_seconds": 1.4,
                "verify_payload_seconds": 2.5,
                "publish_seconds": 0.1,
            },
        },
        duration_seconds=10,
    )

    assert statistics == {
        "file_records": 10,
        "actionscript_records": 2,
        "stub_records": 3,
        "package_records": 4,
        "conflict_records": 5,
        "records": 24,
        "populate_seconds": 1.1,
        "seal_seconds": 0.2,
        "verify_descriptor_seconds": 0.3,
        "verify_manifests_seconds": 1.4,
        "verify_payload_seconds": 2.5,
        "publish_seconds": 0.1,
        "records_per_second": 2.4,
    }

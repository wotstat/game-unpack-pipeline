from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_downloader.wgus import TargetRegistry
from scripts.release_status import (
    TARGETS,
    ReleaseStatusError,
    compare_release,
    load_status,
    main,
    record_run,
)


def write_document(status_dir: Path, target: str, payload: object) -> Path:
    status_dir.mkdir(exist_ok=True)
    path = status_dir / f"{target}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def empty_status() -> dict[str, object]:
    return {"release_name": None, "readable_version": None, "last_run": None}


def successful_status() -> dict[str, object]:
    return {
        "release_name": "2.3.1.5400",
        "readable_version": "2.3.1.3 #925",
        "last_run": {
            "result": "success",
            "release_name": "2.3.1.5400",
            "readable_version": "2.3.1.3 #925",
            "started_at": "2026-08-29T10:00:00Z",
            "completed_at": "2026-08-29T10:45:00Z",
            "duration_seconds": 2700,
            "run_id": 100,
            "run_attempt": 1,
            "run_url": "https://github.com/wotstat/game-unpack-pipeline/actions/runs/100",
        },
    }


def test_successful_run_updates_current_version_and_last_run(tmp_path: Path) -> None:
    write_document(tmp_path, "wot-eu", empty_status())

    changed = record_run(
        tmp_path,
        target="wot-eu",
        result="success",
        release_name="2.3.1.5400",
        readable_version="2.3.1.3 #925",
        started_at="2026-08-29T13:00:00+03:00",
        completed_at="2026-08-29T10:45:00Z",
        run_id=100,
        run_attempt=1,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/100",
    )

    assert changed is True
    assert json.loads((tmp_path / "wot-eu.json").read_text()) == successful_status()
    assert load_status(tmp_path, "wot-eu").readable_version == "2.3.1.3 #925"
    assert (
        record_run(
            tmp_path,
            target="wot-eu",
            result="success",
            release_name="2.3.1.5400",
            readable_version="2.3.1.3 #925",
            started_at="2026-08-29T10:00:00Z",
            completed_at="2026-08-29T10:45:00Z",
            run_id=100,
            run_attempt=1,
            run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/100",
        )
        is False
    )


def test_failed_run_preserves_last_successful_version(tmp_path: Path) -> None:
    write_document(tmp_path, "wot-eu", successful_status())

    assert record_run(
        tmp_path,
        target="wot-eu",
        result="failure",
        release_name="2.3.2.5500",
        readable_version="2.3.2.0 #930",
        started_at="2026-08-30T09:00:00Z",
        completed_at="2026-08-30T09:12:03Z",
        run_id=101,
        run_attempt=2,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/101",
    )

    status = load_status(tmp_path, "wot-eu")
    assert status.release_name == "2.3.1.5400"
    assert status.readable_version == "2.3.1.3 #925"
    assert status.last_run is not None
    assert status.last_run.result == "failure"
    assert status.last_run.readable_version == "2.3.2.0 #930"
    assert status.last_run.duration_seconds == 723


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_release_comparison_blocks_automatic_retry_of_failed_release(
    tmp_path: Path,
    result: str,
) -> None:
    write_document(tmp_path, "wot-eu", successful_status())
    record_run(
        tmp_path,
        target="wot-eu",
        result=result,
        release_name="2.3.2.5500",
        readable_version=None,
        started_at="2026-08-30T09:00:00Z",
        completed_at="2026-08-30T09:12:03Z",
        run_id=101,
        run_attempt=1,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/101",
    )

    failed = compare_release(
        tmp_path,
        target="wot-eu",
        current_release_name="2.3.2.5500",
    )
    newer = compare_release(
        tmp_path,
        target="wot-eu",
        current_release_name="2.3.3.5600",
    )
    published = compare_release(
        tmp_path,
        target="wot-eu",
        current_release_name="2.3.1.5400",
    )

    assert failed.mismatch is True
    assert failed.retry_blocked is True
    assert newer.mismatch is True
    assert newer.retry_blocked is False
    assert published.mismatch is False
    assert published.retry_blocked is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"release_name": None, "readable_version": None, "last_run": None, "extra": True},
        {"release_name": "2.3.1.5400", "readable_version": None, "last_run": None},
        {"release_name": None, "readable_version": "2.3.1.3 #925", "last_run": None},
        {"release_name": "", "readable_version": "2.3.1.3 #925", "last_run": None},
        {"release_name": "2.3.1.5400", "readable_version": "v.2.3.1.3 #925", "last_run": None},
        {"release_name": " 2.3.1.5400", "readable_version": "2.3.1.3 #925", "last_run": None},
        {
            "release_name": "2.3.1\nforged=true",
            "readable_version": "2.3.1.3 #925",
            "last_run": None,
        },
        {"release_name": "x" * 257, "readable_version": "2.3.1.3 #925", "last_run": None},
        {"release_name": 123, "readable_version": "2.3.1.3 #925", "last_run": None},
        [],
    ],
)
def test_status_reader_rejects_invalid_documents(tmp_path: Path, payload: object) -> None:
    write_document(tmp_path, "wot-eu", payload)

    with pytest.raises(ReleaseStatusError):
        load_status(tmp_path, "wot-eu")


def test_status_reader_rejects_mismatched_run_url(tmp_path: Path) -> None:
    payload = successful_status()
    assert isinstance(payload["last_run"], dict)
    payload["last_run"]["run_url"] = (
        "https://github.com/wotstat/game-unpack-pipeline/actions/runs/999"
    )
    write_document(tmp_path, "wot-eu", payload)

    with pytest.raises(ReleaseStatusError, match="run_url"):
        load_status(tmp_path, "wot-eu")


def test_status_reader_fails_closed_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReleaseStatusError, match="absent"):
        load_status(tmp_path, "wot-eu")


def test_status_cli_reports_validation_failure_without_recreating_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--status-dir", str(tmp_path), "read", "--target", "wot-eu"]) == 2
    assert "release status error" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_repository_contains_one_status_per_target() -> None:
    repository_root = Path(__file__).parents[1]
    status_dir = repository_root / "status"

    assert set(TARGETS) == set(TargetRegistry.load().targets)
    assert {path.stem for path in status_dir.glob("*.json")} == set(TARGETS)
    assert len([load_status(status_dir, target) for target in TARGETS]) == len(TARGETS)

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
def test_release_comparison_delays_retry_but_allows_new_release(
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
        now=datetime(2026, 8, 30, 10, tzinfo=UTC),
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
    assert failed.next_retry_at == "2026-08-30T21:12:03Z"
    assert newer.mismatch is True
    assert newer.retry_blocked is False
    assert newer.next_retry_at is None
    assert published.mismatch is False
    assert published.retry_blocked is False
    assert published.next_retry_at is None


def record_attempt(
    status_dir: Path,
    completed_at: datetime,
    *,
    release: str | None = "2.3.2.5500",
    result: str = "failure",
    run_id: int = 101,
) -> bool:
    return record_run(
        status_dir,
        target="wot-eu",
        result=result,
        release_name=release,
        readable_version="2.3.2.0 #930" if result == "success" else None,
        started_at=(completed_at - timedelta(minutes=10)).isoformat(),
        completed_at=completed_at.isoformat(),
        run_id=run_id,
        run_attempt=1,
        run_url=f"https://github.com/wotstat/game-unpack-pipeline/actions/runs/{run_id}",
    )


@pytest.mark.parametrize(
    ("age", "interval"),
    [
        (timedelta(), timedelta(hours=12)),
        (timedelta(days=3, seconds=-1), timedelta(hours=12)),
        (timedelta(days=3), timedelta(days=1)),
        (timedelta(days=10, seconds=-1), timedelta(days=1)),
        (timedelta(days=10), timedelta(days=7)),
        (timedelta(days=100), timedelta(days=7)),
    ],
)
def test_retry_schedule_boundaries_use_first_failure_and_last_attempt(
    tmp_path: Path, age: timedelta, interval: timedelta
) -> None:
    write_document(tmp_path, "wot-eu", successful_status())
    first = datetime(2026, 8, 30, 10, tzinfo=UTC)
    record_attempt(tmp_path, first)
    last = first + age
    record_attempt(tmp_path, last, run_id=102)

    due = last + interval
    for now, blocked in (
        (due - timedelta(seconds=1), True),
        (due, False),
        (due + timedelta(days=30), False),
    ):
        result = compare_release(
            tmp_path, target="wot-eu", current_release_name="2.3.2.5500", now=now
        )
        assert result.retry_blocked is blocked
        assert result.next_retry_at == due.isoformat().replace("+00:00", "Z")
    retry = load_status(tmp_path, "wot-eu").retry
    assert retry is not None
    assert retry.first_failed_at == "2026-08-30T10:00:00Z"
    # Reads, including overdue checks, must not consume a retry or move its deadline.
    assert record_attempt(tmp_path, last, run_id=102) is False


def test_success_clears_retry_and_new_release_starts_new_schedule(tmp_path: Path) -> None:
    write_document(tmp_path, "wot-eu", successful_status())
    first = datetime(2026, 8, 30, 10, tzinfo=UTC)
    record_attempt(tmp_path, first)
    record_attempt(tmp_path, first + timedelta(days=20), release="2.3.3.5600", run_id=102)

    result = compare_release(
        tmp_path,
        target="wot-eu",
        current_release_name="2.3.3.5600",
        now=first + timedelta(days=20),
    )
    assert result.next_retry_at == "2026-09-19T22:00:00Z"

    record_attempt(
        tmp_path,
        first + timedelta(days=21),
        release="2.3.3.5600",
        result="success",
        run_id=103,
    )
    assert load_status(tmp_path, "wot-eu").retry is None
    assert "retry" not in json.loads((tmp_path / "wot-eu.json").read_text())
    assert not compare_release(
        tmp_path, target="wot-eu", current_release_name="2.3.3.5600"
    ).mismatch


def test_unknown_or_published_release_failure_preserves_pending_retry(tmp_path: Path) -> None:
    write_document(tmp_path, "wot-eu", successful_status())
    first = datetime(2026, 8, 30, 10, tzinfo=UTC)
    record_attempt(tmp_path, first)
    pending = load_status(tmp_path, "wot-eu").retry

    for run_id, release in enumerate((None, "2.3.1.5400"), 102):
        record_attempt(tmp_path, first + timedelta(hours=1), release=release, run_id=run_id)
        assert load_status(tmp_path, "wot-eu").retry == pending


def test_old_status_uses_last_failure_as_initial_retry_anchor(tmp_path: Path) -> None:
    write_document(tmp_path, "wot-eu", successful_status())
    first = datetime(2026, 8, 30, 10, tzinfo=UTC)
    record_attempt(tmp_path, first)
    path = tmp_path / "wot-eu.json"
    legacy = json.loads(path.read_text())
    del legacy["retry"]
    write_document(tmp_path, "wot-eu", legacy)

    result = compare_release(
        tmp_path, target="wot-eu", current_release_name="2.3.2.5500", now=first
    )
    assert result.next_retry_at == "2026-08-30T22:00:00Z"
    record_attempt(tmp_path, first + timedelta(days=1), run_id=102)
    retry = load_status(tmp_path, "wot-eu").retry
    assert retry is not None
    assert retry.first_failed_at == "2026-08-30T10:00:00Z"


@pytest.mark.parametrize(
    "retry",
    [
        {},
        {"release_name": "2.3.2.5500", "first_failed_at": "invalid", "last_failed_at": "invalid"},
        {
            "release_name": "2.3.2.5500",
            "first_failed_at": "2026-08-31T10:00:00Z",
            "last_failed_at": "2026-08-30T10:00:00Z",
        },
        {
            "release_name": "2.3.1.5400",
            "first_failed_at": "2026-08-30T10:00:00Z",
            "last_failed_at": "2026-08-30T10:00:00Z",
        },
        {
            "release_name": "2.3.3.5600",
            "first_failed_at": "2026-08-30T10:00:00Z",
            "last_failed_at": "2026-08-30T10:00:00Z",
        },
        {
            "release_name": "2.3.2.5500",
            "first_failed_at": "2026-08-30T10:00:00Z",
            "last_failed_at": "2026-08-31T10:00:00Z",
        },
    ],
)
def test_retry_state_is_validated_before_dispatch(tmp_path: Path, retry: object) -> None:
    write_document(tmp_path, "wot-eu", successful_status())
    record_attempt(tmp_path, datetime(2026, 8, 30, 10, tzinfo=UTC))
    path = tmp_path / "wot-eu.json"
    payload = json.loads(path.read_text())
    payload["retry"] = retry
    write_document(tmp_path, "wot-eu", payload)

    with pytest.raises(ReleaseStatusError, match="retry"):
        compare_release(tmp_path, target="wot-eu", current_release_name="2.3.2.5500")


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


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (["success"] * 5, "success"),
        (["failure", "skipped", "skipped", "skipped", "skipped"], "failure"),
        (["success", "failure", "skipped", "skipped", "skipped"], "failure"),
        (["success", "success", "failure", "success", "success"], "failure"),
        (["success", "success", "success", "failure", "success"], "failure"),
        (["success", "success", "success", "success", "failure"], "failure"),
        (["success", "cancelled", "skipped", "skipped", "skipped"], "cancelled"),
        (["success", "success", "success", "cancelled", "success"], "cancelled"),
        (["success", "success", "skipped", "success", "success"], "failure"),
        (["success", "success", "failure", "cancelled", "success"], "failure"),
    ],
)
def test_processing_result_requires_every_enabled_job_to_succeed(
    results: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["processing-result", *results]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_repository_contains_one_status_per_target() -> None:
    repository_root = Path(__file__).parents[1]
    status_dir = repository_root / "status"

    assert set(TARGETS) == set(TargetRegistry.load().targets)
    assert {path.stem for path in status_dir.glob("*.json")} == set(TARGETS)
    assert len([load_status(status_dir, target) for target in TARGETS]) == len(TARGETS)

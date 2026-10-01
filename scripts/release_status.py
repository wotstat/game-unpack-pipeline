#!/usr/bin/env python3
"""Validate and update repository pipeline status files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

TARGETS = (
    "wot-eu",
    "wot-na",
    "wot-asia",
    "wot-common-test",
    "wot-cn",
    "mt-ru",
    "mt-public-test",
)
PIPELINE_RESULTS = ("success", "failure", "cancelled")
MAX_RELEASE_NAME_LENGTH = 256
MAX_RUN_DURATION_SECONDS = 24 * 60 * 60
READABLE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3} #[0-9]+$")
RUN_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/([0-9]+)$"
)

PipelineResult = Literal["success", "failure", "cancelled"]


class ReleaseStatusError(ValueError):
    """A status file is absent or violates the repository contract."""


@dataclass(frozen=True)
class PipelineRun:
    result: PipelineResult
    release_name: str | None
    readable_version: str | None
    started_at: str
    completed_at: str
    duration_seconds: int
    run_id: int
    run_attempt: int
    run_url: str

    def as_json(self) -> dict[str, str | int | None]:
        return {
            "result": self.result,
            "release_name": self.release_name,
            "readable_version": self.readable_version,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "run_url": self.run_url,
        }


@dataclass(frozen=True)
class ReleaseRetry:
    release_name: str
    first_failed_at: str
    last_failed_at: str

    def as_json(self) -> dict[str, str]:
        return {
            "release_name": self.release_name,
            "first_failed_at": self.first_failed_at,
            "last_failed_at": self.last_failed_at,
        }

    def next_retry(self) -> datetime:
        first = datetime.fromisoformat(self.first_failed_at)
        last = datetime.fromisoformat(self.last_failed_at)
        age = last - first
        if age < timedelta(days=3):
            interval = timedelta(hours=12)
        elif age < timedelta(days=10):
            interval = timedelta(days=1)
        else:
            interval = timedelta(days=7)
        return last + interval


@dataclass(frozen=True)
class ReleaseStatus:
    release_name: str | None
    readable_version: str | None
    last_run: PipelineRun | None
    retry: ReleaseRetry | None = None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "release_name": self.release_name,
            "readable_version": self.readable_version,
            "last_run": self.last_run.as_json() if self.last_run is not None else None,
        }
        if self.retry is not None:
            payload["retry"] = self.retry.as_json()
        return payload


@dataclass(frozen=True)
class ReleaseComparison:
    stored_release_name: str | None
    current_release_name: str
    mismatch: bool
    retry_blocked: bool
    next_retry_at: str | None

    def as_json(self) -> dict[str, str | bool | None]:
        return {
            "stored_release_name": self.stored_release_name,
            "current_release_name": self.current_release_name,
            "mismatch": self.mismatch,
            "retry_blocked": self.retry_blocked,
            "next_retry_at": self.next_retry_at,
        }


def _valid_release_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\r" not in value
        and "\n" not in value
        and len(value) <= MAX_RELEASE_NAME_LENGTH
    )


def _valid_readable_version(value: object) -> bool:
    return isinstance(value, str) and READABLE_VERSION_RE.fullmatch(value) is not None


def _optional(
    value: object,
    validator: Callable[[object], bool],
    label: str,
) -> str | None:
    if value is None:
        return None
    if not validator(value):
        raise ReleaseStatusError(f"{label} has an invalid value")
    assert isinstance(value, str)
    return value


def _timestamp(value: object, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise ReleaseStatusError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseStatusError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReleaseStatusError(f"{label} must include a timezone")
    utc = parsed.astimezone(UTC).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z"), utc


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ReleaseStatusError(f"{label} must be a positive integer")
    return value


def _pipeline_result(value: object) -> PipelineResult:
    if value not in PIPELINE_RESULTS:
        raise ReleaseStatusError(f"result must be one of: {', '.join(PIPELINE_RESULTS)}")
    return cast(PipelineResult, value)


def _load_run(payload: object) -> PipelineRun:
    fields = {
        "result",
        "release_name",
        "readable_version",
        "started_at",
        "completed_at",
        "duration_seconds",
        "run_id",
        "run_attempt",
        "run_url",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ReleaseStatusError("last_run has invalid fields")

    result = _pipeline_result(payload["result"])
    release_name = _optional(payload["release_name"], _valid_release_name, "last_run.release_name")
    readable_version = _optional(
        payload["readable_version"],
        _valid_readable_version,
        "last_run.readable_version",
    )
    if readable_version is not None and release_name is None:
        raise ReleaseStatusError("last_run.readable_version requires last_run.release_name")
    if result == "success" and (release_name is None or readable_version is None):
        raise ReleaseStatusError("a successful last_run requires both version fields")

    started_at, started = _timestamp(payload["started_at"], "last_run.started_at")
    completed_at, completed = _timestamp(payload["completed_at"], "last_run.completed_at")
    if completed < started:
        raise ReleaseStatusError("last_run.completed_at precedes last_run.started_at")
    expected_duration = int((completed - started).total_seconds())
    duration_seconds = payload["duration_seconds"]
    if (
        not isinstance(duration_seconds, int)
        or isinstance(duration_seconds, bool)
        or duration_seconds != expected_duration
        or duration_seconds > MAX_RUN_DURATION_SECONDS
    ):
        raise ReleaseStatusError("last_run.duration_seconds does not match its timestamps")

    run_id = _positive_integer(payload["run_id"], "last_run.run_id")
    run_attempt = _positive_integer(payload["run_attempt"], "last_run.run_attempt")
    run_url = payload["run_url"]
    match = RUN_URL_RE.fullmatch(run_url) if isinstance(run_url, str) else None
    if match is None or int(match.group(1)) != run_id:
        raise ReleaseStatusError("last_run.run_url is not the matching GitHub Actions run URL")

    return PipelineRun(
        result=result,
        release_name=release_name,
        readable_version=readable_version,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        run_id=run_id,
        run_attempt=run_attempt,
        run_url=run_url,
    )


def status_path(status_dir: Path, target: str) -> Path:
    if target not in TARGETS:
        raise ReleaseStatusError(f"unknown target: {target}")
    return status_dir / f"{target}.json"


def parse_status_document(payload: object, source: object = "status document") -> ReleaseStatus:
    fields = {"release_name", "readable_version", "last_run"}
    if not isinstance(payload, dict) or not fields <= set(payload) <= fields | {"retry"}:
        raise ReleaseStatusError(f"status document has invalid fields: {source}")

    release_name = _optional(payload["release_name"], _valid_release_name, "release_name")
    readable_version = _optional(
        payload["readable_version"], _valid_readable_version, "readable_version"
    )
    if (release_name is None) != (readable_version is None):
        raise ReleaseStatusError("release_name and readable_version must both be set or null")
    last_run = _load_run(payload["last_run"]) if payload["last_run"] is not None else None
    if (
        last_run is not None
        and last_run.result == "success"
        and (last_run.release_name, last_run.readable_version) != (release_name, readable_version)
    ):
        raise ReleaseStatusError("a successful last_run must match the current version")
    retry = _load_retry(payload.get("retry"), release_name, last_run)
    return ReleaseStatus(
        release_name=release_name,
        readable_version=readable_version,
        last_run=last_run,
        retry=retry,
    )


def _load_retry(
    payload: object, published_release: str | None, last_run: PipelineRun | None
) -> ReleaseRetry | None:
    if payload is None:
        # Old status documents have only the most recent attempt as a time anchor.
        if (
            last_run is not None
            and last_run.result != "success"
            and last_run.release_name is not None
            and last_run.release_name != published_release
        ):
            return ReleaseRetry(last_run.release_name, last_run.completed_at, last_run.completed_at)
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "release_name",
        "first_failed_at",
        "last_failed_at",
    }:
        raise ReleaseStatusError("retry has invalid fields")
    release = payload["release_name"]
    if not _valid_release_name(release) or release == published_release:
        raise ReleaseStatusError("retry.release_name must identify an unpublished release")
    first_at, first = _timestamp(payload["first_failed_at"], "retry.first_failed_at")
    last_at, last = _timestamp(payload["last_failed_at"], "retry.last_failed_at")
    if first > last:
        raise ReleaseStatusError("retry.last_failed_at precedes retry.first_failed_at")
    if (
        last_run is None
        or last_run.result == "success"
        or last > datetime.fromisoformat(last_run.completed_at)
        or (
            last_run.release_name is not None
            and last_run.release_name != published_release
            and (release, last_at) != (last_run.release_name, last_run.completed_at)
        )
    ):
        raise ReleaseStatusError("retry does not match last_run")
    return ReleaseRetry(release, first_at, last_at)


def load_status(status_dir: Path, target: str) -> ReleaseStatus:
    path = status_path(status_dir, target)
    if path.is_symlink() or not path.is_file():
        raise ReleaseStatusError(f"status file is absent or not a regular file: {path}")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseStatusError(f"cannot read valid JSON from {path}: {exc}") from exc
    return parse_status_document(payload, path)


def _empty_as_none(value: str) -> str | None:
    return None if value == "" else value


def _write_status(status_dir: Path, target: str, status: ReleaseStatus) -> None:
    path = status_path(status_dir, target)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(status.as_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def compare_release(
    status_dir: Path,
    *,
    target: str,
    current_release_name: str,
    now: datetime | None = None,
) -> ReleaseComparison:
    if not _valid_release_name(current_release_name):
        raise ReleaseStatusError("current_release_name has an invalid value")
    current = load_status(status_dir, target)
    mismatch = current.release_name != current_release_name
    _, checked_at = _timestamp((now or datetime.now(UTC)).isoformat(), "now")
    next_retry = (
        current.retry.next_retry()
        if mismatch
        and current.retry is not None
        and current.retry.release_name == current_release_name
        else None
    )
    return ReleaseComparison(
        stored_release_name=current.release_name,
        current_release_name=current_release_name,
        mismatch=mismatch,
        retry_blocked=next_retry is not None and checked_at < next_retry,
        next_retry_at=next_retry.isoformat().replace("+00:00", "Z") if next_retry else None,
    )


def record_run(
    status_dir: Path,
    *,
    target: str,
    result: str,
    release_name: str | None,
    readable_version: str | None,
    started_at: str,
    completed_at: str,
    run_id: int,
    run_attempt: int,
    run_url: str,
) -> bool:
    current = load_status(status_dir, target)
    normalized_started_at, started = _timestamp(started_at, "started_at")
    normalized_completed_at, completed = _timestamp(completed_at, "completed_at")
    if completed < started:
        raise ReleaseStatusError("completed_at precedes started_at")

    normalized_result = _pipeline_result(result)
    normalized_release_name = _optional(release_name, _valid_release_name, "release_name")
    normalized_readable_version = _optional(
        readable_version, _valid_readable_version, "readable_version"
    )
    run = _load_run(
        {
            "result": normalized_result,
            "release_name": normalized_release_name,
            "readable_version": normalized_readable_version,
            "started_at": normalized_started_at,
            "completed_at": normalized_completed_at,
            "duration_seconds": int((completed - started).total_seconds()),
            "run_id": run_id,
            "run_attempt": run_attempt,
            "run_url": run_url,
        }
    )

    if run.result == "success":
        next_release_name = run.release_name
        next_readable_version = run.readable_version
        retry = None
    else:
        next_release_name = current.release_name
        next_readable_version = current.readable_version
        retry = current.retry
        if run.release_name is not None and run.release_name != current.release_name:
            first_failed_at = (
                min(retry.first_failed_at, run.completed_at)
                if retry is not None and retry.release_name == run.release_name
                else run.completed_at
            )
            retry = ReleaseRetry(run.release_name, first_failed_at, run.completed_at)
    updated = ReleaseStatus(
        release_name=next_release_name,
        readable_version=next_readable_version,
        last_run=run,
        retry=retry,
    )
    if updated == current:
        return False
    _write_status(status_dir, target, updated)
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-dir", type=Path, default=Path("status"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    read = subparsers.add_parser("read", help="Validate and print one status document")
    read.add_argument("--target", choices=TARGETS, required=True)

    compare = subparsers.add_parser(
        "compare", help="Compare a detected release with publication and attempt status"
    )
    compare.add_argument("--target", choices=TARGETS, required=True)
    compare.add_argument("--current-release-name", required=True)

    processing_result = subparsers.add_parser(
        "processing-result", help="Summarize required processing jobs, excluding finalization"
    )
    processing_result.add_argument("results", nargs="+", choices=(*PIPELINE_RESULTS, "skipped"))

    record = subparsers.add_parser("record", help="Record one completed pipeline run")
    record.add_argument("--target", choices=TARGETS, required=True)
    record.add_argument("--result", choices=PIPELINE_RESULTS, required=True)
    record.add_argument("--release-name", default="")
    record.add_argument("--readable-version", default="")
    record.add_argument("--started-at", required=True)
    record.add_argument("--completed-at", required=True)
    record.add_argument("--run-id", type=int, required=True)
    record.add_argument("--run-attempt", type=int, required=True)
    record.add_argument("--run-url", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "read":
            status = load_status(arguments.status_dir, arguments.target)
            print(json.dumps(status.as_json(), ensure_ascii=False, separators=(",", ":")))
        elif arguments.command == "compare":
            comparison = compare_release(
                arguments.status_dir,
                target=arguments.target,
                current_release_name=arguments.current_release_name,
            )
            print(json.dumps(comparison.as_json(), ensure_ascii=False, separators=(",", ":")))
        elif arguments.command == "processing-result":
            results = arguments.results
            if "failure" in results:
                print("failure")
            elif "cancelled" in results:
                print("cancelled")
            else:
                # Disabled consumers are excluded by the caller. A skipped required job failed.
                print("success" if all(result == "success" for result in results) else "failure")
        else:
            changed = record_run(
                arguments.status_dir,
                target=arguments.target,
                result=arguments.result,
                release_name=_empty_as_none(arguments.release_name),
                readable_version=_empty_as_none(arguments.readable_version),
                started_at=arguments.started_at,
                completed_at=arguments.completed_at,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                run_url=arguments.run_url,
            )
            print("changed" if changed else "unchanged")
    except ReleaseStatusError as exc:
        print(f"release status error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

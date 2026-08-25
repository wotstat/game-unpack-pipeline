#!/usr/bin/env python3
"""Validate and update the repository release status files."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TARGETS = (
    "wot-eu",
    "wot-na",
    "wot-asia",
    "wot-common-test",
    "wot-cn",
    "mt-ru",
    "mt-public-test",
)
MAX_RELEASE_NAME_LENGTH = 256


class ReleaseStatusError(ValueError):
    """A status file is absent or violates the repository contract."""


@dataclass(frozen=True)
class ReleaseStatus:
    release_name: str | None

    def as_json(self) -> dict[str, str | None]:
        return {"release_name": self.release_name}


def _valid_release_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\r" not in value
        and "\n" not in value
        and len(value) <= MAX_RELEASE_NAME_LENGTH
    )


def status_path(status_dir: Path, target: str) -> Path:
    if target not in TARGETS:
        raise ReleaseStatusError(f"unknown target: {target}")
    return status_dir / f"{target}.json"


def load_status(status_dir: Path, target: str) -> ReleaseStatus:
    path = status_path(status_dir, target)
    if path.is_symlink() or not path.is_file():
        raise ReleaseStatusError(f"status file is absent or not a regular file: {path}")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseStatusError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"release_name"}:
        raise ReleaseStatusError(f"status file must contain only release_name: {path}")
    release_name = payload["release_name"]
    if release_name is not None and not _valid_release_name(release_name):
        raise ReleaseStatusError(f"release_name must be a non-empty trimmed string or null: {path}")
    return ReleaseStatus(release_name=release_name)


def write_status(status_dir: Path, target: str, release_name: str) -> bool:
    current = load_status(status_dir, target)
    if not _valid_release_name(release_name):
        raise ReleaseStatusError("new release_name must be a non-empty trimmed string")
    if current.release_name == release_name:
        return False

    path = status_path(status_dir, target)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                ReleaseStatus(release_name=release_name).as_json(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-dir", type=Path, default=Path("status"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    read = subparsers.add_parser("read", help="Validate and print one status document")
    read.add_argument("--target", choices=TARGETS, required=True)

    write = subparsers.add_parser("write", help="Update one existing status document")
    write.add_argument("--target", choices=TARGETS, required=True)
    write.add_argument("--release-name", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "read":
            status = load_status(arguments.status_dir, arguments.target)
            print(json.dumps(status.as_json(), ensure_ascii=False, separators=(",", ":")))
        else:
            changed = write_status(
                arguments.status_dir,
                arguments.target,
                arguments.release_name,
            )
            print("changed" if changed else "unchanged")
    except ReleaseStatusError as exc:
        print(f"release status error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

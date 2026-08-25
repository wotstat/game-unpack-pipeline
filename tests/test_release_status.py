from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_downloader.wgus import TargetRegistry
from scripts.release_status import (
    TARGETS,
    ReleaseStatusError,
    load_status,
    main,
    write_status,
)


def write_document(status_dir: Path, target: str, payload: object) -> Path:
    status_dir.mkdir(exist_ok=True)
    path = status_dir / f"{target}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_status_round_trip_changes_only_release_name(tmp_path: Path) -> None:
    write_document(tmp_path, "wot-eu", {"release_name": None})

    assert load_status(tmp_path, "wot-eu").release_name is None
    assert write_status(tmp_path, "wot-eu", "2.3.1.5400") is True
    assert load_status(tmp_path, "wot-eu").release_name == "2.3.1.5400"
    assert json.loads((tmp_path / "wot-eu.json").read_text()) == {"release_name": "2.3.1.5400"}
    assert write_status(tmp_path, "wot-eu", "2.3.1.5400") is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"release_name": None, "extra": True},
        {"release_name": ""},
        {"release_name": " 2.3.1.5400"},
        {"release_name": "2.3.1\nforged=true"},
        {"release_name": "x" * 257},
        {"release_name": 123},
        [],
    ],
)
def test_status_reader_rejects_invalid_documents(tmp_path: Path, payload: object) -> None:
    write_document(tmp_path, "wot-eu", payload)

    with pytest.raises(ReleaseStatusError):
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


def test_repository_contains_one_minimal_status_per_target() -> None:
    repository_root = Path(__file__).parents[1]
    status_dir = repository_root / "status"

    assert set(TARGETS) == set(TargetRegistry.load().targets)
    assert {path.stem for path in status_dir.glob("*.json")} == set(TARGETS)
    assert len([load_status(status_dir, target) for target in TARGETS]) == len(TARGETS)

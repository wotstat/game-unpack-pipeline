from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest
from click.testing import CliRunner

from game_downloader._json import JsonValue
from game_downloader.acquisition import AcquisitionPolicy
from game_downloader.cli import _actionscript_workers, _verification_workers, cli
from game_downloader.models import Stage
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation


def test_version_command() -> None:
    result = CliRunner().invoke(cli, ["version", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["name"] == "game-downloader"


def test_probe_release_command_is_machine_readable_and_has_no_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "game_downloader.cli.WgusResolver.probe_release_name",
        lambda _resolver, _client_type: "2.3.1.5400",
    )

    result = CliRunner().invoke(
        cli,
        ["probe-release", "--target", "wot-eu", "--client-type", "sd", "--json"],
        env={"GAME_DOWNLOADER_DATA_ROOT": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "release_name": "2.3.1.5400",
        "target": "wot-eu",
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("logical_workers", "expected"),
    [(1, 1), (6, 1), (8, 1), (16, 2), (24, 3), (32, 4)],
)
def test_actionscript_parallelism_reserves_about_eight_cpus_per_ffdec(
    logical_workers: int,
    expected: int,
) -> None:
    assert _actionscript_workers(logical_workers) == expected


@pytest.mark.parametrize(
    ("logical_workers", "expected"),
    [(1, 1), (2, 2), (4, 4), (16, 4), (32, 4)],
)
def test_verification_parallelism_is_bounded(
    logical_workers: int,
    expected: int,
) -> None:
    assert _verification_workers(logical_workers) == expected


def test_engine_stubs_have_no_separate_cli_command() -> None:
    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0, result.output
    assert "engine-stubs" not in CliRunner().invoke(cli, ["--help"]).output
    assert "ALL for every available language" in result.output


def test_doctor_checks_contracts_and_workspace(tmp_path: Path) -> None:
    ffdec = tmp_path / "ffdec"
    ffdec.write_text("#!/bin/sh\necho 'JPEXS Free Flash Decompiler v.26.2.1'\n")
    ffdec.chmod(0o755)
    prettier = tmp_path / "prettier"
    prettier.write_text("#!/bin/sh\necho '3.9.6'\n")
    prettier.chmod(0o755)
    result = CliRunner().invoke(
        cli,
        ["doctor", "--data-root", str(tmp_path), "--json"],
        env={
            "GAME_DOWNLOADER_FFDEC": str(ffdec),
            "GAME_DOWNLOADER_PRETTIER": str(prettier),
        },
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True
    assert {check["name"] for check in report["checks"]} == {
        "archive-tool",
        "actionscript-decompiler",
        "aria2-torrent-fallback",
        "python-3.13",
        "contracts",
        "pyc-decompiler",
        "targets",
        "web-formatter",
        "workspace",
    }


def test_doctor_json_remains_machine_readable_on_failure(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["doctor", "--data-root", str(tmp_path), "--json"],
        env={"GAME_DOWNLOADER_CONTRACTS_ROOT": str(tmp_path / "missing-contracts")},
    )

    assert result.exit_code == 3
    report = json.loads(result.output)
    assert report["ok"] is False
    assert next(check for check in report["checks"] if check["name"] == "contracts")["ok"] is False


def test_cli_runs_production_resolve_seam_and_reuses_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        calls.append(context.request.target)
        return {"fixture": "offline-resolve"}

    def implementation(_target: object) -> StageImplementation:
        return StageImplementation(implementation_version="offline-cli-test", execute=execute)

    monkeypatch.setattr("game_downloader.cli.create_resolve_implementation", implementation)
    runner = CliRunner()
    started = runner.invoke(
        cli,
        [
            "run",
            "--target",
            "wot-eu",
            "--client-type",
            "sd",
            "--languages",
            "EN",
            "--until",
            "resolve",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert started.exit_code == 0, started.output
    assert "resolve: succeeded in " in started.output
    assert "active: " in started.output
    match = re.search(r"Run (run-[a-f0-9]{32}):", started.output)
    assert match is not None
    resumed = runner.invoke(
        cli,
        [
            "resume",
            match.group(1),
            "--until",
            "resolve",
            "--data-root",
            str(tmp_path),
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    assert calls == ["wot-eu"]

    status = runner.invoke(
        cli,
        ["status", match.group(1), "--json", "--data-root", str(tmp_path)],
    )
    assert status.exit_code == 0, status.output
    report = json.loads(status.output)
    assert set(report["request"]) == {"target", "client_type", "languages"}
    assert report["state"] == "paused"
    assert report["active_duration_seconds"] >= 0
    assert report["stages"][0]["duration_seconds"] >= 0
    assert report["stages"][0]["started_at"] is not None
    assert report["stages"][0]["finished_at"] is not None
    assert report["stages"][0]["state"] == "succeeded"


def test_run_and_resume_can_emit_machine_readable_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(_context: StageContext) -> Mapping[str, JsonValue]:
        return {"fixture": "offline-resolve"}

    monkeypatch.setattr(
        "game_downloader.cli.create_resolve_implementation",
        lambda _target: StageImplementation(
            implementation_version="offline-json-cli-test",
            execute=execute,
        ),
    )
    runner = CliRunner()
    started = runner.invoke(
        cli,
        [
            "run",
            "--target",
            "wot-eu",
            "--client-type",
            "sd",
            "--languages",
            "EN",
            "--until",
            "resolve",
            "--data-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert started.exit_code == 0, started.output
    started_report = json.loads(started.stdout)
    assert started_report["state"] == "paused"

    resumed = runner.invoke(
        cli,
        [
            "resume",
            started_report["run_id"],
            "--until",
            "resolve",
            "--data-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["run_id"] == started_report["run_id"]


def test_run_json_preserves_the_report_when_a_stage_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(_context: StageContext) -> Mapping[str, JsonValue]:
        raise StageExecutionError("source_unavailable", "offline fixture failure")

    monkeypatch.setattr(
        "game_downloader.cli.create_resolve_implementation",
        lambda _target: StageImplementation(
            implementation_version="offline-json-failure-cli-test",
            execute=execute,
        ),
    )
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--target",
            "wot-eu",
            "--client-type",
            "sd",
            "--languages",
            "EN",
            "--until",
            "resolve",
            "--data-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 4
    report = json.loads(result.stdout)
    assert report["state"] == "failed"
    assert report["current_stage"] == "resolve"
    assert report["stages"][0]["error"]["code"] == "source_unavailable"


def test_cli_runs_acquisition_plan_with_configured_disk_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Stage, int | None]] = []

    def resolve_execute(context: StageContext) -> Mapping[str, JsonValue]:
        calls.append((context.stage, None))
        return {"fixture": "offline-resolve"}

    def plan_execute(context: StageContext) -> Mapping[str, JsonValue]:
        assert context.upstream is not None
        calls.append((context.stage, 321))
        return {"fixture": "offline-acquisition-plan"}

    def resolve_implementation(_target: object) -> StageImplementation:
        return StageImplementation(
            implementation_version="offline-resolve-cli-test",
            execute=resolve_execute,
        )

    def acquisition_implementation(
        _target: object,
        *,
        policy: AcquisitionPolicy,
    ) -> StageImplementation:
        assert policy.reserve_bytes == 321
        return StageImplementation(
            implementation_version="offline-acquisition-cli-test",
            execute=plan_execute,
        )

    monkeypatch.setattr("game_downloader.cli.create_resolve_implementation", resolve_implementation)
    monkeypatch.setattr(
        "game_downloader.cli.create_acquisition_implementation",
        acquisition_implementation,
    )
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--target",
            "wot-eu",
            "--client-type",
            "sd",
            "--languages",
            "EN",
            "--until",
            "plan-acquisition",
            "--disk-reserve-bytes",
            "321",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(Stage.RESOLVE, None), (Stage.PLAN_ACQUISITION, 321)]


def test_cli_rejects_unknown_target_before_creating_a_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--target",
            "unknown",
            "--client-type",
            "sd",
            "--languages",
            "EN",
            "--until",
            "resolve",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 3
    assert "unknown target" in result.output
    assert not (tmp_path / "runs").exists()

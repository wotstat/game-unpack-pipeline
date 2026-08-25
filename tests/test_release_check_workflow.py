from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/check-game-releases.yml"


def workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(WORKFLOW_PATH.read_text()))


def test_release_checker_is_manual_only_with_safe_defaults() -> None:
    document = workflow()
    trigger = document[True]
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert set(trigger) == {"workflow_dispatch"}
    assert set(inputs) == {
        "check_wot_eu",
        "check_wot_na",
        "check_wot_asia",
        "check_wot_common_test",
        "check_wot_cn",
        "check_mt_ru",
        "check_mt_public_test",
        "dispatch_pipelines",
    }
    assert inputs["check_wot_eu"]["default"] is True
    assert all(item["default"] is False for name, item in inputs.items() if name != "check_wot_eu")
    assert all(item["type"] == "boolean" for item in inputs.values())


def test_release_checker_builds_a_parallel_fail_slow_matrix() -> None:
    document = workflow()
    plan = document["jobs"]["plan"]
    check = document["jobs"]["check"]
    matrix_script = plan["steps"][0]["run"]

    for target in (
        "wot-eu",
        "wot-na",
        "wot-asia",
        "wot-common-test",
        "wot-cn",
        "mt-ru",
        "mt-public-test",
    ):
        assert f"selected+=({target})" in matrix_script
    assert "targets='[]'" in matrix_script
    assert "--compact-output" in matrix_script
    assert check["strategy"]["fail-fast"] is False
    assert check["strategy"]["matrix"]["target"] == ("${{ fromJSON(needs.plan.outputs.targets) }}")
    assert check["permissions"] == {"actions": "write", "contents": "read"}


def test_release_checker_is_fail_closed_and_dry_run_does_not_dispatch() -> None:
    steps = workflow()["jobs"]["check"]["steps"]
    compare = next(step for step in steps if step["name"] == "Probe and compare release")
    active = next(step for step in steps if step["name"] == "Check for an active pipeline")
    dispatch = next(step for step in steps if step["name"] == "Dispatch game release pipeline")

    assert "game-downloader probe-release" in compare["run"]
    assert "scripts/release_status.py read" in compare["run"]
    assert "action=would-dispatch" in compare["run"]
    assert "inputs.dispatch_pipelines" in active["if"]
    assert '.status != "completed"' in active["run"]
    assert "actions/workflows/process-game-release.yml/runs" in active["run"]
    assert 'prefix="Release · ${TARGET} · "' in active["run"]
    assert "steps.active.outputs.found == 'false'" in dispatch["if"]
    assert "gh workflow run process-game-release.yml" in dispatch["run"]
    assert '--ref "${DEFAULT_BRANCH}"' in dispatch["run"]
    assert "--field client_type=sd" in dispatch["run"]
    assert "--field languages=ALL" in dispatch["run"]
    assert "--field publish_wot_src=true" in dispatch["run"]
    assert "--field publish_wot_gui_assets=true" in dispatch["run"]


def test_release_checker_serializes_checks_without_cancelling_running_work() -> None:
    assert workflow()["concurrency"] == {
        "group": "check-game-releases",
        "cancel-in-progress": False,
    }


def test_release_checker_collects_matrix_results_into_one_report() -> None:
    jobs = workflow()["jobs"]
    check = jobs["check"]
    report = jobs["report"]
    steps = check["steps"]
    record = next(step for step in steps if step["name"] == "Record check result")
    upload = next(step for step in steps if step["name"] == "Upload check result")
    download = next(step for step in report["steps"] if step["name"] == "Download check results")
    render = next(step for step in report["steps"] if step["name"] == "Render release check report")

    assert record["if"] == "always()"
    assert "state=up_to_date" in record["run"]
    assert "state=update_available" in record["run"]
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v7.0.1"
    assert report["if"] == "always()"
    assert set(report["needs"]) == {"plan", "check"}
    assert download["uses"] == "actions/download-artifact@v8.0.1"
    assert download["with"]["merge-multiple"] is True
    assert "render_release_check_report.py" in render["run"]

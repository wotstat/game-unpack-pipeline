from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/check-game-releases.yml"


def workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(WORKFLOW_PATH.read_text()))


def test_release_checker_runs_hourly_and_dispatches_all_targets_by_default() -> None:
    document = workflow()
    trigger = document[True]
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert set(trigger) == {"schedule", "workflow_dispatch"}
    assert trigger["schedule"] == [{"cron": "23 * * * *"}]
    assert set(inputs) == {
        "check_wot_eu",
        "check_wot_na",
        "check_wot_asia",
        "check_wot_common_test",
        "check_wot_cn",
        "check_mt_ru",
        "check_mt_public_test",
        "dispatch_pipelines",
        "force_retry",
        "selectel_location",
    }
    boolean_inputs = {name: item for name, item in inputs.items() if name != "selectel_location"}
    assert all(item["default"] is (name != "force_retry") for name, item in boolean_inputs.items())
    assert all(item["type"] == "boolean" for item in boolean_inputs.values())
    pipeline = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/process-game-release.yml").read_text()
    )
    assert (
        inputs["selectel_location"]
        == pipeline[True]["workflow_dispatch"]["inputs"]["selectel_location"]
    )


def test_release_checker_builds_a_parallel_fail_slow_matrix() -> None:
    document = workflow()
    plan = document["jobs"]["plan"]
    check = document["jobs"]["check"]
    matrix_script = plan["steps"][0]["run"]
    matrix_env = plan["steps"][0]["env"]

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
    assert all("github.event_name == 'schedule'" in value for value in matrix_env.values())
    assert check["strategy"]["fail-fast"] is False
    assert check["strategy"]["matrix"]["target"] == ("${{ fromJSON(needs.plan.outputs.targets) }}")
    assert check["permissions"] == {"actions": "write", "contents": "read"}


def test_release_checker_is_fail_closed_and_dry_run_does_not_dispatch() -> None:
    steps = workflow()["jobs"]["check"]["steps"]
    compare = next(step for step in steps if step["name"] == "Probe and compare release")
    active = next(step for step in steps if step["name"] == "Check for an active pipeline")
    dispatch = next(step for step in steps if step["name"] == "Dispatch game release pipeline")

    assert "game-downloader probe-release" in compare["run"]
    assert "scripts/release_status.py compare" in compare["run"]
    assert "--current-release-name" in compare["run"]
    assert ".retry_blocked" in compare["run"]
    assert "action=retry-wait" in compare["run"]
    assert ".next_retry_at" in compare["run"]
    assert "action=would-dispatch" in compare["run"]
    assert "inputs.dispatch_pipelines" in active["if"]
    assert "github.event_name == 'schedule'" in active["if"]
    assert "steps.compare.outputs.retry_blocked != 'true'" in active["if"]
    assert '.status != "completed"' in active["run"]
    assert "actions/workflows/process-game-release.yml/runs" in active["run"]
    assert 'prefix="Release · ${TARGET} · "' in active["run"]
    assert "steps.active.outputs.found == 'false'" in dispatch["if"]
    assert "github.event_name == 'schedule'" in dispatch["if"]
    assert "steps.compare.outputs.retry_blocked != 'true'" in dispatch["if"]
    assert "gh workflow run process-game-release.yml" in dispatch["run"]
    assert '--ref "${DEFAULT_BRANCH}"' in dispatch["run"]
    assert "--field client_type=sd" in dispatch["run"]
    assert "--field languages=ALL" in dispatch["run"]
    assert '--field selectel_location="${SELECTEL_LOCATION}"' in dispatch["run"]
    assert dispatch["env"]["SELECTEL_LOCATION"] == "${{ inputs.selectel_location || 'ru-7b' }}"
    assert '--field detected_release_name="${RELEASE_NAME}"' in dispatch["run"]
    assert "--field publish_wot_src=true" in dispatch["run"]
    assert "--field publish_wot_gui_assets=true" in dispatch["run"]
    assert "--field publish_wotstat_assets=true" in dispatch["run"]


@pytest.mark.parametrize(
    ("force_retry", "mismatch", "retry_blocked", "dispatch_enabled", "active", "expected_action"),
    [
        (False, True, True, True, False, "retry_wait"),
        (True, True, True, True, False, "dispatched"),
        (True, True, True, False, False, "dry_run"),
        (True, True, True, True, True, "already_running"),
        (True, False, False, True, False, "none"),
        (False, True, False, True, False, "dispatched"),
    ],
)
def test_manual_force_retry_preserves_other_dispatch_guards(
    tmp_path: Path,
    force_retry: bool,
    mismatch: bool,
    retry_blocked: bool,
    dispatch_enabled: bool,
    active: bool,
    expected_action: str,
) -> None:
    steps = workflow()["jobs"]["check"]["steps"]
    compare = next(step for step in steps if step.get("id") == "compare")
    record = next(step for step in steps if step["name"] == "Record check result")
    assert compare["env"]["FORCE_RETRY"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.force_retry }}"
    )
    output = tmp_path / "outputs"
    result_path = tmp_path / "result.json"
    env = dict(
        os.environ,
        TARGET="wot-eu",
        FORCE_RETRY=str(force_retry).lower(),
        GITHUB_OUTPUT=str(output),
        COMPARISON_JSON=json.dumps(
            {
                "stored_release_name": "old" if mismatch else "current",
                "mismatch": mismatch,
                "retry_blocked": retry_blocked,
                "next_retry_at": "2099-01-01T00:00:00Z" if retry_blocked else None,
            }
        ),
    )
    # Stub only external commands; execute the workflow's comparison and result scripts.
    stubs = """
uv() { echo '{"release_name":"current"}'; }
python3() { printf '%s\\n' "$COMPARISON_JSON"; }
"""
    subprocess.run(["bash", "-c", stubs + compare["run"]], env=env, check=True, capture_output=True)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    env.update({name.upper(): value for name, value in outputs.items()})
    env.update(
        CHECK_RESULT_PATH=str(result_path),
        COMPARE_OUTCOME="success",
        CURRENT_RELEASE_NAME=outputs["release_name"],
        DISPATCH_PIPELINES=str(dispatch_enabled).lower(),
        ACTIVE_OUTCOME="success" if dispatch_enabled else "skipped",
        ACTIVE_FOUND=str(active).lower(),
        DISPATCH_OUTCOME="success" if dispatch_enabled and not active else "skipped",
    )
    subprocess.run(["bash", "-c", record["run"]], env=env, check=True, capture_output=True)
    result = json.loads(result_path.read_text())
    assert result["action"] == expected_action
    assert outputs["retry_blocked"] == str(retry_blocked and not force_retry).lower()
    if force_retry:
        assert result["next_retry_at"] is None


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


def test_release_checker_deploys_status_page_with_ephemeral_check_metadata() -> None:
    jobs = workflow()["jobs"]
    report = jobs["report"]
    deploy = jobs["deploy-status-page"]
    metadata = next(
        step for step in report["steps"] if step["name"] == "Capture release check metadata"
    )

    assert "completed_at" in report["outputs"]
    assert "date -u" in metadata["run"]
    assert "actions/runs/${GITHUB_RUN_ID}" in metadata["run"]
    assert deploy["needs"] == "report"
    assert deploy["uses"] == "./.github/workflows/deploy-status-page.yml"
    assert deploy["with"] == {
        "release_check_completed_at": "${{ needs.report.outputs.completed_at }}",
        "release_check_conclusion": "${{ needs.report.outputs.conclusion }}",
        "release_check_run_url": "${{ needs.report.outputs.run_url }}",
    }
    assert deploy["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
        "pages": "write",
    }

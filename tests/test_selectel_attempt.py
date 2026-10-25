from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
import yaml

from scripts import runner_lifecycle as lifecycle
from scripts import selectel_attempt as attempt

ROOT = Path(__file__).parents[1]
IDENTITY = lifecycle.ResourceIdentity(
    "wotstat/game-unpack-pipeline", "123456", "1", "manual-123456-1"
)
SERVER_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
SHA = "a" * 40
CONFIG = lifecycle.SelectelConfig(
    auth_url="https://api.example.test/identity/v3",
    username="test-user",
    password="test-password",
    user_domain_name="test-domain",
    project_id="00000000-0000-0000-0000-000000000001",
    region_name="ru-2",
    availability_zone="ru-2c",
    image_id="image",
    flavor_id="HFL1.8-16384-240",
    public_network_api_url="https://api.example.test/public-network",
    preemptible=True,
)


def owned_server(**changes: Any) -> dict[str, Any]:
    value = {
        "name": IDENTITY.server_name,
        "status": "SHELVED_OFFLOADED",
        "tags": ["preemptible"],
        "created": "2026-10-25T10:00:00Z",
        "properties": {
            "gup_repository": IDENTITY.repository,
            "gup_run_id": IDENTITY.run_id,
            "gup_run_attempt": IDENTITY.run_attempt,
            "gup_instance_key": IDENTITY.instance_key,
            "gup_boot_disk": "local",
            "gup_preemptible": "true",
        },
    }
    value.update(changes)
    return value


def source_run(number: int = 1, **changes: Any) -> dict[str, Any]:
    source = {
        "id": 123456,
        "event": "workflow_dispatch",
        "path": attempt.WORKFLOW_PATH,
        "head_branch": "main",
        "head_sha": SHA,
        "repository": {"full_name": IDENTITY.repository},
        "head_repository": {"full_name": IDENTITY.repository},
        "run_attempt": number,
        "status": "completed",
        "conclusion": "failure",
    }
    source.update(changes)
    return source


@pytest.mark.parametrize("status", ["SHELVED_OFFLOADED", "EXPIRED"])
def test_only_owned_tagged_local_preemptible_vm_qualifies(status: str) -> None:
    assert lifecycle.server_is_preempted(CONFIG, IDENTITY, owned_server(status=status))


@pytest.mark.parametrize("status", ["SHUTOFF", "ERROR", "ACTIVE", "SUSPENDED", ""])
def test_poweroff_errors_and_partial_responses_are_not_preemption(status: str) -> None:
    assert not lifecycle.server_is_preempted(CONFIG, IDENTITY, owned_server(status=status))


def test_other_owner_network_disk_ordinary_vm_and_missing_tag_are_not_preemption() -> None:
    for name, value in [
        ("gup_run_attempt", "2"),
        ("gup_repository", "other/repository"),
        ("gup_boot_disk", "network"),
        ("gup_preemptible", "false"),
    ]:
        server = owned_server()
        server["properties"][name] = value
        assert not lifecycle.server_is_preempted(CONFIG, IDENTITY, server)
    assert not lifecycle.server_is_preempted(CONFIG, IDENTITY, owned_server(tags=[]))
    assert not lifecycle.server_is_preempted(
        replace(CONFIG, preemptible=False), IDENTITY, owned_server()
    )


@pytest.mark.parametrize("preemptible", [True, False])
def test_creation_sets_provider_mode_without_weakening_ownership(preemptible: bool) -> None:
    with patch.object(
        lifecycle, "openstack", return_value=Mock(stdout=json.dumps({"id": SERVER_ID}))
    ) as call:
        lifecycle.create_server(
            replace(CONFIG, preemptible=preemptible), IDENTITY, "port", "/tmp/cloud.yml"
        )
    command = call.call_args.args[1]
    assert f"gup_run_attempt={IDENTITY.run_attempt}" in command
    assert "gup_boot_disk=local" in command
    assert f"gup_preemptible={str(preemptible).lower()}" in command
    assert ("--tag" in command) is preemptible
    if preemptible:
        assert command[command.index("--tag") + 1] == "preemptible"
        assert command[command.index("--os-compute-api-version") + 1] == "2.72"


def test_bootstrap_detects_preemption_before_waiting_for_runner() -> None:
    with (
        patch.object(
            lifecycle,
            "openstack",
            return_value=Mock(returncode=0, stdout=json.dumps(owned_server())),
        ) as call,
        pytest.raises(lifecycle.PreemptedLifecycleError),
    ):
        lifecycle.check_server_not_preempted(CONFIG, IDENTITY, SERVER_ID)
    assert "2.72" in call.call_args.args[1]


def job(role: str, status: str = "completed", conclusion: str = "success") -> dict[str, Any]:
    return {
        "name": f"{attempt.VM_JOB_PREFIXES[role]} ({IDENTITY.instance_key})",
        "status": status,
        "conclusion": conclusion,
    }


def test_monitor_waits_for_enabled_consumers_and_ignores_itself() -> None:
    jobs = [job("download"), job("wot-src", "in_progress")]
    assert not attempt.vm_jobs_finished(jobs, IDENTITY, {"wot-src"})
    assert attempt.vm_jobs_finished(jobs, IDENTITY, set())
    jobs[1]["status"] = "completed"
    jobs.append({"name": f"Queue watchdog ({IDENTITY.instance_key})", "status": "in_progress"})
    assert attempt.vm_jobs_finished(jobs, IDENTITY, {"wot-src"})


def test_application_failure_finishes_observation_without_requiring_skipped_consumers() -> None:
    assert attempt.vm_jobs_finished([job("download", conclusion="failure")], IDENTITY, {"wot-src"})


def environment() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": IDENTITY.repository,
        "GITHUB_RUN_ID": IDENTITY.run_id,
        "GITHUB_RUN_ATTEMPT": IDENTITY.run_attempt,
        "GITHUB_SHA": SHA,
        "GITHUB_WATCH_TOKEN": "watch-token",
        "GH_TOKEN": "retry-token",
        "GITHUB_API_URL": "https://api.github.com",
        "DEFAULT_BRANCH": "main",
        "SELECTEL_PREEMPTIBLE": "true",
    }


@pytest.mark.parametrize("mode", ["preemption", "application_failure", "api_failure", "cancelled"])
def test_monitor_persists_only_confirmed_preemption(tmp_path: Path, mode: str) -> None:
    arguments = argparse.Namespace(
        instance_key=IDENTITY.instance_key,
        server_id=SERVER_ID,
        report=tmp_path / "monitor.json",
        timeout_seconds=60,
        poll_seconds=1,
    )
    jobs = [job("download", "in_progress")]
    if mode == "application_failure":
        jobs = [job("download", conclusion="failure")]
    response = Mock(returncode=0, stdout=json.dumps(owned_server()))
    if mode == "api_failure":
        response = Mock(returncode=1)
    jobs_effect: Any = [jobs, [job("download", conclusion="failure")]]
    if mode == "cancelled":
        jobs_effect = KeyboardInterrupt()
    with (
        patch.dict(os.environ, environment(), clear=True),
        patch.object(lifecycle.SelectelConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "add_mask"),
        patch.object(lifecycle, "write_output") as output,
        patch.object(attempt, "attempt_jobs", side_effect=jobs_effect),
        patch.object(lifecycle, "openstack", return_value=response) as cloud,
        patch("scripts.selectel_attempt.time.sleep"),
    ):
        if mode == "cancelled":
            with pytest.raises(KeyboardInterrupt):
                attempt.watch(arguments)
        else:
            attempt.watch(arguments)
    report = json.loads(arguments.report.read_text())
    assert report["preempted"] is (mode == "preemption")
    assert "test-password" not in arguments.report.read_text()
    assert SERVER_ID not in arguments.report.read_text()
    assert "watch-token" not in arguments.report.read_text()
    if mode == "preemption":
        output.assert_any_call("cancel_required", "true")
        assert "2.72" in cloud.call_args.args[1]
    else:
        assert not any(call.args == ("cancel_required", "true") for call in output.call_args_list)


def test_provision_failure_report_distinguishes_preemption_from_other_errors(
    tmp_path: Path,
) -> None:
    arguments = argparse.Namespace(
        instance_key=IDENTITY.instance_key, report=tmp_path / "provision.json"
    )
    for error in [lifecycle.PreemptedLifecycleError(), lifecycle.LifecycleError("No valid host")]:
        with (
            patch.dict(os.environ, environment(), clear=True),
            patch.object(lifecycle, "provision", side_effect=error),
            patch.object(lifecycle, "write_output"),
            pytest.raises(lifecycle.LifecycleError),
        ):
            attempt.provision(arguments)
        report = json.loads(arguments.report.read_text())
        assert report["preempted"] is isinstance(error, lifecycle.PreemptedLifecycleError)


@pytest.mark.parametrize("number", [1, 2, 3, 4])
def test_automatic_retry_budget_has_only_two_preemptible_attempts(number: int) -> None:
    assert attempt.source_can_retry(source_run(number), IDENTITY.repository, "main", number) is (
        number < 3
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "in_progress"},
        {"conclusion": "success"},
        {"run_attempt": 2},
        {"head_branch": "feature"},
        {"head_repository": {"full_name": "attacker/fork"}},
        {"head_repository": None},
        {"path": ".github/workflows/other.yml"},
        {"event": "pull_request"},
    ],
)
def test_retry_rejects_stale_successful_or_untrusted_source(changes: dict[str, Any]) -> None:
    assert not attempt.source_can_retry(source_run(**changes), IDENTITY.repository, "main", 1)


@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize(
    "outcome",
    ["preempted", "ordinary", "missing", "manual_cancel", "monitor_cancel", "changed_attempt"],
)
def test_retry_uses_full_rerun_once_and_rechecks_latest_attempt(number: int, outcome: str) -> None:
    source = source_run(number)
    if outcome in {"manual_cancel", "monitor_cancel"}:
        source["conclusion"] = "cancelled"
    latest = source_run(number + 1) if outcome == "changed_attempt" else source
    reports = [{"preempted": outcome != "ordinary", "preemptible": outcome != "ordinary"}]
    if outcome == "missing":
        reports = []
    with (
        patch.dict(os.environ, environment(), clear=True),
        patch.object(attempt, "github_get", side_effect=[source, latest]),
        patch.object(attempt, "fetch_reports", return_value=reports),
        patch.object(
            attempt,
            "attempt_jobs",
            return_value=[
                {
                    "name": f"Queue watchdog (manual-{IDENTITY.run_id}-{number})",
                    "steps": [{"name": attempt.CANCEL_STEP, "conclusion": "success"}],
                }
            ]
            if outcome == "monitor_cancel"
            else [],
        ),
        patch.object(lifecycle, "http_json") as post,
        patch.object(lifecycle, "write_output") as output,
        patch.object(lifecycle, "append_summary"),
    ):
        attempt.retry(argparse.Namespace(run_id=IDENTITY.run_id, run_attempt=number))
    if outcome in {"preempted", "monitor_cancel"}:
        post.assert_called_once()
        assert post.call_args.args[0] == "POST"
        assert post.call_args.args[1].endswith("/123456/rerun")
        assert post.call_args.kwargs["expected_statuses"] == (201,)
        assert "retry_safe" not in post.call_args.kwargs
        output.assert_any_call("restarted", "true")
    else:
        post.assert_not_called()


def test_reports_cannot_cross_run_attempt_or_commit_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "monitor.json"
    with patch.dict(os.environ, environment(), clear=True):
        valid = attempt.new_report(IDENTITY, "monitor")
    valid.update(preempted=True, reason="preempted")
    for field, value in [
        ("run_attempt", 2),
        ("run_id", "654321"),
        ("head_sha", "b" * 40),
        ("preemptible", False),
    ]:
        bad = dict(valid)
        bad[field] = value
        path.write_text(json.dumps(bad))
        with pytest.raises(lifecycle.LifecycleError):
            attempt.read_report(path, source_run(), 1, "monitor")
    path.write_text(json.dumps(valid))
    assert attempt.read_report(path, source_run(), 1, "monitor")["preempted"] is True


def test_manual_cancellation_and_monitor_cancellation_are_distinct() -> None:
    watchdog: dict[str, Any] = {
        "name": f"Queue watchdog ({IDENTITY.instance_key})",
        "steps": [{"name": attempt.CANCEL_STEP, "conclusion": "skipped"}],
    }
    assert not attempt.cancellation_was_requested_by_monitor([watchdog], IDENTITY.instance_key)
    watchdog["steps"][0]["conclusion"] = "success"
    assert attempt.cancellation_was_requested_by_monitor([watchdog], IDENTITY.instance_key)


def test_ordinary_third_failure_never_reads_reports_or_posts_a_retry() -> None:
    with (
        patch.dict(os.environ, environment(), clear=True),
        patch.object(attempt, "github_get", return_value=source_run(3)),
        patch.object(attempt, "fetch_reports") as download,
        patch.object(lifecycle, "http_json") as post,
        patch.object(lifecycle, "write_output"),
    ):
        attempt.retry(argparse.Namespace(run_id=IDENTITY.run_id, run_attempt=3))
    download.assert_not_called()
    post.assert_not_called()


def test_expired_absolute_deadline_cancels_without_assuming_preemption(tmp_path: Path) -> None:
    values = environment() | {"SELECTEL_VM_DEADLINE": "2000-01-01T00:00:00Z"}
    arguments = argparse.Namespace(
        instance_key=IDENTITY.instance_key,
        server_id=SERVER_ID,
        report=tmp_path / "monitor.json",
        timeout_seconds=14400,
        poll_seconds=60,
    )
    with (
        patch.dict(os.environ, values, clear=True),
        patch.object(lifecycle.SelectelConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "add_mask"),
        patch.object(lifecycle, "write_output") as output,
        patch.object(attempt, "attempt_jobs") as lookup,
        pytest.raises(lifecycle.LifecycleError, match="deadline"),
    ):
        attempt.watch(arguments)
    lookup.assert_not_called()
    assert json.loads(arguments.report.read_text())["preempted"] is False
    output.assert_any_call("cancel_required", "true")


def test_mismatched_artifact_revision_is_rejected_before_download(tmp_path: Path) -> None:
    name = attempt.report_name(IDENTITY.run_id, "1", "monitor")
    artifact = {
        "name": name,
        "expired": False,
        "size_in_bytes": 100,
        "workflow_run": {"id": 123456, "head_sha": "b" * 40},
    }
    with (
        patch.object(attempt, "github_get", return_value={"artifacts": [artifact]}),
        patch("scripts.selectel_attempt.subprocess.run") as download,
        pytest.raises(lifecycle.LifecycleError, match="source revision"),
    ):
        attempt.fetch_reports(IDENTITY.repository, source_run(), 1, "token", tmp_path)
    download.assert_not_called()


def test_workflows_keep_retry_after_cleanup_and_support_token_originated_reruns() -> None:
    main = yaml.safe_load((ROOT / attempt.WORKFLOW_PATH).read_text())
    checker = yaml.safe_load((ROOT / ".github/workflows/check-game-releases.yml").read_text())
    reconciler = yaml.safe_load(
        (ROOT / ".github/workflows/reconcile-release-resources.yml").read_text()
    )
    inputs = main[True]["workflow_dispatch"]["inputs"]
    assert inputs["preemptible"]["default"] is True
    assert checker[True]["workflow_dispatch"]["inputs"]["preemptible"] == inputs["preemptible"]
    assert (
        main["env"]["SELECTEL_PREEMPTIBLE"]
        == "${{ inputs.preemptible && fromJSON(github.run_attempt) < 3 }}"
    )
    watchdog = main["jobs"]["queue-watchdog"]
    assert watchdog["runs-on"] == "ubuntu-latest"
    assert watchdog["permissions"]["actions"] == "write"
    steps = [step["name"] for step in watchdog["steps"]]
    assert steps.index("Upload VM monitoring report") < steps.index(attempt.CANCEL_STEP)
    retry = reconciler["jobs"]["retry-preempted"]
    assert retry["needs"] == "reconcile"
    assert "always()" not in retry.get("if", "")
    assert reconciler["concurrency"]["cancel-in-progress"] is False
    dispatch = main["jobs"]["dispatch-reconciler"]
    assert "github.triggering_actor" in dispatch["if"]
    assert "cancelled()" in dispatch["if"]
    assert "deploy-status-page" in dispatch["needs"]

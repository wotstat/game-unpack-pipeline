from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts import runner_lifecycle as lifecycle
from scripts import vm_monitor as monitor

IDENTITY = lifecycle.ResourceIdentity("owner/repository", "12345", "2", "manual-12345-2")


def job(role: str, status: str = "completed", conclusion: str = "success") -> dict[str, Any]:
    return {
        "name": f"{monitor.VM_JOB_PREFIXES[role]} ({IDENTITY.instance_key})",
        "status": status,
        "conclusion": conclusion,
    }


def test_monitor_waits_for_enabled_consumer_and_ignores_other_attempts() -> None:
    jobs = [job("download"), job("wot-src", "in_progress")]
    assert not monitor.vm_jobs_finished(jobs, IDENTITY, {"wot-src"})
    assert monitor.vm_jobs_finished(jobs, IDENTITY, set())
    jobs.append(job("wot-gui-assets", "in_progress"))
    jobs[1] = job("wot-src")
    assert monitor.vm_jobs_finished(jobs, IDENTITY, {"wot-src"})
    jobs[0]["name"] = "Download and unpack (manual-12345-1)"
    assert not monitor.vm_jobs_finished(jobs, IDENTITY, set())


def test_download_failure_finishes_observation_without_waiting_for_skipped_consumers() -> None:
    assert monitor.vm_jobs_finished([job("download", conclusion="failure")], IDENTITY, {"wot-src"})


@pytest.mark.parametrize("state", ["active", "deleted", "missing", "expired"])
def test_monitor_cancels_only_for_vm_loss_or_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    monkeypatch.setenv("GITHUB_WATCH_TOKEN", "dummy-token")
    deadline = datetime.now(UTC) + timedelta(hours=-1 if state == "expired" else 1)
    monkeypatch.setenv("AEZA_VM_DEADLINE", deadline.isoformat())
    report = tmp_path / "report.json"
    args = argparse.Namespace(instance_key=IDENTITY.instance_key, server_id="42", report=report)
    server = {
        "name": IDENTITY.server_name,
        "product": {"id": 157},
        "paymentTerm": "hour",
        "status": state,
        "secureParameters": {"password": "do-not-log"},
    }
    with (
        patch.object(
            lifecycle.AezaConfig, "from_environment", return_value=lifecycle.AezaConfig("dummy")
        ),
        patch.object(lifecycle, "build_identity", return_value=IDENTITY),
        patch.object(monitor, "attempt_jobs", return_value=[job("download")]),
        patch.object(lifecycle, "get_server", return_value=None if state == "missing" else server),
        patch.object(monitor, "boolean_environment", return_value=False),
        patch.object(lifecycle, "write_output") as output,
        patch.object(monitor, "vm_jobs_finished", return_value=state == "active"),
    ):
        monitor.watch(args)
    cancelled = any(call.args == ("cancel_required", "true") for call in output.call_args_list)
    assert cancelled is (state != "active")
    text = report.read_text()
    assert "do-not-log" not in text
    assert "server_id" not in text
    assert json.loads(text)["reason"] == (
        "jobs_completed"
        if state == "active"
        else "lifetime_expired"
        if state == "expired"
        else "vm_unavailable"
    )

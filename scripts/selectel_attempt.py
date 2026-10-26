"""Observe one disposable VM and retry confirmed preemptions after reconciliation.

All persisted records deliberately omit credentials, addresses and cloud identifiers.
The retry coordinator only consumes reports from this repository's production workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import runner_lifecycle as lifecycle

REPORT_VERSION = 1
WORKFLOW_PATH = ".github/workflows/process-game-release.yml"
MAX_ATTEMPTS = 3
MAX_REPORT_BYTES = 16 * 1024
RUNNER_SHUTDOWN_GRACE_SECONDS = 120
VM_JOB_PREFIXES = {
    "download": "Download and unpack",
    "wot-src": "Publish wot-src",
    "wot-gui-assets": "Publish wot-gui-assets",
    "wotstat-assets": "Upload wotstat assets",
}
CANCEL_STEP = "Cancel workflow after VM interruption"


def boolean_environment(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise lifecycle.LifecycleError(f"{name} must be true or false")
    return value == "true"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def report_name(run_id: str, attempt: str, phase: str) -> str:
    return f"selectel-attempt-{run_id}-{attempt}-{phase}"


def new_report(identity: lifecycle.ResourceIdentity, phase: str) -> dict[str, Any]:
    return {
        "schema_version": REPORT_VERSION,
        "repository": identity.repository,
        "run_id": identity.run_id,
        "run_attempt": int(identity.run_attempt),
        "head_sha": lifecycle.require_environment("GITHUB_SHA"),
        "phase": phase,
        "preemptible": boolean_environment("SELECTEL_PREEMPTIBLE"),
        "preempted": False,
        "reason": "started",
        "observed_at": now(),
        "server_created_at": None,
        "vm_age_seconds": None,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    report["observed_at"] = now()
    created_at = report.get("server_created_at")
    if isinstance(created_at, str):
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        report["vm_age_seconds"] = round(max(0, (datetime.now(UTC) - created).total_seconds()))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    lifecycle.write_output("preempted", str(report["preempted"]).lower())
    lifecycle.write_output("reason", str(report["reason"]))


def provision(arguments: argparse.Namespace) -> None:
    identity = lifecycle.build_identity(arguments.instance_key)
    report = new_report(identity, "provision")
    try:
        lifecycle.provision(arguments)
        report["reason"] = "provisioned"
    except lifecycle.PreemptedLifecycleError as error:
        report.update(preempted=True, reason="preempted", server_created_at=error.created_at)
        raise
    except BaseException:
        report["reason"] = "provision_failed"
        raise
    finally:
        write_report(arguments.report, report)


def github_get(path: str, token: str) -> Any:
    _, payload, _ = lifecycle.http_json(
        "GET", lifecycle.github_url(path), token=token, github_api=True
    )
    return payload


def attempt_jobs(repository: str, run_id: str, attempt: str, token: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = github_get(
            f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            f"?per_page=100&page={page}",
            token,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise lifecycle.LifecycleError("GitHub returned invalid attempt jobs")
        jobs = payload["jobs"]
        if not all(isinstance(job, dict) for job in jobs):
            raise lifecycle.LifecycleError("GitHub returned an invalid job")
        result.extend(jobs)
        if len(jobs) < 100:
            return result
        page += 1


def matching_vm_jobs(
    jobs: list[dict[str, Any]], identity: lifecycle.ResourceIdentity
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for role, prefix in VM_JOB_PREFIXES.items():
        name = f"{prefix} ({identity.instance_key})"
        matches = [
            job
            for job in jobs
            if job.get("name") == name or str(job.get("name", "")).startswith(name + " /")
        ]
        if len(matches) > 1:
            raise lifecycle.LifecycleError("Duplicate VM jobs in one attempt")
        if matches:
            selected[role] = matches[0]
    return selected


def vm_jobs_finished(
    jobs: list[dict[str, Any]], identity: lifecycle.ResourceIdentity, consumers: set[str]
) -> bool:
    selected = matching_vm_jobs(jobs, identity)
    download = selected.get("download")
    if not download or download.get("status") != "completed":
        return False
    if download.get("conclusion") != "success":
        return True
    return all(selected.get(role, {}).get("status") == "completed" for role in consumers)


def vm_jobs_interrupted(
    jobs: list[dict[str, Any]], identity: lifecycle.ResourceIdentity, consumers: set[str]
) -> bool:
    for role, job in matching_vm_jobs(jobs, identity).items():
        if role not in consumers | {"download"} or job.get("status") != "completed":
            continue
        # A runner shutdown can finish the job as failure while its active step
        # is cancelled (or no steps are reported). An explicit step failure or
        # timeout remains an application error, even if the VM stops later.
        if job.get("conclusion") in {"failure", "cancelled"} and not any(
            step.get("conclusion") in {"failure", "timed_out"} for step in job.get("steps") or []
        ):
            return True
    return False


def watch(arguments: argparse.Namespace) -> None:
    identity = lifecycle.build_identity(arguments.instance_key)
    config = lifecycle.SelectelConfig.from_environment(require_compute=False)
    token = lifecycle.require_environment("GITHUB_WATCH_TOKEN")
    lifecycle.add_mask(config.password)
    if not lifecycle.UUID_RE.fullmatch(arguments.server_id):
        raise lifecycle.LifecycleError("Cannot monitor an invalid VM ID")
    report = new_report(identity, "monitor")
    consumers = {
        role
        for role in ("wot-src", "wot-gui-assets", "wotstat-assets")
        if boolean_environment("PUBLISH_" + role.replace("-", "_").upper(), True)
    }
    if arguments.timeout_seconds <= 0 or arguments.poll_seconds <= 0:
        raise lifecycle.LifecycleError("Monitoring timeout and poll interval must be positive")
    remaining = float(arguments.timeout_seconds)
    if absolute_deadline := os.environ.get("SELECTEL_VM_DEADLINE"):
        parsed_deadline = datetime.fromisoformat(absolute_deadline.replace("Z", "+00:00"))
        if parsed_deadline.tzinfo is None:
            raise lifecycle.LifecycleError("VM deadline must include a time zone")
        remaining = min(remaining, (parsed_deadline - datetime.now(UTC)).total_seconds())
    deadline = time.monotonic() + max(0, remaining)
    shutdown_deadline: float | None = None
    try:
        while time.monotonic() < deadline:
            if shutdown_deadline is None:
                try:
                    jobs = attempt_jobs(
                        identity.repository, identity.run_id, identity.run_attempt, token
                    )
                except lifecycle.TransientLifecycleError:
                    print(
                        "::warning::GitHub jobs temporarily unavailable; preemption is not assumed"
                    )
                    time.sleep(max(0, min(arguments.poll_seconds, deadline - time.monotonic())))
                    continue
                if vm_jobs_finished(jobs, identity, consumers):
                    if not vm_jobs_interrupted(jobs, identity, consumers):
                        report["reason"] = "jobs_completed"
                        return
                    # GitHub may finish the interrupted job before Selectel exposes
                    # the final VM state. Keep observing briefly before cleanup can
                    # delete the evidence, within the original four-hour deadline.
                    shutdown_deadline = min(
                        deadline, time.monotonic() + RUNNER_SHUTDOWN_GRACE_SECONDS
                    )
            observation_deadline = shutdown_deadline if shutdown_deadline is not None else deadline
            try:
                shown = lifecycle.openstack(
                    config,
                    [
                        "server",
                        "show",
                        arguments.server_id,
                        "--os-compute-api-version",
                        "2.72",
                        "-f",
                        "json",
                    ],
                    check=False,
                    timeout=max(1, min(180, int(observation_deadline - time.monotonic()))),
                )
            except lifecycle.TransientLifecycleError:
                shown = None
            if shown is not None and shown.returncode == 0:
                server = lifecycle.parse_json_output(shown, "monitored VM")
                if not isinstance(server, dict) or not lifecycle.server_has_ownership_markers(
                    server, identity
                ):
                    raise lifecycle.LifecycleError("Monitored VM ownership could not be verified")
                report["server_created_at"] = lifecycle.server_created_at(server)
                if lifecycle.server_is_preempted(config, identity, server):
                    report.update(preempted=True, reason="preempted")
                    lifecycle.write_output("cancel_required", "true")
                    print("::warning::Selectel preempted the VM; a full retry may follow cleanup")
                    return
                status = str(server.get("status", "")).upper()
                if status in {"ERROR", "SHUTOFF", "SHELVED_OFFLOADED", "EXPIRED"} and not (
                    shutdown_deadline is not None and status == "SHUTOFF"
                ):
                    report["reason"] = "vm_stopped"
                    lifecycle.write_output("cancel_required", "true")
                    print("::error::VM stopped without confirmed preemption; no automatic retry")
                    return
            else:
                # An unavailable API is not evidence of an eviction.
                print("::warning::VM status temporarily unavailable; preemption is not assumed")
            if shutdown_deadline is not None and time.monotonic() >= shutdown_deadline:
                report["reason"] = "runner_interrupted"
                print(
                    "::warning::Runner stopped without confirmed VM preemption; no automatic retry"
                )
                return
            time.sleep(max(0, min(arguments.poll_seconds, observation_deadline - time.monotonic())))
        report["reason"] = "deadline"
        lifecycle.write_output("cancel_required", "true")
        raise lifecycle.LifecycleError("VM monitoring deadline exceeded; no automatic retry")
    except KeyboardInterrupt:
        report.update(preempted=False, reason="cancelled")
        raise
    except BaseException:
        if report["reason"] == "started":
            report["reason"] = "monitor_failed"
        raise
    finally:
        write_report(arguments.report, report)


def read_report(path: Path, source: Mapping[str, Any], attempt: int, phase: str) -> dict[str, Any]:
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise lifecycle.LifecycleError("Attempt report exceeds its size limit")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise lifecycle.LifecycleError("Attempt report is unreadable") from error
    if not isinstance(report, dict) or any(
        (
            type(report.get("schema_version")) is not int,
            report.get("schema_version") != REPORT_VERSION,
            report.get("repository") != source["repository"]["full_name"],
            report.get("run_id") != str(source["id"]),
            type(report.get("run_attempt")) is not int,
            report.get("run_attempt") != attempt,
            report.get("head_sha") != source["head_sha"],
            report.get("phase") != phase,
            type(report.get("preemptible")) is not bool,
            type(report.get("preempted")) is not bool,
        )
    ):
        raise lifecycle.LifecycleError("Attempt report does not match the source run")
    if report["preempted"] and (not report["preemptible"] or report.get("reason") != "preempted"):
        raise lifecycle.LifecycleError("Attempt report contains contradictory preemption evidence")
    return report


def source_can_retry(source: Mapping[str, Any], repository: str, branch: str, attempt: int) -> bool:
    source_repository = source.get("repository")
    head_repository = source.get("head_repository")
    path = source.get("path")
    return (
        1 <= attempt < MAX_ATTEMPTS
        and source.get("event") == "workflow_dispatch"
        and isinstance(path, str)
        and path.split("@")[0] == WORKFLOW_PATH
        and source.get("head_branch") == branch
        and isinstance(source_repository, Mapping)
        and source_repository.get("full_name") == repository
        and isinstance(head_repository, Mapping)
        and head_repository.get("full_name") == repository
        and source.get("run_attempt") == attempt
        and source.get("status") == "completed"
        and source.get("conclusion") in {"failure", "cancelled"}
    )


def cancellation_was_requested_by_monitor(jobs: list[dict[str, Any]], instance_key: str) -> bool:
    return any(
        job.get("name") == f"Queue watchdog ({instance_key})"
        and any(
            step.get("name") == CANCEL_STEP and step.get("conclusion") == "success"
            for step in job.get("steps", [])
        )
        for job in jobs
    )


def fetch_reports(
    repository: str, source: dict[str, Any], attempt: int, token: str, destination: Path
) -> list[dict[str, Any]]:
    run_id = str(source["id"])
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = github_get(
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}", token
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
            raise lifecycle.LifecycleError("GitHub returned invalid attempt artifacts")
        artifacts.extend(payload["artifacts"])
        if len(payload["artifacts"]) < 100:
            break
        page += 1
    reports = []
    for phase in ("provision", "monitor"):
        name = report_name(run_id, str(attempt), phase)
        matches = [artifact for artifact in artifacts if artifact.get("name") == name]
        if not matches:
            continue
        if len(matches) != 1 or matches[0].get("expired") is not False:
            raise lifecycle.LifecycleError("Attempt artifact is ambiguous or expired")
        if matches[0].get("size_in_bytes", MAX_REPORT_BYTES + 1) > MAX_REPORT_BYTES:
            raise lifecycle.LifecycleError("Attempt artifact exceeds its size limit")
        artifact_run = matches[0].get("workflow_run", {})
        if (
            artifact_run.get("id") != source["id"]
            or artifact_run.get("head_sha") != source["head_sha"]
        ):
            raise lifecycle.LifecycleError(
                "Attempt artifact does not belong to the source revision"
            )
        target = destination / phase
        completed = subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repository,
                "--name",
                name,
                "--dir",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise lifecycle.LifecycleError("Could not download the attempt report")
        reports.append(read_report(target / f"{phase}.json", source, attempt, phase))
    return reports


def retry(arguments: argparse.Namespace) -> None:
    repository = lifecycle.validate_repository(lifecycle.require_environment("GITHUB_REPOSITORY"))
    token = lifecycle.require_environment("GH_TOKEN")
    branch = lifecycle.require_environment("DEFAULT_BRANCH")
    if not re.fullmatch(r"[1-9][0-9]*", arguments.run_id):
        raise lifecycle.LifecycleError("Source run ID must be a positive integer")
    attempt = arguments.run_attempt
    lifecycle.write_output("restarted", "false")
    source = github_get(f"repos/{repository}/actions/runs/{arguments.run_id}", token)
    if not isinstance(source, dict) or not source_can_retry(source, repository, branch, attempt):
        print("No immediate retry: source is ineligible, already restarted, or successful")
        return
    with tempfile.TemporaryDirectory(prefix="selectel-attempt-") as temporary:
        reports = fetch_reports(repository, source, attempt, token, Path(temporary))
    if not reports or not any(report["preempted"] for report in reports):
        print("No immediate retry: no confirmed preemption")
        return
    jobs = attempt_jobs(repository, arguments.run_id, str(attempt), token)
    if source["conclusion"] == "cancelled" and not cancellation_was_requested_by_monitor(
        jobs, f"manual-{arguments.run_id}-{attempt}"
    ):
        print("No immediate retry: cancellation was not requested by the VM monitor")
        return
    # Recheck after downloading reports. A queued duplicate reconciler must not
    # restart a newer attempt; POST is deliberately never retried on unknown outcome.
    latest = github_get(f"repos/{repository}/actions/runs/{arguments.run_id}", token)
    if not isinstance(latest, dict) or not source_can_retry(latest, repository, branch, attempt):
        print("No immediate retry: source attempt changed")
        return
    lifecycle.http_json(
        "POST",
        lifecycle.github_url(f"repos/{repository}/actions/runs/{arguments.run_id}/rerun"),
        token=token,
        github_api=True,
        body={},
        expected_statuses=(201,),
    )
    lifecycle.write_output("restarted", "true")
    mode = "ordinary" if attempt + 1 == MAX_ATTEMPTS else "preemptible"
    lifecycle.append_summary(
        f"- Confirmed preemption: restarting full pipeline as attempt {attempt + 1} ({mode})"
    )
    print(f"Requested full attempt {attempt + 1}/{MAX_ATTEMPTS} ({mode}) after reconciliation")


def interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("provision")
    create.add_argument("--instance-key", required=True)
    create.add_argument("--report", type=Path, required=True)
    create.set_defaults(handler=provision)
    monitor = subparsers.add_parser("watch")
    monitor.add_argument("--instance-key", required=True)
    monitor.add_argument("--server-id", required=True)
    monitor.add_argument("--report", type=Path, required=True)
    monitor.add_argument("--timeout-seconds", type=int, default=4 * 60 * 60)
    monitor.add_argument("--poll-seconds", type=int, default=60)
    monitor.set_defaults(handler=watch)
    recover = subparsers.add_parser("retry")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--run-attempt", type=int, required=True)
    recover.set_defaults(handler=retry)
    arguments = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    try:
        arguments.handler(arguments)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (lifecycle.LifecycleError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"::error::{lifecycle.sanitized(str(error))}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

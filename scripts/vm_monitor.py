"""Monitor the hourly Aeza VM until downloader and enabled consumers finish."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import runner_lifecycle as lifecycle

VM_JOB_PREFIXES = {
    "download": "Download and unpack",
    "wot-src": "Publish wot-src",
    "wot-gui-assets": "Publish wot-gui-assets",
    "wotstat-assets": "Upload wotstat assets",
}


def boolean_environment(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise lifecycle.LifecycleError(f"{name} must be true or false")
    return value == "true"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def write_report(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "aeza",
                "product": "MSKs-5",
                "payment_term": "hour",
                "reason": reason,
                "observed_at": now(),
            },
            indent=2,
        )
        + "\n"
    )
    lifecycle.write_output("reason", reason)


def provision(arguments: argparse.Namespace) -> None:
    reason = "provision_failed"
    try:
        lifecycle.provision(arguments)
        reason = "provisioned"
    finally:
        write_report(arguments.report, reason)


def watch(arguments: argparse.Namespace) -> None:
    config = lifecycle.AezaConfig.from_environment()
    identity = lifecycle.build_identity(arguments.instance_key)
    token = lifecycle.require_environment("GITHUB_WATCH_TOKEN")
    absolute = datetime.fromisoformat(lifecycle.require_environment("AEZA_VM_DEADLINE"))
    if absolute.tzinfo is None:
        raise lifecycle.LifecycleError("VM deadline must include a timezone")
    deadline = time.monotonic() + max(0, (absolute - datetime.now(UTC)).total_seconds())
    consumers = {
        role
        for role in lifecycle.ROLES
        if role != "downloader"
        and boolean_environment("PUBLISH_" + role.replace("-", "_").upper(), True)
    }
    reason = "monitor_failed"
    try:
        while time.monotonic() < deadline:
            try:
                jobs = attempt_jobs(
                    identity.repository, identity.run_id, identity.run_attempt, token
                )
                if vm_jobs_finished(jobs, identity, consumers):
                    reason = "jobs_completed"
                    return
                server = lifecycle.get_server(config, arguments.server_id)
            except lifecycle.TransientLifecycleError:
                time.sleep(min(30, max(0, deadline - time.monotonic())))
                continue
            if server is None or server.get("status") in {"deleted", "suspended", "blocked"}:
                reason = "vm_unavailable"
                lifecycle.write_output("cancel_required", "true")
                return
            if not lifecycle.server_has_ownership_markers(server, identity):
                raise lifecycle.LifecycleError("Monitored Aeza service ownership does not match")
            time.sleep(min(30, max(0, deadline - time.monotonic())))
        reason = "lifetime_expired"
        lifecycle.write_output("cancel_required", "true")
    except lifecycle.LifecycleError:
        lifecycle.write_output("cancel_required", "true")
        raise
    finally:
        write_report(arguments.report, reason)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("provision", "watch"))
    parser.add_argument("--instance-key", required=True)
    parser.add_argument("--server-id", default="")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        (provision if args.command == "provision" else watch)(args)
        return 0
    except lifecycle.LifecycleError as error:
        print(f"::error::{lifecycle.sanitized(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

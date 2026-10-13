from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any, cast

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/deploy-status-page.yml"


def workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(WORKFLOW_PATH.read_text()))


def test_status_page_workflow_supports_call_push_and_manual_rebuilds() -> None:
    trigger = workflow()[True]

    assert set(trigger) == {"workflow_call", "workflow_dispatch", "push"}
    assert set(trigger["workflow_call"]["inputs"]) == {
        "release_check_completed_at",
        "release_check_conclusion",
        "release_check_run_url",
    }
    assert trigger["push"]["branches"] == ["main"]
    assert "status/**" in trigger["push"]["paths"]
    assert workflow()["concurrency"] == {
        "group": "status-page-${{ github.repository }}",
        "cancel-in-progress": False,
    }


def test_status_page_build_checks_out_full_history_and_uploads_site() -> None:
    build = workflow()["jobs"]["build"]
    checkout = build["steps"][0]
    configure = build["steps"][1]
    resolve = build["steps"][2]
    render = build["steps"][3]
    upload = build["steps"][4]

    assert checkout["uses"] == "actions/checkout@v7.0.1"
    assert checkout["with"] == {
        "fetch-depth": 0,
        "ref": "${{ github.event.repository.default_branch }}",
    }
    assert configure["uses"] == "actions/configure-pages@v6.0.0"
    assert "check-game-releases.yml/runs" in resolve["run"]
    assert "event=workflow_dispatch" not in resolve["run"]
    assert 'select(.name == "Release check report")' in resolve["run"]
    assert "scripts/render_status_page.py" in render["run"]
    assert "--release-check-completed-at" in render["run"]
    assert upload["uses"] == "actions/upload-pages-artifact@v5.0.0"
    assert upload["with"]["path"] == "_site"
    assert build["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pages": "write",
    }


def test_status_page_deploy_uses_pages_environment_and_oidc() -> None:
    deploy = workflow()["jobs"]["deploy"]

    assert deploy["needs"] == "build"
    assert deploy["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "pages": "write",
    }
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    assert deploy["steps"][0]["uses"] == "actions/deploy-pages@v5.0.0"


def run_release_check_step(
    tmp_path: Path, statuses: dict[str, list[int]], *, provided: bool = False
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "statuses.json").write_text(json.dumps(statuses))
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + dedent("""\
            import json, os, sys
            from pathlib import Path
            root = Path(os.environ["TEST_ROOT"])
            kind = "jobs" if sys.argv[4].endswith("/jobs") else "runs"
            log = root / "calls"
            calls = log.read_text().splitlines() if log.exists() else []
            attempt = calls.count(kind)
            log.write_text("\\n".join([*calls, kind]) + "\\n")
            statuses = json.loads((root / "statuses.json").read_text()).get(kind, [200])
            status = statuses[min(attempt, len(statuses) - 1)]
            if status != 200:
                print("failed attempt response must not become JSON input")
                print(f"gh: Server Error (HTTP {status})", file=sys.stderr)
                sys.exit(1)
            result = {"workflow_runs": [{
                "id": 42, "html_url": "https://github.com/example/pipeline/actions/runs/42",
                "updated_at": "2026-10-13T21:00:00Z",
            }]} if kind == "runs" else {"jobs": [
                {"name": "Select targets", "conclusion": "success"},
                {"name": "Check wot-asia", "conclusion": "success"},
                {"name": "Release check report", "completed_at": "2026-10-13T20:59:00Z"},
            ]}
            print(json.dumps(result))
            """)
    )
    gh.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$TEST_ROOT/delays"\n')
    sleep.chmod(0o755)
    script = cast(str, workflow()["jobs"]["build"]["steps"][2]["run"])
    return subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TEST_ROOT": str(tmp_path),
            "GITHUB_REPOSITORY": "example/pipeline",
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
            "PROVIDED_COMPLETED_AT": "2026-10-13T20:59:00Z" if provided else "",
            "PROVIDED_CONCLUSION": "success" if provided else "",
            "PROVIDED_RUN_URL": "https://github.com/example/pipeline/actions/runs/42"
            if provided
            else "",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("endpoint", ["runs", "jobs"])
def test_release_check_retries_502_and_keeps_successful_json(tmp_path: Path, endpoint: str) -> None:
    result = run_release_check_step(tmp_path, {endpoint: [502, 502, 502, 200]})

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "calls").read_text().splitlines().count(endpoint) == 4
    assert (tmp_path / "delays").read_text().splitlines() == ["5", "15", "30"]
    assert "HTTP 502" in result.stderr
    assert result.stdout == ""
    assert (tmp_path / "outputs").read_text() == (
        "completed_at=2026-10-13T20:59:00Z\n"
        "conclusion=success\n"
        "run_url=https://github.com/example/pipeline/actions/runs/42\n"
    )


@pytest.mark.parametrize("status", [502, 403, 500])
def test_release_check_api_failure_is_bounded_and_does_not_produce_metadata(
    tmp_path: Path, status: int
) -> None:
    result = run_release_check_step(tmp_path, {"runs": [status]})

    assert result.returncode != 0
    assert (tmp_path / "calls").read_text().splitlines() == ["runs"] * (4 if status == 502 else 1)
    assert not (tmp_path / "outputs").exists()
    if status == 502:
        assert (tmp_path / "delays").read_text().splitlines() == ["5", "15", "30"]
    else:
        assert not (tmp_path / "delays").exists()


def test_release_check_provided_metadata_skips_api(tmp_path: Path) -> None:
    result = run_release_check_step(tmp_path, {"runs": [502]}, provided=True)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "calls").exists()
    assert not (tmp_path / "delays").exists()
    assert "conclusion=success" in (tmp_path / "outputs").read_text()

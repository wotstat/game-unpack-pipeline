from __future__ import annotations

from pathlib import Path
from typing import Any, cast

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

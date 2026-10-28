from __future__ import annotations

import base64
import http.client
import io
import json
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
import yaml

from scripts import runner_lifecycle as lifecycle

ROOT = Path(__file__).parents[1]
IDENTITY = lifecycle.ResourceIdentity("owner/pipeline", "12345", "2", "manual-12345-2")
CONFIG = lifecycle.AezaConfig("dummy-private-api-key")


def server(**changes: Any) -> dict[str, Any]:
    return {
        "id": 42,
        "name": IDENTITY.server_name,
        "productId": 157,
        "paymentTerm": "hour",
        "status": "active",
        **changes,
    }


class JobRoutingTests(unittest.TestCase):
    def test_matches_the_unique_runner_label_independently_of_job_name(self) -> None:
        job: dict[str, object] = {
            "name": "Download and unpack (wot-eu, sd)",
            "labels": ["self-hosted", "gup-manual-123456-2"],
        }

        self.assertTrue(lifecycle.job_targets_runner(job, "gup-manual-123456-2"))
        self.assertFalse(lifecycle.job_targets_runner(job, "gup-manual-another-1"))

    def test_rejects_missing_or_malformed_job_labels(self) -> None:
        self.assertFalse(lifecycle.job_targets_runner({}, "gup-manual-123456-2"))
        self.assertFalse(
            lifecycle.job_targets_runner(
                {"labels": "self-hosted,gup-manual-123456-2"},
                "gup-manual-123456-2",
            )
        )


class SanitizerTests(unittest.TestCase):
    def test_redacts_known_token_shapes_and_jit_lines(self) -> None:
        registration_token = "ghs" + "_" + "a" * 40
        personal_access_token = "github" + "_pat_" + "test_value"
        source = "\n".join(
            [
                f"token={registration_token}",
                personal_access_token,
                "./run.sh --jitconfig very-secret",
                'encoded_jit_config="also-secret"',
                "password=known-value",
            ]
        )
        result = lifecycle.sanitized(source, ["known-value"])
        self.assertNotIn(registration_token, result)
        self.assertNotIn(personal_access_token, result)
        self.assertNotIn("very-secret", result)
        self.assertNotIn("also-secret", result)
        self.assertNotIn("known-value", result)


class HttpJsonTests(unittest.TestCase):
    def test_retries_idempotent_request_after_remote_disconnect(self) -> None:
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"ports":[]}'
        response.headers.items.return_value = []
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with (
            patch(
                "scripts.runner_lifecycle.urllib.request.urlopen",
                side_effect=[http.client.RemoteDisconnected("test"), response],
            ) as urlopen,
            patch("scripts.runner_lifecycle.time.sleep") as sleep,
        ):
            status, payload, _ = lifecycle.http_json(
                "GET", "https://api.example.test/v1/public_ports"
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ports": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "other-attempt"},
        {"productId": 156},
        {"paymentTerm": "month"},
    ],
)
def test_delete_rejects_explicit_service_without_ownership(changes: dict[str, Any]) -> None:
    with (
        patch.object(lifecycle, "owned_servers", return_value=[]),
        patch.object(lifecycle, "get_server", return_value=server(**changes)),
        patch.object(lifecycle.AezaConfig, "request") as request,
        pytest.raises(lifecycle.LifecycleError, match="ownership"),
    ):
        lifecycle.delete_servers(CONFIG, IDENTITY, "42", deleted_resources=[])
    request.assert_not_called()


def test_both_aeza_list_and_detail_shapes_have_the_same_ownership() -> None:
    detail = server()
    summary = server(product={"id": 157})
    del summary["productId"]
    assert lifecycle.server_has_ownership_markers(detail, IDENTITY)
    assert lifecycle.server_has_ownership_markers(summary, IDENTITY)
    other = lifecycle.ResourceIdentity("another/pipeline", "12345", "2", "manual-12345-2")
    assert IDENTITY.server_name != other.server_name


def test_delete_waits_for_actual_deletion_after_204() -> None:
    deleted: list[str] = []
    with (
        patch.object(lifecycle, "owned_servers", return_value=[server()]),
        patch.object(
            lifecycle, "get_server", side_effect=[server(), server(), server(status="deleted")]
        ),
        patch.object(lifecycle.AezaConfig, "request", return_value=(204, None, {})) as request,
        patch.object(time, "sleep"),
    ):
        lifecycle.delete_servers(CONFIG, IDENTITY, deleted_resources=deleted)
    assert deleted == ["aeza-service:42"]
    request.assert_called_once()


def test_unknown_delete_result_is_reconciled() -> None:
    deleted: list[str] = []
    with (
        patch.object(lifecycle, "owned_servers", return_value=[server()]),
        patch.object(lifecycle, "get_server", side_effect=[server(), None]),
        patch.object(
            lifecycle.AezaConfig,
            "request",
            side_effect=lifecycle.TransientLifecycleError("lost response"),
        ),
    ):
        lifecycle.delete_servers(CONFIG, IDENTITY, deleted_resources=deleted)
    assert deleted == ["aeza-service:42"]


def test_paid_order_is_hourly_and_post_is_never_replayed() -> None:
    with (
        patch.object(lifecycle, "aeza_items", return_value=[]),
        patch.object(
            lifecycle.AezaConfig,
            "request",
            side_effect=lifecycle.TransientLifecycleError("lost response"),
        ) as request,
        pytest.raises(lifecycle.TransientLifecycleError),
    ):
        lifecycle.create_server(CONFIG, IDENTITY, "17")
    request.assert_called_once()
    order = request.call_args.kwargs["body"]["orders"][0]
    assert order["term"] == "hour"
    assert order["termCount"] == 1
    assert order["autoProlong"] is True
    assert order["productId"] == 157
    assert order["sshKeyIds"] == [17]


def test_aeza_pagination_does_not_lose_attempt_resources() -> None:
    with patch.object(
        lifecycle.AezaConfig,
        "request",
        side_effect=[
            (200, {"items": [{"id": 1}], "total": 2}, {}),
            (200, {"items": [{"id": 2}], "total": 2}, {}),
        ],
    ) as request:
        assert lifecycle.aeza_items(CONFIG, "services") == [{"id": 1}, {"id": 2}]
    assert "offset=1" in request.call_args.args[1]


def test_cleanup_attempts_all_runners_after_cloud_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_TOKEN", "dummy")
    with (
        patch.object(lifecycle.AezaConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "build_identity", return_value=IDENTITY),
        patch.object(lifecycle, "delete_servers", side_effect=lifecycle.LifecycleError("API down")),
        patch.object(lifecycle, "delete_ssh_keys") as keys,
        patch.object(lifecycle, "delete_github_runners") as runners,
    ):
        args = lifecycle.build_parser().parse_args(
            ["cleanup", "--instance-key", IDENTITY.instance_key]
        )
        with pytest.raises(lifecycle.LifecycleError, match="Cleanup incomplete"):
            lifecycle.cleanup(args)
    assert runners.call_count == 4
    keys.assert_not_called()


def test_bootstrap_arms_timer_before_runner_and_keeps_secrets_out_of_argv() -> None:
    configs = {role: f"secret-jit-{role}" for role in lifecycle.ROLES}
    script = lifecycle.render_bootstrap(
        IDENTITY,
        CONFIG,
        "42",
        datetime(2026, 10, 28, 21, tzinfo=UTC),
        ("https://github.com/actions/runner", "a" * 64, "2.338.0"),
        configs,
    )
    encoded = script.split("base64.b64decode('")[1].split("'")[0]
    files = json.loads(base64.b64decode(encoded))
    assert CONFIG.api_key not in script
    assert all(value not in script for value in configs.values())
    assert (
        "OnCalendar=2026-10-28 21:00:00 UTC"
        in files["/etc/systemd/system/gup-emergency-self-destruct.timer"]
    )
    assert script.index("enable --now gup-emergency") < script.index("systemd-run")
    bootstrap = files["/usr/local/sbin/bootstrap-actions-runner"]
    assert bootstrap.index("policy drop") < bootstrap.index("useradd")
    assert "ct state established,related accept" in bootstrap
    assert "sha256sum --check --strict" in bootstrap
    for name in (
        "game-downloader",
        "wot-src-publisher",
        "wot-gui-assets-publisher",
        "wotstat-assets-uploader",
    ):
        assert f"User={name}" in bootstrap
    assert "StandardOutput=journal+console" in bootstrap
    assert "game-downloader ALL=(ALL) NOPASSWD: ALL" in bootstrap


@pytest.mark.parametrize("method,expected_calls", [("POST", 1), ("GET", 4)])
def test_provider_http_retries_only_safe_methods(method: str, expected_calls: int) -> None:
    error = urllib.error.HTTPError(
        "https://provider.invalid/", 502, "bad gateway", Message(), io.BytesIO()
    )
    with (
        patch.object(urllib.request, "urlopen", side_effect=error) as request,
        patch.object(time, "sleep"),
        pytest.raises(lifecycle.TransientLifecycleError),
    ):
        lifecycle.http_json(method, "https://provider.invalid/", token=CONFIG.api_key)
    assert request.call_count == expected_calls
    assert request.call_args.args[0].get_header("X-api-key") == CONFIG.api_key


def test_provider_error_never_logs_payload_with_credentials() -> None:
    raw = json.dumps(
        {"secureParameters": {"password": "dummy-password"}, "ownerId": 9, "token": CONFIG.api_key}
    ).encode()
    error = urllib.error.HTTPError(
        "https://provider.invalid", 400, "bad", Message(), io.BytesIO(raw)
    )
    with (
        patch.object(urllib.request, "urlopen", side_effect=error),
        pytest.raises(lifecycle.LifecycleError) as raised,
    ):
        lifecycle.http_json("POST", "https://provider.invalid", token=CONFIG.api_key)
    assert "dummy-password" not in str(raised.value)
    assert CONFIG.api_key not in str(raised.value)


def test_workflows_keep_consumers_and_remove_selectel_retry_controller() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/process-game-release.yml").read_text())
    assert set(workflow[True]["workflow_dispatch"]["inputs"]) == {
        "target",
        "client_type",
        "languages",
        "detected_release_name",
        "publish_wot_src",
        "publish_wot_gui_assets",
        "publish_wotstat_assets",
    }
    jobs = workflow["jobs"]
    assert jobs["publish-wot-src"]["uses"].endswith("publish-snapshot.yml@main")
    assert jobs["publish-wot-gui-assets"]["uses"].endswith("publish-snapshot.yml@main")
    assert jobs["publish-wotstat-assets"]["secrets"] == "inherit"
    assert jobs["cleanup"]["if"].startswith("always()")
    assert all(
        jobs[job]["environment"] == "aeza" for job in ("provision", "queue-watchdog", "cleanup")
    )
    reconciler = yaml.safe_load(
        (ROOT / ".github/workflows/reconcile-release-resources.yml").read_text()
    )
    assert "retry-preempted" not in reconciler["jobs"]
    assert reconciler["concurrency"]["cancel-in-progress"] is False


def test_missing_github_app_token_does_not_block_cloud_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_APP_TOKEN", raising=False)
    with (
        patch.object(lifecycle.AezaConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "build_identity", return_value=IDENTITY),
        patch.object(lifecycle, "delete_servers") as servers,
        patch.object(lifecycle, "delete_ssh_keys") as keys,
        patch.object(lifecycle, "delete_github_runners") as runners,
        pytest.raises(lifecycle.LifecycleError, match="GitHub App token unavailable"),
    ):
        args = lifecycle.build_parser().parse_args(
            ["cleanup", "--instance-key", IDENTITY.instance_key]
        )
        lifecycle.cleanup(args)
    servers.assert_called_once()
    keys.assert_called_once()
    runners.assert_not_called()

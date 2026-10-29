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
PROFILES = tuple(lifecycle.AEZA_PRODUCTS.values())


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
    billing_ids: set[str] = set()
    with (
        patch.object(lifecycle, "owned_servers", return_value=[]),
        patch.object(lifecycle, "get_server", return_value=server(**changes)),
        patch.object(lifecycle.AezaConfig, "request") as request,
        pytest.raises(lifecycle.LifecycleError, match="ownership"),
    ):
        lifecycle.delete_servers(
            CONFIG, IDENTITY, "42", deleted_resources=[], billing_service_ids=billing_ids
        )
    request.assert_not_called()
    assert billing_ids == set()


def test_both_aeza_list_and_detail_shapes_have_the_same_ownership() -> None:
    detail = server()
    summary = server(product={"id": 157})
    del summary["productId"]
    assert lifecycle.server_has_ownership_markers(detail, IDENTITY)
    assert lifecycle.server_has_ownership_markers(summary, IDENTITY)
    other = lifecycle.ResourceIdentity("another/pipeline", "12345", "2", "manual-12345-2")
    assert IDENTITY.server_name != other.server_name


def test_reconciliation_discovers_every_approved_profile_independently_of_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEZA_SERVER", "MSKs-5")
    owned = [
        server(id=index, productId=profile.product_id)
        for index, profile in enumerate(PROFILES, start=1)
    ]
    rows = [
        *owned,
        server(name="unrelated"),
        server(productId=999),
        server(paymentTerm="month"),
        server(status="deleted"),
    ]
    with patch.object(lifecycle, "aeza_items", return_value=rows):
        assert lifecycle.owned_servers(CONFIG, IDENTITY) == owned
        assert lifecycle.owned_servers(
            CONFIG, IDENTITY, product=lifecycle.AEZA_PRODUCTS["WAWs-5"]
        ) == [owned[2]]


@pytest.mark.parametrize("profile", PROFILES)
def test_delete_waits_for_actual_deletion_after_204(profile: lifecycle.AezaProduct) -> None:
    deleted: list[str] = []
    billing_ids: set[str] = set()
    with (
        patch.object(
            lifecycle, "owned_servers", return_value=[server(productId=profile.product_id)]
        ),
        patch.object(
            lifecycle,
            "get_server",
            side_effect=[
                server(productId=profile.product_id),
                server(productId=profile.product_id),
                server(productId=profile.product_id, status="deleted"),
            ],
        ),
        patch.object(lifecycle.AezaConfig, "request", return_value=(204, None, {})) as request,
        patch.object(time, "sleep"),
    ):
        lifecycle.delete_servers(
            CONFIG, IDENTITY, deleted_resources=deleted, billing_service_ids=billing_ids
        )
    assert deleted == ["aeza-service:42"]
    assert billing_ids == {"42"}
    request.assert_called_once()


@pytest.mark.parametrize("existing", [None, server(status="deleted")])
def test_already_deleted_vm_keeps_its_billing_identity(existing: dict[str, Any] | None) -> None:
    billing_ids: set[str] = set()
    with (
        patch.object(lifecycle, "owned_servers", return_value=[]),
        patch.object(lifecycle, "get_server", return_value=existing),
        patch.object(lifecycle.AezaConfig, "request") as request,
    ):
        lifecycle.delete_servers(
            CONFIG, IDENTITY, "42", deleted_resources=[], billing_service_ids=billing_ids
        )
    assert billing_ids == {"42"}
    request.assert_not_called()


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


@pytest.mark.parametrize("profile", PROFILES)
def test_paid_order_is_hourly_and_post_is_never_replayed(profile: lifecycle.AezaProduct) -> None:
    with (
        patch.object(lifecycle, "aeza_items", return_value=[]),
        patch.object(
            lifecycle.AezaConfig,
            "request",
            side_effect=lifecycle.TransientLifecycleError("lost response"),
        ) as request,
        pytest.raises(lifecycle.TransientLifecycleError),
    ):
        lifecycle.create_server(CONFIG, IDENTITY, "17", profile)
    request.assert_called_once()
    order = request.call_args.kwargs["body"]["orders"][0]
    assert order["term"] == "hour"
    assert order["termCount"] == 1
    assert order["autoProlong"] is True
    assert order["productId"] == profile.product_id
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


def test_cleanup_attempts_all_runners_after_cloud_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_APP_TOKEN", "dummy")
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def fail_delete(*_args: Any, billing_service_ids: set[str], **_kwargs: Any) -> None:
        billing_service_ids.add("42")
        raise lifecycle.LifecycleError("API down")

    with (
        patch.object(lifecycle.AezaConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "build_identity", return_value=IDENTITY),
        patch.object(lifecycle, "delete_servers", side_effect=fail_delete),
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
    assert 'billing_service_ids=["42"]\n' in output.read_text()
    assert "vm_deleted=false\n" in output.read_text()


@pytest.mark.parametrize("profile", PROFILES)
def test_bootstrap_arms_timer_before_runner_and_keeps_secrets_out_of_argv(
    profile: lifecycle.AezaProduct,
) -> None:
    configs = {role: f"secret-jit-{role}" for role in lifecycle.ROLES}
    script = lifecycle.render_bootstrap(
        IDENTITY,
        CONFIG,
        "42",
        datetime(2026, 10, 28, 21, tzinfo=UTC),
        ("https://github.com/actions/runner", "a" * 64, "2.338.0"),
        configs,
        product=profile,
    )
    encoded = script.split("base64.b64decode('")[1].split("'")[0]
    files = json.loads(base64.b64decode(encoded))
    assert CONFIG.api_key not in script
    assert all(value not in script for value in configs.values())
    assert json.loads(files["/etc/gup-emergency-self-destruct.json"])["product_id"] == (
        profile.product_id
    )
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


def test_workflows_keep_consumers_and_aeza_cleanup() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/process-game-release.yml").read_text())
    assert set(workflow[True]["workflow_dispatch"]["inputs"]) == {
        "target",
        "client_type",
        "languages",
        "detected_release_name",
        "dedicated_download_control",
        "aeza_server",
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
    assert reconciler[True]["workflow_run"]["workflows"] == ["Process game release"]
    assert reconciler["concurrency"]["cancel-in-progress"] is False


def test_missing_github_app_token_does_not_block_cloud_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_APP_TOKEN", raising=False)
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
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
    assert "vm_deleted=true\n" in output.read_text()


def test_product_selection_defaults_to_moscow_and_rejects_arbitrary_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AEZA_SERVER", raising=False)
    assert lifecycle.selected_product().name == "MSKs-5"
    monkeypatch.setenv("AEZA_SERVER", "WAWs-5")
    assert lifecycle.selected_product().product_id == 445
    monkeypatch.setenv("AEZA_SERVER", "other-product")
    with pytest.raises(lifecycle.LifecycleError, match="Unsupported"):
        lifecycle.selected_product()


def catalogue_product(profile: lifecycle.AezaProduct, **changes: Any) -> dict[str, Any]:
    return {
        "id": profile.product_id,
        "name": profile.name,
        "groupId": profile.group_id,
        "isAvailable": True,
        "parameters": {"cpu": 16, "ram": 32, "rom": 480, "net": 25000},
        **changes,
    }


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("price_increase", [0, 1])
def test_preflight_checks_each_selected_profiles_quote(
    profile: lifecycle.AezaProduct, price_increase: int
) -> None:
    with (
        patch.object(lifecycle, "aeza_items", return_value=[catalogue_product(profile)]),
        patch.object(
            lifecycle.AezaConfig,
            "request",
            side_effect=[
                (
                    200,
                    {
                        "totalPrice": profile.max_hourly_cents + price_increase,
                        "availability": {"status": "ok"},
                    },
                    {},
                ),
                (200, {"balance": 10000}, {}),
                (200, {"items": []}, {}),
            ],
        ) as request,
    ):
        if price_increase:
            with pytest.raises(lifecycle.LifecycleError, match="hourly quote"):
                lifecycle.preflight(CONFIG, profile)
        else:
            lifecycle.preflight(CONFIG, profile)
    assert request.call_args_list[0].args == (
        "GET",
        f"services/orders/price?productId={profile.product_id}&paymentTerm=hour&termCount=1",
    )


@pytest.mark.parametrize(
    "changes", [{"isAvailable": False}, {"groupId": 999}, {"parameters": {"rom": 30}}]
)
def test_preflight_rejects_unavailable_or_changed_product_before_allocation(
    changes: dict[str, Any],
) -> None:
    profile = lifecycle.AEZA_PRODUCTS["WAWs-5"]
    with (
        patch.object(lifecycle, "aeza_items", return_value=[catalogue_product(profile, **changes)]),
        patch.object(lifecycle.AezaConfig, "request") as request,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.preflight(CONFIG, profile)
    request.assert_not_called()


@pytest.mark.parametrize(
    "groups,terms,blocked",
    [
        ([2], ["hour"], False),
        ([1], ["month"], False),
        ([1], ["hour"], True),
        ([], ["hour"], True),
        ([1], [], True),
    ],
)
def test_account_limit_applies_only_to_the_selected_group_and_payment_term(
    groups: list[int], terms: list[str], blocked: bool
) -> None:
    profile = lifecycle.AEZA_PRODUCTS["MSK-5"]
    with (
        patch.object(lifecycle, "aeza_items", return_value=[catalogue_product(profile)]),
        patch.object(
            lifecycle.AezaConfig,
            "request",
            side_effect=[
                (200, {"totalPrice": 76, "availability": {"status": "ok"}}, {}),
                (200, {"balance": 10000}, {}),
                (
                    200,
                    {
                        "items": [
                            {"groupIds": groups, "paymentTerms": terms, "used": 2, "available": 2}
                        ]
                    },
                    {},
                ),
            ],
        ),
    ):
        if blocked:
            with pytest.raises(lifecycle.LifecycleError, match="account limit"):
                lifecycle.preflight(CONFIG, profile)
        else:
            lifecycle.preflight(CONFIG, profile)


@pytest.mark.parametrize("workflow_name", ["process-game-release", "check-game-releases"])
def test_all_manual_workflows_offer_the_same_server_choices_and_default(workflow_name: str) -> None:
    document = yaml.safe_load((ROOT / f".github/workflows/{workflow_name}.yml").read_text())
    choice = document[True]["workflow_dispatch"]["inputs"]["aeza_server"]
    assert choice["type"] == "choice"
    assert choice["default"] == "MSKs-5"
    assert (
        choice["options"]
        == list(lifecycle.AEZA_PRODUCTS)
        == ["MSKs-5", "MSK-5", "WAWs-5", "CLTs-5"]
    )
    assert document["env"]["AEZA_SERVER"] == "${{ inputs.aeza_server || 'MSKs-5' }}"
    assert "inputs.aeza_server" in document["run-name"]


def test_failed_ssh_bootstrap_redacts_encoded_secret_files() -> None:
    encoded = base64.b64encode(
        json.dumps(
            {
                "api_key": CONFIG.api_key,
                "server_name": IDENTITY.server_name,
                "jit_config": "dummy-jit-configuration",
            }
        ).encode()
    ).decode()
    result = Mock(returncode=1, stdout=f"Traceback: base64.b64decode('{encoded}')", stderr="")
    with (
        patch("subprocess.run", side_effect=[Mock(returncode=0), result]),
        pytest.raises(lifecycle.LifecycleError) as raised,
    ):
        lifecycle.ssh_bootstrap(
            "1.1.1.1", Path("/tmp/dummy-key"), "safe install", sensitive_values=(CONFIG.api_key,)
        )
    assert encoded not in str(raised.value)
    assert CONFIG.api_key not in str(raised.value)

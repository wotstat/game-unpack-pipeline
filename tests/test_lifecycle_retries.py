from __future__ import annotations

import io
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from unittest.mock import Mock, patch

import pytest

from scripts import runner_lifecycle as lifecycle

PORT_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
GROUP_ID = "123e4567-e89b-12d3-a456-426614174000"
PROJECT_ID = "00000000-0000-0000-0000-000000000001"
IDENTITY = lifecycle.ResourceIdentity(
    "wotstat/game-unpack-pipeline", "123456", "2", "manual-123456-2"
)
CONFIG = lifecycle.SelectelConfig(
    auth_url="https://api.example.test/identity/v3",
    username="test-user",
    password="test-password",
    user_domain_name="test-domain",
    project_id=PROJECT_ID,
    region_name="ru-7",
    availability_zone="ru-7b",
    image_id="test-image",
    flavor_id="test-flavor",
    public_network_api_url="https://api.example.test/public-network",
)


def response(payload: object, status: int = 200) -> Mock:
    result = Mock(status=status)
    result.read.return_value = json.dumps(payload).encode()
    result.headers.items.return_value = []
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example.test", status, "test failure", Message(), io.BytesIO(b"test failure")
    )


def owned_port(**changes: object) -> dict[str, object]:
    return {
        "id": PORT_ID,
        "project_id": PROJECT_ID,
        "description": IDENTITY.descriptor,
        "security_group_ids": [GROUP_ID],
        "ip_address": "192.0.2.1",
        **changes,
    }


def test_provision_recovers_port_created_despite_bad_gateway() -> None:
    # The POST committed, but its response was lost. Discovery can lag behind creation.
    with (
        patch.object(lifecycle, "selectel_token", return_value="test-token"),
        patch.object(lifecycle, "openstack", return_value=Mock(returncode=0)),
        patch.object(time, "sleep"),
        patch.object(
            urllib.request,
            "urlopen",
            side_effect=[
                http_error(502),
                response({"ports": []}),
                response({"ports": [owned_port()]}),
                response({"port": owned_port()}),
            ],
        ) as urlopen,
    ):
        assert lifecycle.create_public_port(CONFIG, IDENTITY, GROUP_ID) == (PORT_ID, "192.0.2.1")
    assert [call.args[0].method for call in urlopen.call_args_list] == ["POST", "GET", "GET", "GET"]


def test_cleanup_retries_credential_lookup_after_tls_eof() -> None:
    deleted: list[str] = []
    with (
        patch.object(time, "sleep"),
        patch.object(
            subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(
                    [], 1, "", "SSL exception: [SSL: UNEXPECTED_EOF_WHILE_READING]"
                ),
                subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps([{"ID": GROUP_ID, "Name": IDENTITY.emergency_credential_name}]),
                    "",
                ),
                subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {
                            "name": IDENTITY.emergency_credential_name,
                            "description": IDENTITY.emergency_credential_description,
                        }
                    ),
                    "",
                ),
                subprocess.CompletedProcess([], 0, "", ""),
            ],
        ),
    ):
        lifecycle.delete_emergency_application_credentials(
            CONFIG, IDENTITY, deleted_resources=deleted
        )
    assert deleted == [f"selectel-application-credential:{GROUP_ID}"]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_safe_http_requests_retry_temporary_statuses(method: str, status: int) -> None:
    with (
        patch.object(time, "sleep"),
        patch.object(
            urllib.request,
            "urlopen",
            side_effect=[http_error(status), response({"ok": True})],
        ),
    ):
        assert lifecycle.http_json(method, "https://api.example.test")[1] == {"ok": True}


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_http_permanent_errors_are_not_retried(status: int) -> None:
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(urllib.request, "urlopen", side_effect=http_error(status)) as urlopen,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.http_json("GET", "https://api.example.test")
    assert urlopen.call_count == 1
    sleep.assert_not_called()


def test_http_retries_are_bounded() -> None:
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(
            urllib.request, "urlopen", side_effect=[http_error(503) for _ in range(4)]
        ) as urlopen,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.http_json("GET", "https://api.example.test")
    assert urlopen.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [5, 15, 30]


@pytest.mark.parametrize("check", [True, False])
def test_exhausted_cli_connection_errors_are_never_reported_as_absence(check: bool) -> None:
    with (
        patch.object(time, "sleep"),
        patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "Connection reset by peer"),
        ) as run,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.openstack(CONFIG, ["server", "show", PORT_ID], check=check)
    assert run.call_count == 4


def test_cli_does_not_repeat_resource_creation() -> None:
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "HTTP 503"),
        ) as run,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.openstack(CONFIG, ["server", "create", "test-server"])
    assert run.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_cli_auth_errors_fail_without_retry(status: int) -> None:
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", f"HTTP {status}"),
        ) as run,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.openstack(CONFIG, ["application", "credential", "list"])
    assert run.call_count == 1
    sleep.assert_not_called()


def test_cli_timeout_can_recover_within_total_budget() -> None:
    with (
        patch.object(time, "sleep"),
        patch.object(time, "monotonic", side_effect=[0, 0, 60, 65]),
        patch.object(
            subprocess,
            "run",
            side_effect=[
                subprocess.TimeoutExpired("openstack", 60),
                subprocess.CompletedProcess([], 0, "[]", ""),
            ],
        ) as run,
    ):
        assert lifecycle.find_emergency_application_credential_ids(CONFIG, IDENTITY) == []
    assert run.call_args_list[0].kwargs["timeout"] == 60


def test_cli_retry_stops_at_total_budget() -> None:
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(time, "monotonic", side_effect=[0, 0, 179]),
        patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "HTTP 503"),
        ) as run,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.openstack(CONFIG, ["application", "credential", "list"])
    assert run.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"description": "another-run"},
        {"project_id": GROUP_ID},
        {"security_group_ids": []},
        {"id": GROUP_ID},
    ],
)
def test_port_recovery_rechecks_ownership_and_security(changes: dict[str, object]) -> None:
    with (
        patch.object(lifecycle, "selectel_token", return_value="test-token"),
        patch.object(time, "sleep"),
        patch.object(
            urllib.request,
            "urlopen",
            side_effect=[
                http_error(502),
                response({"ports": [owned_port()]}),
                response({"port": owned_port(**changes)}),
            ],
        ),
        pytest.raises(lifecycle.LifecycleError, match="ownership or security group"),
    ):
        lifecycle.create_public_port(CONFIG, IDENTITY, GROUP_ID)


def test_port_recovery_never_replays_create_if_port_stays_missing() -> None:
    with (
        patch.object(lifecycle, "selectel_token", return_value="test-token"),
        patch.object(time, "sleep"),
        patch.object(
            urllib.request,
            "urlopen",
            side_effect=[
                http_error(502),
                *[response({"ports": [owned_port(description="another-run")]}) for _ in range(3)],
            ],
        ) as urlopen,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.create_public_port(CONFIG, IDENTITY, GROUP_ID)
    assert [call.args[0].method for call in urlopen.call_args_list] == ["POST", "GET", "GET", "GET"]


def test_port_recovery_rejects_multiple_owned_ports() -> None:
    with (
        patch.object(lifecycle, "selectel_token", return_value="test-token"),
        patch.object(time, "sleep"),
        patch.object(
            urllib.request,
            "urlopen",
            side_effect=[
                http_error(502),
                response({"ports": [owned_port(), owned_port(id=GROUP_ID)]}),
            ],
        ),
        pytest.raises(lifecycle.LifecycleError, match="Multiple owned public ports"),
    ):
        lifecycle.create_public_port(CONFIG, IDENTITY, GROUP_ID)


def test_authentication_post_is_safe_to_retry() -> None:
    issued = response({}, 201)
    issued.headers.items.return_value = [("X-Subject-Token", "test-issued-token")]
    with (
        patch.object(time, "sleep"),
        patch.object(urllib.request, "urlopen", side_effect=[http_error(503), issued]),
    ):
        assert lifecycle.selectel_token(CONFIG) == "test-issued-token"


def test_certificate_verification_error_is_not_retried() -> None:
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError(ssl.SSLCertVerificationError("test")),
        ),
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.http_json("GET", "https://api.example.test")
    sleep.assert_not_called()


def test_cleanup_continues_when_console_diagnostics_api_fails() -> None:
    arguments = Mock(
        instance_key=IDENTITY.instance_key,
        run_id=IDENTITY.run_id,
        run_attempt=IDENTITY.run_attempt,
        diagnostics=True,
        server_id=PORT_ID,
        downloader_runner_id="",
        wot_src_runner_id="",
        wot_gui_assets_runner_id="",
        wotstat_assets_runner_id="",
        port_id="",
        security_group_id="",
        emergency_credential_id="",
    )
    deleted: list[str] = []

    def delete_server(*_args: object, **_kwargs: object) -> None:
        deleted.append("server")

    with (
        patch.dict(
            "os.environ",
            {"GITHUB_APP_TOKEN": "test-token", "GITHUB_REPOSITORY": IDENTITY.repository},
            clear=True,
        ),
        patch.object(lifecycle.SelectelConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "find_server_ids", return_value=[PORT_ID]),
        patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "HTTP 503")
        ),
        patch.object(lifecycle, "delete_github_runners"),
        patch.object(lifecycle, "delete_servers", side_effect=delete_server),
        patch.object(lifecycle, "selectel_token", return_value="test-token"),
        patch.object(lifecycle, "delete_public_ports"),
        patch.object(lifecycle, "delete_security_groups"),
        patch.object(lifecycle, "delete_emergency_application_credentials"),
    ):
        lifecycle.cleanup(arguments)
    assert deleted == ["server"]


@pytest.mark.parametrize("kind", ["server", "credential", "group"])
@pytest.mark.parametrize("already_deleted", [True, False])
def test_cleanup_reconciles_ambiguous_delete_before_retry(kind: str, already_deleted: bool) -> None:
    if kind == "server":
        delete: Callable[..., None] = lifecycle.delete_servers
        finder = "find_server_ids"
        owned = {
            "name": IDENTITY.server_name,
            "properties": {
                "gup_repository": IDENTITY.repository,
                "gup_run_id": IDENTITY.run_id,
                "gup_run_attempt": IDENTITY.run_attempt,
                "gup_instance_key": IDENTITY.instance_key,
            },
        }
    elif kind == "credential":
        delete = lifecycle.delete_emergency_application_credentials
        finder = "find_emergency_application_credential_ids"
        owned = {
            "name": IDENTITY.emergency_credential_name,
            "description": IDENTITY.emergency_credential_description,
        }
    else:
        delete = lifecycle.delete_security_groups
        finder = "find_security_group_ids"
        owned = {"name": IDENTITY.security_group_name, "description": IDENTITY.descriptor}
    shown = subprocess.CompletedProcess([], 0, json.dumps(owned), "")
    absent = subprocess.CompletedProcess([], 1, "", "Not found (HTTP 404)")
    replies = [shown, subprocess.CompletedProcess([], 1, "", "SSL: UNEXPECTED_EOF_WHILE_READING")]
    if already_deleted:
        replies.append(absent)
    else:
        replies.extend([shown, subprocess.CompletedProcess([], 0, "", "")])
        if kind == "server":
            replies.append(absent)
    deleted: list[str] = []
    with (
        patch.object(lifecycle, finder, return_value=[GROUP_ID]),
        patch.object(time, "sleep"),
        patch.object(subprocess, "run", side_effect=replies) as run,
    ):
        delete(CONFIG, IDENTITY, GROUP_ID, deleted_resources=deleted)
    assert len(deleted) == (0 if already_deleted else 1)
    commands = [call.args[0] for call in run.call_args_list]
    assert sum("delete" in command for command in commands) == (1 if already_deleted else 2)


@pytest.mark.parametrize("error", ["HTTP 403", "Certificate verify failed", "Unknown CLI failure"])
def test_delete_recovery_does_not_treat_failed_lookup_as_absence(error: str) -> None:
    with (
        patch.object(
            subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 1, "", "SSL: UNEXPECTED_EOF_WHILE_READING"),
                subprocess.CompletedProcess([], 1, "", error),
            ],
        ) as run,
        pytest.raises(lifecycle.LifecycleError, match="Could not confirm resource state"),
    ):
        lifecycle.delete_openstack_resource(CONFIG, ["security", "group"], GROUP_ID)
    assert run.call_count == 2


def test_delete_retries_are_bounded_when_resource_survives() -> None:
    replies = [
        item
        for _ in range(4)
        for item in (
            subprocess.CompletedProcess([], 1, "", "HTTP 503"),
            subprocess.CompletedProcess([], 0, "{}", ""),
        )
    ]
    with (
        patch.object(time, "sleep") as sleep,
        patch.object(subprocess, "run", side_effect=replies) as run,
        pytest.raises(lifecycle.TransientLifecycleError),
    ):
        lifecycle.delete_openstack_resource(CONFIG, ["security", "group"], GROUP_ID)
    assert run.call_count == 8
    assert [call.args[0] for call in sleep.call_args_list] == [5, 15, 30]


def test_delete_recovery_does_not_repeat_delete_when_lookup_api_stays_unavailable() -> None:
    with (
        patch.object(time, "sleep"),
        patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "HTTP 503")
        ) as run,
        pytest.raises(lifecycle.TransientLifecycleError),
    ):
        lifecycle.delete_openstack_resource(CONFIG, ["server"], GROUP_ID)
    assert sum("delete" in call.args[0] for call in run.call_args_list) == 1


def test_delete_retry_budget_includes_verification() -> None:
    with (
        patch.object(time, "monotonic", side_effect=[0, 0, 180]),
        patch.object(time, "sleep") as sleep,
        patch.object(
            lifecycle, "openstack", side_effect=lifecycle.TransientLifecycleError("timeout")
        ) as command,
        pytest.raises(lifecycle.TransientLifecycleError),
    ):
        lifecycle.delete_openstack_resource(CONFIG, ["server"], GROUP_ID)
    assert command.call_count == 1
    assert command.call_args.kwargs["timeout"] == 60
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "resource", [["server"], ["application", "credential"], ["security", "group"]]
)
@pytest.mark.parametrize("check", [True, False])
def test_resource_can_disappear_between_verification_and_retried_delete(
    resource: list[str], check: bool
) -> None:
    with (
        patch.object(time, "sleep"),
        patch.object(
            subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 1, "", "HTTP 503"),
                subprocess.CompletedProcess([], 0, "{}", ""),
                subprocess.CompletedProcess([], 1, "", "Not found (HTTP 404)"),
                subprocess.CompletedProcess([], 1, "", "Not found (HTTP 404)"),
            ],
        ),
    ):
        assert lifecycle.delete_openstack_resource(CONFIG, resource, GROUP_ID, check=check) is None

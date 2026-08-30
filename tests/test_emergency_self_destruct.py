from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import emergency_self_destruct as watchdog

SERVER_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
CREDENTIAL_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_load_config_requires_https_and_a_valid_region(tmp_path: Path) -> None:
    path = tmp_path / "watchdog.json"
    path.write_text(
        json.dumps(
            {
                "auth_url": "https://cloud.api.selcloud.ru/identity/v3",
                "region": "ru-7",
            }
        ),
        encoding="utf-8",
    )

    config = watchdog.load_config(path)

    assert config == watchdog.EmergencyConfig(
        auth_url="https://cloud.api.selcloud.ru/identity/v3",
        region="ru-7",
    )


def test_load_target_reads_only_instance_specific_metadata() -> None:
    metadata = {
        "uuid": SERVER_ID,
        "meta": {
            "gup_emergency_credential_id": CREDENTIAL_ID,
            "gup_emergency_credential_secret": "one-time-secret",
        },
    }

    with patch.object(watchdog, "request_json", return_value=(200, metadata, {})):
        target = watchdog.load_target()

    assert target == watchdog.EmergencyTarget(
        server_id=SERVER_ID,
        application_credential_id=CREDENTIAL_ID,
        application_credential_secret="one-time-secret",
    )


def test_select_compute_endpoint_requires_one_public_endpoint_in_the_configured_region() -> None:
    token_payload = {
        "token": {
            "catalog": [
                {
                    "type": "compute",
                    "endpoints": [
                        {
                            "interface": "public",
                            "region_id": "ru-7",
                            "url": "https://ru-7.cloud.api.selcloud.ru/compute/v2.1",
                        },
                        {
                            "interface": "public",
                            "region_id": "ru-9",
                            "url": "https://ru-9.cloud.api.selcloud.ru/compute/v2.1",
                        },
                    ],
                }
            ]
        }
    }

    assert watchdog.select_compute_endpoint(token_payload, "ru-7") == (
        "https://ru-7.cloud.api.selcloud.ru/compute/v2.1"
    )
    with pytest.raises(watchdog.EmergencySelfDestructError):
        watchdog.select_compute_endpoint(token_payload, "ru-8")


def test_authentication_and_delete_use_the_application_credential_and_exact_server() -> None:
    config = watchdog.EmergencyConfig(
        auth_url="https://cloud.api.selcloud.ru/identity/v3",
        region="ru-7",
    )
    target = watchdog.EmergencyTarget(
        server_id=SERVER_ID,
        application_credential_id=CREDENTIAL_ID,
        application_credential_secret="one-time-secret",
    )
    token_payload = {
        "token": {
            "catalog": [
                {
                    "type": "compute",
                    "endpoints": [
                        {
                            "interface": "public",
                            "region": "ru-7",
                            "url": "https://ru-7.cloud.api.selcloud.ru/compute/v2.1",
                        }
                    ],
                }
            ]
        }
    }

    with patch.object(
        watchdog,
        "request_json",
        side_effect=[
            (201, token_payload, {"X-Subject-Token": "short-lived-token"}),
            (204, None, {}),
        ],
    ) as request:
        token, endpoint = watchdog.authenticate(config, target)
        watchdog.delete_server(endpoint, target.server_id, token)

    authentication = request.call_args_list[0]
    assert authentication.args == (
        "POST",
        "https://cloud.api.selcloud.ru/identity/v3/auth/tokens",
    )
    assert authentication.kwargs["body"] == {
        "auth": {
            "identity": {
                "methods": ["application_credential"],
                "application_credential": {
                    "id": CREDENTIAL_ID,
                    "secret": "one-time-secret",
                },
            }
        }
    }
    deletion = request.call_args_list[1]
    assert deletion.args == (
        "DELETE",
        f"https://ru-7.cloud.api.selcloud.ru/compute/v2.1/servers/{SERVER_ID}",
    )
    assert deletion.kwargs["headers"] == {"X-Auth-Token": "short-lived-token"}

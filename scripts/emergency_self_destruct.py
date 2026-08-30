#!/usr/bin/env python3
"""Delete the current OpenStack VM when its emergency lifetime expires."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

METADATA_URL = "http://169.254.169.254/openstack/latest/meta_data.json"
UUID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
REGION_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


class EmergencySelfDestructError(RuntimeError):
    """A failure that systemd should retry while the VM still exists."""


@dataclass(frozen=True)
class EmergencyConfig:
    auth_url: str
    region: str


@dataclass(frozen=True)
class EmergencyTarget:
    server_id: str
    application_credential_id: str
    application_credential_secret: str


def _required_string(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise EmergencySelfDestructError(f"missing or invalid {name}")
    if "\n" in item or "\r" in item:
        raise EmergencySelfDestructError(f"invalid newline in {name}")
    return item


def load_config(path: Path) -> EmergencyConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EmergencySelfDestructError("could not read emergency configuration") from error
    if not isinstance(payload, dict):
        raise EmergencySelfDestructError("emergency configuration must be an object")

    auth_url = _required_string(payload, "auth_url").rstrip("/")
    parsed = urllib.parse.urlparse(auth_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise EmergencySelfDestructError("emergency auth_url must be HTTPS")
    region = _required_string(payload, "region")
    if not REGION_RE.fullmatch(region):
        raise EmergencySelfDestructError("emergency region is invalid")
    return EmergencyConfig(auth_url=auth_url, region=region)


def request_json(
    method: str,
    url: str,
    *,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    expected_statuses: Sequence[int] = (200,),
    timeout: int = 15,
) -> tuple[int, Any, Mapping[str, str]]:
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
        response_headers = dict(error.headers.items()) if error.headers else {}
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        raise EmergencySelfDestructError(f"{method} request failed: {reason}") from error

    if status not in expected_statuses:
        raise EmergencySelfDestructError(f"{method} request returned HTTP {status}")
    if not raw:
        payload: Any = None
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise EmergencySelfDestructError(f"{method} request returned invalid JSON") from error
    return status, payload, response_headers


def load_target() -> EmergencyTarget:
    _, payload, _ = request_json("GET", METADATA_URL, timeout=5)
    if not isinstance(payload, dict):
        raise EmergencySelfDestructError("instance metadata must be an object")
    server_id = _required_string(payload, "uuid")
    if not UUID_RE.fullmatch(server_id):
        raise EmergencySelfDestructError("instance metadata contains an invalid UUID")
    metadata = payload.get("meta")
    if not isinstance(metadata, dict):
        raise EmergencySelfDestructError("instance metadata does not contain emergency credentials")
    credential_id = _required_string(metadata, "gup_emergency_credential_id")
    credential_secret = _required_string(metadata, "gup_emergency_credential_secret")
    if not UUID_RE.fullmatch(credential_id):
        raise EmergencySelfDestructError("emergency credential ID is invalid")
    return EmergencyTarget(
        server_id=server_id,
        application_credential_id=credential_id,
        application_credential_secret=credential_secret,
    )


def select_compute_endpoint(token_payload: Any, region: str) -> str:
    if not isinstance(token_payload, dict) or not isinstance(token_payload.get("token"), dict):
        raise EmergencySelfDestructError("identity response does not contain token details")
    catalog = token_payload["token"].get("catalog", [])
    matches: list[str] = []
    for service in catalog if isinstance(catalog, list) else []:
        if not isinstance(service, dict) or service.get("type") != "compute":
            continue
        endpoints = service.get("endpoints", [])
        for endpoint in endpoints if isinstance(endpoints, list) else []:
            if not isinstance(endpoint, dict) or endpoint.get("interface") != "public":
                continue
            endpoint_region = endpoint.get("region_id", endpoint.get("region"))
            url = endpoint.get("url")
            if endpoint_region == region and isinstance(url, str):
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme == "https" and parsed.netloc:
                    matches.append(url.rstrip("/"))
    if len(matches) != 1:
        raise EmergencySelfDestructError("identity catalog has no unique public compute endpoint")
    return matches[0]


def authenticate(config: EmergencyConfig, target: EmergencyTarget) -> tuple[str, str]:
    _, payload, headers = request_json(
        "POST",
        f"{config.auth_url}/auth/tokens",
        body={
            "auth": {
                "identity": {
                    "methods": ["application_credential"],
                    "application_credential": {
                        "id": target.application_credential_id,
                        "secret": target.application_credential_secret,
                    },
                }
            }
        },
        expected_statuses=(201,),
    )
    token = next(
        (value for key, value in headers.items() if key.lower() == "x-subject-token"),
        "",
    )
    if not token:
        raise EmergencySelfDestructError("identity response did not contain a subject token")
    return token, select_compute_endpoint(payload, config.region)


def delete_server(endpoint: str, server_id: str, token: str) -> None:
    request_json(
        "DELETE",
        f"{endpoint}/servers/{server_id}",
        headers={"X-Auth-Token": token},
        expected_statuses=(204, 404),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        config = load_config(arguments.config)
        target = load_target()
        print("gup-emergency-self-destruct: deadline reached; requesting VM deletion")
        token, endpoint = authenticate(config, target)
        delete_server(endpoint, target.server_id, token)
        print("gup-emergency-self-destruct: Selectel accepted VM deletion")
        return 0
    except EmergencySelfDestructError as error:
        print(f"gup-emergency-self-destruct: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

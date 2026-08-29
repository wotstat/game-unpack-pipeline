#!/usr/bin/env python3
"""Provision and clean one Selectel VM with isolated GitHub Actions JIT runners.

The script deliberately keeps all cloud mutations behind explicit subcommands.
Importing it and running its unit tests never contacts Selectel or GitHub.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GITHUB_API_VERSION = "2026-03-10"
PIPELINE_MARKER = "game-unpack-pipeline"
INSTANCE_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
UUID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


class LifecycleError(RuntimeError):
    """An expected, user-facing lifecycle failure."""


def validate_repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise LifecycleError("repository must have owner/repository form")
    return value


@dataclass(frozen=True)
class RunnerIdentity:
    repository: str
    role: str
    name: str
    label: str
    scope_label: str

    def __post_init__(self) -> None:
        validate_repository(self.repository)
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", self.role):
            raise LifecycleError("runner role is invalid")


@dataclass(frozen=True)
class ResourceIdentity:
    repository: str
    run_id: str
    run_attempt: str
    instance_key: str

    def __post_init__(self) -> None:
        validate_instance_key(self.instance_key)
        if not re.fullmatch(r"[1-9][0-9]*", self.run_id):
            raise LifecycleError("GITHUB_RUN_ID must be a positive integer")
        if not re.fullmatch(r"[1-9][0-9]*", self.run_attempt):
            raise LifecycleError("GITHUB_RUN_ATTEMPT must be a positive integer")
        validate_repository(self.repository)

    @property
    def base_name(self) -> str:
        return f"gup-{self.instance_key}"

    @property
    def server_name(self) -> str:
        return self.base_name

    @property
    def security_group_name(self) -> str:
        return f"{self.base_name}-sg"

    @property
    def scope_label(self) -> str:
        return f"gup-run-{self.run_id}-{self.run_attempt}"

    @property
    def descriptor(self) -> str:
        return (
            f"{PIPELINE_MARKER};repository={self.repository};run_id={self.run_id};"
            f"run_attempt={self.run_attempt};instance_key={self.instance_key}"
        )

    def runner(self, role: str) -> RunnerIdentity:
        name = f"{self.base_name}-{role}"
        return RunnerIdentity(
            repository=self.repository,
            role=role,
            name=name,
            label=name,
            scope_label=self.scope_label,
        )


@dataclass(frozen=True)
class SelectelConfig:
    auth_url: str
    username: str
    password: str
    user_domain_name: str
    project_id: str
    region_name: str
    availability_zone: str
    image_id: str
    flavor_id: str
    public_network_api_url: str

    @classmethod
    def from_environment(cls, *, require_compute: bool = True) -> SelectelConfig:
        required = {
            "SELECTEL_OS_AUTH_URL": "auth_url",
            "SELECTEL_OS_USERNAME": "username",
            "SELECTEL_OS_PASSWORD": "password",
            "SELECTEL_OS_USER_DOMAIN_NAME": "user_domain_name",
            "SELECTEL_OS_PROJECT_ID": "project_id",
            "SELECTEL_OS_REGION_NAME": "region_name",
            "SELECTEL_PUBLIC_NETWORK_API_URL": "public_network_api_url",
        }
        if require_compute:
            required.update(
                {
                    "SELECTEL_AVAILABILITY_ZONE": "availability_zone",
                    "SELECTEL_IMAGE_ID": "image_id",
                    "SELECTEL_FLAVOR_ID": "flavor_id",
                }
            )

        values: dict[str, str] = {}
        missing: list[str] = []
        for environment_name, field_name in required.items():
            value = os.environ.get(environment_name, "").strip()
            if not value:
                missing.append(environment_name)
            values[field_name] = value
        if missing:
            raise LifecycleError(f"Missing configuration: {', '.join(sorted(missing))}")

        values.setdefault("availability_zone", "")
        values.setdefault("image_id", "")
        values.setdefault("flavor_id", "")

        if not UUID_RE.fullmatch(values["project_id"]):
            raise LifecycleError("SELECTEL_OS_PROJECT_ID must be a UUID")
        for url_field in ("auth_url", "public_network_api_url"):
            parsed = urllib.parse.urlparse(values[url_field])
            if parsed.scheme != "https" or not parsed.netloc:
                raise LifecycleError(f"{url_field} must be an HTTPS URL")

        return cls(**values)

    def openstack_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "OS_AUTH_URL": self.auth_url,
                "OS_IDENTITY_API_VERSION": "3",
                "OS_VOLUME_API_VERSION": "3",
                "OS_PROJECT_DOMAIN_NAME": self.user_domain_name,
                "OS_PROJECT_ID": self.project_id,
                "OS_REGION_NAME": self.region_name,
                "OS_USER_DOMAIN_NAME": self.user_domain_name,
                "OS_USERNAME": self.username,
                "OS_PASSWORD": self.password,
            }
        )
        return environment


def validate_instance_key(value: str) -> str:
    if not INSTANCE_KEY_RE.fullmatch(value):
        raise LifecycleError(
            "instance-key must be 1-48 lowercase letters, digits, or hyphens; "
            "it must start and end with a letter or digit"
        )
    return value


def normalized_uuid(value: str) -> str:
    return value.replace("-", "").lower()


def require_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LifecycleError(f"{name} is required")
    return value


def add_mask(value: str) -> None:
    if value:
        print(f"::add-mask::{value}")


def write_output(name: str, value: str | int) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    text = str(value)
    if "\n" in text or "\r" in text:
        raise LifecycleError(f"Output {name} unexpectedly contains a newline")
    with open(output_file, "a", encoding="utf-8") as stream:
        stream.write(f"{name}={text}\n")


def append_summary(line: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    with open(summary_file, "a", encoding="utf-8") as stream:
        stream.write(f"{line}\n")


def sanitized(text: str, sensitive_values: Iterable[str] = ()) -> str:
    result = text
    for value in sensitive_values:
        if value:
            result = result.replace(value, "***")
    result = re.sub(r"github_pat_[A-Za-z0-9_]+", "***", result)
    result = re.sub(r"gh[opsu]_[A-Za-z0-9]+", "***", result)
    result = re.sub(r"(--jitconfig(?:=|\s+))\S+", r"\1***", result)
    result = re.sub(r'("encoded_jit_config"\s*:\s*")[^"]+(\")', r"\1***\2", result)
    result = "\n".join(
        "[redacted jit configuration]" if "jitconfig" in line.lower().replace("_", "") else line
        for line in result.splitlines()
    )
    return result


def run_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            env=dict(environment) if environment is not None else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LifecycleError(f"Could not run {command[0]}: {error}") from error

    if check and result.returncode != 0:
        details = sanitized(result.stderr.strip() or result.stdout.strip())
        raise LifecycleError(
            f"Command {shlex.join(command[:4])} failed with {result.returncode}: {details}"
        )
    return result


def openstack(
    config: SelectelConfig,
    arguments: Sequence[str],
    *,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["openstack", *arguments],
        environment=config.openstack_environment(),
        check=check,
        timeout=timeout,
    )


def parse_json_output(result: subprocess.CompletedProcess[str], operation: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LifecycleError(f"{operation} returned invalid JSON") from error


def object_value(value: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): item for key, item in value.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    raise LifecycleError(f"Response does not contain any of: {', '.join(names)}")


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Mapping[str, Any] | None = None,
    expected_statuses: Sequence[int] = (200,),
    github_api: bool = False,
    timeout: int = 60,
) -> tuple[int, Any, Mapping[str, str]]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        if github_api:
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["X-Auth-Token"] = token
    if github_api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION

    retry_delays = (1, 3) if method in {"DELETE", "GET", "HEAD"} else ()
    for attempt in range(len(retry_delays) + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
                response_headers = dict(response.headers.items())
            break
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
            response_headers = dict(error.headers.items()) if error.headers else {}
            if status not in expected_statuses:
                details = sanitized(raw.decode("utf-8", errors="replace"))[:2000]
                raise LifecycleError(f"{method} {url} returned HTTP {status}: {details}") from error
            break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            reason = getattr(error, "reason", None) or str(error) or type(error).__name__
            raise LifecycleError(f"{method} {url} failed: {reason}") from error

    if status not in expected_statuses:
        details = sanitized(raw.decode("utf-8", errors="replace"))[:2000]
        raise LifecycleError(f"{method} {url} returned HTTP {status}: {details}")
    if not raw:
        payload: Any = None
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LifecycleError(f"{method} {url} returned invalid JSON") from error
    return status, payload, response_headers


def github_url(path: str) -> str:
    return f"{require_environment('GITHUB_API_URL').rstrip('/')}/{path.lstrip('/')}"


def selectel_token(config: SelectelConfig) -> str:
    endpoint = f"{config.auth_url.rstrip('/')}/auth/tokens"
    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": config.username,
                        "domain": {"name": config.user_domain_name},
                        "password": config.password,
                    }
                },
            },
            "scope": {"project": {"id": config.project_id}},
        }
    }
    _, _, headers = http_json("POST", endpoint, body=body, expected_statuses=(201,), timeout=60)
    token = next(
        (value for key, value in headers.items() if key.lower() == "x-subject-token"),
        "",
    )
    if not token:
        raise LifecycleError("Selectel identity response did not contain X-Subject-Token")
    add_mask(token)
    return token


def public_network_url(config: SelectelConfig, path: str) -> str:
    return f"{config.public_network_api_url.rstrip('/')}/{path.lstrip('/')}"


def preflight(config: SelectelConfig) -> None:
    token_result = openstack(config, ["token", "issue", "-f", "json"])
    parse_json_output(token_result, "OpenStack authentication")

    image = parse_json_output(
        openstack(config, ["image", "show", config.image_id, "-f", "json"]),
        "image lookup",
    )
    flavor = parse_json_output(
        openstack(config, ["flavor", "show", config.flavor_id, "-f", "json"]),
        "flavor lookup",
    )
    zones = parse_json_output(
        openstack(config, ["availability", "zone", "list", "-f", "json"]),
        "availability-zone lookup",
    )
    if config.availability_zone not in json.dumps(zones):
        raise LifecycleError(
            f"Availability zone {config.availability_zone!r} is not visible to OpenStack"
        )

    token = selectel_token(config)
    compact_project_id = config.project_id.replace("-", "")
    _, quota, _ = http_json(
        "GET",
        public_network_url(config, f"v1/projects/{compact_project_id}/quotas"),
        token=token,
    )
    entries = quota.get("network_direct_public_ips", []) if isinstance(quota, dict) else []
    has_capacity = any(
        int(entry.get("value", 0)) < 0 or int(entry.get("used", 0)) < int(entry.get("value", 0))
        for entry in entries
        if isinstance(entry, dict)
    )
    if not has_capacity:
        raise LifecycleError("Selectel direct-public-IP quota has no free capacity")

    image_name = object_value(image, "name")
    flavor_name = object_value(flavor, "name")
    print(f"Preflight: image={image_name}, flavor={flavor_name}, zone={config.availability_zone}")
    append_summary(
        f"- Preflight: image `{image_name}`, flavor `{flavor_name}`, "
        f"zone `{config.availability_zone}`"
    )


def create_security_group(config: SelectelConfig, identity: ResourceIdentity) -> str:
    payload = parse_json_output(
        openstack(
            config,
            [
                "security",
                "group",
                "create",
                "--description",
                identity.descriptor,
                "-f",
                "json",
                identity.security_group_name,
            ],
        ),
        "security-group creation",
    )
    group_id = str(object_value(payload, "id"))
    if not UUID_RE.fullmatch(group_id):
        raise LifecycleError("OpenStack returned an invalid security-group ID")

    rules = parse_json_output(
        openstack(
            config,
            ["security", "group", "rule", "list", group_id, "-f", "json"],
        ),
        "security-group rule lookup",
    )
    ingress_rules = [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and str(rule.get("Direction", rule.get("direction", ""))).lower() == "ingress"
    ]
    if ingress_rules:
        raise LifecycleError("New security group unexpectedly contains ingress rules")
    return group_id


def create_public_port(
    config: SelectelConfig, identity: ResourceIdentity, security_group_id: str
) -> tuple[str, str]:
    token = selectel_token(config)
    _, response, _ = http_json(
        "POST",
        public_network_url(config, "v1/public_ports"),
        token=token,
        body={
            "description": identity.descriptor,
            "admin_state_up": True,
            "security_group_ids": [security_group_id],
        },
        expected_statuses=(201,),
    )
    port = response.get("port", {}) if isinstance(response, dict) else {}
    port_id = str(port.get("id", ""))
    address = str(port.get("ip_address", ""))
    if not UUID_RE.fullmatch(port_id):
        raise LifecycleError("Selectel returned an invalid direct-public-port ID")
    if not address:
        raise LifecycleError("Selectel did not return a direct public IP address")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if openstack(config, ["port", "show", port_id], check=False).returncode == 0:
            return port_id, address
        time.sleep(3)
    raise LifecycleError("Direct public port did not become visible to OpenStack")


def resolve_runner_package(app_token: str, public_token: str) -> tuple[str, str, str]:
    repository = require_environment("GITHUB_REPOSITORY")
    _, downloads, _ = http_json(
        "GET",
        github_url(f"repos/{repository}/actions/runners/downloads"),
        token=app_token,
        github_api=True,
    )
    candidates = [
        item
        for item in downloads
        if isinstance(item, dict)
        and item.get("os") == "linux"
        and item.get("architecture") == "x64"
    ]
    if len(candidates) != 1:
        raise LifecycleError("GitHub did not return exactly one linux/x64 runner package")
    candidate = candidates[0]
    filename = str(candidate.get("filename", ""))
    download_url = str(candidate.get("download_url", ""))
    version_match = re.fullmatch(r"actions-runner-linux-x64-([0-9.]+)\.tar\.gz", filename)
    parsed_download = urllib.parse.urlparse(download_url)
    if (
        not version_match
        or parsed_download.scheme != "https"
        or parsed_download.netloc != "github.com"
    ):
        raise LifecycleError("GitHub returned an unexpected runner package URL or filename")
    version = version_match.group(1)

    _, release, _ = http_json(
        "GET",
        github_url(f"repos/actions/runner/releases/tags/v{version}"),
        token=public_token,
        github_api=True,
    )
    assets = release.get("assets", []) if isinstance(release, dict) else []
    matches = [asset for asset in assets if asset.get("name") == filename]
    if len(matches) != 1:
        raise LifecycleError("Could not find the runner package in its official GitHub release")
    asset = matches[0]
    if asset.get("browser_download_url") != download_url:
        raise LifecycleError("Runner download URL differs from the official release asset URL")
    digest = str(asset.get("digest", ""))
    digest_match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
    if not digest_match:
        raise LifecycleError("Official runner release asset has no usable SHA-256 digest")
    return download_url, digest_match.group(1).lower(), version


def create_jit_runner(identity: RunnerIdentity, app_token: str) -> tuple[str, str]:
    _, response, _ = http_json(
        "POST",
        github_url(f"repos/{identity.repository}/actions/runners/generate-jitconfig"),
        token=app_token,
        github_api=True,
        body={
            "name": identity.name,
            "runner_group_id": 1,
            "labels": [
                "self-hosted",
                "linux",
                "x64",
                identity.scope_label,
                identity.label,
            ],
            "work_folder": "_work",
        },
        expected_statuses=(201,),
    )
    runner = response.get("runner", {}) if isinstance(response, dict) else {}
    runner_id = str(runner.get("id", ""))
    jit_config = str(response.get("encoded_jit_config", "")) if isinstance(response, dict) else ""
    if not runner_id.isdigit() or not jit_config:
        raise LifecycleError("GitHub returned an invalid JIT runner configuration")
    add_mask(jit_config)
    return runner_id, jit_config


def render_cloud_config(
    template: str,
    *,
    runner_download_url: str,
    runner_sha256: str,
    runner_version: str,
    runner_jit_configs: Mapping[str, str],
) -> str:
    expected_roles = {"downloader", "wot-gui-assets", "wot-src", "wotstat-assets"}
    if set(runner_jit_configs) != expected_roles:
        raise LifecycleError(
            "cloud config requires downloader, wot-src, wot-gui-assets and "
            "wotstat-assets JIT configurations"
        )
    assignments = {
        "RUNNER_DOWNLOAD_URL": runner_download_url,
        "RUNNER_SHA256": runner_sha256,
        "RUNNER_VERSION": runner_version,
        "DOWNLOADER_RUNNER_JIT_CONFIG": runner_jit_configs["downloader"],
        "WOT_GUI_ASSETS_RUNNER_JIT_CONFIG": runner_jit_configs["wot-gui-assets"],
        "WOT_SRC_RUNNER_JIT_CONFIG": runner_jit_configs["wot-src"],
        "WOTSTAT_ASSETS_RUNNER_JIT_CONFIG": runner_jit_configs["wotstat-assets"],
    }
    preamble = "\n".join(
        f"{'readonly ' if not name.endswith('_JIT_CONFIG') else ''}{name}={shlex.quote(value)}"
        for name, value in assignments.items()
    )
    bootstrap = f"#!/usr/bin/env bash\n{preamble}\n{template}"
    encoded = base64.b64encode(bootstrap.encode("utf-8")).decode("ascii")
    return f"""#cloud-config
output:
  all: "| tee -a /var/log/cloud-init-output.log /dev/console"
write_files:
  - path: /usr/local/sbin/bootstrap-actions-runner
    owner: root:root
    permissions: '0700'
    encoding: b64
    content: {encoded}
runcmd:
  - [bash, /usr/local/sbin/bootstrap-actions-runner]
"""


def create_server(
    config: SelectelConfig,
    identity: ResourceIdentity,
    port_id: str,
    cloud_config_path: str,
) -> str:
    payload = parse_json_output(
        openstack(
            config,
            [
                "server",
                "create",
                "--image",
                config.image_id,
                "--flavor",
                config.flavor_id,
                "--availability-zone",
                config.availability_zone,
                "--nic",
                f"port-id={port_id}",
                "--user-data",
                cloud_config_path,
                "--property",
                f"gup_repository={identity.repository}",
                "--property",
                f"gup_run_id={identity.run_id}",
                "--property",
                f"gup_run_attempt={identity.run_attempt}",
                "--property",
                f"gup_instance_key={identity.instance_key}",
                "-f",
                "json",
                identity.server_name,
            ],
            timeout=300,
        ),
        "server creation",
    )
    server_id = str(object_value(payload, "id"))
    if not UUID_RE.fullmatch(server_id):
        raise LifecycleError("OpenStack returned an invalid server ID")
    return server_id


def wait_for_server_active(config: SelectelConfig, server_id: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = openstack(config, ["server", "show", server_id, "-f", "json"], check=False)
        if result.returncode == 0:
            server = parse_json_output(result, "server status lookup")
            status = str(object_value(server, "status")).upper()
            if status == "ACTIVE":
                print("VM ACTIVE")
                append_summary("- VM status: `ACTIVE`")
                return
            if status == "ERROR":
                raise LifecycleError("Selectel server entered ERROR state")
        time.sleep(5)
    raise LifecycleError("Timed out waiting for Selectel server to become ACTIVE")


def wait_for_runner_online(
    identity: RunnerIdentity, runner_id: str, app_token: str, timeout_seconds: int
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, runner, _ = http_json(
            "GET",
            github_url(f"repos/{identity.repository}/actions/runners/{runner_id}"),
            token=app_token,
            github_api=True,
            expected_statuses=(200, 404),
        )
        if status == 200 and isinstance(runner, dict):
            labels = {
                item.get("name") for item in runner.get("labels", []) if isinstance(item, dict)
            }
            if runner.get("status") == "online" and identity.label in labels:
                print(f"Runner {identity.role} online")
                append_summary(f"- Runner `{identity.name}`: `online`")
                return
        time.sleep(5)
    raise LifecycleError("Timed out waiting for the JIT runner to become online")


def server_has_ownership_markers(server: Mapping[str, Any], identity: ResourceIdentity) -> bool:
    try:
        if str(object_value(server, "name")) != identity.server_name:
            return False
        properties = object_value(server, "properties")
    except LifecycleError:
        return False

    expected = {
        "gup_repository": identity.repository,
        "gup_run_id": identity.run_id,
        "gup_run_attempt": identity.run_attempt,
        "gup_instance_key": identity.instance_key,
    }
    if isinstance(properties, Mapping):
        return all(str(properties.get(key, "")) == value for key, value in expected.items())

    serialized = str(properties)
    return all(
        re.search(
            rf"(?:^|[,\s]){re.escape(key)}\s*=\s*['\"]?"
            rf"{re.escape(value)}['\"]?(?:,|$)",
            serialized,
        )
        for key, value in expected.items()
    )


def find_server_ids(config: SelectelConfig, identity: ResourceIdentity) -> list[str]:
    result = openstack(
        config,
        ["server", "list", "--name", identity.server_name, "-f", "json"],
        check=False,
    )
    if result.returncode != 0:
        return []
    items = parse_json_output(result, "server lookup")
    matching_ids = {
        str(item.get("ID", item.get("Id", item.get("id", ""))))
        for item in items
        if isinstance(item, dict)
        and item.get("Name", item.get("name")) == identity.server_name
        and UUID_RE.fullmatch(str(item.get("ID", item.get("Id", item.get("id", "")))))
    }
    owned_ids: list[str] = []
    for server_id in sorted(matching_ids):
        show = openstack(config, ["server", "show", server_id, "-f", "json"], check=False)
        if show.returncode != 0:
            continue
        server = parse_json_output(show, "server ownership lookup")
        if isinstance(server, dict) and server_has_ownership_markers(server, identity):
            owned_ids.append(server_id)
    return owned_ids


def show_console_log(
    config: SelectelConfig,
    server_id: str,
    *,
    sensitive_values: Iterable[str] = (),
    lines: int = 200,
) -> None:
    result = openstack(config, ["console", "log", "show", server_id], check=False, timeout=60)
    if result.returncode != 0:
        print("Console diagnostics are unavailable")
        return
    cleaned = sanitized(result.stdout, [config.password, *sensitive_values])
    tail = cleaned.splitlines()[-lines:]
    print("----- sanitized Selectel serial console tail -----")
    print("\n".join(tail))
    print("----- end serial console tail -----")


def delete_github_runners(
    identity: RunnerIdentity,
    app_token: str,
    runner_id: str = "",
    *,
    deleted_resources: list[str] | None = None,
) -> None:
    candidate_ids: set[str] = set()
    if runner_id.isdigit():
        status, explicit_runner, _ = http_json(
            "GET",
            github_url(f"repos/{identity.repository}/actions/runners/{runner_id}"),
            token=app_token,
            github_api=True,
            expected_statuses=(200, 404),
        )
        if status == 200 and isinstance(explicit_runner, dict):
            explicit_labels = {
                item.get("name")
                for item in explicit_runner.get("labels", [])
                if isinstance(item, dict)
            }
            if (
                explicit_runner.get("name") != identity.name
                or identity.scope_label not in explicit_labels
            ):
                raise LifecycleError(
                    f"Refusing to delete runner {runner_id}: ownership markers do not match"
                )
            candidate_ids.add(runner_id)
    _, response, _ = http_json(
        "GET",
        github_url(f"repos/{identity.repository}/actions/runners?per_page=100"),
        token=app_token,
        github_api=True,
    )
    for runner in response.get("runners", []) if isinstance(response, dict) else []:
        if not isinstance(runner, dict):
            continue
        labels = {item.get("name") for item in runner.get("labels", []) if isinstance(item, dict)}
        if runner.get("name") == identity.name and identity.scope_label in labels:
            candidate_ids.add(str(runner.get("id", "")))

    for candidate in sorted(value for value in candidate_ids if value.isdigit()):
        status, _, _ = http_json(
            "DELETE",
            github_url(f"repos/{identity.repository}/actions/runners/{candidate}"),
            token=app_token,
            github_api=True,
            expected_statuses=(204, 404),
        )
        print(f"Runner {candidate}: {'already absent' if status == 404 else 'deleted'}")
        if status == 204 and deleted_resources is not None:
            deleted_resources.append(f"github-runner:{identity.repository}:{candidate}")


def delete_servers(
    config: SelectelConfig,
    identity: ResourceIdentity,
    server_id: str = "",
    *,
    deleted_resources: list[str] | None = None,
) -> None:
    candidates = set(find_server_ids(config, identity))
    if UUID_RE.fullmatch(server_id):
        candidates.add(server_id)
    for candidate in sorted(candidates):
        show = openstack(config, ["server", "show", candidate, "-f", "json"], check=False)
        if show.returncode != 0:
            print(f"Server {candidate}: already absent")
            continue
        server = parse_json_output(show, "server ownership lookup")
        if not isinstance(server, dict) or not server_has_ownership_markers(server, identity):
            raise LifecycleError(
                f"Refusing to delete server {candidate}: ownership markers do not match"
            )
        openstack(config, ["server", "delete", candidate], timeout=120)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if openstack(config, ["server", "show", candidate], check=False).returncode != 0:
                print(f"Server {candidate}: deleted")
                if deleted_resources is not None:
                    deleted_resources.append(f"selectel-server:{candidate}")
                break
            time.sleep(5)
        else:
            raise LifecycleError(f"Server {candidate} still exists after deletion timeout")


def find_public_port_ids(
    config: SelectelConfig, identity: ResourceIdentity, token: str
) -> list[str]:
    query = urllib.parse.urlencode({"description": identity.descriptor})
    _, response, _ = http_json(
        "GET",
        public_network_url(config, f"v1/public_ports?{query}"),
        token=token,
    )
    ports = response.get("ports", []) if isinstance(response, dict) else []
    return sorted(
        {
            str(port.get("id"))
            for port in ports
            if isinstance(port, dict)
            and port.get("description") == identity.descriptor
            and normalized_uuid(str(port.get("project_id", "")))
            == normalized_uuid(config.project_id)
            and UUID_RE.fullmatch(str(port.get("id", "")))
        }
    )


def delete_public_ports(
    config: SelectelConfig,
    identity: ResourceIdentity,
    token: str,
    port_id: str = "",
    *,
    deleted_resources: list[str] | None = None,
) -> None:
    candidates = set(find_public_port_ids(config, identity, token))
    if UUID_RE.fullmatch(port_id):
        status, response, _ = http_json(
            "GET",
            public_network_url(config, f"v1/public_ports/{port_id}"),
            token=token,
            expected_statuses=(200, 404),
        )
        if status == 200:
            port = response.get("port", {}) if isinstance(response, dict) else {}
            if port.get("description") != identity.descriptor or normalized_uuid(
                str(port.get("project_id", ""))
            ) != normalized_uuid(config.project_id):
                raise LifecycleError(
                    f"Refusing to delete direct public port {port_id}: "
                    "ownership markers do not match"
                )
            candidates.add(port_id)
    for candidate in sorted(candidates):
        deadline = time.monotonic() + 180
        while True:
            status, _, _ = http_json(
                "DELETE",
                public_network_url(config, f"v1/public_ports/{candidate}"),
                token=token,
                expected_statuses=(204, 404, 409),
            )
            if status in (204, 404):
                print(
                    f"Direct public port {candidate}: "
                    f"{'already absent' if status == 404 else 'deleted'}"
                )
                if status == 204 and deleted_resources is not None:
                    deleted_resources.append(f"selectel-public-port:{candidate}")
                break
            if time.monotonic() >= deadline:
                raise LifecycleError(f"Direct public port {candidate} is still attached")
            time.sleep(5)


def find_security_group_ids(config: SelectelConfig, identity: ResourceIdentity) -> list[str]:
    result = openstack(
        config,
        [
            "security",
            "group",
            "list",
            "--name",
            identity.security_group_name,
            "-f",
            "json",
        ],
        check=False,
    )
    if result.returncode != 0:
        return []
    groups = parse_json_output(result, "security-group lookup")
    candidates: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = group.get("Name", group.get("name"))
        description = group.get("Description", group.get("description"))
        group_id = str(group.get("ID", group.get("Id", group.get("id", ""))))
        if (
            name == identity.security_group_name
            and description == identity.descriptor
            and UUID_RE.fullmatch(group_id)
        ):
            candidates.append(group_id)
    return sorted(set(candidates))


def delete_security_groups(
    config: SelectelConfig,
    identity: ResourceIdentity,
    security_group_id: str = "",
    *,
    deleted_resources: list[str] | None = None,
) -> None:
    candidates = set(find_security_group_ids(config, identity))
    if UUID_RE.fullmatch(security_group_id):
        show = openstack(
            config,
            ["security", "group", "show", security_group_id, "-f", "json"],
            check=False,
        )
        if show.returncode == 0:
            group = parse_json_output(show, "security-group ownership lookup")
            if (
                str(object_value(group, "name")) != identity.security_group_name
                or str(object_value(group, "description")) != identity.descriptor
            ):
                raise LifecycleError(
                    f"Refusing to delete security group {security_group_id}: "
                    "ownership markers do not match"
                )
            candidates.add(security_group_id)
    for candidate in sorted(candidates):
        deadline = time.monotonic() + 180
        while True:
            result = openstack(config, ["security", "group", "delete", candidate], check=False)
            if result.returncode == 0:
                print(f"Security group {candidate}: deleted")
                if deleted_resources is not None:
                    deleted_resources.append(f"selectel-security-group:{candidate}")
                break
            show = openstack(config, ["security", "group", "show", candidate], check=False)
            if show.returncode != 0:
                print(f"Security group {candidate}: already absent")
                break
            if time.monotonic() >= deadline:
                details = sanitized(result.stderr.strip())
                raise LifecycleError(f"Security group {candidate} could not be deleted: {details}")
            time.sleep(5)


def build_identity(
    instance_key: str, *, run_id: str | None = None, run_attempt: str | None = None
) -> ResourceIdentity:
    return ResourceIdentity(
        repository=require_environment("GITHUB_REPOSITORY"),
        run_id=run_id or require_environment("GITHUB_RUN_ID"),
        run_attempt=run_attempt or require_environment("GITHUB_RUN_ATTEMPT"),
        instance_key=instance_key,
    )


def provision(arguments: argparse.Namespace) -> None:
    config = SelectelConfig.from_environment(require_compute=True)
    identity = build_identity(arguments.instance_key)
    app_token = require_environment("GITHUB_APP_TOKEN")
    public_token = require_environment("GITHUB_PUBLIC_TOKEN")
    downloader_runner = identity.runner("downloader")
    wot_gui_assets_runner = identity.runner("wot-gui-assets")
    wot_src_runner = identity.runner("wot-src")
    wotstat_assets_runner = identity.runner("wotstat-assets")
    add_mask(config.password)

    for name, value in {
        "resource_key": identity.instance_key,
        "downloader_runner_name": downloader_runner.name,
        "downloader_runner_label": downloader_runner.label,
        "wot_gui_assets_runner_name": wot_gui_assets_runner.name,
        "wot_gui_assets_runner_label": wot_gui_assets_runner.label,
        "wot_src_runner_name": wot_src_runner.name,
        "wot_src_runner_label": wot_src_runner.label,
        "wotstat_assets_runner_name": wotstat_assets_runner.name,
        "wotstat_assets_runner_label": wotstat_assets_runner.label,
        "runner_scope_label": identity.scope_label,
        "server_name": identity.server_name,
        "security_group_name": identity.security_group_name,
        "resource_descriptor": identity.descriptor,
    }.items():
        write_output(name, value)

    append_summary("## Ephemeral Selectel runner")
    append_summary(f"- Resource key: `{identity.instance_key}`")
    preflight(config)

    server_id = ""
    jit_configs: dict[str, str] = {}
    cloud_config_path = ""
    try:
        security_group_id = create_security_group(config, identity)
        write_output("security_group_id", security_group_id)
        print(f"Security group created: {security_group_id}")

        port_id, public_address = create_public_port(config, identity, security_group_id)
        write_output("port_id", port_id)
        write_output("public_address", public_address)
        print(f"Direct public port created: {port_id}")

        runner_url, runner_sha256, runner_version = resolve_runner_package(app_token, public_token)
        write_output("runner_version", runner_version)
        print(f"Runner package resolved: {runner_version}")

        downloader_runner_id, downloader_jit_config = create_jit_runner(
            downloader_runner, app_token
        )
        jit_configs[downloader_runner.role] = downloader_jit_config
        write_output("downloader_runner_id", downloader_runner_id)
        print(f"Downloader JIT runner reserved: {downloader_runner_id}")

        wot_gui_assets_runner_id, wot_gui_assets_jit_config = create_jit_runner(
            wot_gui_assets_runner, app_token
        )
        jit_configs[wot_gui_assets_runner.role] = wot_gui_assets_jit_config
        write_output("wot_gui_assets_runner_id", wot_gui_assets_runner_id)
        print(f"wot-gui-assets JIT runner reserved: {wot_gui_assets_runner_id}")

        wot_src_runner_id, wot_src_jit_config = create_jit_runner(wot_src_runner, app_token)
        jit_configs[wot_src_runner.role] = wot_src_jit_config
        write_output("wot_src_runner_id", wot_src_runner_id)
        print(f"wot-src JIT runner reserved: {wot_src_runner_id}")

        wotstat_assets_runner_id, wotstat_assets_jit_config = create_jit_runner(
            wotstat_assets_runner, app_token
        )
        jit_configs[wotstat_assets_runner.role] = wotstat_assets_jit_config
        write_output("wotstat_assets_runner_id", wotstat_assets_runner_id)
        print(f"wotstat-assets JIT runner reserved: {wotstat_assets_runner_id}")

        template_path = Path(__file__).with_name("bootstrap-actions-runner.sh")
        template = template_path.read_text(encoding="utf-8")
        cloud_config = render_cloud_config(
            template,
            runner_download_url=runner_url,
            runner_sha256=runner_sha256,
            runner_version=runner_version,
            runner_jit_configs=jit_configs,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="gup-cloud-init-",
            suffix=".yaml",
            delete=False,
        ) as temporary:
            temporary.write(cloud_config)
            cloud_config_path = temporary.name
        os.chmod(cloud_config_path, 0o600)

        server_id = create_server(config, identity, port_id, cloud_config_path)
        write_output("server_id", server_id)
        print(f"Server created: {server_id}")
        wait_for_server_active(config, server_id, timeout_seconds=300)
        wait_for_runner_online(
            downloader_runner,
            downloader_runner_id,
            app_token,
            timeout_seconds=int(os.environ.get("RUNNER_ONLINE_TIMEOUT_SECONDS", "600")),
        )
        wait_for_runner_online(
            wot_gui_assets_runner,
            wot_gui_assets_runner_id,
            app_token,
            timeout_seconds=int(os.environ.get("RUNNER_ONLINE_TIMEOUT_SECONDS", "600")),
        )
        wait_for_runner_online(
            wot_src_runner,
            wot_src_runner_id,
            app_token,
            timeout_seconds=int(os.environ.get("RUNNER_ONLINE_TIMEOUT_SECONDS", "600")),
        )
        wait_for_runner_online(
            wotstat_assets_runner,
            wotstat_assets_runner_id,
            app_token,
            timeout_seconds=int(os.environ.get("RUNNER_ONLINE_TIMEOUT_SECONDS", "600")),
        )
        append_summary(f"- Runner version: `{runner_version}`")
        append_summary(f"- Selectel server: `{server_id}`")
    except Exception:
        if server_id:
            show_console_log(
                config,
                server_id,
                sensitive_values=(*jit_configs.values(), app_token, public_token),
            )
        raise
    finally:
        if cloud_config_path:
            Path(cloud_config_path).unlink(missing_ok=True)


def cleanup(arguments: argparse.Namespace) -> None:
    config = SelectelConfig.from_environment(require_compute=False)
    identity = build_identity(
        arguments.instance_key,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
    )
    app_token = require_environment("GITHUB_APP_TOKEN")
    downloader_runner = identity.runner("downloader")
    wot_gui_assets_runner = identity.runner("wot-gui-assets")
    wot_src_runner = identity.runner("wot-src")
    wotstat_assets_runner = identity.runner("wotstat-assets")
    add_mask(config.password)

    if arguments.diagnostics:
        candidates = []
        if UUID_RE.fullmatch(arguments.server_id or ""):
            candidates.append(arguments.server_id)
        candidates.extend(find_server_ids(config, identity))
        for candidate in sorted(set(candidates)):
            show_console_log(config, candidate, sensitive_values=(app_token,))

    failures: list[str] = []
    deleted_resources: list[str] = []
    operations: list[tuple[str, Callable[[], None]]] = [
        (
            "GitHub downloader runner registration",
            lambda: delete_github_runners(
                downloader_runner,
                app_token,
                arguments.downloader_runner_id,
                deleted_resources=deleted_resources,
            ),
        ),
        (
            "GitHub wot-src runner registration",
            lambda: delete_github_runners(
                wot_src_runner,
                app_token,
                arguments.wot_src_runner_id,
                deleted_resources=deleted_resources,
            ),
        ),
        (
            "GitHub wot-gui-assets runner registration",
            lambda: delete_github_runners(
                wot_gui_assets_runner,
                app_token,
                arguments.wot_gui_assets_runner_id,
                deleted_resources=deleted_resources,
            ),
        ),
        (
            "GitHub wotstat-assets runner registration",
            lambda: delete_github_runners(
                wotstat_assets_runner,
                app_token,
                arguments.wotstat_assets_runner_id,
                deleted_resources=deleted_resources,
            ),
        ),
        (
            "Selectel server",
            lambda: delete_servers(
                config,
                identity,
                arguments.server_id,
                deleted_resources=deleted_resources,
            ),
        ),
    ]
    for label, operation in operations:
        try:
            operation()
        except Exception as error:  # continue cleanup after every resource failure
            failures.append(f"{label}: {error}")
            print(f"::error::{label} cleanup failed: {sanitized(str(error))}")

    try:
        token = selectel_token(config)
        delete_public_ports(
            config,
            identity,
            token,
            arguments.port_id,
            deleted_resources=deleted_resources,
        )
    except Exception as error:
        failures.append(f"Direct public port: {error}")
        print(f"::error::Direct public port cleanup failed: {sanitized(str(error))}")

    try:
        delete_security_groups(
            config,
            identity,
            arguments.security_group_id,
            deleted_resources=deleted_resources,
        )
    except Exception as error:
        failures.append(f"Security group: {error}")
        print(f"::error::Security-group cleanup failed: {sanitized(str(error))}")

    write_output("deleted_count", len(deleted_resources))
    append_summary(f"- Resources deleted: `{len(deleted_resources)}`")
    if failures:
        raise LifecycleError("Cleanup incomplete: " + " | ".join(failures))
    append_summary(f"- Cleanup `{identity.instance_key}`: complete")
    print("Cleanup complete")


def job_targets_runner(job: dict[str, object], runner_label: str) -> bool:
    labels = job.get("labels")
    return isinstance(labels, list) and runner_label in {
        str(label) for label in labels if isinstance(label, str)
    }


def watch_queue(arguments: argparse.Namespace) -> None:
    token = require_environment("GITHUB_WATCH_TOKEN")
    repository = require_environment("GITHUB_REPOSITORY")
    run_id = require_environment("GITHUB_RUN_ID")
    deadline = time.monotonic() + arguments.timeout_seconds
    last_status = "not visible"

    while time.monotonic() < deadline:
        try:
            _, response, _ = http_json(
                "GET",
                github_url(
                    f"repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100"
                ),
                token=token,
                github_api=True,
            )
            matches = [
                job
                for job in response.get("jobs", [])
                if isinstance(job, dict) and job_targets_runner(job, arguments.runner_label)
            ]
            if matches:
                last_status = str(matches[0].get("status", "unknown"))
                if last_status in ("in_progress", "completed"):
                    print(f"Download was assigned to a runner (status={last_status})")
                    write_output("timed_out", "false")
                    return
        except LifecycleError as error:
            last_status = f"API error: {error}"
        time.sleep(10)

    print(
        f"::error::Download {arguments.instance_key} queue deadline exceeded; "
        f"last status: {sanitized(last_status)}"
    )
    write_output("timed_out", "true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision_parser = subparsers.add_parser("provision")
    provision_parser.add_argument("--instance-key", required=True)
    provision_parser.set_defaults(handler=provision)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--instance-key", required=True)
    cleanup_parser.add_argument("--run-id")
    cleanup_parser.add_argument("--run-attempt")
    cleanup_parser.add_argument("--downloader-runner-id", default="")
    cleanup_parser.add_argument("--wot-gui-assets-runner-id", default="")
    cleanup_parser.add_argument("--wot-src-runner-id", default="")
    cleanup_parser.add_argument("--wotstat-assets-runner-id", default="")
    cleanup_parser.add_argument("--server-id", default="")
    cleanup_parser.add_argument("--port-id", default="")
    cleanup_parser.add_argument("--security-group-id", default="")
    cleanup_parser.add_argument("--diagnostics", action="store_true")
    cleanup_parser.set_defaults(handler=cleanup)

    watch_parser = subparsers.add_parser("watch-queue")
    watch_parser.add_argument("--instance-key", required=True)
    watch_parser.add_argument("--runner-label", required=True)
    watch_parser.add_argument("--timeout-seconds", type=int, default=600)
    watch_parser.set_defaults(handler=watch_queue)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        arguments.handler(arguments)
        return 0
    except LifecycleError as error:
        print(f"::error::{sanitized(str(error))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("::error::Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

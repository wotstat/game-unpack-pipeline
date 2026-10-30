#!/usr/bin/env python3
"""Provision and reconcile one hourly Aeza VM and four isolated JIT runners."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GITHUB_API_VERSION = "2026-03-10"
PIPELINE_MARKER = "game-unpack-pipeline"
AEZA_API_URL = "https://my.aeza.net/api/v2"
DEFAULT_AEZA_SERVER = "MSKs-5"
AEZA_RETRY_DELAYS = (5, 15, 30)
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
SSH_READINESS_TIMEOUT_SECONDS = 8 * 60
INSTANCE_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
ROLES = ("downloader", "wot-src", "wot-gui-assets", "wotstat-assets")


@dataclass(frozen=True)
class AezaProduct:
    name: str
    product_id: int
    group_id: int
    location: str
    mode: str
    max_hourly_cents: int


# Public catalogue checked on 2026-10-29. Keep allocation restricted to these
# 16 vCPU / 32 GiB / 480 GB profiles; cleanup recognizes every approved product.
AEZA_PRODUCTS = {
    product.name: product
    for product in (
        AezaProduct("MSKs-5", 157, 2, "Moscow", "Shared", 53),
        AezaProduct("MSK-5", 152, 1, "Moscow", "Dedicated", 76),
        AezaProduct("WAWs-5", 445, 77, "Warsaw", "Shared", 53),
        AezaProduct("CLTs-5", 341, 46, "Charlotte", "Shared", 53),
    )
}


def selected_product() -> AezaProduct:
    name = os.environ.get("AEZA_SERVER", DEFAULT_AEZA_SERVER)
    try:
        return AEZA_PRODUCTS[name]
    except KeyError:
        raise LifecycleError("Unsupported Aeza server profile") from None


@dataclass(frozen=True)
class AezaConfig:
    api_key: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> AezaConfig:
        return cls(require_environment("AEZA_API_KEY"))

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        expected_statuses: Sequence[int] = (200,),
    ) -> tuple[int, Any, Mapping[str, str]]:
        return http_json(
            method,
            f"{AEZA_API_URL}/{path.lstrip('/')}",
            token=self.api_key,
            body=body,
            expected_statuses=expected_statuses,
        )


def aeza_items(config: AezaConfig, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        separator = "&" if "?" in path else "?"
        _, payload, _ = config.request("GET", f"{path}{separator}limit=100&offset={offset}")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise LifecycleError("Aeza returned an invalid list")
        page = payload["items"]
        if not all(isinstance(item, dict) for item in page):
            raise LifecycleError("Aeza returned an invalid list item")
        items.extend(page)
        offset += len(page)
        if offset >= int(payload.get("total", offset)):
            return items
        if not page:
            raise LifecycleError("Aeza pagination stopped before total")


def preflight(config: AezaConfig, profile: AezaProduct) -> None:
    products = aeza_items(config, "services/products")
    matches = [p for p in products if p.get("id") == profile.product_id]
    if len(matches) != 1 or matches[0].get("name") != profile.name:
        raise LifecycleError("Aeza product identity changed")
    product = matches[0]
    if product.get("groupId") != profile.group_id:
        raise LifecycleError("Aeza product group changed")
    if product.get("isAvailable") is not True:
        raise LifecycleError(f"Aeza {profile.name} is unavailable")
    parameters = product.get("parameters")
    if not isinstance(parameters, dict) or any(
        parameters.get(key) != value for key, value in {"cpu": 16, "ram": 32, "rom": 480}.items()
    ):
        raise LifecycleError("Aeza product capacity changed")
    _, price, _ = config.request(
        "GET", f"services/orders/price?productId={profile.product_id}&paymentTerm=hour&termCount=1"
    )
    # Prices are euro cents. Refuse unexpected upgrades or a price increase.
    total_price = price.get("totalPrice") if isinstance(price, dict) else None
    if (
        isinstance(total_price, bool)
        or not isinstance(total_price, (int, float))
        or not 0 < total_price <= profile.max_hourly_cents
    ):
        raise LifecycleError(f"Aeza hourly quote exceeds EUR {profile.max_hourly_cents / 100:.2f}")
    if price.get("availability", {}).get("status") != "ok":
        raise LifecycleError("Aeza hourly VM capacity is unavailable")
    _, account, _ = config.request("GET", "accounts/me")
    if not isinstance(account, dict) or account.get("balance", 0) < 4 * total_price:
        raise LifecycleError("Aeza balance cannot cover the four-hour VM lifetime")
    _, limits, _ = config.request("GET", "services/limits")
    for limit in limits.get("items", []):
        groups = limit.get("groupIds", [])
        terms = limit.get("paymentTerms", [])
        if (
            (not groups or profile.group_id in groups)
            and (not terms or "hour" in terms)
            and limit.get("used", 0) >= limit.get("available", 0)
        ):
            raise LifecycleError("Aeza account limit has no free capacity for the selected profile")
    print(
        f"Preflight: {profile.location} {profile.mode} {profile.name}, "
        f"16 vCPU / 32 GiB / 480 GB, EUR {total_price / 100:.2f}/hour"
    )


def server_has_ownership_markers(
    server: Mapping[str, Any], identity: ResourceIdentity, *, product: AezaProduct | None = None
) -> bool:
    nested_product = server.get("product")
    nested_id = nested_product.get("id") if isinstance(nested_product, dict) else None
    product_id = server.get("productId", nested_id)
    return (
        server.get("name") == identity.server_name
        and isinstance(product_id, int)
        and product_id in {profile.product_id for profile in AEZA_PRODUCTS.values()}
        and (product is None or product_id == product.product_id)
        and server.get("paymentTerm") == "hour"
    )


def owned_servers(
    config: AezaConfig, identity: ResourceIdentity, *, product: AezaProduct | None = None
) -> list[dict[str, Any]]:
    candidates = aeza_items(config, "services")
    return [
        item
        for item in candidates
        if server_has_ownership_markers(item, identity, product=product)
        and item.get("status") != "deleted"
    ]


def create_server(
    config: AezaConfig, identity: ResourceIdentity, ssh_key_id: str, product: AezaProduct
) -> str:
    if any(item.get("name") == identity.server_name for item in aeza_items(config, "services")):
        raise LifecycleError("An Aeza service with this attempt name already exists")
    # Never replay an order, including after 502 or a lost response. Cleanup
    # discovers the resulting VM by the repository hash and exact run/attempt.
    config.request(
        "POST",
        "services/orders",
        body={
            "method": "balance",
            "orders": [
                {
                    "productId": product.product_id,
                    "count": 1,
                    "name": identity.server_name,
                    "term": "hour",
                    "termCount": 1,
                    "autoProlong": True,
                    "parameters": {"os": "ubuntu_2404"},
                    "sshKeyIds": [int(ssh_key_id)],
                }
            ],
        },
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        matches = owned_servers(config, identity, product=product)
        if len(matches) > 1:
            raise LifecycleError("Multiple Aeza services match one attempt")
        if matches:
            return str(matches[0]["id"])
        time.sleep(5)
    raise LifecycleError("Aeza order not visible; reconcile this attempt before retrying")


def get_server(config: AezaConfig, server_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[1-9][0-9]*", server_id):
        raise LifecycleError("Invalid Aeza service ID")
    status, server, _ = config.request("GET", f"services/{server_id}", expected_statuses=(200, 404))
    if status == 404:
        return None
    if not isinstance(server, dict):
        raise LifecycleError("Aeza returned an invalid service")
    return server


def wait_for_server_active(
    config: AezaConfig, identity: ResourceIdentity, server_id: str, product: AezaProduct
) -> str:
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        server = get_server(config, server_id)
        if server is None or not server_has_ownership_markers(server, identity, product=product):
            raise LifecycleError("Aeza VM disappeared or ownership does not match")
        if server.get("status") in {"blocked", "suspended", "deleted"}:
            raise LifecycleError("Aeza VM entered an unusable state")
        if server.get("status") == "active" and server.get("ip"):
            address = ipaddress.ip_address(server["ip"])
            if not address.is_global:
                raise LifecycleError("Aeza VM address is not public")
            return str(address)
        time.sleep(5)
    raise LifecycleError("Aeza VM activation timed out")


def delete_servers(
    config: AezaConfig,
    identity: ResourceIdentity,
    server_id: str = "",
    *,
    deleted_resources: list[str],
    billing_service_ids: set[str] | None = None,
) -> None:
    candidates = {str(item["id"]) for item in owned_servers(config, identity)}
    if billing_service_ids is not None:
        billing_service_ids.update(candidates)
    if server_id:
        candidates.add(server_id)
    for candidate in sorted(candidates):
        server = get_server(config, candidate)
        if server is None:
            # An explicit ID comes from this attempt's provision output. Aeza may
            # already have purged the VM; retain its ledger identity for reporting.
            if billing_service_ids is not None:
                billing_service_ids.add(candidate)
            continue
        if server.get("status") == "deleted":
            if billing_service_ids is not None and server_has_ownership_markers(server, identity):
                billing_service_ids.add(candidate)
            continue
        if not server_has_ownership_markers(server, identity):
            raise LifecycleError("Refusing to delete Aeza VM: ownership markers do not match")
        if billing_service_ids is not None:
            billing_service_ids.add(candidate)
        # DELETE may have succeeded; confirm state instead of assuming it.
        with suppress(TransientLifecycleError):
            config.request("DELETE", f"services/{candidate}", expected_statuses=(204, 404))
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = get_server(config, candidate)
            if current is None or current.get("status") == "deleted":
                deleted_resources.append(f"aeza-service:{candidate}")
                break
            if not server_has_ownership_markers(current, identity):
                raise LifecycleError("Aeza VM ownership changed while deleting")
            time.sleep(5)
        else:
            raise LifecycleError("Aeza deletion is not confirmed; billing may still be active")


def delete_ssh_keys(config: AezaConfig, identity: ResourceIdentity, deleted: list[str]) -> None:
    for key in aeza_items(config, "services/ssh-keys"):
        if key.get("name") != identity.base_name:
            continue
        if not str(key.get("publicKey", "")).endswith(" " + identity.descriptor):
            raise LifecycleError("Refusing to delete SSH key with mismatched ownership")
        status, _, _ = config.request(
            "DELETE", f"services/ssh-keys/{int(key['id'])}", expected_statuses=(204, 404)
        )
        if status == 204:
            deleted.append(f"aeza-ssh-key:{key['id']}")


def render_bootstrap(
    identity: ResourceIdentity,
    config: AezaConfig,
    server_id: str,
    deadline: datetime,
    runner_package: tuple[str, str, str],
    jit_configs: Mapping[str, str],
    *,
    product: AezaProduct,
) -> str:
    import base64

    if set(jit_configs) != set(ROLES) or deadline.tzinfo is None:
        raise LifecycleError("Bootstrap requires four JIT configurations and an absolute deadline")
    url, digest, version = runner_package
    assignments = {
        "RUNNER_DOWNLOAD_URL": url,
        "RUNNER_SHA256": digest,
        "RUNNER_VERSION": version,
        **{
            role.replace("-", "_").upper() + "_RUNNER_JIT_CONFIG": jit_configs[role]
            for role in ROLES
        },
    }
    template = Path(__file__).with_name("bootstrap-actions-runner.sh").read_text()
    bootstrap = (
        "#!/bin/bash\n"
        + "\n".join(f"{name}={shlex.quote(value)}" for name, value in assignments.items())
        + "\n"
        + template
    )
    timer = f"""[Unit]
Description=Four-hour Aeza VM lifetime
[Timer]
OnCalendar={deadline.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")}
AccuracySec=1s
Persistent=true
Unit=gup-emergency-self-destruct.service
[Install]
WantedBy=timers.target
"""
    service = """[Unit]
Description=Delete expired Aeza VM
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/gup-emergency-self-destruct \\
  --config /etc/gup-emergency-self-destruct.json
Restart=on-failure
RestartSec=60s
TimeoutStartSec=120s
"""
    files = {
        "/usr/local/sbin/bootstrap-actions-runner": bootstrap,
        "/usr/local/sbin/gup-emergency-self-destruct": Path(__file__)
        .with_name("emergency_self_destruct.py")
        .read_text(),
        "/etc/gup-emergency-self-destruct.json": json.dumps(
            {
                "api_key": config.api_key,
                "server_id": server_id,
                "server_name": identity.server_name,
                "product_id": product.product_id,
            }
        ),
        "/etc/systemd/system/gup-emergency-self-destruct.service": service,
        "/etc/systemd/system/gup-emergency-self-destruct.timer": timer,
    }
    # SSH carries files over stdin; neither credentials nor JIT data enter argv,
    # provider user-data, artifacts, or command output.
    encoded = base64.b64encode(json.dumps(files).encode()).decode()
    return f"""#!/bin/bash
set -Eeuo pipefail
umask 077
python3 - <<'PY'
import base64,json,os
for path, content in json.loads(base64.b64decode('{encoded}')).items():
    with open(path, 'w') as stream:
        os.chmod(path, 0o600)
        stream.write(content)
PY
systemctl daemon-reload
systemctl enable --now gup-emergency-self-destruct.timer
systemd-run --unit=gup-bootstrap --property=Type=exec \\
  /bin/bash /usr/local/sbin/bootstrap-actions-runner
"""


def ssh_readiness_failure(details: str) -> str:
    """Classify local OpenSSH diagnostics without publishing any server data."""
    text = details.lower()
    if "remote host identification has changed" in text:
        raise LifecycleError("Aeza SSH host key changed")
    if "host key verification failed" in text:
        raise LifecycleError("Aeza SSH host key verification failed")
    if any(
        marker in text
        for marker in (
            "bad configuration option",
            "bad owner or permissions",
            "bad permissions",
            "load key",
            "not accessible",
            "unix domain socket too long",
            "too long for unix domain socket",
            "controlpath too long",
        )
    ):
        raise LifecycleError("Aeza SSH local client configuration or private key is invalid")
    if "permission denied" in text or "authentication failed" in text:
        return "authentication_pending"
    if "connection refused" in text:
        return "connection_refused"
    if "timed out" in text or "operation timeout" in text:
        return "connect_timeout"
    if "network is unreachable" in text or "no route to host" in text:
        return "network_unreachable"
    if "connection reset" in text or "connection closed" in text or "kex_exchange" in text:
        return "handshake_interrupted"
    if "offering public key" in text:
        return "authentication_pending"
    if "remote protocol version" in text:
        return "ssh_handshake_pending"
    if "connection established" in text:
        return "ssh_banner_pending"
    if "connecting to" in text:
        return "tcp_connection_pending"
    return "ssh_failed"


def ssh_bootstrap(
    address: str, private_key: Path, payload: str, *, sensitive_values: Iterable[str] = ()
) -> None:
    started_at = time.monotonic()
    deadline = started_at + SSH_READINESS_TIMEOUT_SECONDS
    attempts = 0
    last_reason = "not_attempted"
    next_report = started_at
    command = [
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-i",
        str(private_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={private_key.parent / 'known_hosts'}",
        "-o",
        f"ControlPath={private_key.parent / 'ssh.sock'}",
        "-o",
        "ControlPersist=60",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=10",
        f"root@{address}",
    ]
    try:
        # Keep the authenticated transport instead of opening a second TCP
        # connection after a successful readiness probe. No JIT data is sent yet.
        while time.monotonic() < deadline:
            attempts += 1
            timed_out = False
            result = None
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
                # A background master keeps its standard descriptors open. A
                # private regular file avoids communicate() waiting for EOF on
                # a pipe that belongs to the long-lived master process.
                try:
                    result = subprocess.run(
                        [*command[:-1], "-v", "-M", "-N", "-f", command[-1]],
                        stdout=subprocess.DEVNULL,
                        stderr=diagnostics,
                        text=True,
                        timeout=min(20, max(1, deadline - time.monotonic())),
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    timed_out = True
                diagnostics.seek(0)
                details = diagnostics.read(65536)
            if result is not None and result.returncode == 0:
                print(
                    f"SSH authenticated after {attempts} attempts "
                    f"({time.monotonic() - started_at:.1f}s)"
                )
                break
            reason = ssh_readiness_failure(details)
            if timed_out and reason == "ssh_failed":
                reason = "attempt_timeout"
            now = time.monotonic()
            if reason != last_reason or now >= next_report:
                print(
                    f"SSH readiness: attempt {attempts}, elapsed {now - started_at:.1f}s, "
                    f"result={reason}; retrying within the initial-boot deadline"
                )
                next_report = now + 30
            last_reason = reason
            if now < deadline:
                time.sleep(min(5, deadline - now))
        else:
            raise LifecycleError(
                f"Aeza SSH readiness timed out after {time.monotonic() - started_at:.1f}s "
                f"({attempts} attempts; last failure: {last_reason})"
            )
        # Submit exactly once; systemd owns the detached bootstrap. Completion is
        # observed through GitHub because this unit intentionally closes SSH.
        try:
            result = subprocess.run(
                [*command, "bash -s"],
                input=payload,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise BootstrapOutcomeUnknownError(
                "SSH bootstrap acknowledgement timed out; payload will not be replayed"
            ) from error
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in result.stderr:
            raise LifecycleError("Aeza SSH host key changed")
        if result.returncode:
            details = sanitized(result.stdout + result.stderr, (*sensitive_values, address))[-6000:]
            if result.returncode == 255:
                raise BootstrapOutcomeUnknownError(
                    "SSH bootstrap acknowledgement lost; payload will not be replayed: " + details
                )
            raise LifecycleError(f"Aeza bootstrap failed; reconcile before retrying: {details}")
    finally:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [*command[:-1], "-O", "exit", command[-1]],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )


def provision(arguments: argparse.Namespace) -> None:
    product = selected_product()
    config = AezaConfig.from_environment()
    identity = build_identity(arguments.instance_key)
    app_token = require_environment("GITHUB_APP_TOKEN")
    public_token = require_environment("GITHUB_PUBLIC_TOKEN")
    add_mask(config.api_key)
    write_output("resource_key", identity.instance_key)
    write_output("server_name", identity.server_name)
    write_output("runner_scope_label", identity.scope_label)
    for role in ROLES:
        runner = identity.runner(role)
        write_output(role.replace("-", "_") + "_runner_name", runner.name)
        write_output(role.replace("-", "_") + "_runner_label", runner.label)
    preflight(config, product)
    package = resolve_runner_package(app_token, public_token)
    write_output("runner_version", package[2])
    deadline = (datetime.now(UTC) + timedelta(hours=4)).replace(microsecond=0)
    write_output("emergency_self_destruct_at", deadline.isoformat())
    with tempfile.TemporaryDirectory(prefix="gup-aeza-") as temporary:
        private_key = Path(temporary) / "id_ed25519"
        run_command(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                identity.descriptor,
                "-f",
                str(private_key),
            ]
        )
        _, key, _ = config.request(
            "POST",
            "services/ssh-keys",
            body={
                "name": identity.base_name,
                "publicKey": private_key.with_suffix(".pub").read_text().strip(),
                "autoAssign": False,
            },
            expected_statuses=(200, 201),
        )
        key_id = str(key["id"])
        write_output("ssh_key_id", key_id)
        server_id = create_server(config, identity, key_id, product)
        write_output("server_id", server_id)
        address = wait_for_server_active(config, identity, server_id, product)
        configs: dict[str, str] = {}
        runners: dict[str, str] = {}
        for role in ROLES:
            runner_id, jit = create_jit_runner(identity.runner(role), app_token)
            configs[role] = jit
            runners[role] = runner_id
            write_output(role.replace("-", "_") + "_runner_id", runner_id)
        bootstrap_error: BootstrapOutcomeUnknownError | None = None
        try:
            ssh_bootstrap(
                address,
                private_key,
                render_bootstrap(
                    identity, config, server_id, deadline, package, configs, product=product
                ),
                sensitive_values=(config.api_key, *configs.values()),
            )
        except BootstrapOutcomeUnknownError as error:
            bootstrap_error = error
            print(f"::warning::{error}")
        print("Bootstrap submitted once; confirming all four JIT runners through GitHub")
        online_deadline = time.monotonic() + int(
            os.environ.get("RUNNER_ONLINE_TIMEOUT_SECONDS", "600")
        )
        try:
            for role in ROLES:
                wait_for_runner_online(
                    identity.runner(role),
                    runners[role],
                    app_token,
                    max(1, int(online_deadline - time.monotonic())),
                )
        except LifecycleError as error:
            if bootstrap_error is not None:
                raise LifecycleError(f"{bootstrap_error}; {error}") from error
            raise
    append_summary("## Ephemeral Aeza runner")
    append_summary(f"- {product.location} {product.mode} {product.name}; hourly billing")
    append_summary(f"- Emergency deletion deadline: `{deadline.isoformat()}`")


def cleanup(arguments: argparse.Namespace) -> None:
    config = AezaConfig.from_environment()
    identity = build_identity(
        arguments.instance_key, run_id=arguments.run_id, run_attempt=arguments.run_attempt
    )
    app_token = os.environ.get("GITHUB_APP_TOKEN", "").strip()
    failures: list[str] = []
    deleted: list[str] = []
    billing_service_ids: set[str] = set()
    vm_deleted = False
    # Delete billed resources first. One failed GitHub call must never prevent
    # the cloud cleanup, and a cloud failure must not skip runner cleanup.
    try:
        delete_servers(
            config,
            identity,
            arguments.server_id,
            deleted_resources=deleted,
            billing_service_ids=billing_service_ids,
        )
    except Exception as error:  # continue every cleanup despite a malformed provider response
        failures.append(str(error) if isinstance(error, LifecycleError) else type(error).__name__)
    else:
        vm_deleted = True
        try:
            delete_ssh_keys(config, identity, deleted)
        except Exception as error:
            failures.append(
                str(error) if isinstance(error, LifecycleError) else type(error).__name__
            )
    if not app_token:
        failures.append("GitHub App token unavailable; runner reconciliation is still required")
    for role in ROLES if app_token else ():
        try:
            delete_github_runners(
                identity.runner(role),
                app_token,
                getattr(arguments, role.replace("-", "_") + "_runner_id"),
                deleted_resources=deleted,
            )
        except Exception as error:
            failures.append(
                str(error) if isinstance(error, LifecycleError) else type(error).__name__
            )
    write_output("deleted_count", len(deleted))
    write_output("billing_service_ids", json.dumps(sorted(billing_service_ids)))
    write_output("vm_deleted", "true" if vm_deleted else "false")
    append_summary(f"- Resources deleted: `{len(deleted)}`")
    if failures:
        raise LifecycleError("Cleanup incomplete: " + " | ".join(failures))
    print("Cleanup confirmed: Aeza service deleted and runner registrations removed")


class LifecycleError(RuntimeError):
    """An expected, user-facing lifecycle failure."""


class BootstrapOutcomeUnknownError(LifecycleError):
    """The one-time SSH payload may have been accepted; observe JIT readiness only."""


class TransientLifecycleError(LifecycleError):
    """An API failure with an unknown result; replay only safe operations."""


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
        repository_hash = hashlib.sha256(self.repository.encode()).hexdigest()[:10]
        return f"gup-{repository_hash}-{self.instance_key}"

    @property
    def server_name(self) -> str:
        return self.base_name

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


def validate_instance_key(value: str) -> str:
    if not INSTANCE_KEY_RE.fullmatch(value):
        raise LifecycleError(
            "instance-key must be 1-48 lowercase letters, digits, or hyphens; "
            "it must start and end with a letter or digit"
        )
    return value


def require_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LifecycleError(f"{name} is required")
    return value


def add_mask(value: str) -> None:
    if value and os.environ.get("GITHUB_ACTIONS") == "true":
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
    for value in (
        *sensitive_values,
        *(
            os.environ.get(name, "")
            for name in (
                "AEZA_API_KEY",
                "GITHUB_APP_TOKEN",
                "GITHUB_PUBLIC_TOKEN",
                "GITHUB_WATCH_TOKEN",
            )
        ),
    ):
        if value:
            result = result.replace(value, "***")
    result = re.sub(r"github_pat_[A-Za-z0-9_]+", "***", result)
    result = re.sub(r"gh[opsu]_[A-Za-z0-9]+", "***", result)
    result = re.sub(r"(--jitconfig(?:=|\s+))\S+", r"\1***", result)
    result = re.sub(r'("encoded_jit_config"\s*:\s*")[^"]+(\")', r"\1***\2", result)
    # A remote Python traceback can echo the base64 file bundle, including
    # partial bundles after a broken SSH stream. Raw-secret masking cannot
    # recognize credentials inside those encoded files.
    result = re.sub(r"[A-Za-z0-9+/_=-]{80,}", "[redacted encoded value]", result)
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
    sensitive_values: Iterable[str] = (),
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
        details = sanitized(result.stderr.strip() or result.stdout.strip(), sensitive_values)
        raise LifecycleError(
            f"Command {shlex.join(command[:4])} failed with {result.returncode}: {details}"
        )
    return result


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Mapping[str, Any] | None = None,
    expected_statuses: Sequence[int] = (200,),
    github_api: bool = False,
    timeout: int = 60,
    retry_safe: bool = False,
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
            headers["X-API-KEY"] = token
    if github_api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION

    retry_delays = (
        ((1, 3) if github_api else AEZA_RETRY_DELAYS)
        if method in {"DELETE", "GET", "HEAD"} or retry_safe
        else ()
    )
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
                transient = status in TRANSIENT_HTTP_STATUSES
                if transient and not github_api and attempt < len(retry_delays):
                    delay = retry_delays[attempt]
                    print(
                        f"::warning::Temporary Aeza HTTP {status}; "
                        f"retry {attempt + 1}/3 in {delay}s"
                    )
                    time.sleep(delay)
                    continue
                details = "request rejected"
                error_type = TransientLifecycleError if transient else LifecycleError
                raise error_type(f"{method} {url} returned HTTP {status}: {details}") from error
            break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
                raise LifecycleError(f"{method} {url} failed certificate verification") from error
            if attempt < len(retry_delays):
                print(
                    f"::warning::Temporary HTTP connection failure; "
                    f"retry {attempt + 1} in {retry_delays[attempt]}s"
                )
                time.sleep(retry_delays[attempt])
                continue
            reason = type(error).__name__
            raise TransientLifecycleError(f"{method} {url} failed: {reason}") from error

    if status not in expected_statuses:
        details = "request rejected"
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


def wait_for_runner_online(
    identity: RunnerIdentity,
    runner_id: str,
    app_token: str,
    timeout_seconds: int,
    *,
    server_check: Callable[[], None] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if server_check is not None:
            server_check()
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


def build_identity(
    instance_key: str, *, run_id: str | None = None, run_attempt: str | None = None
) -> ResourceIdentity:
    return ResourceIdentity(
        repository=require_environment("GITHUB_REPOSITORY"),
        run_id=run_id or require_environment("GITHUB_RUN_ID"),
        run_attempt=run_attempt or require_environment("GITHUB_RUN_ATTEMPT"),
        instance_key=instance_key,
    )


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
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("provision")
    create.add_argument("--instance-key", required=True)
    create.set_defaults(handler=provision)
    remove = commands.add_parser("cleanup")
    remove.add_argument("--instance-key", required=True)
    remove.add_argument("--run-id")
    remove.add_argument("--run-attempt")
    remove.add_argument("--server-id", default="")
    for role in ROLES:
        remove.add_argument(f"--{role}-runner-id", default="")
    remove.set_defaults(handler=cleanup)
    watch = commands.add_parser("watch-queue")
    watch.add_argument("--instance-key", required=True)
    watch.add_argument("--runner-label", required=True)
    watch.add_argument("--timeout-seconds", type=int, default=600)
    watch.set_defaults(handler=watch_queue)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        arguments.handler(arguments)
        return 0
    except LifecycleError as error:
        print(f"::error::{sanitized(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts import runner_lifecycle as lifecycle

IDENTITY = lifecycle.ResourceIdentity("fixture/pipeline", "12345", "1", "manual-12345-1")
CONFIG = lifecycle.AezaConfig("fixture-api-key")


def test_background_ssh_master_does_not_keep_the_provisioner_output_pipe_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ssh"
    pid_file = tmp_path / "master.pid"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "pid_file=Path(os.environ['BOOTSTRAP_TEST_MASTER_PID'])\n"
        "if '-M' in sys.argv:\n"
        "    child=os.fork()\n"
        "    if child == 0:\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    pid_file.write_text(str(child))\n"
        "elif '-O' in sys.argv:\n"
        "    os.kill(int(pid_file.read_text()), signal.SIGTERM)\n"
        "else:\n"
        "    sys.stdin.read()\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BOOTSTRAP_TEST_MASTER_PID", str(pid_file))
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from scripts.runner_lifecycle import ssh_bootstrap; "
                "ssh_bootstrap('vm.example.invalid', Path(sys.argv[1]), 'fixture payload')",
                str(tmp_path / "private-key"),
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGTERM)


def test_ssh_payload_returns_after_systemd_accepts_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = lifecycle.render_bootstrap(
        IDENTITY,
        CONFIG,
        "42",
        datetime(2030, 1, 1, tzinfo=UTC),
        ("https://example.invalid/runner", "a" * 64, "fixture"),
        {role: f"fixture-jit-{role}" for role in lifecycle.ROLES},
        product=lifecycle.AEZA_PRODUCTS["MSKs-5"],
    )
    # Execute the real handoff shell, with every remote path and system service
    # redirected into this test's sandbox. The bootstrap is accepted but not ready.
    encoded = payload.split("base64.b64decode('")[1].split("'")[0]
    files = json.loads(base64.b64decode(encoded))
    sandboxed = {}
    for name, content in files.items():
        path = tmp_path / "remote" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        sandboxed[str(path)] = content
    payload = payload.replace(encoded, base64.b64encode(json.dumps(sandboxed).encode()).decode())
    payload = payload.replace("/run/gup-bootstrap.ready", str(tmp_path / "not-ready"))
    executables = tmp_path / "bin"
    executables.mkdir()
    trace = tmp_path / "commands"
    for name in ("systemctl", "systemd-run"):
        command = executables / name
        command.write_text(
            '#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >>"$BOOTSTRAP_TEST_TRACE"\n'
            '[ "$1" != is-failed ]\n'
        )
        command.chmod(0o755)
    monkeypatch.setenv("PATH", str(executables) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BOOTSTRAP_TEST_TRACE", str(trace))

    result = subprocess.run(
        ["bash", "-s"], input=payload, text=True, capture_output=True, timeout=1, check=False
    )

    assert result.returncode == 0, result.stderr
    commands = trace.read_text()
    assert commands.index("enable --now gup-emergency") < commands.index("systemd-run")
    assert not (tmp_path / "not-ready").exists()
    assert "is-failed" not in commands


@pytest.mark.parametrize("missing_runner", [None, *lifecycle.ROLES])
@pytest.mark.parametrize("transport_failure", ["disconnect", "timeout"])
def test_lost_ssh_ack_requires_all_four_runners_and_never_replays_payload(
    missing_runner: str | None, transport_failure: str
) -> None:
    def create_key(command: list[str]) -> str:
        private_key = Path(command[-1])
        private_key.with_suffix(".pub").write_text("ssh-ed25519 fixture")
        return ""

    def ssh_result(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "bash -s" and transport_failure == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        return subprocess.CompletedProcess(
            command,
            255 if command[-1] == "bash -s" else 0,
            "",
            "Connection reset by peer; Broken pipe" if command[-1] == "bash -s" else "",
        )

    def runner_online(
        identity: lifecycle.RunnerIdentity, _runner_id: str, _token: str, _timeout: int
    ) -> None:
        if identity.role == missing_runner:
            raise lifecycle.LifecycleError("Timed out waiting for the JIT runner to become online")

    with (
        patch.object(lifecycle.AezaConfig, "from_environment", return_value=CONFIG),
        patch.object(lifecycle, "build_identity", return_value=IDENTITY),
        patch.object(lifecycle, "require_environment", return_value="fixture-token"),
        patch.object(lifecycle, "preflight"),
        patch.object(lifecycle, "add_mask"),
        patch.object(lifecycle, "write_output"),
        patch.object(lifecycle, "append_summary"),
        patch.object(
            lifecycle,
            "resolve_runner_package",
            return_value=("https://example.invalid/runner", "a" * 64, "fixture"),
        ),
        patch.object(lifecycle, "run_command", side_effect=create_key),
        patch.object(lifecycle.AezaConfig, "request", return_value=(200, {"id": 7}, {})),
        patch.object(lifecycle, "create_server", return_value="42"),
        patch.object(lifecycle, "wait_for_server_active", return_value="vm.example.invalid"),
        patch.object(
            lifecycle,
            "create_jit_runner",
            side_effect=[
                (str(index), f"fixture-jit-{role}") for index, role in enumerate(lifecycle.ROLES)
            ],
        ),
        patch.object(lifecycle, "wait_for_runner_online", side_effect=runner_online) as wait_online,
        patch("subprocess.run", side_effect=ssh_result) as ssh,
    ):
        if missing_runner is None:
            lifecycle.provision(Mock(instance_key=IDENTITY.instance_key))
        else:
            with pytest.raises(lifecycle.LifecycleError, match="waiting for the JIT runner"):
                lifecycle.provision(Mock(instance_key=IDENTITY.instance_key))

    checked_roles = (
        lifecycle.ROLES
        if missing_runner is None
        else lifecycle.ROLES[: lifecycle.ROLES.index(missing_runner) + 1]
    )
    assert [call.args[0].role for call in wait_online.call_args_list] == list(checked_roles)
    assert sum(call.args[0][-1] == "bash -s" for call in ssh.call_args_list) == 1
    assert sum("-M" in call.args[0] for call in ssh.call_args_list) == 1
    assert ssh.call_args_list[-1].args[0][-3:-1] == ["-O", "exit"]
    for call in ssh.call_args_list:
        assert CONFIG.api_key not in " ".join(call.args[0])
        assert "fixture-jit-" not in " ".join(call.args[0])


@pytest.mark.parametrize("failure_phase", ["master", "payload"])
def test_bootstrap_rejects_changed_host_key_without_runner_receipt_fallback(
    tmp_path: Path, failure_phase: str
) -> None:
    marker = "REMOTE HOST IDENTIFICATION HAS CHANGED"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        failed = (failure_phase == "master" and "-M" in command) or (
            failure_phase == "payload" and command[-1] == "bash -s"
        )
        if failed and "-M" in command:
            diagnostics = kwargs["stderr"]
            assert hasattr(diagnostics, "write") and hasattr(diagnostics, "flush")
            diagnostics.write(marker)
            diagnostics.flush()
        return subprocess.CompletedProcess(
            command, 255 if failed else 0, "", marker if failed else ""
        )

    with (
        patch("subprocess.run", side_effect=run) as ssh,
        pytest.raises(lifecycle.LifecycleError, match="SSH host key changed") as error,
    ):
        lifecycle.ssh_bootstrap("vm.example.invalid", tmp_path / "private-key", "fixture payload")

    assert not isinstance(error.value, lifecycle.BootstrapOutcomeUnknownError)
    assert sum(call.args[0][-1] == "bash -s" for call in ssh.call_args_list) == (
        0 if failure_phase == "master" else 1
    )

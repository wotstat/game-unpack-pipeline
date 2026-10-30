from __future__ import annotations

import base64
import getpass
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts import runner_lifecycle as lifecycle

IDENTITY = lifecycle.ResourceIdentity("fixture/pipeline", "12345", "1", "manual-12345-1")
CONFIG = lifecycle.AezaConfig("fixture-api-key")


@pytest.mark.skipif(shutil.which("sshd") is None, reason="Local OpenSSH server is unavailable")
def test_real_openssh_waits_for_key_installation_and_reuses_one_authenticated_connection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ssh, sshd = shutil.which("ssh"), shutil.which("sshd")
    assert ssh is not None and sshd is not None
    sleep = time.sleep
    # Unix control sockets have a short path limit, including on macOS.
    with tempfile.TemporaryDirectory(prefix="gup-ssh-test-", dir="/tmp") as temporary:
        root = Path(temporary)
        for name in ("host", "client"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)],
                check=True,
            )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        authorized = root / "authorized_keys"
        authorized.touch(mode=0o600)
        config = root / "sshd_config"
        config.write_text(
            f"Port {port}\nListenAddress 127.0.0.1\nHostKey {root}/host\n"
            f"AuthorizedKeysFile {authorized}\nPidFile {root}/pid\n"
            "UsePAM no\nStrictModes no\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nLogLevel VERBOSE\n"
        )
        executable = root / "bin" / "ssh"
        executable.parent.mkdir()
        executable.write_text(
            "#!/usr/bin/env python3\nimport os,sys\n"
            f"args=[{(getpass.getuser() + '@127.0.0.1')!r} if arg == 'root@vm.example.invalid' "
            "else arg for arg in sys.argv[1:]]\n"
            f"os.execv({ssh!r},[{ssh!r},'-p',{str(port)!r},*args])\n"
        )
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
        monkeypatch.setattr(lifecycle, "SSH_READINESS_TIMEOUT_SECONDS", 3)
        monkeypatch.setattr(time, "sleep", lambda seconds: sleep(min(seconds, 0.05)))

        def install_key() -> None:
            sleep(0.5)
            authorized.write_text((root / "client.pub").read_text())

        installer = threading.Thread(target=install_key)
        received = root / "received"
        with (root / "sshd.log").open("w+") as log:
            server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
            try:
                sleep(0.15)
                if server.poll() is not None:
                    log.seek(0)
                    details = log.read()
                    if "privilege separation directory" in details:
                        pytest.skip("Local sshd privilege-separation directory is unavailable")
                    pytest.fail("Local sshd did not start: " + details)
                installer.start()
                lifecycle.ssh_bootstrap(
                    "vm.example.invalid",
                    root / "client",
                    "printf %s fixture-payload >> " + shlex.quote(str(received)),
                )
            finally:
                if installer.ident is not None:
                    installer.join(timeout=2)
                server.terminate()
                server.wait(timeout=3)
            log.seek(0)
            diagnostics = log.read()
        assert "authentication_pending" in capsys.readouterr().out
        assert "Failed publickey" in diagnostics
        assert diagnostics.count("Accepted publickey") == 1
        assert received.read_text() == "fixture-payload"


def test_active_vm_can_finish_booting_after_the_old_ssh_readiness_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 0.0
    payloads: list[str] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-M" in command:
            if now < 210:
                diagnostics = kwargs["stderr"]
                assert hasattr(diagnostics, "write")
                diagnostics.write(
                    "ssh: connect to host vm.example.invalid port 22: Connection refused"
                )
                return subprocess.CompletedProcess(command, 255, "", "")
        elif command[-1] == "bash -s":
            payloads.append(str(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(subprocess, "run", run)

    lifecycle.ssh_bootstrap("vm.example.invalid", tmp_path / "key", "fixture payload")

    assert now >= 210
    assert payloads == ["fixture payload"]


@pytest.mark.parametrize(
    ("diagnostic", "reason"),
    [
        ("connect to host vm.example.invalid port 22: Connection refused", "connection_refused"),
        ("connect to host vm.example.invalid port 22: Connection timed out", "connect_timeout"),
        (
            "root@vm.example.invalid: Permission denied (publickey,password).",
            "authentication_pending",
        ),
        ("kex_exchange_identification: Connection closed by remote host", "handshake_interrupted"),
        ("debug1: Connecting to vm.example.invalid port 22.", "tcp_connection_pending"),
        ("debug1: Connection established.", "ssh_banner_pending"),
    ],
)
def test_readiness_timeout_reports_a_safe_failure_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    diagnostic: str,
    reason: str,
) -> None:
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-M" in command:
            diagnostics = kwargs["stderr"]
            assert hasattr(diagnostics, "write")
            diagnostics.write(diagnostic + " sensitive-value")
        assert command[-1] != "bash -s"
        return subprocess.CompletedProcess(command, 255, "", "")

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(lifecycle.LifecycleError, match=reason) as failure:
        lifecycle.ssh_bootstrap("vm.example.invalid", tmp_path / "key", "fixture payload")
    output = capsys.readouterr().out + str(failure.value)
    assert "vm.example.invalid" not in output
    assert "sensitive-value" not in output


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Load key /private/path: invalid format",
        "Bad configuration option: fixture",
        "Host key verification failed.",
    ],
)
def test_local_ssh_errors_fail_immediately_without_submitting_bootstrap(
    tmp_path: Path, diagnostic: str
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-M" in command:
            diagnostics = kwargs["stderr"]
            assert hasattr(diagnostics, "write")
            diagnostics.write(diagnostic)
        assert command[-1] != "bash -s"
        assert command[command.index("-F") + 1] == "/dev/null"
        assert "IdentityAgent=none" in command
        assert "PreferredAuthentications=publickey" in command
        assert "StrictHostKeyChecking=accept-new" in command
        return subprocess.CompletedProcess(command, 255, "", "")

    with (
        patch("subprocess.run", side_effect=run) as ssh,
        pytest.raises(lifecycle.LifecycleError),
    ):
        lifecycle.ssh_bootstrap("vm.example.invalid", tmp_path / "key", "fixture payload")
    assert sum("-M" in call.args[0] for call in ssh.call_args_list) == 1
    assert len(ssh.call_args_list) == 2, "The control-master cleanup still runs"


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

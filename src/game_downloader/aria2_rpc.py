"""A private, short-lived aria2 process. Payload bytes never pass through Python."""

from __future__ import annotations

import math
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from game_downloader.pipeline import StageExecutionError


class Aria2Session:
    def __init__(
        self,
        root: Path,
        *,
        executable: str,
        workers: int,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._executable = shutil.which(executable)
        if self._executable is None:
            raise StageExecutionError("source_unavailable", "aria2c is required for HTTP downloads")
        self._directory = tempfile.TemporaryDirectory(prefix="aria2-http-", dir=root)
        self._secret = secrets.token_hex(32)
        self._process: subprocess.Popen[bytes] | None = None
        # aria2 serves RPC from the same event loop as DNS and disk work. A busy
        # healthy process can take longer than two seconds to answer.
        self._client = httpx.Client(
            trust_env=False, timeout=httpx.Timeout(read_timeout, connect=connect_timeout)
        )
        self._close_lock = threading.Lock()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self._port = reservation.getsockname()[1]
        options = {
            "enable-rpc": "true",
            "rpc-listen-all": "false",
            "rpc-listen-port": str(self._port),
            "rpc-secret": self._secret,
            "rpc-allow-origin-all": "false",
            "max-concurrent-downloads": str(workers),
            "max-tries": "1",
            "async-dns": "false",
            "max-file-not-found": "1",
            "connect-timeout": str(max(1, math.ceil(connect_timeout))),
            "timeout": str(max(1, math.ceil(read_timeout))),
            "retry-wait": "0",
            "lowest-speed-limit": "0",
            "file-allocation": "none",
            "disk-cache": "0",
            "auto-file-renaming": "false",
            "allow-overwrite": "false",
            "always-resume": "true",
            "auto-save-interval": "1",
            "no-netrc": "true",
            "check-certificate": "true",
            "http-accept-gzip": "false",
            "follow-torrent": "false",
            "follow-metalink": "false",
            "enable-dht": "false",
            "enable-dht6": "false",
            "enable-peer-exchange": "false",
            "bt-enable-lpd": "false",
            "summary-interval": "0",
            "download-result": "hide",
            "console-log-level": "error",
            "quiet": "true",
        }
        if certificate := os.environ.get("SSL_CERT_FILE"):
            if "\r" in certificate or "\n" in certificate:
                raise StageExecutionError("source_unavailable", "Invalid CA certificate path")
            options["ca-certificate"] = certificate
        self._configuration = Path(self._directory.name) / "aria2.conf"
        descriptor = os.open(self._configuration, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write("".join(f"{name}={value}\n" for name, value in options.items()))

    def __enter__(self) -> Aria2Session:
        try:
            # The token and source URLs never appear in argv, environment or console output.
            self._process = subprocess.Popen(
                [str(self._executable), f"--conf-path={self._configuration}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise StageExecutionError(
                        "source_unavailable", "aria2 HTTP process failed to start"
                    )
                try:
                    self.call("getVersion", timeout=min(2, deadline - time.monotonic()))
                    return self
                except StageExecutionError:
                    time.sleep(0.02)
            raise StageExecutionError("source_unavailable", "aria2 HTTP startup timed out")
        except BaseException:
            self.close()
            raise

    def call(self, method: str, *parameters: Any, timeout: float | None = None) -> Any:
        process = self._process
        if process is None or process.poll() is not None:
            raise StageExecutionError("source_unavailable", "aria2 HTTP process is not running")
        try:
            response = self._client.post(
                f"http://127.0.0.1:{self._port}/jsonrpc",
                json={
                    "jsonrpc": "2.0",
                    "id": "download",
                    "method": f"aria2.{method}",
                    "params": [f"token:{self._secret}", *parameters],
                },
                timeout=self._client.timeout if timeout is None else timeout,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            raise StageExecutionError(
                "source_unavailable",
                f"aria2 HTTP control request failed ({method}: {type(error).__name__})",
            ) from None
        if not isinstance(result, dict) or "error" in result or "result" not in result:
            # RPC diagnostics can contain source credentials. Never include them in errors.
            raise StageExecutionError("source_unavailable", "aria2 HTTP control returned an error")
        return result["result"]

    def remove(self, gid: str) -> dict[str, Any]:
        """Wait for all writers to stop before a caller touches a partial/control file."""
        deadline = time.monotonic() + 5

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise StageExecutionError("source_unavailable", "aria2 HTTP transfer did not stop")
            return value

        try:
            result: dict[str, Any] = self.call("tellStatus", gid, timeout=remaining())
            if result["status"] in {"active", "waiting", "paused"}:
                self.call("forceRemove", gid, timeout=remaining())
                while True:
                    result = self.call("tellStatus", gid, timeout=remaining())
                    if result["status"] in {"complete", "error", "removed"}:
                        break
                    time.sleep(min(0.02, remaining()))
            self.call("removeDownloadResult", gid, timeout=remaining())
            return result
        except StageExecutionError:
            self.close()
            raise

    def close(self) -> None:
        with self._close_lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self._process = None
            self._client.close()
            self._directory.cleanup()

    def __exit__(self, *_arguments: object) -> None:
        self.close()

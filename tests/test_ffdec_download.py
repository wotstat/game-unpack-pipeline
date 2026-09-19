from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("valid_digest", [True, False])
@pytest.mark.parametrize("failure", ["http", "connection"])
def test_ffdec_download_recovers_from_transient_http_failure_and_checks_digest(
    tmp_path: Path, valid_digest: bool, failure: str
) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("ffdec", "#!/bin/sh\n")
        output.writestr("ffdec.sh", "#!/bin/sh\n")
    payload = archive.getvalue()
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal requests
            requests += 1
            if requests == 1 and failure == "connection":
                self.close_connection = True
                return
            self.send_response(503 if requests == 1 else 200)
            self.end_headers()
            if requests > 1:
                self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/process-game-release.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["download"]["steps"]
        if step["name"] == "Install system tools"
    )
    script = step["run"][step["run"].index("ffdec_root=") :]
    if sys.platform == "darwin":
        # macOS sha256sum lacks GNU long options; shasum implements this check.
        script = script.replace("sha256sum --check --strict", "shasum -a 256 --check --strict")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = script.replace(
            "https://github.com/jindrapetrik/jpexs-decompiler/releases/download/"
            "version${FFDEC_VERSION}/ffdec_${FFDEC_VERSION}.zip",
            f"http://127.0.0.1:{server.server_port}/ffdec.zip",
        )
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "RUNNER_TEMP": str(tmp_path),
                "GITHUB_ENV": str(tmp_path / "env"),
                "FFDEC_SHA256": hashlib.sha256(payload if valid_digest else b"wrong").hexdigest(),
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert requests == 2, result.stderr
    assert (result.returncode == 0) == valid_digest, result.stderr
    assert (tmp_path / "ffdec/ffdec").exists() == valid_digest

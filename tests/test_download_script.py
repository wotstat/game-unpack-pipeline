from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
DOWNLOAD_SCRIPT = REPOSITORY_ROOT / "download.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_download_help_documents_the_short_interface() -> None:
    completed = subprocess.run(
        ["bash", str(DOWNLOAD_SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "./download.sh TARGET [DIRECTORY]" in completed.stdout
    assert "./download.sh wot-eu ./.data --language ALL" in completed.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["wot-eu", "workspace", "--language", "ALL", "--workers", "3"],
        ["--target", "wot-eu", "--language", "ALL", "--workers", "3", "workspace"],
    ],
)
def test_download_maps_both_interfaces_to_the_internal_cli(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    recorded_arguments = tmp_path / "uv-arguments"
    ffdec = tmp_path / "ffdec"
    _write_executable(
        ffdec,
        "printf '%s\\n' 'JPEXS Free Flash Decompiler v.26.2.1'\n",
    )
    _write_executable(fake_bin / "java", "exit 0\n")
    _write_executable(fake_bin / "7zz", "exit 0\n")
    _write_executable(
        fake_bin / "uv",
        'printf \'%s\\n\' "$@" >"${DOWNLOAD_TEST_ARGUMENTS:?}"\n',
    )
    environment = {
        **os.environ,
        "DOWNLOAD_TEST_ARGUMENTS": str(recorded_arguments),
        "GAME_DOWNLOADER_FFDEC": str(ffdec),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    completed = subprocess.run(
        ["bash", str(DOWNLOAD_SCRIPT), *arguments],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert recorded_arguments.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--frozen",
        "game-downloader",
        "run",
        "--target",
        "wot-eu",
        "--client-type",
        "sd",
        "--languages",
        "ALL",
        "--data-root",
        str(tmp_path / "workspace"),
        "--download-workers",
        "3",
    ]
    assert f"Done. Verified snapshots: {tmp_path}/workspace/snapshots" in completed.stdout


def test_download_rejects_invalid_client_before_running_tools(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["bash", str(DOWNLOAD_SCRIPT), "wot-eu", "--client", "ultra"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "client type must be sd or hd" in completed.stderr

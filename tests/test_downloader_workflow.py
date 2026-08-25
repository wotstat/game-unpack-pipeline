from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from game_downloader.models import STAGE_ORDER

REPOSITORY_ROOT = Path(__file__).parents[1]


def _workflow() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/ephemeral-snapshot.yml").read_text()),
    )


def test_download_job_exposes_every_pipeline_stage_as_one_step() -> None:
    steps = _workflow()["jobs"]["download"]["steps"]
    stage_steps = [step for step in steps if "run-stage.sh" in str(step.get("run", ""))]

    assert [step["run"].split()[-1] for step in stage_steps] == [
        stage.value for stage in STAGE_ORDER
    ]
    assert [step["name"] for step in stage_steps[7:13]] == [
        "Readable · Plan output",
        "Readable · Transform Python, XML and translations",
        "Readable · Decompile ActionScript",
        "Readable · Assemble and link tree",
        "Readable · Generate engine stubs",
        "Readable · Finalize references",
    ]
    assert all("if" not in step for step in stage_steps)


def test_downloader_is_internal_to_the_primary_workflow() -> None:
    workflow_path = REPOSITORY_ROOT / ".github/workflows/ephemeral-snapshot.yml"
    download = _workflow()["jobs"]["download"]

    assert "uses" not in download
    assert download["runs-on"] == [
        "self-hosted",
        "${{ needs.provision.outputs.downloader_runner_label }}",
    ]
    assert "game-snapshot-builder" not in workflow_path.read_text()


def test_download_job_exports_sealed_snapshot_identity() -> None:
    outputs = _workflow()["jobs"]["download"]["outputs"]

    assert outputs == {
        "snapshot_id": "${{ steps.result.outputs.snapshot_id }}",
        "version_name": "${{ steps.result.outputs.version_name }}",
        "snapshot_path": "${{ steps.result.outputs.snapshot_path }}",
        "descriptor_sha256": "${{ steps.result.outputs.descriptor_sha256 }}",
    }


def test_download_job_exposes_only_the_sealed_snapshot_tree_to_publishers() -> None:
    share = next(
        step
        for step in _workflow()["jobs"]["download"]["steps"]
        if step["name"] == "Expose sealed snapshot to publisher runners"
    )

    assert "steps.result.outputs.snapshot_path != ''" in share["if"]
    assert 'chmod 0711 "${GAME_DOWNLOADER_DATA_ROOT}"' in share["run"]
    assert 'chmod 0711 "${GAME_DOWNLOADER_DATA_ROOT}/snapshots"' in share["run"]
    assert "chmod -R" not in share["run"]


def test_stage_runner_uses_the_canonical_stage_sequence_numbers() -> None:
    script = (REPOSITORY_ROOT / ".github/scripts/run-stage.sh").read_text()

    for stage in STAGE_ORDER:
        assert f"{stage.value}) readonly sequence={stage.number * 10:03d} ;;" in script
    assert "--skip-check" in script
    assert '--until "${stage}"' in script
    assert 'readonly metrics_stage="${stage}"' in script


def test_stage_runner_records_bottleneck_metrics_without_logging_process_arguments() -> None:
    workflow = _workflow()
    install_tools = next(
        step
        for step in workflow["jobs"]["download"]["steps"]
        if step["name"] == "Install system tools"
    )
    script = (REPOSITORY_ROOT / ".github/scripts/run-stage.sh").read_text()
    metrics = (REPOSITORY_ROOT / ".github/scripts/performance-metrics.py").read_text()

    assert "sysstat" in install_tools["run"]
    assert " time " in install_tools["run"]
    assert "Record runner performance inventory" in [
        step["name"] for step in workflow["jobs"]["download"]["steps"]
    ]
    assert '"${metrics_script}" monitor' in script
    assert "/usr/bin/time --verbose" in script
    assert "${sequence}-${stage}-performance.json" in script
    assert 'if [[ "${stage}" == download ]]' in script
    assert "iostat -d -x -m -t -y 5" in script
    assert "${sequence}-${stage}-iostat.log" in script
    assert "cmdline" not in metrics
    assert 'directory / "io"' in metrics

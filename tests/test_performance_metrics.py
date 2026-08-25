from __future__ import annotations

import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPOSITORY_ROOT = Path(__file__).parents[1]
METRICS_SCRIPT = REPOSITORY_ROOT / ".github/scripts/performance-metrics.py"
COLLECT_RESULT_SCRIPT = REPOSITORY_ROOT / ".github/scripts/collect-result.py"


def _sample(monotonic: float, factor: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "stage": "fixture",
        "timestamp": f"2026-08-22T00:00:{factor:02d}+00:00",
        "monotonic_seconds": monotonic,
        "logical_cpus": 4,
        "cpu": {
            "user": 100 + 40 * factor,
            "nice": 0,
            "system": 50 + 10 * factor,
            "idle": 500 + 40 * factor,
            "iowait": 20 + 10 * factor,
            "irq": 0,
            "softirq": 0,
            "steal": 0,
        },
        "cpu_per_core": {
            "cpu0": {
                "user": 100 + 40 * factor,
                "nice": 0,
                "system": 50 + 10 * factor,
                "idle": 500 + 40 * factor,
                "iowait": 20 + 10 * factor,
                "irq": 0,
                "softirq": 0,
                "steal": 0,
            }
        },
        "cpu_frequency": {
            "minimum_mhz": 2_000.0,
            "maximum_mhz": 3_000.0,
            "mean_mhz": 2_500.0,
        },
        "cgroup_cpu": {
            "nr_periods": 10 * factor,
            "nr_throttled": factor,
            "throttled_usec": 10_000 * factor,
        },
        "memory": {
            "total_bytes": 1_000,
            "available_bytes": 800 - 100 * factor,
            "dirty_bytes": 10 * factor,
            "swap_total_bytes": 100,
            "swap_free_bytes": 100,
        },
        "load": {"one_minute": float(factor), "runnable": factor, "processes": 10},
        "pressure": {
            "cpu": {"some": {"total": 100_000 * factor}},
            "memory": {"some": {"total": 0}, "full": {"total": 0}},
            "io": {"some": {"total": 200_000 * factor}, "full": {"total": 50_000 * factor}},
        },
        "disk": {
            "device": "vda1",
            "reads_completed": 10 * factor,
            "reads_merged": 0,
            "sectors_read": 100 * factor,
            "read_milliseconds": 20 * factor,
            "writes_completed": 20 * factor,
            "writes_merged": 0,
            "sectors_written": 200 * factor,
            "write_milliseconds": 40 * factor,
            "io_in_progress": 0,
            "io_milliseconds": 1_000 * factor,
            "weighted_io_milliseconds": 2_000 * factor,
        },
        "network": {
            "received_bytes": 1_000 * factor,
            "received_packets": 10 * factor,
            "receive_errors": 0,
            "receive_drops": 0,
            "transmitted_bytes": 2_000 * factor,
            "transmitted_packets": 20 * factor,
            "transmit_errors": 0,
            "transmit_drops": 0,
        },
        "tcp": {"segments_retransmitted": factor},
        "filesystem": {"used_bytes": 100 * factor, "free_bytes": 10_000 - 100 * factor},
        "processes": [
            {
                "pid": 7,
                "start_ticks": 1,
                "name": "python",
                "cpu_ticks": 100 * factor,
                "rss_bytes": 200 * factor,
                "read_bytes": 300 * factor,
                "write_bytes": 400 * factor,
            }
        ],
    }


def test_summarize_emits_resource_saturation_metrics(tmp_path: Path) -> None:
    samples = tmp_path / "samples.jsonl"
    samples.write_text(
        "\n".join(
            json.dumps(_sample(monotonic, factor)) for monotonic, factor in [(10, 1), (20, 2)]
        )
        + "\n"
    )
    time_report = tmp_path / "time.log"
    time_report.write_text(
        "\n".join(
            [
                "User time (seconds): 8.00",
                "System time (seconds): 1.00",
                "Percent of CPU this job got: 90%",
                "Elapsed (wall clock) time (h:mm:ss or m:ss): 0:10.00",
                "Maximum resident set size (kbytes): 1024",
                "Major (requiring I/O) page faults: 2",
                "Minor (reclaiming a frame) page faults: 3",
                "File system inputs: 4",
                "File system outputs: 5",
                "Voluntary context switches: 6",
                "Involuntary context switches: 7",
            ]
        )
        + "\n"
    )
    output = tmp_path / "summary.json"

    subprocess.run(
        [
            sys.executable,
            str(METRICS_SCRIPT),
            "summarize",
            "--stage",
            "fixture",
            "--samples",
            str(samples),
            "--time-report",
            str(time_report),
            "--output",
            str(output),
        ],
        check=True,
    )

    summary = json.loads(output.read_text())
    assert summary["duration_seconds"] == 10.0
    assert summary["cpu"] == {
        "busy_percent": 50.0,
        "effective_busy_cores": 2.0,
        "idle_percent": 40.0,
        "iowait_percent": 10.0,
        "steal_percent": 0.0,
        "system_percent": 10.0,
        "user_percent": 40.0,
    }
    assert summary["pressure"]["io"]["some_stalled_percent"] == 2.0
    assert summary["cpu_per_core"]["maximum_busy_percent"] == 50.0
    assert summary["cpu_frequency"]["mean_mhz"] == 2_500.0
    assert summary["cgroup_cpu"]["throttled_percent"] == 0.1
    assert summary["disk"]["read_bytes"] == 51_200
    assert summary["disk"]["write_bytes"] == 102_400
    assert summary["disk"]["util_percent"] == 10.0
    assert summary["network"]["received_bytes_per_second"] == 100.0
    assert summary["tcp"]["segments_retransmitted"] == 1
    assert summary["top_processes"][0]["name"] == "python"
    assert summary["command"]["maximum_rss_bytes"] == 1024 * 1024


def test_actions_summary_separates_stage_time_from_replay_overhead() -> None:
    namespace = runpy.run_path(COLLECT_RESULT_SCRIPT.as_posix())
    render = cast(
        Callable[[tuple[dict[str, Any], ...], dict[str, float | None]], list[str]],
        namespace["_performance_lines"],
    )
    performance = (
        {
            "stage": "transform-readable",
            "duration_seconds": 90,
            "command": {"elapsed_seconds": 90, "maximum_rss_bytes": 1024},
            "cpu": {"busy_percent": 25, "effective_busy_cores": 4},
            "disk": {},
            "network": {},
            "pressure": {},
        },
    )

    rendered = "\n".join(render(performance, {"transform-readable": 60}))

    assert "Replay/report overhead" in rendered
    assert "1m 30s" in rendered
    assert "30.0 s" in rendered
    assert "25.0% (4.00 cores)" in rendered


def test_actions_summary_formats_snapshot_phase_seconds_as_durations() -> None:
    namespace = runpy.run_path(COLLECT_RESULT_SCRIPT.as_posix())
    format_statistic = cast(Callable[[str, object], str], namespace["_format_statistic"])

    assert format_statistic("verify_payload_seconds", 125.0) == "verify payload: 2m 05s"

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

from game_downloader._json import canonical_json_bytes
from game_downloader.models import (
    DownloadResult,
    ResolveResult,
    SnapshotResult,
    Stage,
    StageResult,
)
from game_downloader.pipeline import Pipeline
from game_downloader.workspace import Workspace

VERSION_XML_READABLE_RE = re.compile(
    r"^v\.(?P<version>[0-9]+(?:\.[0-9]+){3})"
    r"(?: [A-Za-z]+(?: [A-Za-z]+)*)? #(?P<build>[0-9]+)$"
)


def _readable_version(snapshot_path: Path) -> str:
    version_path = snapshot_path / "sources/base/version.xml"
    try:
        root = ElementTree.fromstring(version_path.read_bytes())
    except (OSError, ElementTree.ParseError) as error:
        raise ValueError(f"cannot parse root version.xml: {error}") from error
    version = root.find("version")
    value = " ".join(version.text.split()) if version is not None and version.text else ""
    match = VERSION_XML_READABLE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"root version.xml has an invalid readable version: {value!r}")
    return f"{match.group('version')} #{match.group('build')}"


def _format_duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "—"
    if seconds < 1:
        return f"{round(seconds * 1000)} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    total = round(seconds)
    minutes, remaining = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remaining:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {remaining:02d}s"


def _format_bytes(value: float) -> str:
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1024 or candidate == units[-1]:
            break
        amount /= 1024
    return f"{round(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"


def _format_statistic(key: str, value: object) -> str:
    label_key = key.removesuffix("_seconds") if key.endswith("_seconds") else key
    label = label_key.replace("_", " ")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key.endswith("bytes_per_second"):
            rendered = f"{_format_bytes(float(value))}/s"
        elif key.endswith("_bytes"):
            rendered = _format_bytes(float(value))
        elif key.endswith("_seconds"):
            rendered = _format_duration(value)
        else:
            rendered = str(value)
    else:
        rendered = str(value)
    return f"{label}: {rendered}"


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parallel_range_fallback_report(
    download_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    fallbacks: list[dict[str, Any]] = []
    raw_artifacts = download_payload.get("artifacts", []) if download_payload else []
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    for raw_item in artifacts:
        item = _object(raw_item)
        if item.get("reused") is True:
            continue
        artifact = _object(item.get("artifact"))
        transport = _object(item.get("transport"))
        raw_fallbacks = transport.get("parallel_range_fallbacks", [])
        if not isinstance(raw_fallbacks, list):
            continue
        for raw_fallback in raw_fallbacks:
            fallback = _object(raw_fallback)
            fallbacks.append(
                {
                    "artifact_id": artifact.get("artifact_id"),
                    "artifact_path": artifact.get("path"),
                    "reason": fallback.get("reason"),
                    "source_host": fallback.get("source_host"),
                    "response_status": fallback.get("response_status"),
                    "range_index": fallback.get("range_index"),
                    "attempts": fallback.get("attempts"),
                    "discarded_bytes": fallback.get("discarded_bytes"),
                }
            )
    return {
        "schema_version": 1,
        "download_result_available": download_payload is not None,
        "fallback_artifacts": len({item["artifact_id"] for item in fallbacks}),
        "fallback_count": len(fallbacks),
        "discarded_bytes": sum(_integer(item["discarded_bytes"]) for item in fallbacks),
        "fallbacks": fallbacks,
    }


def _load_performance(report_dir: Path) -> tuple[dict[str, Any], ...]:
    reports: list[dict[str, Any]] = []
    for path in sorted(report_dir.glob("[0-9][0-9][0-9]-*-performance.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            reports.append(value)
    return tuple(reports)


def _format_rate(value: object) -> str:
    return f"{_format_bytes(_number(value))}/s"


def _performance_lines(
    performance: tuple[dict[str, Any], ...],
    stage_durations: dict[str, float | None],
) -> list[str]:
    if not performance:
        return []
    lines = [
        "",
        "## Resource utilisation",
        "",
        (
            "| Stage | Command | Replay/report overhead | CPU | Peak RSS | "
            "Disk R/W | Disk await/util | Network RX/TX | Pressure CPU/IO |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in performance:
        stage = str(item.get("stage", "unknown"))
        command = _object(item.get("command"))
        cpu = _object(item.get("cpu"))
        disk = _object(item.get("disk"))
        network = _object(item.get("network"))
        pressure = _object(item.get("pressure"))
        cpu_pressure = _object(pressure.get("cpu"))
        io_pressure = _object(pressure.get("io"))
        elapsed = _number(command.get("elapsed_seconds")) or _number(item.get("duration_seconds"))
        active = stage_durations.get(stage)
        overhead = max(0.0, elapsed - active) if active is not None else 0.0
        lines.append(
            "| "
            f"`{_escape(stage)}` | "
            f"{_format_duration(elapsed)} | "
            f"{_format_duration(overhead) if active is not None else '—'} | "
            f"{_number(cpu.get('busy_percent')):.1f}% "
            f"({_number(cpu.get('effective_busy_cores')):.2f} cores) | "
            f"{_format_bytes(_number(command.get('maximum_rss_bytes')))} | "
            f"{_format_rate(disk.get('read_bytes_per_second'))} / "
            f"{_format_rate(disk.get('write_bytes_per_second'))} | "
            f"{_number(disk.get('average_await_milliseconds')):.1f} ms / "
            f"{_number(disk.get('util_percent')):.1f}% | "
            f"{_format_rate(network.get('received_bytes_per_second'))} / "
            f"{_format_rate(network.get('transmitted_bytes_per_second'))} | "
            f"{_number(cpu_pressure.get('some_stalled_percent')):.1f}% / "
            f"{_number(io_pressure.get('some_stalled_percent')):.1f}% |"
        )
    return lines


def main() -> None:
    data_root = Path(os.environ["GAME_DOWNLOADER_DATA_ROOT"]).absolute()
    report_dir = Path(os.environ["GAME_DOWNLOADER_REPORT_DIR"]).absolute()
    run_id = os.environ["GAME_DOWNLOADER_RUN_ID"]
    report_dir.mkdir(parents=True, exist_ok=True)

    workspace = Workspace(data_root)
    report = Pipeline(workspace).status(run_id)
    (report_dir / "run-report.json").write_bytes(
        canonical_json_bytes(report.model_dump(mode="json"))
    )

    download_payload: dict[str, Any] | None = None
    download_stage = next(item for item in report.stages if item.stage is Stage.DOWNLOAD)
    if download_stage.state.value == "succeeded":
        raw_result = workspace.read_bytes(
            workspace.stage_path(run_id, Stage.DOWNLOAD) / "result.json"
        )
        stage_result = StageResult.model_validate_json(raw_result)
        download = DownloadResult.model_validate(stage_result.payload)
        download_payload = download.model_dump(mode="json")
    (report_dir / "parallel-range-fallbacks.json").write_bytes(
        canonical_json_bytes(_parallel_range_fallback_report(download_payload))
    )

    resolve: ResolveResult | None = None
    resolve_stage = next(item for item in report.stages if item.stage is Stage.RESOLVE)
    if resolve_stage.state.value == "succeeded":
        raw_result = workspace.read_bytes(
            workspace.stage_path(run_id, Stage.RESOLVE) / "result.json"
        )
        stage_result = StageResult.model_validate_json(raw_result)
        resolve = ResolveResult.model_validate(stage_result.payload)

    snapshot: SnapshotResult | None = None
    snapshot_stage = next(item for item in report.stages if item.stage is Stage.SNAPSHOT)
    if snapshot_stage.state.value == "succeeded":
        raw_result = workspace.read_bytes(
            workspace.stage_path(run_id, Stage.SNAPSHOT) / "result.json"
        )
        stage_result = StageResult.model_validate_json(raw_result)
        snapshot = SnapshotResult.model_validate(stage_result.payload)

    _write_output("run_id", report.run_id)
    _write_output("data_root", data_root.as_posix())
    if resolve is not None:
        _write_output("version_name", resolve.release_name)
    if snapshot is not None:
        snapshot_path = data_root / snapshot.snapshot_path
        readable_version = _readable_version(snapshot_path)
        _write_output("snapshot_id", snapshot.snapshot_id)
        _write_output("readable_version", readable_version)
        _write_output("snapshot_path", snapshot_path.as_posix())
        _write_output("descriptor_sha256", snapshot.descriptor_sha256)

    state_icons = {
        "succeeded": "✅",
        "failed": "❌",
        "interrupted": "⛔",
        "running": "🟡",
        "pending": "⚪",
    }
    lines = [
        "## GameSnapshot build",
        "",
        f"- Run: `{_escape(report.run_id)}`",
        f"- Result: **{_escape(report.state.value)}**",
        f"- Target: `{_escape(report.request.target)}`",
        f"- Client type: `{_escape(report.request.client_type.value)}`",
        f"- Languages: `{_escape(','.join(report.request.languages))}`",
        f"- Active time: `{_format_duration(report.active_duration_seconds)}`",
    ]
    if resolve is not None:
        lines.append(f"- Version: `{_escape(resolve.release_name)}`")
    if snapshot is not None:
        lines.append(f"- Snapshot: `{_escape(snapshot.snapshot_id)}`")
        lines.append(f"- Readable version: `{_escape(readable_version)}`")
    lines.extend(
        [
            "",
            "| Stage | State | Duration | Statistics |",
            "|---|---:|---:|---|",
        ]
    )
    for stage in report.stages:
        if stage.state.value == "pending":
            continue
        statistics = ", ".join(
            _format_statistic(key, value) for key, value in stage.statistics.items()
        )
        if stage.error is not None:
            error = f"{stage.error.code}: {stage.error.message}"
            statistics = f"{statistics}; {error}" if statistics else error
        lines.append(
            "| "
            f"`{_escape(stage.stage.value)}` | "
            f"{state_icons.get(stage.state.value, '')} {_escape(stage.state.value)} | "
            f"{_format_duration(stage.duration_seconds)} | "
            f"{_escape(statistics) or '—'} |"
        )

    performance = _load_performance(report_dir)
    stage_durations = {item.stage.value: item.duration_seconds for item in report.stages}
    lines.extend(_performance_lines(performance, stage_durations))
    inventory_path = report_dir / "host-performance.json"
    inventory: dict[str, Any] = {}
    try:
        raw_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if isinstance(raw_inventory, dict):
            inventory = raw_inventory
    except (OSError, json.JSONDecodeError):
        pass
    (report_dir / "performance-report.json").write_text(
        json.dumps(
            {"schema_version": 1, "host": inventory, "stages": performance},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    summary = "\n".join(lines) + "\n"
    print(summary, end="")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write(summary)


if __name__ == "__main__":
    main()

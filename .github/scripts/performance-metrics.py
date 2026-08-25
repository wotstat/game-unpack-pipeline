#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import time
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SECTOR_BYTES = 512


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _integer(value: object, default: int = 0) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _round(value: float) -> float:
    return round(value, 3)


def _counter_delta(start: object, finish: object) -> int:
    return max(0, _integer(finish) - _integer(start))


def _cpu_fields(line: str) -> dict[str, int]:
    values = [_integer(item) for item in line.split()[1:]]
    names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    return {name: values[index] if index < len(values) else 0 for index, name in enumerate(names)}


def _read_cpu() -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    raw = _read_text(Path("/proc/stat")) or ""
    line = next((item for item in raw.splitlines() if item.startswith("cpu ")), "")
    per_core = {
        item.split()[0]: _cpu_fields(item)
        for item in raw.splitlines()
        if item.startswith("cpu") and not item.startswith("cpu ")
    }
    return _cpu_fields(line), per_core


def _read_cpu_frequency() -> dict[str, float | None]:
    raw = _read_text(Path("/proc/cpuinfo")) or ""
    frequencies: list[float] = []
    for line in raw.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == "cpu MHz":
            try:
                frequencies.append(float(value))
            except ValueError:
                continue
    return {
        "minimum_mhz": _round(min(frequencies)) if frequencies else None,
        "maximum_mhz": _round(max(frequencies)) if frequencies else None,
        "mean_mhz": _round(sum(frequencies) / len(frequencies)) if frequencies else None,
    }


def _read_cgroup_cpu() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in (_read_text(Path("/sys/fs/cgroup/cpu.stat")) or "").splitlines():
        name, _, value = line.partition(" ")
        result[name] = _integer(value)
    return result


def _read_memory() -> dict[str, int]:
    selected = {
        "MemTotal": "total_bytes",
        "MemAvailable": "available_bytes",
        "Cached": "cached_bytes",
        "Buffers": "buffers_bytes",
        "Dirty": "dirty_bytes",
        "Writeback": "writeback_bytes",
        "SwapTotal": "swap_total_bytes",
        "SwapFree": "swap_free_bytes",
    }
    result = {name: 0 for name in selected.values()}
    raw = _read_text(Path("/proc/meminfo")) or ""
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key not in selected:
            continue
        result[selected[key]] = _integer(value.split()[0]) * 1024
    return result


def _read_load() -> dict[str, float | int]:
    fields = (_read_text(Path("/proc/loadavg")) or "0 0 0 0/0").split()
    runnable, _, processes = (fields[3] if len(fields) > 3 else "0/0").partition("/")
    return {
        "one_minute": float(fields[0]) if fields else 0.0,
        "five_minutes": float(fields[1]) if len(fields) > 1 else 0.0,
        "fifteen_minutes": float(fields[2]) if len(fields) > 2 else 0.0,
        "runnable": _integer(runnable),
        "processes": _integer(processes),
    }


def _read_pressure() -> dict[str, dict[str, dict[str, float | int]]]:
    result: dict[str, dict[str, dict[str, float | int]]] = {}
    for resource in ("cpu", "memory", "io"):
        raw = _read_text(Path("/proc/pressure") / resource)
        if raw is None:
            continue
        classes: dict[str, dict[str, float | int]] = {}
        for line in raw.splitlines():
            fields = line.split()
            if not fields:
                continue
            values: dict[str, float | int] = {}
            for field in fields[1:]:
                name, separator, value = field.partition("=")
                if not separator:
                    continue
                values[name] = _integer(value) if name == "total" else float(value)
            classes[fields[0]] = values
        result[resource] = classes
    return result


def _root_block_device(data_root: Path) -> str | None:
    try:
        device = os.stat(data_root).st_dev
        link = Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}"
        return link.resolve(strict=True).name
    except OSError:
        return None


def _read_disk(device: str | None) -> dict[str, int | str] | None:
    if device is None:
        return None
    raw = _read_text(Path("/proc/diskstats")) or ""
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 14 or fields[2] != device:
            continue
        values = [_integer(item) for item in fields[3:14]]
        return {
            "device": device,
            "reads_completed": values[0],
            "reads_merged": values[1],
            "sectors_read": values[2],
            "read_milliseconds": values[3],
            "writes_completed": values[4],
            "writes_merged": values[5],
            "sectors_written": values[6],
            "write_milliseconds": values[7],
            "io_in_progress": values[8],
            "io_milliseconds": values[9],
            "weighted_io_milliseconds": values[10],
        }
    return None


def _read_network() -> dict[str, int]:
    totals = {
        "received_bytes": 0,
        "received_packets": 0,
        "receive_errors": 0,
        "receive_drops": 0,
        "transmitted_bytes": 0,
        "transmitted_packets": 0,
        "transmit_errors": 0,
        "transmit_drops": 0,
    }
    raw = _read_text(Path("/proc/net/dev")) or ""
    for line in raw.splitlines()[2:]:
        name, separator, counters = line.partition(":")
        if not separator or name.strip() == "lo":
            continue
        values = [_integer(item) for item in counters.split()]
        if len(values) < 16:
            continue
        totals["received_bytes"] += values[0]
        totals["received_packets"] += values[1]
        totals["receive_errors"] += values[2]
        totals["receive_drops"] += values[3]
        totals["transmitted_bytes"] += values[8]
        totals["transmitted_packets"] += values[9]
        totals["transmit_errors"] += values[10]
        totals["transmit_drops"] += values[11]
    return totals


def _read_tcp() -> dict[str, int]:
    raw = (_read_text(Path("/proc/net/snmp")) or "").splitlines()
    for index in range(len(raw) - 1):
        if not raw[index].startswith("Tcp:") or not raw[index + 1].startswith("Tcp:"):
            continue
        names = raw[index].split()[1:]
        values = raw[index + 1].split()[1:]
        parsed = {name: _integer(value) for name, value in zip(names, values, strict=False)}
        return {
            "active_opens": parsed.get("ActiveOpens", 0),
            "passive_opens": parsed.get("PassiveOpens", 0),
            "segments_received": parsed.get("InSegs", 0),
            "segments_sent": parsed.get("OutSegs", 0),
            "segments_retransmitted": parsed.get("RetransSegs", 0),
            "input_errors": parsed.get("InErrs", 0),
        }
    return {}


def _read_filesystem(data_root: Path) -> dict[str, int]:
    try:
        value = os.statvfs(data_root)
    except OSError:
        return {}
    size = value.f_blocks * value.f_frsize
    free = value.f_bavail * value.f_frsize
    inode_free = value.f_favail
    return {
        "size_bytes": size,
        "free_bytes": free,
        "used_bytes": max(0, size - free),
        "inodes": value.f_files,
        "free_inodes": inode_free,
    }


def _read_processes() -> list[dict[str, int | str]]:
    page_size = os.sysconf("SC_PAGE_SIZE")
    processes: list[dict[str, int | str]] = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return processes
    for directory in entries:
        if not directory.name.isdigit():
            continue
        raw_stat = _read_text(directory / "stat")
        if raw_stat is None:
            continue
        opening = raw_stat.find("(")
        closing = raw_stat.rfind(")")
        if opening < 0 or closing <= opening:
            continue
        fields = raw_stat[closing + 2 :].split()
        if len(fields) <= 21:
            continue
        io_values: dict[str, int] = {}
        for line in (_read_text(directory / "io") or "").splitlines():
            name, separator, value = line.partition(":")
            if separator:
                io_values[name] = _integer(value)
        processes.append(
            {
                "pid": _integer(directory.name),
                "start_ticks": _integer(fields[19]),
                "name": raw_stat[opening + 1 : closing],
                "cpu_ticks": _integer(fields[11]) + _integer(fields[12]),
                "rss_bytes": max(0, _integer(fields[21])) * page_size,
                "read_bytes": io_values.get("read_bytes", 0),
                "write_bytes": io_values.get("write_bytes", 0),
            }
        )
    return processes


def _sample(data_root: Path, stage: str, device: str | None) -> dict[str, Any]:
    cpu, cpu_per_core = _read_cpu()
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "timestamp": datetime.now(UTC).isoformat(),
        "monotonic_seconds": round(time.monotonic(), 6),
        "logical_cpus": os.cpu_count() or 1,
        "cpu": cpu,
        "cpu_per_core": cpu_per_core,
        "cpu_frequency": _read_cpu_frequency(),
        "cgroup_cpu": _read_cgroup_cpu(),
        "memory": _read_memory(),
        "load": _read_load(),
        "pressure": _read_pressure(),
        "disk": _read_disk(device),
        "network": _read_network(),
        "tcp": _read_tcp(),
        "filesystem": _read_filesystem(data_root),
        "processes": _read_processes(),
    }


def _run_monitor(arguments: argparse.Namespace) -> None:
    data_root = Path(arguments.data_root).absolute()
    samples_path = Path(arguments.samples)
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    device = _root_block_device(data_root)
    with samples_path.open("w", encoding="utf-8") as output:
        while True:
            output.write(json.dumps(_sample(data_root, arguments.stage, device), sort_keys=True))
            output.write("\n")
            output.flush()
            if stop:
                break
            deadline = time.monotonic() + arguments.interval_seconds
            while not stop and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _percentages(start: dict[str, Any], finish: dict[str, Any]) -> dict[str, float]:
    deltas = {name: _counter_delta(start.get(name), finish.get(name)) for name in finish}
    total = sum(deltas.values())
    if total <= 0:
        return {}
    busy_names = ("user", "nice", "system", "irq", "softirq", "steal")
    return {
        "busy_percent": _round(sum(deltas.get(name, 0) for name in busy_names) * 100 / total),
        "user_percent": _round((deltas.get("user", 0) + deltas.get("nice", 0)) * 100 / total),
        "system_percent": _round(
            (deltas.get("system", 0) + deltas.get("irq", 0) + deltas.get("softirq", 0))
            * 100
            / total
        ),
        "iowait_percent": _round(deltas.get("iowait", 0) * 100 / total),
        "steal_percent": _round(deltas.get("steal", 0) * 100 / total),
        "idle_percent": _round(deltas.get("idle", 0) * 100 / total),
    }


def _gauge_summary(samples: list[dict[str, Any]], section: str, key: str) -> list[int]:
    return [_integer(sample.get(section, {}).get(key)) for sample in samples]


def _pressure_summary(
    start: dict[str, Any], finish: dict[str, Any], duration_seconds: float
) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    if duration_seconds <= 0:
        return result
    for resource, classes in finish.items():
        if not isinstance(classes, dict):
            continue
        summarized: dict[str, float] = {}
        for pressure_class, values in classes.items():
            if not isinstance(values, dict):
                continue
            initial = start.get(resource, {}).get(pressure_class, {})
            stalled_usec = _counter_delta(initial.get("total"), values.get("total"))
            summarized[f"{pressure_class}_stalled_percent"] = _round(
                stalled_usec / (duration_seconds * 1_000_000) * 100
            )
        if summarized:
            result[resource] = summarized
    return result


def _per_core_summary(start: dict[str, Any], finish: dict[str, Any]) -> dict[str, Any]:
    percentages = {
        core: _percentages(values, finish[core])
        for core, values in start.items()
        if core in finish and isinstance(values, dict) and isinstance(finish[core], dict)
    }
    busy = [value["busy_percent"] for value in percentages.values() if "busy_percent" in value]
    return {
        "minimum_busy_percent": _round(min(busy)) if busy else 0.0,
        "mean_busy_percent": _round(sum(busy) / len(busy)) if busy else 0.0,
        "maximum_busy_percent": _round(max(busy)) if busy else 0.0,
        "cores": percentages,
    }


def _frequency_summary(samples: list[dict[str, Any]]) -> dict[str, float | None]:
    minimums = [
        float(value)
        for sample in samples
        if (value := sample.get("cpu_frequency", {}).get("minimum_mhz")) is not None
    ]
    maximums = [
        float(value)
        for sample in samples
        if (value := sample.get("cpu_frequency", {}).get("maximum_mhz")) is not None
    ]
    means = [
        float(value)
        for sample in samples
        if (value := sample.get("cpu_frequency", {}).get("mean_mhz")) is not None
    ]
    return {
        "minimum_mhz": _round(min(minimums)) if minimums else None,
        "maximum_mhz": _round(max(maximums)) if maximums else None,
        "mean_mhz": _round(sum(means) / len(means)) if means else None,
    }


def _cgroup_cpu_summary(
    start: dict[str, Any], finish: dict[str, Any], duration_seconds: float
) -> dict[str, int | float]:
    throttled_usec = _counter_delta(start.get("throttled_usec"), finish.get("throttled_usec"))
    result: dict[str, int | float] = {
        "periods": _counter_delta(start.get("nr_periods"), finish.get("nr_periods")),
        "throttled_periods": _counter_delta(start.get("nr_throttled"), finish.get("nr_throttled")),
        "throttled_usec": throttled_usec,
    }
    if duration_seconds > 0:
        result["throttled_percent"] = _round(throttled_usec / (duration_seconds * 1_000_000) * 100)
    return result


def _disk_summary(
    start: dict[str, Any] | None,
    finish: dict[str, Any] | None,
    duration_seconds: float,
) -> dict[str, Any]:
    if not start or not finish or start.get("device") != finish.get("device"):
        return {}
    reads = _counter_delta(start.get("reads_completed"), finish.get("reads_completed"))
    writes = _counter_delta(start.get("writes_completed"), finish.get("writes_completed"))
    read_ms = _counter_delta(start.get("read_milliseconds"), finish.get("read_milliseconds"))
    write_ms = _counter_delta(start.get("write_milliseconds"), finish.get("write_milliseconds"))
    read_bytes = (
        _counter_delta(start.get("sectors_read"), finish.get("sectors_read")) * SECTOR_BYTES
    )
    write_bytes = (
        _counter_delta(start.get("sectors_written"), finish.get("sectors_written")) * SECTOR_BYTES
    )
    io_ms = _counter_delta(start.get("io_milliseconds"), finish.get("io_milliseconds"))
    weighted_ms = _counter_delta(
        start.get("weighted_io_milliseconds"), finish.get("weighted_io_milliseconds")
    )
    operations = reads + writes
    result: dict[str, Any] = {
        "device": finish.get("device"),
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
        "read_operations": reads,
        "write_operations": writes,
    }
    if duration_seconds > 0:
        result.update(
            {
                "read_bytes_per_second": _round(read_bytes / duration_seconds),
                "write_bytes_per_second": _round(write_bytes / duration_seconds),
                "operations_per_second": _round(operations / duration_seconds),
                "util_percent": _round(io_ms / (duration_seconds * 1000) * 100),
                "average_queue_depth": _round(weighted_ms / (duration_seconds * 1000)),
            }
        )
    if operations:
        result["average_await_milliseconds"] = _round((read_ms + write_ms) / operations)
    return result


def _counter_section(
    start: dict[str, Any], finish: dict[str, Any], duration_seconds: float
) -> dict[str, int | float]:
    result: dict[str, int | float] = {
        key: _counter_delta(start.get(key), value) for key, value in finish.items()
    }
    if duration_seconds > 0:
        for key in ("received_bytes", "transmitted_bytes"):
            if key in result:
                result[f"{key}_per_second"] = _round(float(result[key]) / duration_seconds)
    return result


def _process_summary(
    samples: list[dict[str, Any]], duration_seconds: float
) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cpu_ticks": 0, "read_bytes": 0, "write_bytes": 0, "peak_rss_bytes": 0}
    )
    previous: dict[tuple[int, int], dict[str, Any]] | None = None
    for sample in samples:
        current = {
            (_integer(item.get("pid")), _integer(item.get("start_ticks"))): item
            for item in sample.get("processes", [])
            if isinstance(item, dict)
        }
        rss_by_name: dict[str, int] = defaultdict(int)
        for item in current.values():
            rss_by_name[str(item.get("name", "unknown"))] += _integer(item.get("rss_bytes"))
        for name, rss in rss_by_name.items():
            totals[name]["peak_rss_bytes"] = max(totals[name]["peak_rss_bytes"], rss)
        if previous is not None:
            for identity, item in current.items():
                name = str(item.get("name", "unknown"))
                initial = previous.get(identity)
                totals[name]["cpu_ticks"] += _counter_delta(
                    initial.get("cpu_ticks") if initial else 0, item.get("cpu_ticks")
                )
                totals[name]["read_bytes"] += _counter_delta(
                    initial.get("read_bytes") if initial else 0, item.get("read_bytes")
                )
                totals[name]["write_bytes"] += _counter_delta(
                    initial.get("write_bytes") if initial else 0, item.get("write_bytes")
                )
        previous = current
    clock_ticks = os.sysconf("SC_CLK_TCK")
    result: list[dict[str, Any]] = []
    for name, values in totals.items():
        cpu_seconds = values["cpu_ticks"] / clock_ticks
        result.append(
            {
                "name": name,
                "cpu_seconds": _round(cpu_seconds),
                "average_cpu_cores": _round(cpu_seconds / duration_seconds)
                if duration_seconds > 0
                else 0.0,
                "read_bytes": values["read_bytes"],
                "write_bytes": values["write_bytes"],
                "peak_rss_bytes": values["peak_rss_bytes"],
            }
        )
    result.sort(
        key=lambda item: (
            float(item["cpu_seconds"]),
            int(item["read_bytes"]) + int(item["write_bytes"]),
        ),
        reverse=True,
    )
    return result[:12]


def _elapsed_seconds(value: str) -> float | None:
    fields = value.strip().split(":")
    try:
        if len(fields) == 3:
            return int(fields[0]) * 3600 + int(fields[1]) * 60 + float(fields[2])
        if len(fields) == 2:
            return int(fields[0]) * 60 + float(fields[1])
        return float(fields[0])
    except (ValueError, IndexError):
        return None


def _read_time_report(path: Path) -> dict[str, Any]:
    raw = _read_text(path)
    if raw is None:
        return {"available": False}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.strip().rpartition(": ")
        if separator:
            values[key] = value
    elapsed_key = next((key for key in values if key.startswith("Elapsed (wall clock)")), "")
    elapsed = _elapsed_seconds(values.get(elapsed_key, "")) if elapsed_key else None
    result: dict[str, Any] = {
        "available": True,
        "user_seconds": float(values.get("User time (seconds)", 0)),
        "system_seconds": float(values.get("System time (seconds)", 0)),
        "elapsed_seconds": elapsed,
        "cpu_percent": _integer(values.get("Percent of CPU this job got", "").rstrip("%")),
        "maximum_rss_bytes": _integer(values.get("Maximum resident set size (kbytes)")) * 1024,
        "major_page_faults": _integer(values.get("Major (requiring I/O) page faults")),
        "minor_page_faults": _integer(values.get("Minor (reclaiming a frame) page faults")),
        "filesystem_inputs": _integer(values.get("File system inputs")),
        "filesystem_outputs": _integer(values.get("File system outputs")),
        "voluntary_context_switches": _integer(values.get("Voluntary context switches")),
        "involuntary_context_switches": _integer(values.get("Involuntary context switches")),
    }
    return result


def _load_samples(path: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line in (_read_text(path) or "").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            samples.append(value)
    return samples


def summarize(samples: list[dict[str, Any]], stage: str, time_report: Path) -> dict[str, Any]:
    if not samples:
        raise ValueError("performance sample log is empty")
    first = samples[0]
    last = samples[-1]
    duration = max(
        0.0,
        float(last.get("monotonic_seconds", 0)) - float(first.get("monotonic_seconds", 0)),
    )
    logical_cpus = max(1, _integer(last.get("logical_cpus"), 1))
    cpu = _percentages(first.get("cpu", {}), last.get("cpu", {}))
    if "busy_percent" in cpu:
        cpu["effective_busy_cores"] = _round(cpu["busy_percent"] * logical_cpus / 100)
    available = _gauge_summary(samples, "memory", "available_bytes")
    total = _gauge_summary(samples, "memory", "total_bytes")
    dirty = _gauge_summary(samples, "memory", "dirty_bytes")
    swap_total = _gauge_summary(samples, "memory", "swap_total_bytes")
    swap_free = _gauge_summary(samples, "memory", "swap_free_bytes")
    filesystem_used = _gauge_summary(samples, "filesystem", "used_bytes")
    filesystem_free = _gauge_summary(samples, "filesystem", "free_bytes")
    loads = [float(sample.get("load", {}).get("one_minute", 0)) for sample in samples]
    runnable = [_integer(sample.get("load", {}).get("runnable")) for sample in samples]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "started_at": first.get("timestamp"),
        "finished_at": last.get("timestamp"),
        "duration_seconds": _round(duration),
        "sample_count": len(samples),
        "logical_cpus": logical_cpus,
        "cpu": cpu,
        "cpu_per_core": _per_core_summary(
            first.get("cpu_per_core", {}), last.get("cpu_per_core", {})
        ),
        "cpu_frequency": _frequency_summary(samples),
        "cgroup_cpu": _cgroup_cpu_summary(
            first.get("cgroup_cpu", {}), last.get("cgroup_cpu", {}), duration
        ),
        "memory": {
            "peak_used_bytes": max(
                (maximum - free for maximum, free in zip(total, available, strict=False)),
                default=0,
            ),
            "minimum_available_bytes": min(available, default=0),
            "peak_dirty_bytes": max(dirty, default=0),
            "peak_swap_used_bytes": max(
                (maximum - free for maximum, free in zip(swap_total, swap_free, strict=False)),
                default=0,
            ),
        },
        "load": {
            "mean_one_minute": _round(sum(loads) / len(loads)) if loads else 0.0,
            "maximum_one_minute": _round(max(loads, default=0.0)),
            "maximum_runnable": max(runnable, default=0),
        },
        "pressure": _pressure_summary(
            first.get("pressure", {}), last.get("pressure", {}), duration
        ),
        "disk": _disk_summary(first.get("disk"), last.get("disk"), duration),
        "network": _counter_section(first.get("network", {}), last.get("network", {}), duration),
        "tcp": _counter_section(first.get("tcp", {}), last.get("tcp", {}), duration),
        "filesystem": {
            "starting_used_bytes": filesystem_used[0] if filesystem_used else 0,
            "ending_used_bytes": filesystem_used[-1] if filesystem_used else 0,
            "peak_used_bytes": max(filesystem_used, default=0),
            "minimum_free_bytes": min(filesystem_free, default=0),
        },
        "top_processes": _process_summary(samples, duration),
        "command": _read_time_report(time_report),
        "notes": [
            "process samples omit processes that both start and exit between sampling intervals",
            "GNU time command totals include all waited-for descendants and are authoritative",
        ],
    }
    return result


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _run_summarize(arguments: argparse.Namespace) -> None:
    summary = summarize(
        _load_samples(Path(arguments.samples)),
        arguments.stage,
        Path(arguments.time_report),
    )
    _write_json(Path(arguments.output), summary)


def _cpu_inventory() -> dict[str, Any]:
    raw = _read_text(Path("/proc/cpuinfo")) or ""
    records = [item for item in raw.split("\n\n") if item.strip()]
    parsed: list[dict[str, str]] = []
    for record in records:
        values: dict[str, str] = {}
        for line in record.splitlines():
            name, separator, value = line.partition(":")
            if separator:
                values[name.strip()] = value.strip()
        parsed.append(values)
    model = next((item.get("model name") for item in parsed if item.get("model name")), None)
    physical_cores = {
        (item.get("physical id"), item.get("core id"))
        for item in parsed
        if item.get("physical id") is not None and item.get("core id") is not None
    }
    frequencies = [
        float(frequency) for item in parsed if (frequency := item.get("cpu MHz")) is not None
    ]
    return {
        "logical_cpus": os.cpu_count() or 1,
        "physical_cores_observed": len(physical_cores) or None,
        "model": model,
        "frequency_mhz_min": _round(min(frequencies)) if frequencies else None,
        "frequency_mhz_max": _round(max(frequencies)) if frequencies else None,
        "frequency_mhz_mean": _round(sum(frequencies) / len(frequencies)) if frequencies else None,
    }


def _block_inventory(device: str | None) -> dict[str, Any]:
    if device is None:
        return {}
    device_path = Path("/sys/class/block") / device
    try:
        resolved = device_path.resolve(strict=True)
    except OSError:
        return {"device": device}
    base = resolved
    while base.parent != base and not (base / "queue").is_dir():
        base = base.parent
    queue = base / "queue"
    return {
        "device": device,
        "base_device": base.name,
        "model": _read_text(base / "device/model"),
        "rotational": _integer(_read_text(queue / "rotational")),
        "scheduler": _read_text(queue / "scheduler"),
        "logical_block_bytes": _integer(_read_text(queue / "logical_block_size")),
        "physical_block_bytes": _integer(_read_text(queue / "physical_block_size")),
        "max_request_kib": _integer(_read_text(queue / "max_sectors_kb")),
        "queue_requests": _integer(_read_text(queue / "nr_requests")),
        "size_bytes": _integer(_read_text(base / "size")) * SECTOR_BYTES,
    }


def _network_inventory() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    root = Path("/sys/class/net")
    try:
        interfaces = sorted(root.iterdir())
    except OSError:
        return result
    for interface in interfaces:
        if interface.name == "lo":
            continue
        driver: str | None = None
        with suppress(OSError):
            driver = (interface / "device/driver").resolve(strict=True).name
        speed = _integer(_read_text(interface / "speed"), -1)
        result.append(
            {
                "name": interface.name,
                "driver": driver,
                "speed_mbps": speed if speed >= 0 else None,
                "mtu": _integer(_read_text(interface / "mtu")),
                "operstate": _read_text(interface / "operstate"),
            }
        )
    return result


def _cgroup_inventory() -> dict[str, str | None]:
    root = Path("/sys/fs/cgroup")
    return {
        name: _read_text(root / name)
        for name in ("cpu.max", "cpuset.cpus.effective", "memory.max", "memory.high", "pids.max")
    }


def _run_inventory(arguments: argparse.Namespace) -> None:
    data_root = Path(arguments.data_root).absolute()
    memory = _read_memory()
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "cpu": _cpu_inventory(),
        "memory": {
            "total_bytes": memory.get("total_bytes", 0),
            "swap_total_bytes": memory.get("swap_total_bytes", 0),
        },
        "cgroup": _cgroup_inventory(),
        "filesystem": _read_filesystem(data_root),
        "block": _block_inventory(_root_block_device(data_root)),
        "network": _network_inventory(),
        "selected_workers": _integer(os.environ.get("GAME_DOWNLOADER_DOWNLOAD_WORKERS"), default=0),
        "privacy": {
            "addresses_recorded": False,
            "mac_addresses_recorded": False,
            "process_command_lines_recorded": False,
        },
    }
    _write_json(Path(arguments.output), inventory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Linux snapshot build performance metrics")
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("--stage", required=True)
    monitor.add_argument("--data-root", required=True)
    monitor.add_argument("--samples", required=True)
    monitor.add_argument("--interval-seconds", type=float, default=5.0)
    monitor.set_defaults(handler=_run_monitor)

    summarize_parser = subparsers.add_parser("summarize")
    summarize_parser.add_argument("--stage", required=True)
    summarize_parser.add_argument("--samples", required=True)
    summarize_parser.add_argument("--time-report", required=True)
    summarize_parser.add_argument("--output", required=True)
    summarize_parser.set_defaults(handler=_run_summarize)

    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--data-root", required=True)
    inventory.add_argument("--output", required=True)
    inventory.set_defaults(handler=_run_inventory)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if getattr(arguments, "interval_seconds", 1) <= 0:
        raise SystemExit("interval-seconds must be positive")
    arguments.handler(arguments)


if __name__ == "__main__":
    main()

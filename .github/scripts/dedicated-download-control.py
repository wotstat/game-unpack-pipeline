"""Temporary opt-in A/B control; remove after the dedicated experiment."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from game_downloader.models import AcquisitionPlan, Stage, StageResult
from game_downloader.workspace import Workspace

BASELINE = "5227fbf076d1e52b2e93e27b2a5d5d168a8edba3"
HELPER = "16b50da4368c21eac2f6fd5d5d58f86e6a461675"
CASE_TIMEOUT = 480


def load_helper(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("dedicated_worker_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the historical comparison helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def terminate(process: subprocess.Popen[bytes]) -> None:
    # GNU time can exit before its descendants. Stop the entire private session
    # before deleting a case's files or starting the next control.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def worker(args: argparse.Namespace) -> int:
    try:
        helper = load_helper(args.helper)
        helper.worker(args)
        return 0
    except Exception as error:
        # Historical transport errors can contain URLs. Keep diagnostics bounded.
        print(f"CONTROL WORKER FAILED: {type(error).__name__}", flush=True)
        return 1


@contextmanager
def pinned_sources(helper: Any, plan: AcquisitionPlan, reports: Path) -> Iterator[None]:
    hosts = {
        urlsplit(url).hostname
        for part in plan.parts
        for artifact in part.artifacts
        for url in artifact.source_urls
    }
    pins_path = Path(os.environ["RUNNER_TEMP"]) / "dedicated-control-dns.json"
    if pins_path.exists():
        pins = json.loads(pins_path.read_text())
    else:
        pins = {}
        for host in sorted(hosts, key=str):
            if host is None:
                raise RuntimeError("Missing CDN hostname")
            records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            preferred = next((r for r in records if r[0] == socket.AF_INET), records[0])
            pins[host] = str(preferred[4][0])
        pins_path.write_text(json.dumps(pins))
    if set(pins) != hosts:
        raise RuntimeError("CDN host set changed between controls")
    marker = "gup-benchmark-" + secrets.token_hex(8)
    arguments = ["sudo", "python3", "-c", helper._HOSTS_SCRIPT, "/etc/hosts"]
    subprocess.run([*arguments, "add", marker, json.dumps(pins)], check=True)
    try:
        helper.write_json(
            reports / "pinned-source-dns.json",
            {
                host: {"address_sha256": hashlib.sha256(address.encode()).hexdigest()}
                for host, address in pins.items()
            },
        )
        yield
    finally:
        subprocess.run([*arguments, "remove", marker, json.dumps(pins)], check=True)


def compare(root: Path, reports: Path, selected_case: int) -> None:
    root.mkdir(mode=0o700)
    reports.mkdir(parents=True, exist_ok=True)
    helper_path = root / "historical-helpers.py"
    helper_path.write_bytes(
        subprocess.check_output(["git", "show", f"{HELPER}:scripts/benchmark_download.py"])
    )
    helper = load_helper(helper_path)
    for revision in (BASELINE, HELPER):
        subprocess.run(["git", "merge-base", "--is-ancestor", revision, "HEAD"], check=True)
    archive = subprocess.check_output(["git", "archive", BASELINE, "src"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(root / "historical", filter="data")
    baseline_source = (root / "historical/src").resolve()
    metadata = Workspace(Path(os.environ["GAME_DOWNLOADER_DATA_ROOT"]))
    stage = StageResult.model_validate_json(
        metadata.read_bytes(
            metadata.stage_path(os.environ["GAME_DOWNLOADER_RUN_ID"], Stage.PLAN_ACQUISITION)
            / "result.json"
        )
    )
    full_plan = AcquisitionPlan.model_validate(stage.payload)
    selected = sorted(
        (artifact for part in full_plan.parts for artifact in part.artifacts),
        key=lambda artifact: (-artifact.size, artifact.artifact_id),
    )[:16]
    plan = helper.subset_plan(full_plan, selected)
    plan_path = root / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    helper.write_json(
        reports / "manifest.json",
        {
            "baseline": BASELINE,
            "artifact_workers": 16,
            "case_timeout_seconds": CASE_TIMEOUT,
            "artifacts": [{"id": item.artifact_id, "size": item.size} for item in selected],
        },
    )
    results_path = reports / "results.json"
    results: list[dict[str, Any]] = (
        json.loads(results_path.read_text()) if results_path.exists() else []
    )
    reference = next((r["digests"] for r in results if "digests" in r), None)
    metrics = Path(".github/scripts/performance-metrics.py").resolve()
    # Each tool gets both the earlier and later positions in a pair.
    with pinned_sources(helper, plan, reports):
        for index, method in enumerate(
            ("downloader", "downloader-baseline", "downloader-baseline", "downloader"), 1
        ):
            if index != selected_case:
                continue
            label = f"{index}-{method}"
            work = root / label
            result_path = reports / f"{label}-result.json"
            samples = reports / f"{label}-samples.jsonl"
            time_path = reports / f"{label}-time.log"
            subprocess.run(["sudo", "sync"], check=True, timeout=60)
            subprocess.run(
                ["sudo", "tee", "/proc/sys/vm/drop_caches"],
                input=b"3\n",
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=10,
            )
            environment = dict(os.environ)
            environment["BENCHMARK_BASELINE_WORKERS"] = "16"
            if method == "downloader-baseline":
                environment["PYTHONPATH"] = str(baseline_source)
                environment["BENCHMARK_BASELINE_SOURCE"] = str(baseline_source)
            monitor = subprocess.Popen(
                [
                    sys.executable,
                    str(metrics),
                    "monitor",
                    "--stage",
                    label,
                    "--data-root",
                    str(root),
                    "--samples",
                    str(samples),
                    "--interval-seconds",
                    "1",
                ]
            )
            process: subprocess.Popen[bytes] | None = None
            print(f"CONTROL START {label}: {sum(item.size for item in selected)} bytes", flush=True)
            try:
                with (reports / f"{label}.log").open("wb") as output:
                    process = subprocess.Popen(
                        [
                            "/usr/bin/time",
                            "--verbose",
                            f"--output={time_path}",
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "worker",
                            "--helper",
                            str(helper_path),
                            "--method",
                            method,
                            "--plan",
                            str(plan_path),
                            "--work",
                            str(work),
                            "--result",
                            str(result_path),
                        ],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        start_new_session=True,
                    )
                    try:
                        process.wait(timeout=CASE_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        terminate(process)
            finally:
                if process is not None:
                    terminate(process)
                monitor.terminate()
                monitor.wait(timeout=15)
            subprocess.run(
                [
                    sys.executable,
                    str(metrics),
                    "summarize",
                    "--stage",
                    label,
                    "--samples",
                    str(samples),
                    "--time-report",
                    str(time_path),
                    "--output",
                    str(reports / f"{label}-performance.json"),
                ],
                check=True,
            )
            measurement: dict[str, Any] = {
                "method": method,
                "returncode": process.returncode if process else None,
            }
            if result_path.exists():
                measurement.update(json.loads(result_path.read_text()))
                if reference is None:
                    reference = measurement["digests"]
                elif reference != measurement["digests"]:
                    raise RuntimeError("The tools downloaded different bytes")
                pins = json.loads((reports / "pinned-source-dns.json").read_text())
                for host, records in measurement["source_dns"].items():
                    if host in pins and (
                        not records
                        or any(
                            record["address_sha256"] != pins[host]["address_sha256"]
                            for record in records
                        )
                    ):
                        raise RuntimeError("A worker did not use the pinned CDN endpoint")
                print(
                    f"CONTROL RESULT {label}: {measurement['mib_per_second']:.1f} MiB/s; "
                    "source hashes, SHA-256 and fsync completed",
                    flush=True,
                )
            else:
                print(f"CONTROL INCOMPLETE {label}; raw progress and metrics retained", flush=True)
            results.append({"case": label, **measurement})
            helper.write_json(results_path, results)
            if work.exists():
                shutil.rmtree(work)
    # Test disk capacity after the network controls, so it cannot consume burst
    # credits before one of the A/B passes. O_DIRECT excludes the page cache.
    if selected_case != 4:
        return
    disk = root / "disk-control"
    disk.mkdir()
    subprocess.run(
        [
            "/usr/bin/fio",
            "--name=parallel-write",
            f"--directory={disk}",
            "--ioengine=psync",
            "--rw=write",
            "--bs=1M",
            "--size=512M",
            "--numjobs=16",
            "--thread=1",
            "--direct=1",
            "--end_fsync=1",
            "--group_reporting",
            "--time_based=1",
            "--runtime=20",
            "--output-format=json",
            f"--output={reports / 'disk-control.json'}",
        ],
        check=True,
        timeout=90,
    )


def main() -> int:
    def interrupt(_number: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    selected = commands.add_parser("worker")
    selected.add_argument("--helper", type=Path, required=True)
    selected.add_argument("--method", required=True)
    selected.add_argument("--plan", type=Path, required=True)
    selected.add_argument("--work", type=Path, required=True)
    selected.add_argument("--result", type=Path, required=True)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--case", type=int, choices=(1, 2, 3, 4), required=True)
    args = parser.parse_args()
    if args.command == "worker":
        return worker(args)
    with tempfile.TemporaryDirectory(
        prefix="dedicated-control-", dir=os.environ["RUNNER_TEMP"]
    ) as temporary:
        compare(
            Path(temporary) / "work",
            Path(os.environ["GAME_DOWNLOADER_REPORT_DIR"]) / "dedicated",
            args.case,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare curl, aria2 and the production downloader against one pinned file set."""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from game_downloader import delivery
from game_downloader.delivery import ArtifactDownloader, DownloadPolicy, _hash_file
from game_downloader.models import AcquisitionArtifact, AcquisitionPlan, Stage, StageResult
from game_downloader.workspace import Workspace

MIB = 1024 * 1024
METHODS = ("curl", "aria2", "aria2-split", "downloader")
DIAGNOSTIC_METHODS = (
    "curl",
    "aria2",
    "aria2-split",
    "downloader",
    "downloader-4",
    "downloader-1",
)

_HOSTS_SCRIPT = r"""
import fcntl, ipaddress, json, re, sys
path, mode, marker, raw = sys.argv[1:]
pins = json.loads(raw)
assert re.fullmatch(r"gup-benchmark-[a-f0-9]{16}", marker)
for host, address in pins.items():
    assert re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)
    assert ipaddress.ip_address(address).is_global
with open(path, "r+") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    lines = stream.read().splitlines(keepends=True)
    if mode == "add":
        existing = {name for line in lines for name in line.split("#", 1)[0].split()[1:]}
        assert not existing.intersection(pins), "CDN already has a hosts override"
        lines += ["\n"] + [f"{address} {host} # {marker}\n" for host, address in pins.items()]
    else:
        assert mode == "remove"
        lines = [line for line in lines if not line.rstrip().endswith("# " + marker)]
    stream.seek(0)
    stream.write("".join(lines))
    stream.truncate()
    stream.flush()
"""


@contextmanager
def pinned_game_sources(plan: AcquisitionPlan, reports: Path) -> Iterator[None]:
    hosts = sorted(
        {
            host
            for part in plan.parts
            for artifact in part.artifacts
            for url in artifact.source_urls
            if (host := urlsplit(url).hostname) is not None
        }
    )
    pins: dict[str, str] = {}
    for host in hosts:
        if host is None or re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) is None:
            raise ValueError("Invalid game CDN hostname")
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        preferred = next(
            (str(item[4][0]) for item in records if item[0] == socket.AF_INET),
            str(records[0][4][0]),
        )
        if not ipaddress.ip_address(preferred).is_global:
            raise ValueError("Game CDN did not resolve to a public endpoint")
        pins[host] = preferred
    marker = "gup-benchmark-" + secrets.token_hex(8)
    arguments = ["sudo", "python3", "-c", _HOSTS_SCRIPT, "/etc/hosts"]
    subprocess.run([*arguments, "add", marker, json.dumps(pins)], check=True)
    try:
        write_json(
            reports / "pinned-source-dns.json",
            {
                host: {"address_sha256": hashlib.sha256(address.encode()).hexdigest()}
                for host, address in pins.items()
            },
        )
        yield
    finally:
        # Remove only our own lines, preserving any unrelated changes to this throwaway VM.
        subprocess.run([*arguments, "remove", marker, json.dumps(pins)], check=True)


class _PayloadServer(ThreadingHTTPServer):
    payloads: dict[str, tuple[bytes, str]]


class _PayloadHandler(BaseHTTPRequestHandler):
    server: _PayloadServer
    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_GET(self) -> None:
        self._serve(body=True)

    def _serve(self, *, body: bool) -> None:
        # Exact in-memory keys only: no filesystem paths or credentials can be served.
        item = self.server.payloads.get(self.path)
        if item is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload, etag = item
        start, end, status = 0, len(payload) - 1, 200
        raw_range = self.headers.get("Range")
        if raw_range is not None and self.headers.get("If-Range", etag) == etag:
            matched = re.fullmatch(r"bytes=(\d{1,20})-(\d{0,20})", raw_range)
            if matched is not None:
                start = int(matched[1])
                end = min(int(matched[2]), end) if matched[2] else end
            if matched is None or not 0 <= start <= end < len(payload):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        self.end_headers()
        if body:
            with suppress(OSError):
                self.wfile.write(memoryview(payload)[start : end + 1])

    def log_message(self, _format: str, *args: object) -> None:
        pass


@contextmanager
def loopback_source(
    payloads: dict[str, tuple[bytes, str]], directory: Path
) -> Iterator[tuple[str, Path]]:
    directory.mkdir(mode=0o700)
    certificate, key = directory / "certificate.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = _PayloadServer(("127.0.0.1", 0), _PayloadHandler)
    server.payloads = payloads
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", certificate
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def selected_plan(plan: AcquisitionPlan, case: str) -> AcquisitionPlan:
    artifacts = [artifact for part in plan.parts for artifact in part.artifacts]
    if case == "small":
        candidates = [a for a in artifacts if 64 * MIB <= a.size < 256 * MIB]
        selected = sorted(candidates, key=lambda a: (abs(a.size - 128 * MIB), a.artifact_id))[:16]
    else:
        candidates = [a for a in artifacts if a.size >= 512 * MIB]
        selected = sorted(candidates, key=lambda a: (a.size, a.artifact_id))[:4]
    if len(selected) < (16 if case == "small" else 4):
        raise ValueError(f"Not enough pinned artifacts for {case}")
    return subset_plan(plan, selected)


def subset_plan(plan: AcquisitionPlan, selected: list[AcquisitionArtifact]) -> AcquisitionPlan:
    selected_ids = {a.artifact_id for a in selected}
    parts = tuple(
        part.model_copy(
            update={"artifacts": tuple(a for a in part.artifacts if a.artifact_id in selected_ids)}
        )
        for part in plan.parts
    )
    total = sum(a.size for a in selected)
    descriptor_bytes = plan.disk_space.descriptor_bytes
    reduced = plan.model_copy(
        update={
            "parts": parts,
            "disk_space": plan.disk_space.model_copy(
                update={
                    "download_bytes": total,
                    "assembled_bytes": total,
                    "reserve_bytes": 0,
                    "required_free_bytes": descriptor_bytes + 2 * total,
                }
            ),
        }
    )
    return AcquisitionPlan.model_validate(reduced.model_dump())


def artifact_name(artifact: AcquisitionArtifact) -> str:
    return artifact.artifact_id.removeprefix("sha256:") + ".bin"


def verify_file(item: tuple[AcquisitionArtifact, Path]) -> tuple[str, str]:
    artifact, path = item
    if path.stat().st_size != artifact.size:
        raise ValueError("Benchmark received an unexpected file size")
    sha256, source_verified = _hash_file(path, artifact.source_hash)
    if artifact.source_hash is not None and not source_verified:
        raise ValueError("Benchmark file failed its declared source hash")
    return artifact.artifact_id, sha256


def sync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def source_dns(artifacts: tuple[AcquisitionArtifact, ...]) -> dict[str, Any]:
    hosts = sorted(
        {
            host
            for a in artifacts
            for url in a.source_urls
            if (host := urlsplit(url).hostname) is not None
        }
    )
    result: dict[str, Any] = {}
    for host in hosts:
        try:
            records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            result[host] = {"unavailable": True}
            continue
        result[host] = [
            {"family": family, "address_sha256": hashlib.sha256(address.encode()).hexdigest()}
            for family, address in sorted(
                {(family, str(address[0])) for family, _, _, _, address in records}
            )
        ]
    return result


def curl_file(item: tuple[AcquisitionArtifact, Path]) -> None:
    artifact, directory = item
    subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "15",
            "--max-time",
            "300",
            "--output",
            str(directory / artifact_name(artifact)),
            "--url",
            artifact.source_urls[0],
        ],
        check=True,
        timeout=310,
    )


def worker(args: argparse.Namespace) -> None:
    plan = AcquisitionPlan.model_validate_json(args.plan.read_bytes())
    artifacts = tuple(a for part in plan.parts for a in part.artifacts)
    directory: Path = args.work
    directory.mkdir(parents=True, exist_ok=False)
    dns = source_dns(artifacts)
    started = time.monotonic()
    details: dict[str, Any] = {}
    synced_at: float | None = None
    if args.method.startswith("downloader"):
        workers = {
            "downloader-4": 4,
            "downloader-1": 1,
            "downloader-baseline": int(os.environ.get("BENCHMARK_BASELINE_WORKERS", "4")),
        }.get(args.method, 16)
        if args.method == "downloader-baseline":
            baseline = Path(os.environ["BENCHMARK_BASELINE_SOURCE"]).resolve()
            if not Path(delivery.__file__).resolve().is_relative_to(baseline):
                raise ValueError("Baseline worker did not import the historical implementation")
        result = ArtifactDownloader(
            DownloadPolicy(max_workers=workers),
            progress_observer=lambda msg: print(msg, flush=True),
        ).download(plan, Workspace(directory))
        digests = {item.artifact.artifact_id: item.blob_sha256 for item in result.artifacts}
        details = {
            "artifact_workers": workers,
            "parallel_range_artifacts": sum(
                item.transport.parallel_segments > 1 for item in result.artifacts
            ),
            "parallel_range_fallbacks": sum(
                len(item.transport.parallel_range_fallbacks) for item in result.artifacts
            ),
            "reused_artifacts": result.reused_artifacts,
        }
        if result.reused_artifacts:
            raise ValueError("A benchmark must not reuse downloaded files")
        transferred_at = time.monotonic()
    else:
        if args.method == "curl":
            with ThreadPoolExecutor(max_workers=16) as executor:
                list(executor.map(curl_file, ((a, directory) for a in artifacts)))
        else:
            input_path = directory / "aria2-input.txt"
            input_path.write_text(
                "".join(f"{a.source_urls[0]}\n  out={artifact_name(a)}\n" for a in artifacts)
            )
            split = "16" if args.method == "aria2-split" else "1"
            subprocess.run(
                [
                    "aria2c",
                    "--no-conf=true",
                    "--async-dns=false",
                    *(
                        [f"--ca-certificate={os.environ['BENCHMARK_CA_FILE']}"]
                        if os.environ.get("BENCHMARK_CA_FILE")
                        else []
                    ),
                    "--max-concurrent-downloads=16",
                    f"--split={split}",
                    f"--max-connection-per-server={split}",
                    "--min-split-size=128M",
                    "--file-allocation=none",
                    "--summary-interval=0",
                    "--download-result=hide",
                    "--console-log-level=warn",
                    "--max-tries=4",
                    "--connect-timeout=15",
                    "--timeout=60",
                    "--auto-file-renaming=false",
                    f"--dir={directory}",
                    f"--input-file={input_path}",
                ],
                check=True,
                timeout=600,
            )
        transferred_at = time.monotonic()
        # Match the downloader's durable payload writes instead of deleting dirty pages.
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(sync_file, (directory / artifact_name(a) for a in artifacts)))
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        synced_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=4) as executor:
            digests = dict(
                executor.map(verify_file, ((a, directory / artifact_name(a)) for a in artifacts))
            )
    finished = time.monotonic()
    if synced_at is None:
        synced_at = transferred_at
    total = sum(a.size for a in artifacts)
    write_json(
        args.result,
        {
            "method": args.method,
            "bytes": total,
            "files": len(artifacts),
            "transfer_seconds": transferred_at - started,
            "post_transfer_fsync_seconds": synced_at - transferred_at,
            "validation_seconds": finished - synced_at,
            "elapsed_seconds": finished - started,
            "mib_per_second": total / MIB / (finished - started),
            "digests": digests,
            "source_dns": dns,
            **details,
        },
    )


def benchmark(args: argparse.Namespace) -> None:
    root: Path = args.work
    reports: Path = args.reports
    root.mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=True)
    baseline_source: Path | None = None
    if args.baseline_sha:
        if re.fullmatch(r"[a-f0-9]{40}", args.baseline_sha) is None:
            raise ValueError("Baseline must be a full commit SHA")
        # Accept only source code already present in this main branch's history.
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", args.baseline_sha, "HEAD"], check=True
        )
        archive = subprocess.check_output(
            ["git", "archive", "--format=tar", args.baseline_sha, "src"]
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(root / "baseline", filter="data")
        baseline_source = root / "baseline/src"
        write_json(
            reports / "baseline.json",
            {"commit": args.baseline_sha, "artifact_workers": args.baseline_workers},
        )
    metadata_root = root / "metadata"
    resolved = subprocess.run(
        [
            "game-downloader",
            "run",
            "--target",
            "wot-eu",
            "--client-type",
            "sd",
            "--languages",
            "ALL",
            "--until",
            "plan-acquisition",
            "--skip-check",
            "--json",
            "--data-root",
            str(metadata_root),
        ],
        stdout=subprocess.PIPE,
        check=True,
        timeout=300,
    )
    report = json.loads(resolved.stdout)
    workspace = Workspace(metadata_root)
    stage_result = StageResult.model_validate_json(
        workspace.read_bytes(
            workspace.stage_path(report["run_id"], Stage.PLAN_ACQUISITION) / "result.json"
        )
    )
    full_plan = AcquisitionPlan.model_validate(stage_result.payload)
    results: list[dict[str, Any]] = []
    plans: dict[str, AcquisitionPlan] = {}
    references: dict[str, dict[str, str]] = {}
    choices: tuple[str, ...] = DIAGNOSTIC_METHODS if args.diagnose else METHODS
    if baseline_source is not None:
        choices += ("downloader-baseline",)

    def measure(
        plan: AcquisitionPlan,
        case: str,
        work_root: Path,
        choices: tuple[str, ...],
        repeats: int,
        reference: dict[str, str] | None = None,
        environment: dict[str, str] | None = None,
    ) -> dict[str, str]:
        plan_path = root / f"{case}-plan.json"
        plan_path.write_text(plan.model_dump_json())
        for repeat in range(repeats):
            # Rotate order so a warmed CDN or a changing route cannot consistently favor one tool.
            methods = choices[repeat % len(choices) :] + choices[: repeat % len(choices)]
            for method in methods:
                label = f"{case}-{repeat + 1}-{method}"
                work = work_root / label
                samples = reports / f"{label}-samples.jsonl"
                monitor = subprocess.Popen(
                    [
                        sys.executable,
                        ".github/scripts/performance-metrics.py",
                        "monitor",
                        "--stage",
                        label,
                        "--data-root",
                        str(work_root),
                        "--samples",
                        str(samples),
                        "--interval-seconds",
                        "1",
                    ]
                )
                result_path = reports / f"{label}-result.json"
                time_path = reports / f"{label}-time.log"
                print(f"BENCHMARK START {label}", flush=True)
                child_environment = dict(os.environ)
                child_environment["BENCHMARK_BASELINE_WORKERS"] = str(args.baseline_workers)
                child_environment.update(environment or {})
                if method == "downloader-baseline":
                    child_environment["PYTHONPATH"] = str(baseline_source)
                    child_environment["BENCHMARK_BASELINE_SOURCE"] = str(baseline_source)
                try:
                    with (reports / f"{label}.log").open("w") as output:
                        subprocess.run(
                            [
                                "/usr/bin/time",
                                "--verbose",
                                f"--output={time_path}",
                                sys.executable,
                                "-m",
                                "scripts.benchmark_download",
                                "worker",
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
                            env=child_environment,
                            check=True,
                            timeout=900,
                        )
                finally:
                    monitor.terminate()
                    monitor.wait(timeout=15)
                subprocess.run(
                    [
                        sys.executable,
                        ".github/scripts/performance-metrics.py",
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
                measurement = json.loads(result_path.read_text())
                if case in {"small", "large"}:
                    pins = json.loads((reports / "pinned-source-dns.json").read_text())
                    for host, records in measurement["source_dns"].items():
                        if host in pins and (
                            not isinstance(records, list)
                            or not records
                            or any(
                                record["address_sha256"] != pins[host]["address_sha256"]
                                for record in records
                            )
                        ):
                            raise ValueError(
                                "A benchmark worker did not use the pinned CDN resolver"
                            )
                if reference is None:
                    reference = measurement["digests"]
                elif reference != measurement["digests"]:
                    raise ValueError("The tools did not download identical bytes")
                results.append({"case": case, "repeat": repeat + 1, **measurement})
                write_json(reports / "results.json", results)
                print(
                    f"BENCHMARK RESULT {label}: {measurement['mib_per_second']:.1f} MiB/s, "
                    f"{measurement['elapsed_seconds']:.2f}s, verified identical bytes",
                    flush=True,
                )
                shutil.rmtree(work)
        assert reference is not None
        return reference

    with pinned_game_sources(full_plan, reports):
        for case in ("small", "large"):
            plan = selected_plan(full_plan, case)
            artifacts = [a for part in plan.parts for a in part.artifacts]
            # The report identifies files without publishing redirect queries or credentials.
            write_json(
                reports / f"{case}-manifest.json",
                [
                    {
                        "artifact_id": a.artifact_id,
                        "path": a.path.utf8,
                        "size": a.size,
                        "source_hosts": [urlsplit(url).hostname for url in a.source_urls],
                    }
                    for a in artifacts
                ],
            )
            plan_path = root / f"{case}-plan.json"
            plan_path.write_text(plan.model_dump_json())
            plans[case] = plan
            references[case] = measure(plan, case, root, choices, args.repeats)
        if args.loopback_control:
            controls: tuple[str, ...] = ("curl", "aria2-split", "downloader")
            if baseline_source is not None:
                controls += ("downloader-baseline",)
            for case, plan in plans.items():
                print(f"BENCHMARK PREPARE {case} loopback sources", flush=True)
                source_workspace = Workspace(root / f"loopback-source-{case}")
                source_result = ArtifactDownloader(DownloadPolicy(max_workers=16)).download(
                    plan, source_workspace
                )
                if {
                    x.artifact.artifact_id: x.blob_sha256 for x in source_result.artifacts
                } != references[case]:
                    raise ValueError("Loopback sources differ from the verified CDN files")
                payloads = {
                    "/" + artifact_name(x.artifact): (
                        source_workspace.blobs.path_for(x.blob_sha256).read_bytes(),
                        '"' + x.blob_sha256 + '"',
                    )
                    for x in source_result.artifacts
                }
                with loopback_source(payloads, root / f"loopback-tls-{case}") as (url, certificate):
                    parts = tuple(
                        part.model_copy(
                            update={
                                "artifacts": tuple(
                                    a.model_copy(
                                        update={"source_urls": (url + "/" + artifact_name(a),)}
                                    )
                                    for a in part.artifacts
                                )
                            }
                        )
                        for part in plan.parts
                    )
                    local_plan = AcquisitionPlan.model_validate(
                        plan.model_copy(update={"parts": parts}).model_dump()
                    )
                    environment = {
                        "SSL_CERT_FILE": str(certificate),
                        "CURL_CA_BUNDLE": str(certificate),
                        "BENCHMARK_CA_FILE": str(certificate),
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                    }
                    measure(
                        local_plan,
                        f"{case}-loopback-disk",
                        root,
                        controls,
                        min(2, args.repeats),
                        references[case],
                        environment,
                    )
                    with tempfile.TemporaryDirectory(
                        prefix="gup-http-control-", dir="/dev/shm"
                    ) as memory_root:
                        if (
                            shutil.disk_usage(memory_root).free
                            < local_plan.disk_space.required_free_bytes
                        ):
                            raise ValueError("Not enough tmpfs capacity for the RAM control")
                        measure(
                            local_plan,
                            f"{case}-loopback-ram",
                            Path(memory_root),
                            controls,
                            min(2, args.repeats),
                            references[case],
                            environment,
                        )
                payloads.clear()
                shutil.rmtree(source_workspace.root)
    summary = [
        "| File set | Run | Tool | MiB/s including fsync and validation | Seconds |",
        "| --- | ---: | --- | ---: | ---: |",
    ]
    summary.extend(
        f"| {r['case']} | {r['repeat']} | {r['method']} | "
        f"{r['mib_per_second']:.1f} | {r['elapsed_seconds']:.2f} |"
        for r in results
    )
    rendered = "\n".join(summary) + "\n"
    (reports / "summary.md").write_text(rendered)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(rendered)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--work", type=Path, required=True)
    run.add_argument("--reports", type=Path, required=True)
    run.add_argument("--repeats", type=int, choices=(1, 2, 3), default=2)
    run.add_argument("--diagnose", action="store_true")
    run.add_argument("--loopback-control", action="store_true")
    run.add_argument("--baseline-sha", default="")
    run.add_argument("--baseline-workers", type=int, choices=(1, 4, 16), default=4)
    run.set_defaults(handler=benchmark)
    child = subparsers.add_parser("worker")
    child.add_argument(
        "--method",
        choices=tuple(dict.fromkeys(METHODS + DIAGNOSTIC_METHODS + ("downloader-baseline",))),
        required=True,
    )
    child.add_argument("--plan", type=Path, required=True)
    child.add_argument("--work", type=Path, required=True)
    child.add_argument("--result", type=Path, required=True)
    child.set_defaults(handler=worker)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

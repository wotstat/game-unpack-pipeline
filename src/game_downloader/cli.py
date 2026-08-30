from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

import click
from pydantic import ValidationError

from game_downloader import __version__
from game_downloader._json import JsonObject, canonical_json_bytes
from game_downloader.acquisition import AcquisitionPolicy, create_acquisition_implementation
from game_downloader.client_tree import create_assemble_client_implementation
from game_downloader.contracts import ContractRegistry, ContractValidationError
from game_downloader.delivery import (
    DownloadPolicy,
    VerificationPolicy,
    create_download_implementation,
    create_verify_implementation,
)
from game_downloader.models import (
    STAGE_ORDER,
    ClientType,
    RunReport,
    RunRequest,
    Stage,
    StageSummary,
)
from game_downloader.pipeline import (
    Pipeline,
    PipelineInterrupted,
    PipelineRunError,
    StageExecutionError,
)
from game_downloader.readable import (
    FfdecTransformer,
    ReadablePolicy,
    Uncompyle6Transformer,
    create_readable_implementations,
)
from game_downloader.snapshot import (
    Snapshot,
    SnapshotVerificationError,
    SnapshotVerificationPolicy,
    create_snapshot_implementation,
)
from game_downloader.vfs import (
    VfsPolicy,
    create_index_vfs_implementation,
    create_materialize_vfs_implementation,
)
from game_downloader.wgus import (
    HttpxTransport,
    ResolvePolicy,
    TargetConfig,
    TargetConfigurationError,
    TargetRegistry,
    WgusResolver,
    create_resolve_implementation,
)
from game_downloader.workspace import Workspace, WorkspaceError

EXIT_VALIDATION = 3
EXIT_TRANSIENT = 4
EXIT_CORRUPT = 5
EXIT_UNSUPPORTED = 6
EXIT_INTERNAL = 70


class CliFailure(click.ClickException):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.__dict__["exit_code"] = exit_code


def _default_data_root() -> Path:
    return Path(os.environ.get("GAME_DOWNLOADER_DATA_ROOT", "/data")).absolute()


def _workspace(data_root: Path) -> Workspace:
    return Workspace(data_root.absolute())


def _actionscript_workers(logical_workers: int) -> int:
    # A single FFDec JVM used about seven cores in the production profile. Keep
    # roughly eight logical CPUs per JVM so parallel libraries do not oversubscribe
    # the runner, while still allowing the 16-vCPU runner to process two at once.
    return max(1, min(4, logical_workers // 8))


def _verification_workers(logical_workers: int) -> int:
    # Archive integrity checks are independent and mostly run in zlib/native code. Four
    # workers keep enough CPU and I/O headroom for the rest of a 16-vCPU ephemeral runner.
    return max(1, min(4, logical_workers))


def _build_pipeline(
    workspace: Workspace,
    target: TargetConfig,
    *,
    disk_reserve_bytes: int,
    workers: int,
) -> Pipeline:
    return Pipeline(
        workspace,
        {
            Stage.RESOLVE: create_resolve_implementation(target),
            Stage.PLAN_ACQUISITION: create_acquisition_implementation(
                target,
                policy=AcquisitionPolicy(reserve_bytes=disk_reserve_bytes),
            ),
            Stage.DOWNLOAD: create_download_implementation(DownloadPolicy(max_workers=workers)),
            Stage.VERIFY: create_verify_implementation(
                VerificationPolicy(max_workers=_verification_workers(workers))
            ),
            Stage.ASSEMBLE_CLIENT: create_assemble_client_implementation(),
            Stage.INDEX_VFS: create_index_vfs_implementation(),
            Stage.MATERIALIZE_VFS: create_materialize_vfs_implementation(
                VfsPolicy(materialize_workers=workers)
            ),
            **create_readable_implementations(
                ReadablePolicy(
                    transform_workers=workers,
                    actionscript_workers=_actionscript_workers(workers),
                    engine_stub_workers=min(8, workers),
                )
            ),
            Stage.SNAPSHOT: create_snapshot_implementation(
                verification_policy=SnapshotVerificationPolicy(
                    max_workers=_verification_workers(workers)
                )
            ),
        },
        progress_observer=lambda run_id, stage, message: click.echo(
            f"Run {run_id} [{stage.value}]: {message}",
            err=True,
        ),
    )


def _pipeline_error_exit_code(error_code: str) -> int:
    if error_code == "stage_not_implemented":
        return EXIT_UNSUPPORTED
    if error_code in {"source_changed", "source_unavailable"}:
        return EXIT_TRANSIENT
    if error_code == "protocol_incompatible":
        return EXIT_VALIDATION
    if error_code == "insufficient_disk":
        return EXIT_VALIDATION
    if error_code in {"artifact_corrupt", "snapshot_invalid", "unsafe_archive"}:
        return EXIT_CORRUPT
    if error_code in {"transform_failed", "unsupported_install_bundle", "vfs_order_unknown"}:
        return EXIT_UNSUPPORTED
    if error_code.startswith("pinned_resolve_"):
        return EXIT_VALIDATION
    return EXIT_INTERNAL


def _languages(value: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in value.split(","))
    if not values or any(not part for part in values):
        raise click.BadParameter("provide a comma-separated non-empty language list")
    return values


@contextmanager
def _translate_sigterm() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(_signal_number: int, _frame: FrameType | None) -> None:
        raise PipelineInterrupted("received SIGTERM")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="game-downloader")
def cli() -> None:
    """Download, unpack and inspect persistent game Runs."""


@cli.command()
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def version(json_output: bool) -> None:
    """Show the application and Python versions."""

    payload = {
        "name": "game-downloader",
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "version": __version__,
    }
    if json_output:
        click.echo(canonical_json_bytes(payload).decode("utf-8"), nl=False)
    else:
        click.echo(f"game-downloader {__version__} (Python {payload['python']})")


@cli.command(name="probe-release")
@click.option("--target", required=True, help="Configured Target ID.")
@click.option(
    "--client-type",
    type=click.Choice([member.value for member in ClientType], case_sensitive=False),
    default=ClientType.SD.value,
    show_default=True,
)
@click.option(
    "--targets-config",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Override the packaged TargetConfig YAML.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def probe_release(
    target: str,
    client_type: str,
    targets_config: Path | None,
    json_output: bool,
) -> None:
    """Read the current release name without creating a Run or downloading payloads."""

    try:
        target_config = TargetRegistry.load(targets_config).get(target)
        policy = ResolvePolicy()
        release_name = WgusResolver(
            target_config,
            HttpxTransport(policy),
            policy,
        ).probe_release_name(ClientType(client_type.lower()))
    except (TargetConfigurationError, ValidationError, ValueError) as exc:
        raise CliFailure(str(exc), EXIT_VALIDATION) from exc
    except StageExecutionError as exc:
        raise CliFailure(
            f"{exc.error.code}: {exc.error.message}",
            _pipeline_error_exit_code(exc.error.code),
        ) from exc

    payload = {"release_name": release_name, "target": target}
    if json_output:
        click.echo(canonical_json_bytes(payload).decode("utf-8"), nl=False)
    else:
        click.echo(f"{target}: {release_name}")


@cli.command()
@click.option(
    "--data-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=_default_data_root,
    show_default=True,
)
@click.option(
    "--targets-config",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Override the packaged TargetConfig YAML.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--quiet", is_flag=True, help="Print nothing when all checks pass.")
def doctor(
    data_root: Path,
    targets_config: Path | None,
    json_output: bool,
    quiet: bool,
) -> None:
    """Check runtime, contracts, TargetConfig, and writable workspace."""

    checks: list[JsonObject] = []
    python_ok = sys.version_info[:2] == (3, 13)
    checks.append(
        {
            "detail": ".".join(str(part) for part in sys.version_info[:3]),
            "name": "python-3.13",
            "ok": python_ok,
        }
    )

    try:
        ContractRegistry().validate_example()
        checks.append(
            {
                "detail": "GameSnapshot v1.1.0 example is valid",
                "name": "contracts",
                "ok": True,
            }
        )
    except ContractValidationError as exc:
        checks.append({"detail": str(exc), "name": "contracts", "ok": False})

    try:
        registry = TargetRegistry.load(targets_config)
        checks.append(
            {
                "detail": f"{len(registry.targets)} targets from {registry.source}",
                "name": "targets",
                "ok": True,
            }
        )
    except TargetConfigurationError as exc:
        checks.append({"detail": str(exc), "name": "targets", "ok": False})

    try:
        identity = Uncompyle6Transformer(ReadablePolicy()).identity
        checks.append(
            {
                "detail": f"{identity.name} {identity.version}",
                "name": "pyc-decompiler",
                "ok": True,
            }
        )
    except Exception as exc:
        checks.append({"detail": str(exc), "name": "pyc-decompiler", "ok": False})

    try:
        identity = FfdecTransformer(ReadablePolicy()).identity
        checks.append(
            {
                "detail": f"{identity.name} {identity.version}",
                "name": "actionscript-decompiler",
                "ok": True,
            }
        )
    except Exception as exc:
        checks.append({"detail": str(exc), "name": "actionscript-decompiler", "ok": False})

    archive_tool = next(
        (name for name in ("7zz", "7z", "bsdtar") if shutil.which(name) is not None),
        None,
    )
    checks.append(
        {
            "detail": archive_tool or "install 7-Zip or bsdtar",
            "name": "archive-tool",
            "ok": archive_tool is not None,
        }
    )
    aria2 = shutil.which("aria2c")
    checks.append(
        {
            "detail": aria2 or "not installed; HTTPS web-seed transport remains available",
            "name": "aria2-torrent-fallback",
            "ok": True,
        }
    )

    try:
        workspace = _workspace(data_root)
        workspace.initialize()
        with tempfile.NamedTemporaryFile(
            dir=workspace.tmp_root, prefix="doctor-", delete=True
        ) as probe:
            probe.write(b"ok")
            probe.flush()
            os.fsync(probe.fileno())
        free_bytes = shutil.disk_usage(workspace.root).free
        checks.append(
            {
                "detail": f"{workspace.root} ({free_bytes} bytes free)",
                "name": "workspace",
                "ok": True,
            }
        )
    except (OSError, WorkspaceError, ValueError) as exc:
        checks.append({"detail": str(exc), "name": "workspace", "ok": False})

    ok = all(bool(check["ok"]) for check in checks)
    payload = {"checks": checks, "ok": ok, "version": __version__}
    if json_output:
        click.echo(canonical_json_bytes(payload).decode("utf-8"), nl=False)
    elif not quiet or not ok:
        for check in checks:
            marker = "ok" if check["ok"] else "FAILED"
            click.echo(f"[{marker}] {check['name']}: {check['detail']}")
    if not ok:
        if json_output:
            raise click.exceptions.Exit(EXIT_VALIDATION)
        raise CliFailure("doctor checks failed", EXIT_VALIDATION)


@cli.command(name="run")
@click.option("--target", required=True, help="Configured Target ID.")
@click.option(
    "--client-type",
    required=True,
    type=click.Choice([member.value for member in ClientType], case_sensitive=False),
)
@click.option(
    "--languages",
    required=True,
    help="Comma-separated language codes (e.g. EN,RU), or ALL for every available language.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit the final Run report as JSON.")
@click.option(
    "--until",
    "until_value",
    type=click.Choice([stage.value for stage in STAGE_ORDER]),
    default=Stage.SNAPSHOT.value,
    show_default=True,
)
@click.option(
    "--data-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=_default_data_root,
    show_default=True,
)
@click.option(
    "--targets-config",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Override the packaged TargetConfig YAML.",
)
@click.option(
    "--disk-reserve-bytes",
    type=click.IntRange(min=0),
    default=2 * 1024 * 1024 * 1024,
    envvar="GAME_DOWNLOADER_DISK_RESERVE_BYTES",
    show_default=True,
    help="Extra free-space reserve included in the Acquisition Plan.",
)
@click.option(
    "--download-workers",
    type=click.IntRange(min=1, max=32),
    default=6,
    envvar="GAME_DOWNLOADER_DOWNLOAD_WORKERS",
    show_default=True,
    help="Maximum number of Artifacts downloaded concurrently.",
)
@click.option(
    "--skip-check",
    is_flag=True,
    help=(
        "Trust stages committed earlier in this single process and skip optional audits; "
        "the final Snapshot verifier still checks every published payload."
    ),
)
def run_command(
    target: str,
    client_type: str,
    languages: str,
    json_output: bool,
    until_value: str,
    data_root: Path,
    targets_config: Path | None,
    disk_reserve_bytes: int,
    download_workers: int,
    skip_check: bool,
) -> None:
    """Create a pinned Run and execute it through a stage."""

    try:
        request = RunRequest(
            target=target,
            client_type=ClientType(client_type.lower()),
            languages=_languages(languages),
        )
        target_config = TargetRegistry.load(targets_config).get(request.target)
    except (TargetConfigurationError, ValidationError, ValueError) as exc:
        raise CliFailure(str(exc), EXIT_VALIDATION) from exc
    pipeline = _build_pipeline(
        _workspace(data_root),
        target_config,
        disk_reserve_bytes=disk_reserve_bytes,
        workers=download_workers,
    )
    try:
        with _translate_sigterm():
            report = pipeline.start(
                request,
                Stage(until_value),
                skip_checks=skip_check,
            )
    except PipelineRunError as exc:
        report = pipeline.status(exc.run_id)
        if not json_output:
            click.echo(f"Run ID: {exc.run_id}", err=True)
        _print_report_output(report, json_output=json_output, err=not json_output)
        exit_code = _pipeline_error_exit_code(exc.error.code)
        raise CliFailure(f"{exc.error.code}: {exc.error.message}", exit_code) from exc
    except (OSError, WorkspaceError) as exc:
        raise CliFailure(str(exc), EXIT_VALIDATION) from exc
    _print_report_output(report, json_output=json_output)


@cli.command()
@click.argument("run_id")
@click.option(
    "--until",
    "until_value",
    type=click.Choice([stage.value for stage in STAGE_ORDER]),
    default=Stage.SNAPSHOT.value,
    show_default=True,
)
@click.option("--json", "json_output", is_flag=True, help="Emit the final Run report as JSON.")
@click.option(
    "--data-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=_default_data_root,
    show_default=True,
)
@click.option(
    "--targets-config",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Override the packaged TargetConfig YAML.",
)
@click.option(
    "--disk-reserve-bytes",
    type=click.IntRange(min=0),
    default=2 * 1024 * 1024 * 1024,
    envvar="GAME_DOWNLOADER_DISK_RESERVE_BYTES",
    show_default=True,
    help="Extra free-space reserve included in the Acquisition Plan.",
)
@click.option(
    "--download-workers",
    type=click.IntRange(min=1, max=32),
    default=6,
    envvar="GAME_DOWNLOADER_DOWNLOAD_WORKERS",
    show_default=True,
    help="Maximum number of Artifacts downloaded concurrently.",
)
@click.option(
    "--skip-check",
    is_flag=True,
    help=(
        "Trust the committed prefix from an immediately preceding same-version stage; "
        "structural checks and the final Snapshot verification still run."
    ),
)
def resume(
    run_id: str,
    until_value: str,
    json_output: bool,
    data_root: Path,
    targets_config: Path | None,
    disk_reserve_bytes: int,
    download_workers: int,
    skip_check: bool,
) -> None:
    """Continue an existing Run without changing its pinned request."""

    try:
        workspace = _workspace(data_root)
        record = workspace.load_run(run_id)
        target_config = TargetRegistry.load(targets_config).get(record.request.target)
        pipeline = _build_pipeline(
            workspace,
            target_config,
            disk_reserve_bytes=disk_reserve_bytes,
            workers=download_workers,
        )
        click.echo(f"Run {run_id}: resuming through {until_value}", err=True)
        with _translate_sigterm():
            report = pipeline.resume(
                run_id,
                Stage(until_value),
                skip_checks=skip_check,
            )
    except (OSError, TargetConfigurationError, WorkspaceError) as exc:
        raise CliFailure(str(exc), EXIT_VALIDATION) from exc
    except PipelineRunError as exc:
        _print_report_output(
            pipeline.status(exc.run_id, verify_results=not skip_check),
            json_output=json_output,
            err=not json_output,
        )
        exit_code = _pipeline_error_exit_code(exc.error.code)
        raise CliFailure(f"{exc.error.code}: {exc.error.message}", exit_code) from exc
    _print_report_output(report, json_output=json_output)


@cli.command()
@click.argument("run_id")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--data-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=_default_data_root,
    show_default=True,
)
def status(run_id: str, json_output: bool, data_root: Path) -> None:
    """Inspect committed and interrupted stages without taking the Run lock."""

    try:
        report = Pipeline(_workspace(data_root)).status(run_id)
    except (OSError, WorkspaceError) as exc:
        raise CliFailure(str(exc), EXIT_VALIDATION) from exc
    _print_report_output(report, json_output=json_output)


@cli.group()
def snapshot() -> None:
    """Inspect sealed GameSnapshots."""


@snapshot.command(name="verify")
@click.argument("path", type=click.Path(path_type=Path))
def snapshot_verify(path: Path) -> None:
    """Independently verify a sealed GameSnapshot."""

    try:
        opened = Snapshot.open_and_verify(path)
    except (OSError, SnapshotVerificationError, ValueError) as exc:
        raise CliFailure(str(exc), EXIT_CORRUPT) from exc
    click.echo(
        f"GameSnapshot {opened.descriptor.snapshot_id}: verified "
        f"({opened.descriptor.manifests.files.records} files)"
    )


def _print_report(report: RunReport, *, err: bool = False) -> None:
    completed = report.completed_until.value if report.completed_until is not None else "none"
    active = _format_duration(report.active_duration_seconds)
    click.echo(
        f"Run {report.run_id}: {report.state.value} "
        f"(completed through: {completed}, active: {active})",
        err=err,
    )
    for stage in report.stages:
        if stage.state.value == "pending":
            continue
        suffix = f" [{stage.error.code}]" if stage.error is not None else ""
        timing = ""
        if stage.duration_seconds is not None:
            preposition = "for" if stage.state.value == "running" else "in"
            timing = f" {preposition} {_format_duration(stage.duration_seconds)}"
        statistics = _format_stage_statistics(stage)
        details = f" | {statistics}" if statistics else ""
        click.echo(
            f"  {stage.stage.value}: {stage.state.value}{timing}{suffix}{details}",
            err=err,
        )


def _print_report_output(
    report: RunReport,
    *,
    json_output: bool,
    err: bool = False,
) -> None:
    if json_output:
        click.echo(
            canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8"),
            nl=False,
        )
        return
    _print_report(report, err=err)


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{round(seconds * 1000):d}ms"
    total_seconds = round(seconds)
    if total_seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes:02d}m {remaining_seconds:02d}s"


def _format_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1024 or candidate == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{value} B"
    return f"{amount:.1f} {unit}"


def _integer_stat(stage: StageSummary, key: str) -> int:
    value = stage.statistics.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _number_stat(stage: StageSummary, key: str) -> float | None:
    value = stage.statistics.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _format_byte_rate(stage: StageSummary, key: str = "bytes_per_second") -> str:
    value = _number_stat(stage, key)
    return f"{_format_bytes(round(value))}/s" if value is not None else "n/a"


def _format_stage_statistics(stage: StageSummary) -> str:
    if not stage.statistics:
        return ""
    if stage.stage is Stage.RESOLVE:
        return (
            f"{_integer_stat(stage, 'parts')} parts, "
            f"{_integer_stat(stage, 'protocol_responses')} protocol responses "
            f"({_format_bytes(_integer_stat(stage, 'protocol_response_bytes'))})"
        )
    if stage.stage is Stage.PLAN_ACQUISITION:
        return (
            f"{_integer_stat(stage, 'artifacts')} artifacts, "
            f"{_format_bytes(_integer_stat(stage, 'planned_download_bytes'))} download, "
            f"{_format_bytes(_integer_stat(stage, 'planned_assembled_bytes'))} assembled, "
            f"{_format_bytes(_integer_stat(stage, 'required_free_bytes'))} required free"
        )
    if stage.stage is Stage.DOWNLOAD:
        fetched = _integer_stat(stage, "fetched_artifacts")
        network_bytes = _integer_stat(stage, "network_bytes_estimate")
        reused = _integer_stat(stage, "reused_artifacts")
        fragments = [
            f"{_integer_stat(stage, 'artifacts')} artifacts",
            (
                f"{fetched} fetched (~{_format_bytes(network_bytes)} network at "
                f"{_format_byte_rate(stage, 'network_bytes_per_second')})"
            ),
        ]
        if reused:
            fragments.append(
                f"{reused} reused ({_format_bytes(_integer_stat(stage, 'reused_bytes'))})"
            )
        resumed = _integer_stat(stage, "resumed_bytes")
        if resumed:
            fragments.append(f"{_format_bytes(resumed)} resumed")
        attempts = _integer_stat(stage, "download_attempts")
        if attempts:
            fragments.append(f"{attempts} attempts")
        ranged = _integer_stat(stage, "parallel_range_artifacts")
        if ranged:
            fragments.append(
                f"{ranged} striped across "
                f"{_integer_stat(stage, 'parallel_range_segments')} HTTP ranges"
            )
        fallbacks = _integer_stat(stage, "parallel_range_fallbacks")
        if fallbacks:
            fragments.append(
                f"{fallbacks} parallel Range fallbacks on "
                f"{_integer_stat(stage, 'parallel_range_fallback_artifacts')} artifacts "
                f"({_format_bytes(_integer_stat(stage, 'parallel_range_discarded_bytes'))} "
                "discarded)"
            )
        return ", ".join(fragments)
    if stage.stage is Stage.VERIFY:
        return (
            f"{_integer_stat(stage, 'artifacts')} artifacts, "
            f"{_format_bytes(_integer_stat(stage, 'checked_bytes'))} checked at "
            f"{_format_byte_rate(stage)}, "
            f"{_integer_stat(stage, 'archive_entries')} archive entries"
        )
    if stage.stage is Stage.ASSEMBLE_CLIENT:
        return (
            f"{_integer_stat(stage, 'files')} files / "
            f"{_format_bytes(_integer_stat(stage, 'output_bytes'))} at "
            f"{_format_byte_rate(stage)}, "
            f"{_integer_stat(stage, 'hardlinked_files')} hardlinked, "
            f"{_integer_stat(stage, 'copied_files')} copied"
        )
    if stage.stage is Stage.INDEX_VFS:
        return (
            f"{_integer_stat(stage, 'packages')} packages / "
            f"{_format_bytes(_integer_stat(stage, 'package_bytes'))}, "
            f"{_integer_stat(stage, 'indexed_files')} files, "
            f"{_integer_stat(stage, 'candidates')} candidates, "
            f"{_integer_stat(stage, 'conflicts')} conflicts"
        )
    if stage.stage is Stage.MATERIALIZE_VFS:
        return (
            f"{_integer_stat(stage, 'files')} files / "
            f"{_format_bytes(_integer_stat(stage, 'output_bytes'))} at "
            f"{_format_byte_rate(stage)}, "
            f"{_integer_stat(stage, 'package_files')} from packages, "
            f"{_integer_stat(stage, 'loose_files')} loose"
        )
    if stage.stage is Stage.PLAN_READABLE:
        return (
            f"{_integer_stat(stage, 'files')} files: "
            f"{_integer_stat(stage, 'transform_files')} transforms, "
            f"{_integer_stat(stage, 'actionscript_libraries')} AS3 libraries, "
            f"{_integer_stat(stage, 'passthrough_files')} passthrough"
        )
    if stage.stage is Stage.TRANSFORM_READABLE:
        return (
            f"{_integer_stat(stage, 'files')} transformed / "
            f"{_format_bytes(_integer_stat(stage, 'output_bytes'))} at "
            f"{_format_byte_rate(stage)}, "
            f"{_integer_stat(stage, 'diagnostics')} diagnostics"
        )
    if stage.stage is Stage.DECOMPILE_ACTIONSCRIPT:
        return (
            f"{_integer_stat(stage, 'libraries')} libraries -> "
            f"{_integer_stat(stage, 'files')} AS3 files / "
            f"{_format_bytes(_integer_stat(stage, 'output_bytes'))} at "
            f"{_format_byte_rate(stage)}"
        )
    if stage.stage is Stage.ASSEMBLE_READABLE:
        return (
            f"{_integer_stat(stage, 'files')} files + "
            f"{_integer_stat(stage, 'actionscript_files')} AS3, "
            f"{_integer_stat(stage, 'passthrough_files')} passthrough"
        )
    if stage.stage is Stage.GENERATE_ENGINE_STUBS:
        return (
            f"{_integer_stat(stage, 'typing_stubs')} typing stubs / "
            f"{_integer_stat(stage, 'files')} artifacts / "
            f"{_format_bytes(_integer_stat(stage, 'output_bytes'))}"
        )
    if stage.stage is Stage.FINALIZE_READABLE:
        return (
            f"{_integer_stat(stage, 'files')} files + "
            f"{_integer_stat(stage, 'actionscript_files')} AS, "
            f"{_integer_stat(stage, 'stub_files')} stubs, "
            f"{_format_bytes(_integer_stat(stage, 'output_bytes'))} output at "
            f"{_format_byte_rate(stage)}, "
            f"{_integer_stat(stage, 'transformed_files')} transformed"
        )
    if stage.stage is Stage.SNAPSHOT:
        rate = _number_stat(stage, "records_per_second")
        rate_text = f" at {rate:.1f} records/s" if rate is not None else ""
        return (
            f"{_integer_stat(stage, 'file_records')} files + "
            f"{_integer_stat(stage, 'actionscript_records')} AS, "
            f"{_integer_stat(stage, 'stub_records')} stubs, "
            f"{_integer_stat(stage, 'package_records')} packages, "
            f"{_integer_stat(stage, 'conflict_records')} conflicts{rate_text}"
        )
    return ""


def main() -> None:
    cli(prog_name="game-downloader")


__all__ = ["cli", "main"]

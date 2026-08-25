from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError

from game_downloader._json import (
    JsonObject,
    JsonValue,
    as_json_object,
    canonical_json_bytes,
    canonical_sha256_digest,
)
from game_downloader.models import (
    STAGE_ORDER,
    ErrorInfo,
    RunRecord,
    RunReport,
    RunRequest,
    RunState,
    Stage,
    StageInputDocument,
    StageInputRecord,
    StageResult,
    StageState,
    StageStatus,
    StageSummary,
    UpstreamReference,
)
from game_downloader.reporting import stage_statistics
from game_downloader.workspace import (
    BlobStore,
    RunLockedError,
    Workspace,
    WorkspaceCorruptError,
    sha256_digest,
    sha256_file_digest,
    utc_now,
)


class PipelineError(RuntimeError):
    pass


class StageExecutionError(PipelineError):
    """Expected stage failure carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error = ErrorInfo(code=code, message=message, exception_type=type(self).__name__)


class StageNotImplementedError(StageExecutionError):
    def __init__(self, stage: Stage) -> None:
        super().__init__(
            "stage_not_implemented",
            f"stage {stage.value!r} has no production implementation yet",
        )


class PipelineInterrupted(StageExecutionError):
    def __init__(self, message: str = "pipeline execution was interrupted") -> None:
        super().__init__("interrupted", message)


class PipelineRunError(PipelineError):
    def __init__(self, run_id: str, stage: Stage, error: ErrorInfo) -> None:
        super().__init__(f"Run {run_id}, stage {stage.value}: {error.message}")
        self.run_id = run_id
        self.stage = stage
        self.error = error


class StageFailedError(PipelineRunError):
    pass


class RunInterruptedError(PipelineRunError):
    pass


class PinnedResolveError(PipelineRunError):
    pass


PayloadModel = TypeVar("PayloadModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class StageContext:
    run_id: str
    request: RunRequest
    stage: Stage
    input_digest: str
    run_directory: Path
    work_directory: Path
    upstream: StageResult | None
    upstream_result_sha256: str | None
    workspace: Workspace
    blobs: BlobStore
    progress: Callable[[str], None]
    _upstream_models: dict[type[BaseModel], BaseModel] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )
    _committed_models: dict[tuple[Stage, type[BaseModel]], BaseModel] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def upstream_as(self, model: type[PayloadModel]) -> PayloadModel:
        """Validate an upstream payload once and reuse it within this Stage."""

        if self.upstream is None:
            raise ValueError(f"stage {self.stage.value} has no upstream result")
        cached = self._upstream_models.get(model)
        if cached is None:
            cached = model.model_validate(self.upstream.payload)
            self._upstream_models[model] = cached
        return cast(PayloadModel, cached)

    def require_upstream_digest(self) -> str:
        if self.upstream_result_sha256 is None:
            raise ValueError(f"stage {self.stage.value} has no upstream result digest")
        return self.upstream_result_sha256

    def committed_as(self, stage: Stage, model: type[PayloadModel]) -> PayloadModel:
        """Load and validate an earlier committed payload once within this Stage."""

        key = (stage, model)
        cached = self._committed_models.get(key)
        if cached is None:
            status = self.workspace.load_stage_status(self.run_id, stage)
            _stored_input, payload = _load_trusted_committed_payload(
                self.workspace,
                self.run_id,
                stage=stage,
                status=status,
            )
            cached = model.model_validate(payload)
            self._committed_models[key] = cached
        return cast(PayloadModel, cached)


type StageExecutor = Callable[[StageContext], Mapping[str, JsonValue]]
type StageResultValidator = Callable[[StageContext, JsonObject], None]


@dataclass(frozen=True, slots=True)
class StageImplementation:
    implementation_version: str
    execute: StageExecutor
    configuration: Mapping[str, JsonValue] = field(default_factory=dict)
    validate: StageResultValidator | None = None
    audit: StageResultValidator | None = None

    def __post_init__(self) -> None:
        if not self.implementation_version:
            raise ValueError("stage implementation_version must not be empty")
        object.__setattr__(self, "configuration", as_json_object(dict(self.configuration)))


class CommitPoint(StrEnum):
    INPUT_WRITTEN = "input-written"
    RUNNING_WRITTEN = "running-written"
    RESULT_WRITTEN = "result-written"
    SUCCEEDED_WRITTEN = "succeeded-written"


type CommitObserver = Callable[[CommitPoint, str, Stage], None]
type ProgressObserver = Callable[[str, Stage, str], None]


def _unavailable(context: StageContext) -> Mapping[str, JsonValue]:
    raise StageNotImplementedError(context.stage)


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _decode_trusted_result(
    encoded: bytes,
    *,
    stage: Stage,
    input_digest: str,
) -> tuple[JsonObject, str]:
    try:
        raw_result = json.loads(encoded, parse_constant=_reject_non_finite_json)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkspaceCorruptError(f"stage {stage.value} result is not valid JSON") from exc
    expected_keys = {
        "schema_version",
        "stage",
        "input_digest",
        "implementation_version",
        "payload",
    }
    if (
        not isinstance(raw_result, dict)
        or set(raw_result) != expected_keys
        or raw_result.get("schema_version") != 1
        or raw_result.get("stage") != stage.value
        or raw_result.get("input_digest") != input_digest
        or not isinstance(raw_result.get("implementation_version"), str)
        or not raw_result["implementation_version"]
        or not isinstance(raw_result.get("payload"), dict)
    ):
        raise WorkspaceCorruptError(f"stage {stage.value} result does not match its input")
    return cast(JsonObject, raw_result["payload"]), raw_result["implementation_version"]


def _load_trusted_committed_payload(
    workspace: Workspace,
    run_id: str,
    *,
    stage: Stage,
    status: StageStatus,
) -> tuple[StageInputRecord, JsonObject]:
    """Verify a same-run checkpoint without recursively re-encoding its large payload."""

    if (
        status.stage is not stage
        or status.state is not StageState.SUCCEEDED
        or status.input_digest is None
        or status.result_sha256 is None
    ):
        raise WorkspaceCorruptError(f"stage {stage.value} is not a committed result")
    stage_path = workspace.stage_path(run_id, stage)
    input_bytes = workspace.read_bytes(stage_path / "input.json")
    stored_input = StageInputRecord.model_validate_json(input_bytes)
    canonical_input = canonical_json_bytes(stored_input.model_dump(mode="json"))
    calculated_input_digest = canonical_sha256_digest(stored_input.document.model_dump(mode="json"))
    if (
        input_bytes != canonical_input
        or stored_input.digest != calculated_input_digest
        or stored_input.digest != status.input_digest
        or stored_input.document.stage is not stage
    ):
        raise WorkspaceCorruptError(f"stage {stage.value} input digest does not match status")

    result_bytes = workspace.read_bytes(stage_path / "result.json")
    if sha256_digest(result_bytes) != status.result_sha256:
        raise WorkspaceCorruptError(f"stage {stage.value} result digest does not match status")
    payload, implementation_version = _decode_trusted_result(
        result_bytes,
        stage=stage,
        input_digest=stored_input.digest,
    )
    if implementation_version != stored_input.document.implementation_version:
        raise WorkspaceCorruptError(f"stage {stage.value} result does not match its input")
    return stored_input, payload


_UNAVAILABLE_IMPLEMENTATION = StageImplementation(
    implementation_version="unavailable-v1",
    execute=_unavailable,
)


class Pipeline:
    """Run the fixed linear pipeline with digest-bound, atomic Stage Results."""

    def __init__(
        self,
        workspace: Workspace,
        implementations: Mapping[Stage, StageImplementation] | None = None,
        *,
        commit_observer: CommitObserver | None = None,
        progress_observer: ProgressObserver | None = None,
    ) -> None:
        self.workspace = workspace
        self.implementations = dict(implementations or {})
        self._commit_observer = commit_observer
        self._progress_observer = progress_observer

    def start(
        self,
        request: RunRequest,
        until: Stage,
        *,
        skip_checks: bool = False,
    ) -> RunReport:
        record = self.workspace.create_run(request)
        return self._execute(record, until, skip_checks=skip_checks)

    def resume(
        self,
        run_id: str,
        until: Stage,
        *,
        skip_checks: bool = False,
    ) -> RunReport:
        record = self.workspace.load_run(run_id)
        return self._execute(
            record,
            until,
            skip_checks=skip_checks,
            direct_resume=skip_checks,
        )

    def status(self, run_id: str, *, verify_results: bool = True) -> RunReport:
        record = self.workspace.load_run(run_id)
        observed_at = utc_now()
        locked = self.workspace.is_run_locked(run_id)
        raw_statuses = [self.workspace.load_stage_status(run_id, stage) for stage in STAGE_ORDER]
        locked = locked or self.workspace.is_run_locked(run_id)
        statuses: list[StageStatus] = []
        derived_statistics: dict[Stage, JsonObject] = {}
        for status in raw_statuses:
            stage = status.stage
            if status.state is StageState.RUNNING and not locked:
                status = StageStatus(
                    stage=stage,
                    state=StageState.INTERRUPTED,
                    attempt=status.attempt,
                    input_digest=status.input_digest,
                    started_at=status.started_at,
                    finished_at=utc_now(),
                    error=ErrorInfo(
                        code="interrupted",
                        message="stage was left running without a Run lock holder",
                    ),
                )
            elif status.state is StageState.SUCCEEDED:
                try:
                    if not verify_results:
                        pass
                    elif status.statistics:
                        self._validate_committed_result_digest(run_id, stage, status)
                    else:
                        result = self._load_committed_result(run_id, stage, status)
                        duration_seconds = self._duration_seconds(status, observed_at)
                        derived_statistics[stage] = stage_statistics(
                            stage,
                            result.payload,
                            duration_seconds,
                        )
                except (OSError, ValidationError, ValueError, WorkspaceCorruptError) as exc:
                    status = StageStatus(
                        stage=stage,
                        state=StageState.FAILED,
                        attempt=status.attempt,
                        input_digest=status.input_digest,
                        started_at=status.started_at,
                        finished_at=status.finished_at,
                        error=ErrorInfo(
                            code="checkpoint_invalid",
                            message=str(exc) or type(exc).__name__,
                            exception_type=type(exc).__name__,
                        ),
                    )
            statuses.append(status)

        completed_until: Stage | None = None
        for status in statuses:
            if status.state is not StageState.SUCCEEDED:
                break
            completed_until = status.stage

        current_status = next(
            (
                status
                for status in statuses
                if status.state in {StageState.RUNNING, StageState.FAILED, StageState.INTERRUPTED}
            ),
            None,
        )
        current_stage = current_status.stage if current_status is not None else None

        if current_status is not None:
            state_by_stage = {
                StageState.RUNNING: RunState.RUNNING,
                StageState.FAILED: RunState.FAILED,
                StageState.INTERRUPTED: RunState.INTERRUPTED,
            }
            state = state_by_stage[current_status.state]
        elif completed_until is Stage.SNAPSHOT:
            state = RunState.SUCCEEDED
        elif completed_until is not None:
            state = RunState.PAUSED
        else:
            state = RunState.CREATED

        summaries: list[StageSummary] = []
        for status in statuses:
            duration_seconds = self._duration_seconds(status, observed_at)
            summaries.append(
                StageSummary(
                    stage=status.stage,
                    state=status.state,
                    attempt=status.attempt,
                    input_digest=status.input_digest,
                    started_at=status.started_at,
                    finished_at=status.finished_at,
                    duration_seconds=duration_seconds,
                    result_sha256=status.result_sha256,
                    statistics=status.statistics or derived_statistics.get(status.stage, {}),
                    error=status.error,
                )
            )

        return RunReport(
            run_id=record.run_id,
            request=record.request,
            created_at=record.created_at,
            state=state,
            completed_until=completed_until,
            current_stage=current_stage,
            locked=locked,
            active_duration_seconds=round(
                sum(summary.duration_seconds or 0.0 for summary in summaries),
                3,
            ),
            stages=tuple(summaries),
        )

    @staticmethod
    def _duration_seconds(status: StageStatus, observed_at: datetime) -> float | None:
        if status.started_at is None:
            return None
        finished_at = status.finished_at or observed_at
        return round(max(0.0, (finished_at - status.started_at).total_seconds()), 3)

    def _execute(
        self,
        record: RunRecord,
        until: Stage,
        *,
        skip_checks: bool = False,
        direct_resume: bool = False,
    ) -> RunReport:
        lock = self.workspace.run_lock(record.run_id)
        lock.acquire()
        try:
            self._reconcile_interrupted(record)
            upstream: StageResult | None = None
            upstream_result_sha256: str | None = None
            if direct_resume and until.predecessor is not None:
                upstream = self._load_sequential_predecessor(record, until.predecessor)
                predecessor_status = self.workspace.load_stage_status(
                    record.run_id,
                    until.predecessor,
                )
                upstream_result_sha256 = predecessor_status.result_sha256
                implementation = self.implementations.get(until, _UNAVAILABLE_IMPLEMENTATION)
                prepared_input = self._prepare_input(
                    record,
                    until,
                    implementation,
                    upstream,
                    upstream_result_sha256,
                )
                context = self._context(
                    record,
                    prepared_input,
                    upstream,
                    upstream_result_sha256,
                )
                reusable = self._load_reusable(context, prepared_input, implementation)
                if reusable is None:
                    self._execute_stage(
                        context,
                        prepared_input,
                        implementation,
                        run_audit=False,
                    )
            else:
                last_index = STAGE_ORDER.index(until)
                for stage in STAGE_ORDER[: last_index + 1]:
                    implementation = self.implementations.get(stage, _UNAVAILABLE_IMPLEMENTATION)
                    if stage is Stage.RESOLVE:
                        pinned = self._load_pinned_resolve(record, implementation)
                        if pinned is not None:
                            upstream = pinned
                            upstream_result_sha256 = self.workspace.load_stage_status(
                                record.run_id,
                                stage,
                            ).result_sha256
                            continue
                    prepared_input = self._prepare_input(
                        record,
                        stage,
                        implementation,
                        upstream,
                        upstream_result_sha256,
                    )
                    context = self._context(
                        record,
                        prepared_input,
                        upstream,
                        upstream_result_sha256,
                    )
                    reusable = self._load_reusable(context, prepared_input, implementation)
                    if reusable is not None:
                        upstream = reusable
                        upstream_result_sha256 = self.workspace.load_stage_status(
                            record.run_id,
                            stage,
                        ).result_sha256
                        continue
                    upstream = self._execute_stage(
                        context,
                        prepared_input,
                        implementation,
                        run_audit=not skip_checks,
                    )
                    upstream_result_sha256 = self.workspace.load_stage_status(
                        record.run_id,
                        stage,
                    ).result_sha256
        finally:
            lock.release()
        return self.status(record.run_id, verify_results=not skip_checks)

    def _load_sequential_predecessor(
        self,
        record: RunRecord,
        predecessor: Stage,
    ) -> StageResult:
        """Trust a same-version sequential run while still checking its direct input."""

        predecessor_index = STAGE_ORDER.index(predecessor)
        for stage in STAGE_ORDER[: predecessor_index + 1]:
            status = self.workspace.load_stage_status(record.run_id, stage)
            if (
                status.state is not StageState.SUCCEEDED
                or status.input_digest is None
                or status.result_sha256 is None
            ):
                raise WorkspaceCorruptError(
                    "--skip-check requires a fully committed predecessor chain; "
                    f"{stage.value} is {status.state.value}"
                )
        predecessor_status = self.workspace.load_stage_status(record.run_id, predecessor)
        return self._load_trusted_committed_result(
            record.run_id,
            predecessor,
            predecessor_status,
        )

    def _load_pinned_resolve(
        self,
        record: RunRecord,
        implementation: StageImplementation,
    ) -> StageResult | None:
        status = self.workspace.load_stage_status(record.run_id, Stage.RESOLVE)
        if status.state is not StageState.SUCCEEDED:
            return None
        try:
            result = self._load_committed_result(record.run_id, Stage.RESOLVE, status)
            stage_path = self.workspace.stage_path(record.run_id, Stage.RESOLVE)
            stored_input = StageInputRecord.model_validate_json(
                self.workspace.read_bytes(stage_path / "input.json")
            )
            if (
                stored_input.document.run_request != record.request
                or stored_input.document.upstream is not None
            ):
                raise WorkspaceCorruptError("pinned resolve input does not match its Run")
            if implementation.validate is not None:
                context = StageContext(
                    run_id=record.run_id,
                    request=record.request,
                    stage=Stage.RESOLVE,
                    input_digest=result.input_digest,
                    run_directory=self.workspace.run_path(record.run_id),
                    work_directory=self.workspace.stage_work_path(
                        record.run_id,
                        Stage.RESOLVE,
                        result.input_digest,
                    ),
                    upstream=None,
                    upstream_result_sha256=None,
                    workspace=self.workspace,
                    blobs=self.workspace.blobs,
                    progress=lambda _message: None,
                )
                implementation.validate(context, result.payload)
        except PipelineInterrupted:
            raise
        except Exception as exc:
            error = ErrorInfo(
                code="pinned_resolve_invalid",
                message=(
                    "committed resolve cannot be reused safely; create a new Run instead of "
                    f"resolving a newer Version Vector in place: {exc}"
                ),
                exception_type=type(exc).__name__,
            )
            raise PinnedResolveError(record.run_id, Stage.RESOLVE, error) from exc
        return result

    def _validate_committed_result_digest(
        self,
        run_id: str,
        stage: Stage,
        status: StageStatus,
    ) -> None:
        if status.result_sha256 is None:
            raise WorkspaceCorruptError(f"stage {stage.value} has no committed result digest")
        result_path = self.workspace.stage_path(run_id, stage) / "result.json"
        if sha256_file_digest(result_path) != status.result_sha256:
            raise WorkspaceCorruptError(f"stage {stage.value} result digest does not match status")

    def _reconcile_interrupted(self, record: RunRecord) -> None:
        for stage in STAGE_ORDER:
            status = self.workspace.load_stage_status(record.run_id, stage)
            if status.state is not StageState.RUNNING:
                continue
            self.workspace.write_stage_status(
                record.run_id,
                StageStatus(
                    stage=stage,
                    state=StageState.INTERRUPTED,
                    attempt=status.attempt,
                    input_digest=status.input_digest,
                    started_at=status.started_at,
                    finished_at=utc_now(),
                    error=ErrorInfo(
                        code="interrupted",
                        message="previous writer stopped before committing stage status",
                    ),
                ),
            )

    def _prepare_input(
        self,
        record: RunRecord,
        stage: Stage,
        implementation: StageImplementation,
        upstream: StageResult | None,
        upstream_result_sha256: str | None,
    ) -> StageInputRecord:
        predecessor = stage.predecessor
        if predecessor is None:
            if upstream is not None or upstream_result_sha256 is not None:
                raise WorkspaceCorruptError("resolve stage unexpectedly has an upstream result")
            upstream_reference = None
        else:
            if (
                upstream is None
                or upstream.stage is not predecessor
                or upstream_result_sha256 is None
            ):
                raise WorkspaceCorruptError(
                    f"stage {stage.value} requires committed predecessor {predecessor.value}"
                )
            upstream_reference = UpstreamReference(
                stage=predecessor,
                result_sha256=upstream_result_sha256,
            )

        document = StageInputDocument(
            stage=stage,
            implementation_version=implementation.implementation_version,
            run_request=record.request,
            upstream=upstream_reference,
            configuration=as_json_object(dict(implementation.configuration)),
        )
        digest = canonical_sha256_digest(document.model_dump(mode="json"))
        return StageInputRecord(digest=digest, document=document)

    def _context(
        self,
        record: RunRecord,
        prepared_input: StageInputRecord,
        upstream: StageResult | None,
        upstream_result_sha256: str | None,
    ) -> StageContext:
        stage = prepared_input.document.stage
        observer = self._progress_observer
        progress = (
            (lambda message: observer(record.run_id, stage, message))
            if observer is not None
            else (lambda _message: None)
        )
        return StageContext(
            run_id=record.run_id,
            request=record.request,
            stage=stage,
            input_digest=prepared_input.digest,
            run_directory=self.workspace.run_path(record.run_id),
            work_directory=self.workspace.stage_work_path(
                record.run_id,
                stage,
                prepared_input.digest,
            ),
            upstream=upstream,
            upstream_result_sha256=upstream_result_sha256,
            workspace=self.workspace,
            blobs=self.workspace.blobs,
            progress=progress,
        )

    def _load_reusable(
        self,
        context: StageContext,
        prepared_input: StageInputRecord,
        implementation: StageImplementation,
    ) -> StageResult | None:
        stage_path = self.workspace.stage_path(context.run_id, context.stage)
        status = self.workspace.load_stage_status(context.run_id, context.stage)
        if (
            status.state is not StageState.SUCCEEDED
            or status.input_digest != prepared_input.digest
            or status.result_sha256 is None
        ):
            return None
        try:
            result = self._load_committed_result(context.run_id, context.stage, status)
            stored_input = StageInputRecord.model_validate_json(
                self.workspace.read_bytes(stage_path / "input.json")
            )
            if stored_input != prepared_input:
                return None
            if (
                result.input_digest != prepared_input.digest
                or result.implementation_version != implementation.implementation_version
            ):
                return None
        except PipelineInterrupted:
            raise
        except Exception:
            return None
        return result

    def _load_committed_result(
        self,
        run_id: str,
        stage: Stage,
        status: StageStatus,
    ) -> StageResult:
        if (
            status.state is not StageState.SUCCEEDED
            or status.input_digest is None
            or status.result_sha256 is None
        ):
            raise WorkspaceCorruptError(f"stage {stage.value} is not a committed result")
        stage_path = self.workspace.stage_path(run_id, stage)
        input_bytes = self.workspace.read_bytes(stage_path / "input.json")
        stored_input = StageInputRecord.model_validate_json(input_bytes)
        canonical_input = canonical_json_bytes(stored_input.model_dump(mode="json"))
        if input_bytes != canonical_input:
            raise WorkspaceCorruptError(f"stage {stage.value} input.json is not canonical")
        calculated_input_digest = canonical_sha256_digest(
            stored_input.document.model_dump(mode="json")
        )
        if (
            stored_input.digest != calculated_input_digest
            or stored_input.digest != status.input_digest
            or stored_input.document.stage is not stage
        ):
            raise WorkspaceCorruptError(f"stage {stage.value} input digest does not match status")

        result_bytes = self.workspace.read_bytes(stage_path / "result.json")
        if sha256_digest(result_bytes) != status.result_sha256:
            raise WorkspaceCorruptError(f"stage {stage.value} result digest does not match status")
        result = StageResult.model_validate_json(result_bytes)
        if result_bytes != canonical_json_bytes(result.model_dump(mode="json")):
            raise WorkspaceCorruptError(f"stage {stage.value} result.json is not canonical")
        if (
            result.stage is not stage
            or result.input_digest != stored_input.digest
            or result.implementation_version != stored_input.document.implementation_version
        ):
            raise WorkspaceCorruptError(f"stage {stage.value} result does not match its input")
        return result

    def _load_trusted_committed_result(
        self,
        run_id: str,
        stage: Stage,
        status: StageStatus,
    ) -> StageResult:
        """Load the direct sequential predecessor without redundant canonical round-trips."""

        stored_input, payload = _load_trusted_committed_payload(
            self.workspace,
            run_id,
            stage=stage,
            status=status,
        )
        return StageResult.model_construct(
            schema_version=1,
            stage=stage,
            input_digest=stored_input.digest,
            implementation_version=stored_input.document.implementation_version,
            payload=payload,
        )

    def _execute_stage(
        self,
        context: StageContext,
        prepared_input: StageInputRecord,
        implementation: StageImplementation,
        *,
        run_audit: bool = True,
    ) -> StageResult:
        stage_path = self.workspace.stage_path(context.run_id, context.stage)
        stage_path.mkdir(parents=True, exist_ok=True)
        previous = self.workspace.load_stage_status(context.run_id, context.stage)
        attempt = previous.attempt + 1
        running = StageStatus(
            stage=context.stage,
            state=StageState.RUNNING,
            attempt=attempt,
            input_digest=prepared_input.digest,
            started_at=utc_now(),
        )
        committed = False
        result: StageResult | None = None
        try:
            self.workspace.write_stage_status(context.run_id, running)
            self._notify(CommitPoint.RUNNING_WRITTEN, context)
            self.workspace.atomic_write_json(
                stage_path / "input.json",
                prepared_input.model_dump(mode="json"),
            )
            self._notify(CommitPoint.INPUT_WRITTEN, context)
            context.work_directory.mkdir(parents=True, exist_ok=True)

            raw_payload = implementation.execute(context)
            if not isinstance(raw_payload, Mapping):
                raise TypeError("stage implementation must return a mapping")
            payload = cast(JsonObject, dict(raw_payload))
            if implementation.validate is not None:
                implementation.validate(context, payload)
            if run_audit and implementation.audit is not None:
                implementation.audit(context, payload)
            result_document = {
                "schema_version": 1,
                "stage": context.stage.value,
                "input_digest": prepared_input.digest,
                "implementation_version": implementation.implementation_version,
                "payload": payload,
            }
            result_bytes = canonical_json_bytes(result_document)
            result_sha256 = sha256_digest(result_bytes)
            payload, encoded_implementation_version = _decode_trusted_result(
                result_bytes,
                stage=context.stage,
                input_digest=prepared_input.digest,
            )
            if encoded_implementation_version != implementation.implementation_version:
                raise AssertionError("encoded Stage Result changed its implementation version")
            result = StageResult.model_construct(
                schema_version=1,
                stage=context.stage,
                input_digest=prepared_input.digest,
                implementation_version=implementation.implementation_version,
                payload=payload,
            )
            self.workspace.atomic_write_bytes(stage_path / "result.json", result_bytes)
            self._notify(CommitPoint.RESULT_WRITTEN, context)
            finished_at = utc_now()
            assert running.started_at is not None
            duration_seconds = round(
                max(0.0, (finished_at - running.started_at).total_seconds()),
                3,
            )
            self.workspace.write_stage_status(
                context.run_id,
                StageStatus(
                    stage=context.stage,
                    state=StageState.SUCCEEDED,
                    attempt=attempt,
                    input_digest=prepared_input.digest,
                    started_at=running.started_at,
                    finished_at=finished_at,
                    result_sha256=result_sha256,
                    statistics=stage_statistics(
                        context.stage,
                        payload,
                        duration_seconds,
                    ),
                ),
            )
            committed = True
            self._notify(CommitPoint.SUCCEEDED_WRITTEN, context)
            return result
        except PipelineInterrupted as exc:
            if committed and result is not None:
                return result
            self._write_failure_status(context, running, attempt, exc.error, StageState.INTERRUPTED)
            raise RunInterruptedError(context.run_id, context.stage, exc.error) from exc
        except Exception as exc:
            error = (
                exc.error
                if isinstance(exc, StageExecutionError)
                else ErrorInfo(
                    code="internal_error",
                    message=str(exc) or type(exc).__name__,
                    exception_type=type(exc).__name__,
                )
            )
            self._write_failure_status(context, running, attempt, error, StageState.FAILED)
            raise StageFailedError(context.run_id, context.stage, error) from exc

    def _write_failure_status(
        self,
        context: StageContext,
        running: StageStatus,
        attempt: int,
        error: ErrorInfo,
        state: StageState,
    ) -> None:
        self.workspace.write_stage_status(
            context.run_id,
            StageStatus(
                stage=context.stage,
                state=state,
                attempt=attempt,
                input_digest=context.input_digest,
                started_at=running.started_at,
                finished_at=utc_now(),
                error=error,
            ),
        )

    def _notify(self, point: CommitPoint, context: StageContext) -> None:
        if self._commit_observer is not None:
            self._commit_observer(point, context.run_id, context.stage)


__all__ = [
    "CommitPoint",
    "PinnedResolveError",
    "Pipeline",
    "PipelineInterrupted",
    "RunInterruptedError",
    "RunLockedError",
    "StageContext",
    "StageExecutionError",
    "StageFailedError",
    "StageImplementation",
]

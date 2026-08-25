from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import pytest

from game_downloader._json import JsonValue
from game_downloader.models import (
    STAGE_ORDER,
    ClientType,
    RunRequest,
    RunState,
    Stage,
    StageResult,
    StageState,
    StageStatus,
)
from game_downloader.pipeline import (
    CommitPoint,
    PinnedResolveError,
    Pipeline,
    PipelineInterrupted,
    RunInterruptedError,
    StageContext,
    StageImplementation,
)
from game_downloader.workspace import RunLockedError, Workspace, WorkspaceCorruptError


class SimulatedProcessCrash(BaseException):
    pass


def request() -> RunRequest:
    return RunRequest(target="synthetic", client_type=ClientType.SD, languages=("EN",))


def implementations(calls: list[Stage]) -> dict[Stage, StageImplementation]:
    result: dict[Stage, StageImplementation] = {}
    for stage in STAGE_ORDER:

        def execute(context: StageContext, expected: Stage = stage) -> Mapping[str, JsonValue]:
            assert context.stage is expected
            calls.append(expected)
            return {
                "stage": expected.value,
                "upstream": context.upstream.stage.value if context.upstream is not None else None,
            }

        result[stage] = StageImplementation(
            implementation_version="synthetic-v1",
            execute=execute,
            configuration={"fixture_policy": "1"},
        )
    return result


def test_fixed_stage_order_and_directory_names() -> None:
    assert tuple(stage.value for stage in STAGE_ORDER) == (
        "resolve",
        "plan-acquisition",
        "download",
        "verify",
        "assemble-client",
        "index-vfs",
        "materialize-vfs",
        "plan-readable",
        "transform-readable",
        "decompile-actionscript",
        "assemble-readable",
        "generate-engine-stubs",
        "finalize-readable",
        "snapshot",
    )
    assert tuple(stage.directory_name for stage in STAGE_ORDER) == (
        "010-resolve",
        "020-plan-acquisition",
        "030-download",
        "040-verify",
        "050-assemble-client",
        "060-index-vfs",
        "070-materialize-vfs",
        "080-plan-readable",
        "090-transform-readable",
        "100-decompile-actionscript",
        "110-assemble-readable",
        "120-generate-engine-stubs",
        "130-finalize-readable",
        "140-snapshot",
    )


def test_report_includes_persisted_stage_timing(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    def execute(_context: StageContext) -> Mapping[str, JsonValue]:
        return {
            "version_vector": [{"part": "fixture"}],
            "raw_responses": [{"raw_xml": "<fixture/>"}],
        }

    pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: StageImplementation(
                implementation_version="reporting-fixture-v1",
                execute=execute,
            )
        },
    )

    report = pipeline.start(request(), Stage.RESOLVE)

    stage = report.stages[0]
    assert report.created_at.tzinfo is not None
    assert report.active_duration_seconds >= 0
    assert stage.started_at is not None
    assert stage.finished_at is not None
    assert stage.duration_seconds is not None
    assert stage.duration_seconds >= 0
    assert stage.statistics == {
        "parts": 1,
        "protocol_responses": 1,
        "protocol_response_bytes": 10,
    }
    assert workspace.load_stage_status(report.run_id, Stage.RESOLVE).statistics == stage.statistics


def test_status_does_not_deserialize_payload_when_statistics_are_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(workspace, implementations([]))
    completed = pipeline.start(request(), Stage.RESOLVE)
    persisted = workspace.load_stage_status(completed.run_id, Stage.RESOLVE)
    workspace.write_stage_status(
        completed.run_id,
        persisted.model_copy(update={"statistics": {"cached": 1}}),
    )

    def fail_if_deserialized(*_args: object, **_kwargs: object) -> None:
        pytest.fail("status deserialized a committed StageResult payload")

    monkeypatch.setattr(pipeline, "_load_committed_result", fail_if_deserialized)

    report = pipeline.status(completed.run_id)

    assert report.stages[0].statistics == {"cached": 1}


def test_pipeline_stops_and_resumes_at_every_stage_without_reexecution(tmp_path: Path) -> None:
    calls: list[Stage] = []
    pipeline = Pipeline(Workspace(tmp_path), implementations(calls))

    report = pipeline.start(request(), Stage.RESOLVE)
    run_id = report.run_id
    assert report.state is RunState.PAUSED

    for index, stage in enumerate(STAGE_ORDER):
        if stage is not Stage.RESOLVE:
            report = pipeline.resume(run_id, stage)
        repeated = pipeline.resume(run_id, stage)
        assert repeated.completed_until is stage
        assert Counter(calls) == Counter({completed: 1 for completed in STAGE_ORDER[: index + 1]})

    assert report.state is RunState.SUCCEEDED
    assert report.completed_until is Stage.SNAPSHOT


def test_resume_does_not_repeat_expensive_validator_for_reusable_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    validations = 0

    def execute(_context: StageContext) -> Mapping[str, JsonValue]:
        return {"value": "committed"}

    def validate(_context: StageContext, _payload: dict[str, JsonValue]) -> None:
        nonlocal validations
        validations += 1

    pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: implementations([])[Stage.RESOLVE],
            Stage.PLAN_ACQUISITION: StageImplementation(
                implementation_version="expensive-validator-v1",
                execute=execute,
                validate=validate,
            ),
        },
    )
    completed = pipeline.start(request(), Stage.PLAN_ACQUISITION)
    assert validations == 1

    pipeline.resume(completed.run_id, Stage.PLAN_ACQUISITION)

    assert validations == 1


def test_sequential_resume_loads_only_predecessor_and_skips_optional_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Stage] = []
    validations: list[Stage] = []
    audits: list[Stage] = []
    workspace = Workspace(tmp_path)
    selected = implementations(calls)

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        calls.append(context.stage)
        return {"stage": context.stage.value}

    def validate(context: StageContext, _payload: dict[str, JsonValue]) -> None:
        validations.append(context.stage)

    def audit(context: StageContext, _payload: dict[str, JsonValue]) -> None:
        audits.append(context.stage)

    selected[Stage.DOWNLOAD] = StageImplementation(
        implementation_version="sequential-download-v1",
        execute=execute,
        validate=validate,
        audit=audit,
    )
    pipeline = Pipeline(workspace, selected)
    started = pipeline.start(request(), Stage.PLAN_ACQUISITION)
    loaded: list[Stage] = []
    original = pipeline._load_trusted_committed_result

    def tracked_load(run_id: str, stage: Stage, status: StageStatus) -> StageResult:
        loaded.append(stage)
        return original(run_id, stage, status)

    monkeypatch.setattr(pipeline, "_load_trusted_committed_result", tracked_load)

    report = pipeline.resume(started.run_id, Stage.DOWNLOAD, skip_checks=True)

    assert report.completed_until is Stage.DOWNLOAD
    assert report.locked is False
    assert loaded == [Stage.PLAN_ACQUISITION]
    assert validations == [Stage.DOWNLOAD]
    assert audits == []


def test_single_process_start_runs_validators_but_skips_optional_audits(
    tmp_path: Path,
) -> None:
    calls: list[Stage] = []
    validations: list[Stage] = []
    audits: list[Stage] = []
    selected = implementations(calls)
    for stage in STAGE_ORDER[:3]:
        implementation = selected[stage]

        def validate(
            context: StageContext,
            _payload: dict[str, JsonValue],
            expected: Stage = stage,
        ) -> None:
            assert context.stage is expected
            validations.append(expected)

        def audit(
            context: StageContext,
            _payload: dict[str, JsonValue],
            expected: Stage = stage,
        ) -> None:
            assert context.stage is expected
            audits.append(expected)

        selected[stage] = StageImplementation(
            implementation_version=implementation.implementation_version,
            execute=implementation.execute,
            configuration=implementation.configuration,
            validate=validate,
            audit=audit,
        )

    report = Pipeline(Workspace(tmp_path), selected).start(
        request(),
        Stage.DOWNLOAD,
        skip_checks=True,
    )

    assert report.completed_until is Stage.DOWNLOAD
    assert calls == list(STAGE_ORDER[:3])
    assert validations == list(STAGE_ORDER[:3])
    assert audits == []


def test_sequential_resume_requires_a_fully_committed_prefix(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    record = workspace.create_run(request())

    with pytest.raises(WorkspaceCorruptError, match="fully committed predecessor chain"):
        Pipeline(workspace, implementations([])).resume(
            record.run_id,
            Stage.PLAN_ACQUISITION,
            skip_checks=True,
        )


def test_lightweight_status_can_defer_checkpoint_digest_audit(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(workspace, implementations([]))
    completed = pipeline.start(request(), Stage.RESOLVE)
    result_path = workspace.stage_path(completed.run_id, Stage.RESOLVE) / "result.json"
    result_path.write_bytes(b"{}\n")

    lightweight = pipeline.status(completed.run_id, verify_results=False)
    strict = pipeline.status(completed.run_id)

    assert lightweight.state is RunState.PAUSED
    assert strict.state is RunState.FAILED


def test_canonical_input_digest_is_independent_of_run_id_and_language_order(
    tmp_path: Path,
) -> None:
    calls: list[Stage] = []
    pipeline = Pipeline(Workspace(tmp_path), implementations(calls))

    first = pipeline.start(
        RunRequest(target="synthetic", client_type=ClientType.HD, languages=("RU", "EN")),
        Stage.RESOLVE,
    )
    second = pipeline.start(
        RunRequest(target="synthetic", client_type=ClientType.HD, languages=("EN", "RU")),
        Stage.RESOLVE,
    )

    assert first.run_id != second.run_id
    assert first.stages[0].input_digest == second.stages[0].input_digest


@pytest.mark.parametrize("target_stage", STAGE_ORDER)
@pytest.mark.parametrize("commit_point", tuple(CommitPoint))
def test_pipeline_recovers_from_process_crash_after_every_commit_point(
    tmp_path: Path,
    target_stage: Stage,
    commit_point: CommitPoint,
) -> None:
    calls: list[Stage] = []
    workspace = Workspace(tmp_path)
    normal = Pipeline(workspace, implementations(calls))
    record = workspace.create_run(request())

    predecessor = target_stage.predecessor
    if predecessor is not None:
        normal.resume(record.run_id, predecessor)
    calls_before_crash = Counter(calls)
    did_crash = False

    def crash(point: CommitPoint, _run_id: str, stage: Stage) -> None:
        nonlocal did_crash
        if not did_crash and point is commit_point and stage is target_stage:
            did_crash = True
            raise SimulatedProcessCrash

    crashing = Pipeline(workspace, implementations(calls), commit_observer=crash)
    with pytest.raises(SimulatedProcessCrash):
        crashing.resume(record.run_id, target_stage)

    restarted = Pipeline(workspace, implementations(calls))
    report = restarted.resume(record.run_id, target_stage)

    assert report.completed_until is target_stage
    for earlier in STAGE_ORDER[: target_stage.number - 1]:
        assert Counter(calls)[earlier] == calls_before_crash[earlier]
    expected_target_calls = 2 if commit_point is CommitPoint.RESULT_WRITTEN else 1
    assert Counter(calls)[target_stage] == expected_target_calls


def test_running_stage_without_lock_is_reported_and_reconciled_as_interrupted(
    tmp_path: Path,
) -> None:
    calls: list[Stage] = []
    workspace = Workspace(tmp_path)
    record = workspace.create_run(request())

    def crash_after_running(point: CommitPoint, _run_id: str, stage: Stage) -> None:
        if point is CommitPoint.RUNNING_WRITTEN and stage is Stage.RESOLVE:
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        Pipeline(
            workspace,
            implementations(calls),
            commit_observer=crash_after_running,
        ).resume(record.run_id, Stage.RESOLVE)

    interrupted = Pipeline(workspace, implementations(calls)).status(record.run_id)
    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.stages[0].state is StageState.INTERRUPTED

    resumed = Pipeline(workspace, implementations(calls)).resume(record.run_id, Stage.RESOLVE)
    assert resumed.state is RunState.PAUSED
    assert resumed.stages[0].attempt == 2


def test_explicit_interruption_is_persisted_and_can_resume(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    def interrupt(_context: StageContext) -> Mapping[str, JsonValue]:
        raise PipelineInterrupted("fixture interruption")

    interrupted_pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: StageImplementation(
                implementation_version="interrupt-v1",
                execute=interrupt,
            )
        },
    )
    with pytest.raises(RunInterruptedError) as raised:
        interrupted_pipeline.start(request(), Stage.RESOLVE)

    run_id = raised.value.run_id
    status = interrupted_pipeline.status(run_id)
    assert status.state is RunState.INTERRUPTED
    assert status.stages[0].error is not None
    assert status.stages[0].error.code == "interrupted"

    calls: list[Stage] = []
    resumed = Pipeline(workspace, implementations(calls)).resume(run_id, Stage.RESOLVE)
    assert resumed.completed_until is Stage.RESOLVE
    assert resumed.stages[0].attempt == 2


def test_run_lock_rejects_a_second_writer_but_status_remains_readable(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    record = workspace.create_run(request())
    lock = workspace.run_lock(record.run_id).acquire()
    try:
        with pytest.raises(RunLockedError, match="already locked"):
            Pipeline(workspace, implementations([])).resume(record.run_id, Stage.RESOLVE)
        assert Pipeline(workspace).status(record.run_id).locked
    finally:
        lock.release()


def test_implementation_version_changes_input_digest_and_recomputes_downstream(
    tmp_path: Path,
) -> None:
    calls: list[Stage] = []
    workspace = Workspace(tmp_path)
    original = implementations(calls)
    initial = Pipeline(workspace, original).start(request(), Stage.DOWNLOAD)
    old_resolve_digest = initial.stages[0].input_digest
    old_plan_digest = initial.stages[1].input_digest

    changed_calls: list[Stage] = []
    changed = implementations(changed_calls)
    changed[Stage.PLAN_ACQUISITION] = StageImplementation(
        implementation_version="synthetic-v2",
        execute=changed[Stage.PLAN_ACQUISITION].execute,
        configuration={"fixture_policy": "2"},
    )
    report = Pipeline(workspace, changed).resume(initial.run_id, Stage.DOWNLOAD)

    assert report.stages[0].input_digest == old_resolve_digest
    assert report.stages[1].input_digest != old_plan_digest
    assert changed_calls == [Stage.PLAN_ACQUISITION, Stage.DOWNLOAD]


def test_committed_resolve_remains_pinned_across_implementation_changes(tmp_path: Path) -> None:
    original_calls: list[Stage] = []
    workspace = Workspace(tmp_path)
    initial = Pipeline(workspace, implementations(original_calls)).start(request(), Stage.RESOLVE)

    changed_calls: list[Stage] = []
    changed = implementations(changed_calls)
    changed[Stage.RESOLVE] = StageImplementation(
        implementation_version="synthetic-v2",
        execute=changed[Stage.RESOLVE].execute,
        configuration={"source": "newer-live-source"},
    )
    resumed = Pipeline(workspace, changed).resume(initial.run_id, Stage.RESOLVE)

    assert resumed.stages[0].input_digest == initial.stages[0].input_digest
    assert changed_calls == []


def test_status_detects_corrupted_committed_result_and_resume_repairs_it(tmp_path: Path) -> None:
    calls: list[Stage] = []
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(workspace, implementations(calls))
    completed = pipeline.start(request(), Stage.PLAN_ACQUISITION)
    result_path = workspace.stage_path(completed.run_id, Stage.PLAN_ACQUISITION) / "result.json"
    result_path.write_bytes(b"{}\n")

    corrupt = pipeline.status(completed.run_id)
    assert corrupt.state is RunState.FAILED
    assert corrupt.stages[1].error is not None
    assert corrupt.stages[1].error.code == "checkpoint_invalid"

    repaired = pipeline.resume(completed.run_id, Stage.PLAN_ACQUISITION)
    assert repaired.state is RunState.PAUSED
    assert repaired.stages[1].attempt == 2
    assert calls == [Stage.RESOLVE, Stage.PLAN_ACQUISITION, Stage.PLAN_ACQUISITION]


def test_corrupted_pinned_resolve_blocks_resume_instead_of_fetching_a_new_version(
    tmp_path: Path,
) -> None:
    calls: list[Stage] = []
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(workspace, implementations(calls))
    completed = pipeline.start(request(), Stage.RESOLVE)
    result_path = workspace.stage_path(completed.run_id, Stage.RESOLVE) / "result.json"
    result_path.write_bytes(b"{}\n")

    with pytest.raises(PinnedResolveError) as raised:
        pipeline.resume(completed.run_id, Stage.PLAN_ACQUISITION)

    assert raised.value.error.code == "pinned_resolve_invalid"
    assert calls == [Stage.RESOLVE]

from __future__ import annotations

import os
from pathlib import Path

import pytest

from game_downloader.acquisition import AcquisitionPolicy, create_acquisition_implementation
from game_downloader.models import (
    AcquisitionMode,
    AcquisitionPlan,
    ClientType,
    PartName,
    RunRequest,
    Stage,
    StageResult,
)
from game_downloader.pipeline import Pipeline
from game_downloader.wgus import (
    HttpxTransport,
    ResolvePolicy,
    TargetRegistry,
    WgusResolver,
    create_resolve_implementation,
)
from game_downloader.workspace import Workspace

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("GAME_DOWNLOADER_LIVE") != "1",
        reason="set GAME_DOWNLOADER_LIVE=1 to call official WGUS/LSTUS endpoints",
    ),
]


@pytest.mark.parametrize(
    ("target_id", "client_type", "language", "required_parts"),
    [
        (
            "wot-eu",
            ClientType.SD,
            "EN",
            {PartName.CLIENT, PartName.SD_CONTENT, PartName.LOCALE},
        ),
        (
            "mt-ru",
            ClientType.HD,
            "RU",
            {
                PartName.CLIENT,
                PartName.SD_CONTENT,
                PartName.HD_CONTENT,
                PartName.LOCALE,
            },
        ),
        (
            "wot-na",
            ClientType.SD,
            "EN",
            {PartName.CLIENT, PartName.SD_CONTENT, PartName.LOCALE},
        ),
        (
            "wot-asia",
            ClientType.SD,
            "EN",
            {PartName.CLIENT, PartName.SD_CONTENT, PartName.LOCALE},
        ),
        (
            "wot-common-test",
            ClientType.SD,
            "RU",
            {PartName.CLIENT, PartName.SD_CONTENT, PartName.LOCALE},
        ),
        (
            "mt-public-test",
            ClientType.HD,
            "RU",
            {
                PartName.CLIENT,
                PartName.SD_CONTENT,
                PartName.HD_CONTENT,
                PartName.LOCALE,
            },
        ),
    ],
)
def test_live_resolve_smoke(
    target_id: str,
    client_type: ClientType,
    language: str,
    required_parts: set[PartName],
) -> None:
    target = TargetRegistry.load().get(target_id)
    policy = ResolvePolicy(http_attempts=2)
    resolver = WgusResolver(target, HttpxTransport(policy), policy)

    result = resolver.resolve(
        RunRequest(target=target_id, client_type=client_type, languages=(language,))
    )

    assert result.resolved_target.app_id
    assert result.metadata_version
    assert result.release_name
    assert {part.name for part in result.version_vector} == required_parts
    assert all(part.version for part in result.version_vector)
    assert {response.kind for response in result.raw_responses} >= {
        "metadata",
        "patches_chain",
    }


@pytest.mark.parametrize(
    ("target_id", "client_type", "language", "reference_parts", "bundle_parts"),
    [
        (
            "wot-eu",
            ClientType.SD,
            "EN",
            {PartName.CLIENT, PartName.SD_CONTENT},
            {PartName.LOCALE},
        ),
        (
            "mt-ru",
            ClientType.HD,
            "RU",
            {PartName.CLIENT, PartName.SD_CONTENT, PartName.HD_CONTENT},
            {PartName.LOCALE},
        ),
    ],
)
def test_live_acquisition_plan_fetches_descriptors_without_payload(
    tmp_path: Path,
    target_id: str,
    client_type: ClientType,
    language: str,
    reference_parts: set[PartName],
    bundle_parts: set[PartName],
) -> None:
    target = TargetRegistry.load().get(target_id)
    workspace = Workspace(tmp_path / target_id)
    pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: create_resolve_implementation(target),
            Stage.PLAN_ACQUISITION: create_acquisition_implementation(
                target,
                policy=AcquisitionPolicy(reserve_bytes=0),
            ),
        },
    )

    report = pipeline.start(
        RunRequest(target=target_id, client_type=client_type, languages=(language,)),
        Stage.PLAN_ACQUISITION,
    )
    result_path = workspace.stage_path(report.run_id, Stage.PLAN_ACQUISITION) / "result.json"
    stage_result = StageResult.model_validate_json(result_path.read_bytes())
    plan = AcquisitionPlan.model_validate(stage_result.payload)

    assert {
        part.part for part in plan.parts if part.acquisition_mode is AcquisitionMode.REFERENCE
    } == reference_parts
    assert {
        part.part for part in plan.parts if part.acquisition_mode is AcquisitionMode.INSTALL_BUNDLE
    } == bundle_parts
    assert all(descriptor.blob_size < 16 * 1024 * 1024 for descriptor in plan.descriptors)
    assert all(
        artifact.source_hash is not None for part in plan.parts for artifact in part.artifacts
    )
    assert all(
        url.startswith("https://")
        for part in plan.parts
        for artifact in part.artifacts
        for url in artifact.source_urls
    )


@pytest.mark.parametrize(
    ("target_id", "client_type", "language"),
    [
        ("wot-na", ClientType.SD, "EN"),
        ("wot-asia", ClientType.SD, "EN"),
        ("wot-common-test", ClientType.SD, "RU"),
        ("mt-public-test", ClientType.HD, "RU"),
    ],
)
def test_live_secondary_target_acquisition_descriptors(
    tmp_path: Path,
    target_id: str,
    client_type: ClientType,
    language: str,
) -> None:
    target = TargetRegistry.load().get(target_id)
    workspace = Workspace(tmp_path / target_id)
    pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: create_resolve_implementation(target),
            Stage.PLAN_ACQUISITION: create_acquisition_implementation(
                target,
                policy=AcquisitionPolicy(reserve_bytes=0),
            ),
        },
    )

    report = pipeline.start(
        RunRequest(target=target_id, client_type=client_type, languages=(language,)),
        Stage.PLAN_ACQUISITION,
    )
    result_path = workspace.stage_path(report.run_id, Stage.PLAN_ACQUISITION) / "result.json"
    stage_result = StageResult.model_validate_json(result_path.read_bytes())
    plan = AcquisitionPlan.model_validate(stage_result.payload)

    assert len(plan.parts) == (4 if client_type is ClientType.HD else 3)
    assert plan.descriptors
    assert all(part.artifacts for part in plan.parts)


@pytest.mark.skipif(
    os.environ.get("GAME_DOWNLOADER_LIVE_CN") != "1",
    reason="set GAME_DOWNLOADER_LIVE_CN=1 when the regional WOT CN endpoint is reachable",
)
def test_live_wot_cn_resolve_smoke() -> None:
    target = TargetRegistry.load().get("wot-cn")
    policy = ResolvePolicy(
        http_attempts=1,
        connect_timeout_seconds=5,
        read_timeout_seconds=10,
    )
    resolver = WgusResolver(target, HttpxTransport(policy), policy)

    result = resolver.resolve(
        RunRequest(target="wot-cn", client_type=ClientType.SD, languages=("ZH_CN",))
    )

    assert result.resolved_target.app_id
    assert result.release_name

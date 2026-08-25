from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest

from game_downloader.acquisition import (
    AcquisitionPolicy,
    ArtifactCorruptError,
    create_acquisition_implementation,
)
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    AcquisitionPlan,
    ClientType,
    PartName,
    RunRequest,
    RunState,
    Stage,
    StageResult,
)
from game_downloader.pipeline import Pipeline, StageFailedError
from game_downloader.torrent import bytes_path_from_text
from game_downloader.wgus import (
    ResolvePolicy,
    TargetRegistry,
    TransportResponse,
    create_resolve_implementation,
)
from game_downloader.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures"


def xml_fixture(relative_path: str) -> bytes:
    return (FIXTURES / "wgus" / relative_path).read_bytes()


def torrent_fixture(relative_path: str) -> bytes:
    return base64.b64decode((FIXTURES / "torrent" / relative_path).read_text(encoding="ascii"))


@dataclass(frozen=True, slots=True)
class ExpectedProtocolResponse:
    host: str
    path: str
    body: bytes


class ScriptedProtocolTransport:
    def __init__(self, responses: list[ExpectedProtocolResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def get(
        self,
        host: str,
        path: str,
        params: Mapping[str, str],
    ) -> TransportResponse:
        assert self.responses, f"unexpected protocol request: {host}{path}"
        expected = self.responses.pop(0)
        assert (host, path) == (expected.host, expected.path)
        copied = dict(params)
        self.calls.append((host, path, copied))
        url = f"{host}{path}?{urlencode(tuple(sorted(copied.items())))}"
        return TransportResponse(
            status_code=200,
            body=expected.body,
            request_url=url,
            final_url=url,
        )


@dataclass(frozen=True, slots=True)
class ExpectedDescriptorResponse:
    url: str
    body: bytes


class ScriptedDescriptorTransport:
    def __init__(self, responses: list[ExpectedDescriptorResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> TransportResponse:
        assert self.responses, f"unexpected descriptor request: {url}"
        expected = self.responses.pop(0)
        assert url == expected.url
        self.calls.append(url)
        return TransportResponse(
            status_code=200,
            body=expected.body,
            request_url=url,
            final_url=url,
        )


def load_plan(workspace: Workspace, run_id: str) -> AcquisitionPlan:
    result_path = workspace.stage_path(run_id, Stage.PLAN_ACQUISITION) / "result.json"
    result = StageResult.model_validate_json(result_path.read_bytes())
    return AcquisitionPlan.model_validate(result.payload)


def acquisition_artifact(
    path: str,
    index: int,
    *,
    part: PartName = PartName.CLIENT,
    language: str | None = None,
    mode: AcquisitionMode = AcquisitionMode.REFERENCE,
) -> AcquisitionArtifact:
    return AcquisitionArtifact(
        artifact_id=f"sha256:{index:064x}",
        role="client-file" if mode is AcquisitionMode.REFERENCE else "delivery-bundle",
        part=part,
        language=language,
        part_version="1",
        acquisition_mode=mode,
        path=bytes_path_from_text(path),
        size=index,
        source_urls=(f"https://cdn.example.test/{path}",),
        torrent_descriptor_sha256="a" * 64,
        transition_to="1" if mode is AcquisitionMode.INSTALL_BUNDLE else None,
    )


def test_wargaming_plan_uses_exact_reference_and_zero_state_fallback(tmp_path: Path) -> None:
    host = "https://wgus-woteu.wargaming.net"
    protocol = ScriptedProtocolTransport(
        [
            ExpectedProtocolResponse(
                host, "/api/v1/metadata/", xml_fixture("wargaming/metadata.xml")
            ),
            ExpectedProtocolResponse(
                host, "/api/v1/patches_chain/", xml_fixture("wargaming/patches_en.xml")
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                xml_fixture("wargaming/integrity_client.xml"),
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                xml_fixture("wargaming/integrity_empty.xml"),
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                xml_fixture("wargaming/integrity_empty.xml"),
            ),
        ]
    )
    reference_url = "https://cdn.example.test/reference/client.torrent"
    patch_url = "https://cdn.example.test/patch/bundle.torrent"
    descriptors = ScriptedDescriptorTransport(
        [
            ExpectedDescriptorResponse(
                reference_url, torrent_fixture("reference-multifile.torrent.b64")
            ),
            ExpectedDescriptorResponse(patch_url, torrent_fixture("install-bundle.torrent.b64")),
        ]
    )
    target = TargetRegistry.load().get("wot-eu")
    resolve_policy = ResolvePolicy(http_attempts=1)
    acquisition_policy = AcquisitionPolicy(http_attempts=1, reserve_bytes=100)
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: create_resolve_implementation(
                target,
                transport=protocol,
                policy=resolve_policy,
            ),
            Stage.PLAN_ACQUISITION: create_acquisition_implementation(
                target,
                protocol_transport=protocol,
                descriptor_transport=descriptors,
                policy=acquisition_policy,
            ),
        },
    )

    report = pipeline.start(
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",)),
        Stage.PLAN_ACQUISITION,
    )
    plan = load_plan(workspace, report.run_id)

    assert report.state is RunState.PAUSED
    assert [part.acquisition_mode for part in plan.parts] == [
        AcquisitionMode.REFERENCE,
        AcquisitionMode.INSTALL_BUNDLE,
        AcquisitionMode.INSTALL_BUNDLE,
    ]
    client, sd_content, locale = plan.parts
    assert (client.part, sd_content.part, locale.part) == (
        PartName.CLIENT,
        PartName.SD_CONTENT,
        PartName.LOCALE,
    )
    assert len(client.artifacts) == 2
    assert {
        artifact.source_hash.value for artifact in client.artifacts if artifact.source_hash
    } == {
        "11" * 20,
        "22" * 20,
    }
    assert any(artifact.path.utf8 is None for artifact in client.artifacts)
    assert all(
        url.startswith("https://") for artifact in client.artifacts for url in artifact.source_urls
    )
    assert any("%FF.bin" in url for artifact in client.artifacts for url in artifact.source_urls)
    assert len(sd_content.artifacts) == 2
    assert [
        artifact.split_segment.index for artifact in sd_content.artifacts if artifact.split_segment
    ] == [
        1,
        2,
    ]
    assert all(
        artifact.split_segment is not None and artifact.split_segment.count == 2
        for artifact in sd_content.artifacts
    )
    assert locale.language == "EN"
    assert locale.artifacts[0].path.utf8 == "bundle-release/locale.wgpkg"
    assert len(plan.descriptors) == 2
    assert descriptors.calls == [reference_url, patch_url]
    assert plan.raw_responses[0].unknown_top_level_fields == ("future_integrity_field",)
    assert plan.disk_space.descriptor_bytes == 348 + 407
    assert plan.disk_space.download_bytes == 15
    assert plan.disk_space.assembled_bytes == 21
    assert plan.disk_space.required_free_bytes == 348 + 407 + 15 + 21 + 100

    integrity_calls = protocol.calls[2:]
    assert len(integrity_calls) == 3
    assert [
        next(key for key in params if key.endswith("_check_version"))
        for _host, _path, params in integrity_calls
    ] == [
        "client_check_version",
        "sdcontent_check_version",
        "locale_check_version",
    ]
    assert all(params["locale_lang"] == "EN" for _host, _path, params in integrity_calls)
    assert protocol.responses == []
    assert descriptors.responses == []

    protocol_call_count = len(protocol.calls)
    descriptor_call_count = len(descriptors.calls)
    resumed = pipeline.resume(report.run_id, Stage.PLAN_ACQUISITION)
    assert resumed.state is RunState.PAUSED
    assert len(protocol.calls) == protocol_call_count
    assert len(descriptors.calls) == descriptor_call_count


def integrity_for_lesta(part: str, version: str) -> bytes:
    return (
        xml_fixture("lesta/integrity_client.xml")
        .replace(b"<part>client</part>", f"<part>{part}</part>".encode())
        .replace(b"<version>1.44.0.7899</version>", f"<version>{version}</version>".encode())
    )


def test_lesta_plan_skips_integrity_for_false_locale_parts(tmp_path: Path) -> None:
    host = "https://lstus-ru.lesta.ru"
    protocol = ScriptedProtocolTransport(
        [
            ExpectedProtocolResponse(host, "/api/v1/metadata/", xml_fixture("lesta/metadata.xml")),
            ExpectedProtocolResponse(
                host, "/api/v1/patches_chain/", xml_fixture("lesta/patches_be.xml")
            ),
            ExpectedProtocolResponse(
                host, "/api/v1/patches_chain/", xml_fixture("lesta/patches_ru.xml")
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                integrity_for_lesta("client", "1.44.0.7899"),
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                integrity_for_lesta("sdcontent", "1.44.0.7721"),
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                integrity_for_lesta("hdcontent", "1.44.0.7721"),
            ),
        ]
    )
    descriptors = ScriptedDescriptorTransport(
        [
            ExpectedDescriptorResponse(
                "https://cdn.example.test/reference/client.torrent",
                torrent_fixture("reference-multifile.torrent.b64"),
            ),
            ExpectedDescriptorResponse(
                "https://cdn.example.test/patch/bundle.torrent",
                torrent_fixture("install-bundle.torrent.b64"),
            ),
        ]
    )
    target = TargetRegistry.load().get("mt-ru")
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(
        workspace,
        {
            Stage.RESOLVE: create_resolve_implementation(
                target,
                transport=protocol,
                policy=ResolvePolicy(http_attempts=1),
            ),
            Stage.PLAN_ACQUISITION: create_acquisition_implementation(
                target,
                protocol_transport=protocol,
                descriptor_transport=descriptors,
                policy=AcquisitionPolicy(http_attempts=1, reserve_bytes=0),
            ),
        },
    )

    report = pipeline.start(
        RunRequest(target="mt-ru", client_type=ClientType.HD, languages=("RU", "BE")),
        Stage.PLAN_ACQUISITION,
    )
    plan = load_plan(workspace, report.run_id)

    assert [part.part for part in plan.parts] == [
        PartName.CLIENT,
        PartName.SD_CONTENT,
        PartName.HD_CONTENT,
        PartName.LOCALE,
        PartName.LOCALE,
    ]
    assert [part.language for part in plan.parts[-2:]] == ["BE", "RU"]
    assert all(part.acquisition_mode is AcquisitionMode.INSTALL_BUNDLE for part in plan.parts[-2:])
    integrity_calls = [call for call in protocol.calls if call[1] == "/api/v2/integrity_check/"]
    assert len(integrity_calls) == 3
    assert {raw.part for raw in plan.raw_responses} == {
        PartName.CLIENT,
        PartName.SD_CONTENT,
        PartName.HD_CONTENT,
    }
    assert descriptors.calls == [
        "https://cdn.example.test/reference/client.torrent",
        "https://cdn.example.test/patch/bundle.torrent",
    ]


def test_descriptor_hash_mismatch_is_artifact_corrupt(tmp_path: Path) -> None:
    host = "https://wgus-woteu.wargaming.net"
    protocol = ScriptedProtocolTransport(
        [
            ExpectedProtocolResponse(
                host, "/api/v1/metadata/", xml_fixture("wargaming/metadata.xml")
            ),
            ExpectedProtocolResponse(
                host, "/api/v1/patches_chain/", xml_fixture("wargaming/patches_en.xml")
            ),
            ExpectedProtocolResponse(
                host,
                "/api/v2/integrity_check/",
                xml_fixture("wargaming/integrity_client.xml"),
            ),
        ]
    )
    descriptors = ScriptedDescriptorTransport(
        [
            ExpectedDescriptorResponse(
                "https://cdn.example.test/reference/client.torrent",
                b"not the declared torrent",
            )
        ]
    )
    target = TargetRegistry.load().get("wot-eu")
    pipeline = Pipeline(
        Workspace(tmp_path),
        {
            Stage.RESOLVE: create_resolve_implementation(
                target,
                transport=protocol,
                policy=ResolvePolicy(http_attempts=1),
            ),
            Stage.PLAN_ACQUISITION: create_acquisition_implementation(
                target,
                protocol_transport=protocol,
                descriptor_transport=descriptors,
                policy=AcquisitionPolicy(http_attempts=1),
            ),
        },
    )

    with pytest.raises(StageFailedError) as raised:
        pipeline.start(
            RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",)),
            Stage.PLAN_ACQUISITION,
        )

    assert raised.value.error.code == "artifact_corrupt"
    assert raised.value.error.exception_type == ArtifactCorruptError.__name__

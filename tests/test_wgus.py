from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest

from game_downloader.models import (
    ChainBasis,
    ClientType,
    PartName,
    ResolveResult,
    RunRequest,
    RunState,
    Stage,
    StageResult,
)
from game_downloader.pipeline import Pipeline
from game_downloader.wgus import (
    ProtocolIncompatibleError,
    ResolvePolicy,
    SourceChangedError,
    SourceUnavailableError,
    TargetConfigurationError,
    TargetRegistry,
    TransportResponse,
    WgusResolver,
    create_resolve_implementation,
)
from game_downloader.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures/wgus"


def fixture(relative_path: str) -> bytes:
    return (FIXTURES / relative_path).read_bytes()


@dataclass(frozen=True, slots=True)
class ExpectedResponse:
    host: str
    path: str
    body: bytes
    status_code: int = 200
    redirect_urls: tuple[str, ...] = ()


class ScriptedTransport:
    def __init__(self, responses: list[ExpectedResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def get(
        self,
        host: str,
        path: str,
        params: Mapping[str, str],
    ) -> TransportResponse:
        assert self.responses, f"unexpected request: {host}{path}"
        expected = self.responses.pop(0)
        assert (host, path) == (expected.host, expected.path)
        copied_params = dict(params)
        self.calls.append((host, path, copied_params))
        query = urlencode(tuple(sorted(copied_params.items())))
        url = f"{host}{path}?{query}"
        return TransportResponse(
            status_code=expected.status_code,
            body=expected.body,
            request_url=url,
            final_url=url,
            redirect_urls=expected.redirect_urls,
        )


def resolver_for(
    target_id: str,
    responses: list[ExpectedResponse],
    *,
    max_metadata_refreshes: int = 2,
) -> tuple[WgusResolver, ScriptedTransport]:
    target = TargetRegistry.load().get(target_id)
    transport = ScriptedTransport(responses)
    policy = ResolvePolicy(
        http_attempts=1,
        max_metadata_refreshes=max_metadata_refreshes,
    )
    return WgusResolver(target, transport, policy), transport


def test_target_config_loads_known_publishers_and_rejects_unknown_target() -> None:
    registry = TargetRegistry.load()

    assert registry.get("wot-eu").app_id == "WOT.EU.PRODUCTION"
    assert registry.get("mt-ru").metadata_protocol == "7.6"
    assert registry.get("wot-cn").host.startswith("https://")
    with pytest.raises(TargetConfigurationError, match="unknown target"):
        registry.get("does-not-exist")


def test_wargaming_resolve_builds_exact_version_vector_and_preserves_raw_xml() -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, transport = resolver_for(
        "wot-eu",
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("wargaming/patches_en.xml")),
        ],
    )

    result = resolver.resolve(
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",))
    )

    assert result.release_name == "2.3.1.5400"
    assert result.metadata.metadata_version == "20260310101200"
    assert result.metadata.observed_protocol_version == "7.2"
    assert result.metadata.requested_protocol_version == "7.5"
    versions = {(part.name, part.language): part.version for part in result.version_vector}
    assert versions == {
        (PartName.CLIENT, None): "2.3.1.23990",
        (PartName.SD_CONTENT, None): "2.3.1.23990",
        (PartName.LOCALE, "EN"): "2.3.1.2598153",
    }
    client = next(part for part in result.version_vector if part.name is PartName.CLIENT)
    assert [transition.version_from for transition in client.transitions] == ["0", "2.3.1.23950"]
    assert client.chain_basis is ChainBasis.EXPLICIT
    assert result.raw_responses[0].raw_xml == fixture("wargaming/metadata.xml").decode()
    assert result.raw_responses[0].unknown_top_level_fields == ("future_extension",)
    patch_params = transport.calls[1][2]
    assert {
        key: value for key, value in patch_params.items() if key.endswith("_current_version")
    } == {
        "client_current_version": "0",
        "locale_current_version": "0",
        "sdcontent_current_version": "0",
    }
    assert transport.responses == []


def test_all_languages_resolves_every_language_supported_by_pinned_metadata() -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, transport = resolver_for(
        "wot-eu",
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("wargaming/patches_en.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("wargaming/patches_en.xml")),
        ],
    )

    result = resolver.resolve(
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("all",))
    )

    assert result.languages == ("EN", "RU")
    assert [call[2]["lang"] for call in transport.calls[1:]] == ["EN", "RU"]
    locale_languages = {
        part.language for part in result.version_vector if part.name is PartName.LOCALE
    }
    assert locale_languages == {"EN", "RU"}
    assert transport.responses == []


def test_lesta_resolve_keeps_locale_per_language_and_ordered_zero_state_chain() -> None:
    host = "https://lstus-ru.lesta.ru"
    resolver, transport = resolver_for(
        "mt-ru",
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("lesta/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("lesta/patches_be.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("lesta/patches_ru.xml")),
        ],
    )

    result = resolver.resolve(
        RunRequest(target="mt-ru", client_type=ClientType.HD, languages=("RU", "BE"))
    )

    assert result.languages == ("BE", "RU")
    versions = {(part.name, part.language): part.version for part in result.version_vector}
    assert versions[(PartName.LOCALE, "BE")] == "1.37.0.4001"
    assert versions[(PartName.LOCALE, "RU")] == "1.37.0.4000"
    assert versions[(PartName.CLIENT, None)] == "1.44.0.7899"
    assert versions[(PartName.HD_CONTENT, None)] == "1.44.0.7721"
    client = next(part for part in result.version_vector if part.name is PartName.CLIENT)
    assert client.chain_basis is ChainBasis.ORDERED_ZERO_STATE
    assert all(transition.version_from is None for transition in client.transitions)
    assert [call[2]["lang"] for call in transport.calls[1:]] == ["BE", "RU"]
    assert transport.responses == []


def test_changed_game_info_is_bounded_and_recorded_separately() -> None:
    legacy_host = "https://legacy.example.test"
    canonical_host = "https://canonical.example.test"
    target = (
        TargetRegistry.load()
        .get("wot-eu")
        .model_copy(update={"host": legacy_host, "app_id": "WOT.LEGACY.PRODUCTION"})
    )
    transport = ScriptedTransport(
        [
            ExpectedResponse(legacy_host, "/api/v1/metadata/", fixture("changed_game_info.xml")),
            ExpectedResponse(
                canonical_host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")
            ),
            ExpectedResponse(
                canonical_host,
                "/api/v1/patches_chain/",
                fixture("wargaming/patches_en.xml"),
            ),
        ]
    )
    policy = ResolvePolicy(http_attempts=1)

    result = WgusResolver(target, transport, policy).resolve(
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",))
    )

    assert result.resolved_target.api_host == canonical_host
    assert result.resolved_target.app_id == "WOT.EU.PRODUCTION"
    assert len(result.resolved_target.application_redirects) == 1
    assert [raw.kind for raw in result.raw_responses] == [
        "changed_game_info",
        "metadata",
        "patches_chain",
    ]
    assert transport.calls[0][2]["guid"] == "WOT.LEGACY.PRODUCTION"
    assert transport.calls[1][2]["guid"] == "WOT.EU.PRODUCTION"


def test_http_redirect_trace_does_not_become_an_application_redirect() -> None:
    host = "https://wgus-woteu.wargaming.net"
    redirected = (
        f"{host}/legacy-metadata",
        f"{host}/api/v1/metadata/",
    )
    resolver, _transport = resolver_for(
        "wot-eu",
        [
            ExpectedResponse(
                host,
                "/api/v1/metadata/",
                fixture("wargaming/metadata.xml"),
                redirect_urls=redirected,
            ),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("wargaming/patches_en.xml")),
        ],
    )

    result = resolver.resolve(
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",))
    )

    assert result.resolved_target.application_redirects == ()
    assert result.raw_responses[0].http_redirects == redirected


def test_changed_game_info_redirect_loop_is_a_protocol_error() -> None:
    legacy_host = "https://legacy.example.test"
    canonical_host = "https://canonical.example.test"
    target = (
        TargetRegistry.load()
        .get("wot-eu")
        .model_copy(update={"host": legacy_host, "app_id": "WOT.LEGACY.PRODUCTION"})
    )
    transport = ScriptedTransport(
        [
            ExpectedResponse(legacy_host, "/api/v1/metadata/", fixture("changed_game_info.xml")),
            ExpectedResponse(canonical_host, "/api/v1/metadata/", fixture("changed_game_info.xml")),
        ]
    )

    with pytest.raises(ProtocolIncompatibleError, match="redirect loop") as raised:
        WgusResolver(target, transport, ResolvePolicy(http_attempts=1)).resolve(
            RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",))
        )
    assert raised.value.error.code == "protocol_incompatible"


def test_meta_need_update_refetches_metadata_with_a_bounded_retry() -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, transport = resolver_for(
        "wot-eu",
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("meta_need_update.xml")),
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("wargaming/patches_en.xml")),
        ],
        max_metadata_refreshes=1,
    )

    result = resolver.resolve(
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",))
    )

    assert [raw.attempt for raw in result.raw_responses] == [1, 1, 2, 2]
    assert len(transport.calls) == 4


def test_meta_need_update_exhaustion_has_stable_source_changed_error() -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, _transport = resolver_for(
        "wot-eu",
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("meta_need_update.xml")),
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("meta_need_update.xml")),
        ],
        max_metadata_refreshes=1,
    )

    with pytest.raises(SourceChangedError) as raised:
        resolver.resolve(RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",)))
    assert raised.value.error.code == "source_changed"


def test_http_failure_has_stable_source_unavailable_error() -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, _transport = resolver_for(
        "wot-eu",
        [ExpectedResponse(host, "/api/v1/metadata/", b"unavailable", status_code=503)],
    )

    with pytest.raises(SourceUnavailableError) as raised:
        resolver.resolve(RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",)))
    assert raised.value.error.code == "source_unavailable"


def test_unsupported_language_fails_before_patches_request() -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, transport = resolver_for(
        "wot-eu",
        [ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml"))],
    )

    with pytest.raises(ProtocolIncompatibleError, match="not supported"):
        resolver.resolve(RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("DE",)))
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "body, message",
    [
        (fixture("error.xml"), "protocol error 101"),
        (
            b'<!DOCTYPE protocol [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<protocol name="app_metadata" version="7.5"><version>&xxe;</version></protocol>',
            "unsafe or malformed",
        ),
    ],
)
def test_http_200_error_body_and_unsafe_xml_are_not_success(body: bytes, message: str) -> None:
    host = "https://wgus-woteu.wargaming.net"
    resolver, _transport = resolver_for(
        "wot-eu",
        [ExpectedResponse(host, "/api/v1/metadata/", body)],
    )

    with pytest.raises(ProtocolIncompatibleError, match=message):
        resolver.resolve(RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",)))


def test_disconnected_part_graph_is_rejected_without_filename_inference() -> None:
    host = "https://wgus-woteu.wargaming.net"
    disconnected = fixture("wargaming/patches_en.xml").replace(
        b"<version_from>0</version_from>\n      <version_to>2.3.1.23950</version_to>",
        b"<version_from>orphan</version_from>\n      <version_to>2.3.1.23950</version_to>",
    )
    resolver, _transport = resolver_for(
        "wot-eu",
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", disconnected),
        ],
    )

    with pytest.raises(ProtocolIncompatibleError, match="zero-state"):
        resolver.resolve(RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",)))


def test_pipeline_commits_immutable_resolve_and_resume_does_not_refetch(tmp_path: Path) -> None:
    host = "https://wgus-woteu.wargaming.net"
    target = TargetRegistry.load().get("wot-eu")
    transport = ScriptedTransport(
        [
            ExpectedResponse(host, "/api/v1/metadata/", fixture("wargaming/metadata.xml")),
            ExpectedResponse(host, "/api/v1/patches_chain/", fixture("wargaming/patches_en.xml")),
        ]
    )
    implementation = create_resolve_implementation(
        target,
        transport=transport,
        policy=ResolvePolicy(http_attempts=1),
    )
    workspace = Workspace(tmp_path)
    pipeline = Pipeline(workspace, {Stage.RESOLVE: implementation})
    request = RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("EN",))

    completed = pipeline.start(request, Stage.RESOLVE)
    call_count = len(transport.calls)
    resumed = pipeline.resume(completed.run_id, Stage.RESOLVE)

    assert completed.state is RunState.PAUSED
    assert resumed.state is RunState.PAUSED
    assert len(transport.calls) == call_count
    result_path = workspace.stage_path(completed.run_id, Stage.RESOLVE) / "result.json"
    stage_result = StageResult.model_validate_json(result_path.read_bytes())
    resolved = ResolveResult.model_validate(stage_result.payload)
    assert resolved.version_vector[0].version == "2.3.1.23990"

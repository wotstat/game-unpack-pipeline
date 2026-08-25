from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree.ElementTree import Element

import httpx
import yaml
from defusedxml.ElementTree import fromstring as safe_xml_fromstring
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from game_downloader._json import JsonValue
from game_downloader.models import (
    ApplicationRedirect,
    ChainBasis,
    ChangedGameInfo,
    ClientPartMetadata,
    ClientType,
    ClientTypeMetadata,
    IntegrityCheckDocument,
    IntegrityTorrent,
    PartName,
    PatchesChainDocument,
    PatchFile,
    PatchTorrent,
    PatchTransition,
    ProtocolWebSeed,
    Publisher,
    RawProtocolResponse,
    ResolvedMetadata,
    ResolvedPart,
    ResolvedTarget,
    ResolveResult,
    RunRequest,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation

_METADATA_PATH = "/api/v1/metadata/"
_PATCHES_CHAIN_PATH = "/api/v1/patches_chain/"
_INTEGRITY_CHECK_PATH = "/api/v2/integrity_check/"
_SENSITIVE_QUERY_PARTS = ("credential", "key", "password", "secret", "signature", "token")


class TargetConfigurationError(ValueError):
    pass


class TransportFailure(RuntimeError):
    pass


class ResponseLimitExceeded(TransportFailure):
    pass


class SourceUnavailableError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("source_unavailable", message)


class SourceChangedError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("source_changed", message)


class ProtocolIncompatibleError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("protocol_incompatible", message)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_host(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("WGUS host must use http or https")
    if parsed.hostname is None:
        raise ValueError("WGUS host must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("WGUS host must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("WGUS host must be an origin URL without path, query, or fragment")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


class TargetConfig(_FrozenModel):
    target_id: str = Field(min_length=1)
    publisher: Publisher
    host: str
    app_id: str = Field(min_length=1)
    metadata_protocol: str = Field(min_length=1)
    patches_protocol: str = Field(min_length=1)
    integrity_protocol: str = Field(min_length=1)
    installation_id: str = Field(min_length=1)
    allow_http: bool = False

    @field_validator("target_id", "app_id", "metadata_protocol", "patches_protocol")
    @classmethod
    def values_are_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("configuration values must not have surrounding whitespace")
        return value

    @field_validator("host")
    @classmethod
    def host_is_canonical_origin(cls, value: str) -> str:
        return _canonical_host(value)

    @model_validator(mode="after")
    def http_requires_explicit_opt_in(self) -> TargetConfig:
        if self.host.startswith("http://") and not self.allow_http:
            raise ValueError("plain HTTP WGUS hosts require allow_http: true")
        return self


class _TargetDefaults(_FrozenModel):
    installation_id: str = Field(min_length=1)


class _TargetEntry(_FrozenModel):
    publisher: Publisher
    host: str
    app_id: str = Field(min_length=1)
    metadata_protocol: str = Field(min_length=1)
    patches_protocol: str = Field(min_length=1)
    integrity_protocol: str = Field(min_length=1)
    installation_id: str | None = None
    allow_http: bool = False


class _TargetsDocument(_FrozenModel):
    defaults: _TargetDefaults
    targets: dict[str, _TargetEntry] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class TargetRegistry:
    targets: Mapping[str, TargetConfig]
    source: str

    @classmethod
    def load(cls, path: Path | None = None) -> TargetRegistry:
        configured_path = path
        if configured_path is None:
            environment_path = os.environ.get("GAME_DOWNLOADER_TARGETS_CONFIG")
            if environment_path:
                configured_path = Path(environment_path)

        source: str
        try:
            if configured_path is not None:
                source = str(configured_path.absolute())
                raw = configured_path.read_bytes()
            else:
                repository_path = (
                    Path(__file__).resolve().parents[2] / "config/targets.example.yaml"
                )
                if repository_path.is_file():
                    source = str(repository_path)
                    raw = repository_path.read_bytes()
                else:
                    packaged = resources.files("game_downloader").joinpath("_config/targets.yaml")
                    source = "package:game_downloader/_config/targets.yaml"
                    raw = packaged.read_bytes()
        except OSError as exc:
            raise TargetConfigurationError(f"cannot read target configuration: {exc}") from exc

        if len(raw) > 1024 * 1024:
            raise TargetConfigurationError("target configuration exceeds the 1 MiB limit")
        try:
            loaded = yaml.safe_load(raw)
            document = _TargetsDocument.model_validate(loaded)
        except (UnicodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
            raise TargetConfigurationError(f"invalid target configuration {source}: {exc}") from exc

        targets: dict[str, TargetConfig] = {}
        for target_id, entry in document.targets.items():
            if not target_id or target_id != target_id.strip():
                raise TargetConfigurationError("target IDs must be non-empty and trimmed")
            installation_id = entry.installation_id or document.defaults.installation_id
            try:
                target = TargetConfig(
                    target_id=target_id,
                    installation_id=installation_id,
                    **entry.model_dump(exclude={"installation_id"}),
                )
            except ValidationError as exc:
                raise TargetConfigurationError(f"invalid target {target_id!r}: {exc}") from exc
            targets[target_id] = target
        return cls(targets=MappingProxyType(targets), source=source)

    def get(self, target_id: str) -> TargetConfig:
        try:
            return self.targets[target_id]
        except KeyError as exc:
            choices = ", ".join(sorted(self.targets))
            raise TargetConfigurationError(
                f"unknown target {target_id!r}; configured targets: {choices}"
            ) from exc


class ResolvePolicy(_FrozenModel):
    max_application_redirects: int = Field(default=5, ge=0, le=20)
    max_http_redirects: int = Field(default=5, ge=0, le=20)
    max_metadata_refreshes: int = Field(default=2, ge=0, le=10)
    http_attempts: int = Field(default=3, ge=1, le=10)
    max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_xml_elements: int = Field(default=100_000, ge=1)
    max_xml_depth: int = Field(default=64, ge=1)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    read_timeout_seconds: float = Field(default=30.0, gt=0)


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    body: bytes
    request_url: str
    final_url: str
    redirect_urls: tuple[str, ...] = ()


class HttpTransport(Protocol):
    def get(
        self,
        host: str,
        path: str,
        params: Mapping[str, str],
    ) -> TransportResponse: ...


class HttpxTransport:
    def __init__(self, policy: ResolvePolicy) -> None:
        self._policy = policy

    def get(
        self,
        host: str,
        path: str,
        params: Mapping[str, str],
    ) -> TransportResponse:
        timeout = httpx.Timeout(
            connect=self._policy.connect_timeout_seconds,
            read=self._policy.read_timeout_seconds,
            write=self._policy.read_timeout_seconds,
            pool=self._policy.connect_timeout_seconds,
        )
        try:
            with (
                httpx.Client(
                    follow_redirects=True,
                    max_redirects=self._policy.max_http_redirects,
                    timeout=timeout,
                    headers={"User-Agent": "game-downloader/0.1 WGUS resolver"},
                ) as client,
                client.stream(
                    "GET", f"{host}{path}", params=tuple(sorted(params.items()))
                ) as response,
            ):
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._policy.max_response_bytes:
                        raise ResponseLimitExceeded(
                            "WGUS response exceeds the configured byte limit"
                        )
                history = tuple(str(item.url) for item in response.history)
                redirects = history + ((str(response.url),) if history else ())
                return TransportResponse(
                    status_code=response.status_code,
                    body=bytes(body),
                    request_url=str(response.request.url),
                    final_url=str(response.url),
                    redirect_urls=redirects,
                )
        except (httpx.HTTPError, OSError) as exc:
            raise TransportFailure(f"WGUS request failed: {type(exc).__name__}: {exc}") from exc


def _request(
    transport: HttpTransport,
    policy: ResolvePolicy,
    host: str,
    path: str,
    params: Mapping[str, str],
    *,
    allow_http: bool,
) -> TransportResponse:
    last_error = "unknown transport failure"
    for _attempt in range(policy.http_attempts):
        try:
            response = transport.get(host, path, params)
        except ResponseLimitExceeded as exc:
            raise ProtocolIncompatibleError(str(exc)) from exc
        except TransportFailure as exc:
            last_error = str(exc)
            continue
        if response.status_code in {404, 429} or response.status_code >= 500:
            last_error = f"WGUS returned HTTP {response.status_code}"
            continue
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceUnavailableError(f"WGUS returned HTTP {response.status_code}")
        if len(response.redirect_urls) > policy.max_http_redirects + 1:
            raise SourceUnavailableError("WGUS exceeded the HTTP redirect limit")
        final_scheme = urlsplit(response.final_url).scheme.lower()
        if final_scheme == "http" and not allow_http:
            raise ProtocolIncompatibleError("WGUS redirected HTTPS traffic to plain HTTP")
        if final_scheme not in {"http", "https"}:
            raise ProtocolIncompatibleError("WGUS redirected to a non-HTTP URL")
        return response
    raise SourceUnavailableError(
        f"WGUS request failed after {policy.http_attempts} attempts: {last_error}"
    )


@dataclass(frozen=True, slots=True)
class _XmlDocument:
    root: Element
    raw_xml: str
    protocol_name: str
    protocol_version: str | None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_children(element: Element, name: str) -> list[Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _one_child(element: Element, name: str, *, required: bool = True) -> Element | None:
    matches = _direct_children(element, name)
    if len(matches) > 1:
        raise ProtocolIncompatibleError(f"XML field {name!r} occurs more than once")
    if not matches:
        if required:
            raise ProtocolIncompatibleError(f"required XML field {name!r} is missing")
        return None
    return matches[0]


def _element_text(element: Element, field: str) -> str:
    text = element.text.strip() if element.text else ""
    if not text:
        raise ProtocolIncompatibleError(f"XML field {field!r} must not be empty")
    return text


def _required_text(element: Element, name: str) -> str:
    child = _one_child(element, name)
    assert child is not None
    return _element_text(child, name)


def _optional_text(element: Element, name: str) -> str | None:
    child = _one_child(element, name, required=False)
    return None if child is None else _element_text(child, name)


def _descendant_text(element: Element, names: Sequence[str]) -> str | None:
    for wanted in names:
        for candidate in element.iter():
            if _local_name(candidate.tag) == wanted and candidate.text and candidate.text.strip():
                return candidate.text.strip()
    return None


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ProtocolIncompatibleError(f"XML field {field!r} is not a boolean: {value!r}")


def _parse_nonnegative_int(value: str, field: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ProtocolIncompatibleError(f"XML field {field!r} is not an integer") from exc
    if parsed < 0:
        raise ProtocolIncompatibleError(f"XML field {field!r} must be non-negative")
    return parsed


def _parse_xml(response: TransportResponse, policy: ResolvePolicy) -> _XmlDocument:
    if len(response.body) > policy.max_response_bytes:
        raise ProtocolIncompatibleError(
            f"WGUS XML response exceeds the {policy.max_response_bytes}-byte limit"
        )
    try:
        root = safe_xml_fromstring(response.body)
        raw_xml = response.body.decode("utf-8-sig")
    except Exception as exc:
        raise ProtocolIncompatibleError(f"unsafe or malformed WGUS XML: {exc}") from exc

    element_count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        element_count += 1
        if element_count > policy.max_xml_elements:
            raise ProtocolIncompatibleError("WGUS XML exceeds the element-count limit")
        if depth > policy.max_xml_depth:
            raise ProtocolIncompatibleError("WGUS XML exceeds the nesting-depth limit")
        stack.extend((child, depth + 1) for child in element)

    root_name = _local_name(root.tag)
    protocol_name = root.attrib.get("name", "").strip() if root_name == "protocol" else root_name
    protocol_version = root.attrib.get("version")
    if not protocol_name:
        raise ProtocolIncompatibleError("WGUS protocol response has no protocol name")
    if root_name == "error" or protocol_name.lower() in {"error", "protocol_error"}:
        code = _descendant_text(root, ("code", "error_code")) or "unknown"
        description = _descendant_text(root, ("description", "message", "error")) or "unknown"
        raise ProtocolIncompatibleError(f"WGUS protocol error {code}: {description}")
    return _XmlDocument(
        root=root,
        raw_xml=raw_xml,
        protocol_name=protocol_name,
        protocol_version=protocol_version,
    )


def _unknown_top_level(root: Element, known: set[str]) -> tuple[str, ...]:
    return tuple(
        sorted({_local_name(child.tag) for child in root if _local_name(child.tag) not in known})
    )


def _parse_metadata(document: _XmlDocument, requested_protocol: str) -> ResolvedMetadata:
    if document.protocol_name != "app_metadata":
        raise ProtocolIncompatibleError(
            f"expected app_metadata, received {document.protocol_name!r}"
        )
    if not document.protocol_version:
        raise ProtocolIncompatibleError("app_metadata response has no observed protocol version")
    metadata_version = _required_text(document.root, "version")
    predefined = _one_child(document.root, "predefined_section")
    assert predefined is not None
    app_id = _required_text(predefined, "app_id")
    chain_id = _required_text(predefined, "chain_id")
    languages_text = _required_text(predefined, "supported_languages")
    supported_languages = tuple(
        language.strip().upper() for language in languages_text.split(",") if language.strip()
    )
    if not supported_languages:
        raise ProtocolIncompatibleError("metadata supported_languages is empty")
    default_language = _required_text(predefined, "default_language").upper()

    client_types_element = _one_child(predefined, "client_types")
    assert client_types_element is not None
    client_types: list[ClientTypeMetadata] = []
    for client_element in _direct_children(client_types_element, "client_type"):
        raw_client_type = client_element.attrib.get("id", "").strip()
        if not raw_client_type:
            raise ProtocolIncompatibleError("metadata client_type has no id")
        try:
            client_type = ClientType(raw_client_type.lower())
        except ValueError as exc:
            raise ProtocolIncompatibleError(
                f"unsupported metadata client type {raw_client_type!r}"
            ) from exc
        parts_element = _one_child(client_element, "client_parts")
        assert parts_element is not None
        parts: list[ClientPartMetadata] = []
        for part_element in _direct_children(parts_element, "client_part"):
            raw_part = part_element.attrib.get("id", "").strip()
            if not raw_part:
                raise ProtocolIncompatibleError("metadata client_part has no id")
            try:
                part_name = PartName(raw_part)
            except ValueError as exc:
                raise ProtocolIncompatibleError(f"unsupported metadata Part {raw_part!r}") from exc
            raw_integrity = part_element.attrib.get("integrity")
            if raw_integrity is None:
                raise ProtocolIncompatibleError(f"metadata Part {raw_part!r} has no integrity flag")
            parts.append(
                ClientPartMetadata(
                    name=part_name,
                    integrity=_parse_bool(raw_integrity, "client_part.integrity"),
                    language_specific=_parse_bool(
                        part_element.attrib.get("lang", "false"), "client_part.lang"
                    ),
                    app_type=part_element.attrib.get("app_type") or None,
                )
            )
        if not parts:
            raise ProtocolIncompatibleError(
                f"metadata client type {client_type.value!r} contains no Parts"
            )
        client_types.append(
            ClientTypeMetadata(
                client_type=client_type,
                architecture=client_element.attrib.get("arch") or None,
                parts=tuple(parts),
            )
        )
    if not client_types:
        raise ProtocolIncompatibleError("metadata contains no client types")

    try:
        return ResolvedMetadata(
            requested_protocol_version=requested_protocol,
            observed_protocol_version=document.protocol_version,
            observed_publishers=document.root.attrib.get("wgc_publisher_id") or None,
            metadata_version=metadata_version,
            app_id=app_id,
            chain_id=chain_id,
            supported_languages=supported_languages,
            default_language=default_language,
            client_types=tuple(client_types),
        )
    except ValidationError as exc:
        raise ProtocolIncompatibleError(f"invalid app_metadata semantics: {exc}") from exc


def _parse_changed_game_info(document: _XmlDocument) -> ChangedGameInfo:
    if document.protocol_name != "changed_game_info":
        raise ProtocolIncompatibleError(
            f"expected changed_game_info, received {document.protocol_name!r}"
        )
    host = _descendant_text(
        document.root,
        ("new_host", "new_url", "new_game_url", "wgus_host", "host", "url"),
    )
    app_id = _descendant_text(
        document.root,
        ("new_app_id", "new_game_id", "app_id", "game_id", "guid"),
    )
    try:
        return ChangedGameInfo(
            observed_protocol_version=document.protocol_version,
            new_host=_canonical_host(host) if host is not None else None,
            new_app_id=app_id,
            unknown_top_level_fields=_unknown_top_level(
                document.root,
                {
                    "new_host",
                    "new_url",
                    "new_game_url",
                    "wgus_host",
                    "host",
                    "url",
                    "new_app_id",
                    "new_game_id",
                    "app_id",
                    "game_id",
                    "guid",
                },
            ),
        )
    except (ValidationError, ValueError) as exc:
        raise ProtocolIncompatibleError(f"invalid changed_game_info semantics: {exc}") from exc


def _value_from_child_or_attribute(element: Element, name: str) -> str | None:
    attribute = element.attrib.get(name)
    if attribute is not None and attribute.strip():
        return attribute.strip()
    return _optional_text(element, name)


def _parse_patch_files(patch: Element) -> tuple[PatchFile, ...]:
    files_element = _one_child(patch, "files", required=False)
    if files_element is None:
        return ()
    parsed: list[PatchFile] = []
    for file_element in _direct_children(files_element, "file"):
        name = _value_from_child_or_attribute(file_element, "name")
        size = _value_from_child_or_attribute(file_element, "size")
        if name is None or size is None:
            continue
        unpacked = _value_from_child_or_attribute(file_element, "unpacked_size")
        diff = _value_from_child_or_attribute(
            file_element, "diff_size"
        ) or _value_from_child_or_attribute(file_element, "diffs_size")
        parsed.append(
            PatchFile(
                name=name,
                size=_parse_nonnegative_int(size, "file.size"),
                unpacked_size=(
                    _parse_nonnegative_int(unpacked, "file.unpacked_size")
                    if unpacked is not None
                    else None
                ),
                diff_size=(
                    _parse_nonnegative_int(diff, "file.diff_size") if diff is not None else None
                ),
            )
        )
    return tuple(parsed)


def _parse_patch_torrent(patch: Element) -> PatchTorrent | None:
    torrent_element = _one_child(patch, "torrent", required=False)
    if torrent_element is None:
        return None
    info_hash = _descendant_text(torrent_element, ("hash", "info_hash"))
    urls = tuple(
        _element_text(element, "torrent.url")
        for element in torrent_element.iter()
        if _local_name(element.tag) in {"url", "web_seed"} and element.text and element.text.strip()
    )
    if info_hash is None and not urls:
        return None
    try:
        return PatchTorrent(info_hash=info_hash, urls=urls)
    except ValidationError as exc:
        raise ProtocolIncompatibleError(f"invalid patch torrent semantics: {exc}") from exc


def _parse_web_seeds(root: Element) -> tuple[ProtocolWebSeed, ...]:
    container = _one_child(root, "web_seeds", required=False)
    if container is None:
        return ()
    seeds: list[ProtocolWebSeed] = []
    for element in _direct_children(container, "url"):
        raw_threads = element.attrib.get("threads", "1")
        threads = _parse_nonnegative_int(raw_threads, "web_seed.threads")
        if threads < 1:
            raise ProtocolIncompatibleError("web seed threads must be positive")
        seeds.append(
            ProtocolWebSeed(
                url=_element_text(element, "web_seed.url"),
                threads=threads,
            )
        )
    return tuple(seeds)


def _parse_patches_chain(document: _XmlDocument) -> PatchesChainDocument:
    if document.protocol_name != "patches_chain":
        raise ProtocolIncompatibleError(
            f"expected patches_chain, received {document.protocol_name!r}"
        )
    if not document.protocol_version:
        raise ProtocolIncompatibleError("patches_chain response has no observed protocol version")
    meta_need_update = _parse_bool(
        _required_text(document.root, "meta_need_update"), "meta_need_update"
    )
    unknown = _unknown_top_level(
        document.root,
        {
            "delay_preload",
            "meta_need_update",
            "parts_info",
            "patches_chain",
            "version_name",
            "web_seeds",
        },
    )
    if meta_need_update:
        return PatchesChainDocument(
            observed_protocol_version=document.protocol_version,
            observed_publishers=document.root.attrib.get("wgc_publisher_id") or None,
            meta_need_update=True,
            web_seeds=_parse_web_seeds(document.root),
            unknown_top_level_fields=unknown,
        )

    release_name = _required_text(document.root, "version_name")
    install_chains = [
        element
        for element in _direct_children(document.root, "patches_chain")
        if element.attrib.get("type", "").lower() == "install"
    ]
    if len(install_chains) != 1:
        raise ProtocolIncompatibleError(
            "patches_chain response must contain exactly one install chain"
        )
    transitions: list[PatchTransition] = []
    for patch in _direct_children(install_chains[0], "patch"):
        raw_part = _required_text(patch, "part")
        try:
            part = PartName(raw_part)
        except ValueError as exc:
            raise ProtocolIncompatibleError(f"unsupported patches_chain Part {raw_part!r}") from exc
        transitions.append(
            PatchTransition(
                part=part,
                version_from=_optional_text(patch, "version_from"),
                version_to=_required_text(patch, "version_to"),
                files=_parse_patch_files(patch),
                torrent=_parse_patch_torrent(patch),
            )
        )
    try:
        return PatchesChainDocument(
            observed_protocol_version=document.protocol_version,
            observed_publishers=document.root.attrib.get("wgc_publisher_id") or None,
            meta_need_update=False,
            release_name=release_name,
            transitions=tuple(transitions),
            web_seeds=_parse_web_seeds(document.root),
            unknown_top_level_fields=unknown,
        )
    except ValidationError as exc:
        raise ProtocolIncompatibleError(f"invalid patches_chain semantics: {exc}") from exc


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        redacted = (
            "REDACTED" if any(part in key.lower() for part in _SENSITIVE_QUERY_PARTS) else item
        )
        query.append((key, redacted))
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment))


def _raw_record(
    response: TransportResponse,
    document: _XmlDocument,
    *,
    attempt: int,
    kind: Literal["metadata", "changed_game_info", "patches_chain", "integrity_check"],
    language: str | None,
    unknown: tuple[str, ...],
    part: PartName | None = None,
) -> RawProtocolResponse:
    return RawProtocolResponse(
        attempt=attempt,
        kind=kind,
        part=part,
        language=language,
        request_url=_redact_url(response.request_url),
        final_url=_redact_url(response.final_url),
        http_redirects=tuple(_redact_url(value) for value in response.redirect_urls),
        observed_protocol_name=document.protocol_name,
        observed_protocol_version=document.protocol_version,
        unknown_top_level_fields=unknown,
        raw_xml=document.raw_xml,
    )


def parse_patches_chain_xml(
    raw_xml: str,
    policy: ResolvePolicy | None = None,
) -> PatchesChainDocument:
    response = TransportResponse(
        status_code=200,
        body=raw_xml.encode("utf-8"),
        request_url="https://fixture.invalid/patches_chain",
        final_url="https://fixture.invalid/patches_chain",
    )
    return _parse_patches_chain(_parse_xml(response, policy or ResolvePolicy()))


def _parse_integrity_check(
    document: _XmlDocument,
    requested_protocol: str,
) -> IntegrityCheckDocument:
    if document.protocol_name != "integrity_check":
        raise ProtocolIncompatibleError(
            f"expected integrity_check, received {document.protocol_name!r}"
        )
    if not document.protocol_version:
        raise ProtocolIncompatibleError("integrity_check response has no protocol version")
    torrents_element = _one_child(document.root, "torrents")
    assert torrents_element is not None
    torrents: list[IntegrityTorrent] = []
    for torrent in _direct_children(torrents_element, "torrent"):
        raw_part = _required_text(torrent, "part")
        try:
            part = PartName(raw_part)
        except ValueError as exc:
            raise ProtocolIncompatibleError(
                f"unsupported integrity_check Part {raw_part!r}"
            ) from exc
        try:
            parsed_torrent = IntegrityTorrent(
                part=part,
                version=_required_text(torrent, "version"),
                descriptor_url=_required_text(torrent, "file"),
                descriptor_sha256=_required_text(torrent, "hash"),
                blacklist_url=_optional_text(torrent, "blacklist"),
            )
        except ValidationError as exc:
            raise ProtocolIncompatibleError(
                f"invalid integrity_check torrent semantics: {exc}"
            ) from exc
        torrents.append(parsed_torrent)
    unknown = _unknown_top_level(document.root, {"torrents", "web_seeds"})
    try:
        return IntegrityCheckDocument(
            requested_protocol_version=requested_protocol,
            observed_protocol_version=document.protocol_version,
            observed_publishers=document.root.attrib.get("wgc_publisher_id") or None,
            torrents=tuple(torrents),
            web_seeds=_parse_web_seeds(document.root),
            unknown_top_level_fields=unknown,
        )
    except ValidationError as exc:
        raise ProtocolIncompatibleError(f"invalid integrity_check semantics: {exc}") from exc


class WgusIntegrityClient:
    def __init__(
        self,
        target: TargetConfig,
        transport: HttpTransport,
        policy: ResolvePolicy,
    ) -> None:
        self._target = target
        self._transport = transport
        self._policy = policy

    def check(
        self,
        resolved: ResolveResult,
        part: ResolvedPart,
        language: str,
    ) -> tuple[IntegrityCheckDocument, RawProtocolResponse]:
        if resolved.resolved_target.target != self._target.target_id:
            raise ProtocolIncompatibleError("integrity client TargetConfig does not match resolve")
        params = {
            "chain_id": resolved.chain_id,
            "game_id": resolved.resolved_target.app_id,
            "locale_lang": language,
            "protocol_version": self._target.integrity_protocol,
            f"{part.name.value}_check_version": part.version,
        }
        response = _request(
            self._transport,
            self._policy,
            resolved.resolved_target.api_host,
            _INTEGRITY_CHECK_PATH,
            params,
            allow_http=self._target.allow_http,
        )
        xml = _parse_xml(response, self._policy)
        document = _parse_integrity_check(xml, self._target.integrity_protocol)
        raw = _raw_record(
            response,
            xml,
            attempt=1,
            kind="integrity_check",
            part=part.name,
            language=language,
            unknown=document.unknown_top_level_fields,
        )
        return document, raw


def _part_signature(part: ResolvedPart) -> tuple[object, ...]:
    return (
        part.name,
        part.version,
        part.chain_basis,
        tuple((transition.version_from, transition.version_to) for transition in part.transitions),
    )


def _connected_transitions(
    part: PartName,
    transitions: Sequence[PatchTransition],
    publisher: Publisher,
) -> tuple[tuple[PatchTransition, ...], ChainBasis]:
    if not transitions:
        raise ProtocolIncompatibleError(f"patches_chain contains no transitions for {part.value}")
    missing = [transition for transition in transitions if transition.version_from is None]
    if publisher is Publisher.LESTA and len(missing) == len(transitions) and len(transitions) > 1:
        targets = [transition.version_to for transition in transitions]
        if len(targets) != len(set(targets)):
            raise ProtocolIncompatibleError(
                f"Lesta zero-state install list for {part.value} contains duplicate targets"
            )
        return tuple(transitions), ChainBasis.ORDERED_ZERO_STATE
    if len(missing) > 1:
        raise ProtocolIncompatibleError(
            f"Part {part.value} has multiple transitions without version_from"
        )

    by_source: dict[str, PatchTransition] = {}
    for transition in transitions:
        source = transition.version_from if transition.version_from is not None else "0"
        if source in by_source:
            raise ProtocolIncompatibleError(f"Part {part.value} branches from version {source!r}")
        by_source[source] = transition

    cursor = "0"
    visited_versions = {cursor}
    connected: list[PatchTransition] = []
    while cursor in by_source:
        transition = by_source[cursor]
        connected.append(transition)
        cursor = transition.version_to
        if cursor in visited_versions:
            raise ProtocolIncompatibleError(f"Part {part.value} version graph contains a cycle")
        visited_versions.add(cursor)
    if not connected:
        raise ProtocolIncompatibleError(
            f"Part {part.value} version graph has no zero-state transition"
        )
    if len(connected) != len(transitions):
        raise ProtocolIncompatibleError(
            f"Part {part.value} version graph is disconnected from zero state"
        )
    return tuple(connected), ChainBasis.EXPLICIT


class WgusResolver:
    def __init__(
        self,
        target: TargetConfig,
        transport: HttpTransport,
        policy: ResolvePolicy,
    ) -> None:
        self._target = target
        self._transport = transport
        self._policy = policy

    def resolve(self, request: RunRequest) -> ResolveResult:
        if request.target != self._target.target_id:
            raise ProtocolIncompatibleError(
                f"resolver configured for {self._target.target_id!r}, not {request.target!r}"
            )

        host = self._target.host
        app_id = self._target.app_id
        raw_responses: list[RawProtocolResponse] = []
        application_redirects: list[ApplicationRedirect] = []
        last_change = "metadata requested an update"

        for attempt in range(1, self._policy.max_metadata_refreshes + 2):
            metadata, host, app_id, redirects, metadata_raw = self._load_metadata(
                host, app_id, attempt
            )
            raw_responses.extend(metadata_raw)
            application_redirects.extend(redirects)
            languages = (
                tuple(sorted(metadata.supported_languages))
                if request.selects_all_languages
                else request.languages
            )
            selected = self._validate_request(metadata, request, languages)

            language_documents: list[tuple[str, PatchesChainDocument]] = []
            refresh_requested = False
            for language in languages:
                patches, raw = self._load_patches(
                    host,
                    app_id,
                    metadata,
                    selected,
                    language,
                    request.client_type,
                    attempt,
                )
                raw_responses.append(raw)
                if patches.meta_need_update:
                    refresh_requested = True
                    last_change = f"patches_chain for {language} requested fresh metadata"
                    break
                language_documents.append((language, patches))
            if refresh_requested:
                continue

            try:
                release_name, version_vector = self._build_version_vector(
                    selected, language_documents
                )
            except SourceChangedError as exc:
                last_change = str(exc)
                continue
            return ResolveResult(
                resolved_target=ResolvedTarget(
                    target=self._target.target_id,
                    publisher=self._target.publisher,
                    api_host=host,
                    app_id=app_id,
                    application_redirects=tuple(application_redirects),
                ),
                chain_id=metadata.chain_id,
                client_type=request.client_type,
                languages=languages,
                metadata_version=metadata.metadata_version,
                release_name=release_name,
                metadata=metadata,
                version_vector=version_vector,
                raw_responses=tuple(raw_responses),
            )

        raise SourceChangedError(
            f"WGUS source did not stabilize after bounded metadata refreshes: {last_change}"
        )

    def _load_metadata(
        self,
        initial_host: str,
        initial_app_id: str,
        attempt: int,
    ) -> tuple[
        ResolvedMetadata,
        str,
        str,
        tuple[ApplicationRedirect, ...],
        tuple[RawProtocolResponse, ...],
    ]:
        host = initial_host
        app_id = initial_app_id
        visited = {(host, app_id)}
        redirects: list[ApplicationRedirect] = []
        raw_responses: list[RawProtocolResponse] = []
        for _hop in range(self._policy.max_application_redirects + 1):
            response = _request(
                self._transport,
                self._policy,
                host,
                _METADATA_PATH,
                {
                    "chain_id": "unknown",
                    "guid": app_id,
                    "protocol_version": self._target.metadata_protocol,
                },
                allow_http=self._target.allow_http,
            )
            document = _parse_xml(response, self._policy)
            if document.protocol_name == "changed_game_info":
                changed = _parse_changed_game_info(document)
                raw_responses.append(
                    _raw_record(
                        response,
                        document,
                        attempt=attempt,
                        kind="changed_game_info",
                        language=None,
                        unknown=changed.unknown_top_level_fields,
                    )
                )
                new_host = changed.new_host or host
                new_app_id = changed.new_app_id or app_id
                if new_host.startswith("http://") and not self._target.allow_http:
                    raise ProtocolIncompatibleError(
                        "changed_game_info attempted an unconfigured HTTP downgrade"
                    )
                destination = (new_host, new_app_id)
                if destination in visited:
                    raise ProtocolIncompatibleError("changed_game_info contains a redirect loop")
                redirect = ApplicationRedirect(
                    from_host=host,
                    from_app_id=app_id,
                    to_host=new_host,
                    to_app_id=new_app_id,
                )
                redirects.append(redirect)
                visited.add(destination)
                host, app_id = destination
                continue

            metadata = _parse_metadata(document, self._target.metadata_protocol)
            unknown = _unknown_top_level(
                document.root,
                {"generated_section", "predefined_section", "version", "web_seeds"},
            )
            raw_responses.append(
                _raw_record(
                    response,
                    document,
                    attempt=attempt,
                    kind="metadata",
                    language=None,
                    unknown=unknown,
                )
            )
            if metadata.app_id != app_id:
                destination = (host, metadata.app_id)
                if destination in visited:
                    raise ProtocolIncompatibleError("metadata canonical app ID creates a loop")
                redirects.append(
                    ApplicationRedirect(
                        from_host=host,
                        from_app_id=app_id,
                        to_host=host,
                        to_app_id=metadata.app_id,
                    )
                )
                app_id = metadata.app_id
            return metadata, host, app_id, tuple(redirects), tuple(raw_responses)
        raise ProtocolIncompatibleError("changed_game_info exceeded the application redirect limit")

    def _validate_request(
        self,
        metadata: ResolvedMetadata,
        request: RunRequest,
        languages: tuple[str, ...],
    ) -> ClientTypeMetadata:
        selected = next(
            (
                client
                for client in metadata.client_types
                if client.client_type is request.client_type
            ),
            None,
        )
        if selected is None:
            raise ProtocolIncompatibleError(
                f"client type {request.client_type.value!r} is not supported by metadata"
            )
        unsupported = sorted(set(languages) - set(metadata.supported_languages))
        if unsupported:
            raise ProtocolIncompatibleError(
                f"languages are not supported by metadata: {', '.join(unsupported)}"
            )
        part_names = {part.name for part in selected.parts}
        required = {PartName.CLIENT, PartName.LOCALE, PartName.SD_CONTENT}
        if request.client_type is ClientType.HD:
            required.add(PartName.HD_CONTENT)
        missing = sorted(part.value for part in required - part_names)
        if missing:
            raise ProtocolIncompatibleError(
                f"metadata client type is missing required Parts: {', '.join(missing)}"
            )
        for part in selected.parts:
            if (part.name is PartName.LOCALE) != part.language_specific:
                raise ProtocolIncompatibleError(
                    f"metadata Part {part.name.value!r} has unsupported lang semantics"
                )
        return selected

    def _load_patches(
        self,
        host: str,
        app_id: str,
        metadata: ResolvedMetadata,
        selected: ClientTypeMetadata,
        language: str,
        client_type: ClientType,
        attempt: int,
    ) -> tuple[PatchesChainDocument, RawProtocolResponse]:
        params = {
            "client_type": client_type.value,
            "game_id": app_id,
            "installation_id": self._target.installation_id,
            "lang": language,
            "metadata_protocol_version": self._target.metadata_protocol,
            "metadata_version": metadata.metadata_version,
            "protocol_version": self._target.patches_protocol,
        }
        for part in selected.parts:
            params[f"{part.name.value}_current_version"] = "0"
        response = _request(
            self._transport,
            self._policy,
            host,
            _PATCHES_CHAIN_PATH,
            params,
            allow_http=self._target.allow_http,
        )
        document = _parse_xml(response, self._policy)
        patches = _parse_patches_chain(document)
        raw = _raw_record(
            response,
            document,
            attempt=attempt,
            kind="patches_chain",
            language=language,
            unknown=patches.unknown_top_level_fields,
        )
        return patches, raw

    def _build_version_vector(
        self,
        selected: ClientTypeMetadata,
        language_documents: Sequence[tuple[str, PatchesChainDocument]],
    ) -> tuple[str, tuple[ResolvedPart, ...]]:
        expected = {part.name: part for part in selected.parts}
        base_parts: dict[PartName, ResolvedPart] = {}
        locale_parts: list[ResolvedPart] = []
        release_name: str | None = None
        for language, document in language_documents:
            assert document.release_name is not None
            if release_name is None:
                release_name = document.release_name
            elif release_name != document.release_name:
                raise SourceChangedError("release name changed between locale requests")

            grouped: dict[PartName, list[PatchTransition]] = {}
            for transition in document.transitions:
                grouped.setdefault(transition.part, []).append(transition)
            missing = sorted(part.value for part in set(expected) - set(grouped))
            unexpected = sorted(part.value for part in set(grouped) - set(expected))
            if missing or unexpected:
                detail = []
                if missing:
                    detail.append(f"missing: {', '.join(missing)}")
                if unexpected:
                    detail.append(f"unexpected: {', '.join(unexpected)}")
                raise ProtocolIncompatibleError(
                    f"patches_chain Parts do not match metadata ({'; '.join(detail)})"
                )

            for name, metadata_part in expected.items():
                connected, basis = _connected_transitions(
                    name, grouped[name], self._target.publisher
                )
                resolved = ResolvedPart(
                    name=name,
                    language=language if name is PartName.LOCALE else None,
                    version=connected[-1].version_to,
                    integrity=metadata_part.integrity,
                    chain_basis=basis,
                    transitions=connected,
                )
                if name is PartName.LOCALE:
                    locale_parts.append(resolved)
                elif name not in base_parts:
                    base_parts[name] = resolved
                elif _part_signature(base_parts[name]) != _part_signature(resolved):
                    raise SourceChangedError(f"Part {name.value} changed between locale requests")

        if release_name is None:
            raise ProtocolIncompatibleError("no patches_chain locale responses were resolved")
        ordered_names = (PartName.CLIENT, PartName.SD_CONTENT, PartName.HD_CONTENT)
        vector = tuple(base_parts[name] for name in ordered_names if name in base_parts) + tuple(
            sorted(locale_parts, key=lambda part: part.language or "")
        )
        return release_name, vector


def create_resolve_implementation(
    target: TargetConfig,
    *,
    transport: HttpTransport | None = None,
    policy: ResolvePolicy | None = None,
) -> StageImplementation:
    selected_policy = policy or ResolvePolicy()
    resolver = WgusResolver(
        target,
        transport or HttpxTransport(selected_policy),
        selected_policy,
    )

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        result = resolver.resolve(context.request)
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = ResolveResult.model_validate(payload)
        if result.resolved_target.target != context.request.target:
            raise ValueError("pinned resolve target does not match its Run request")
        if result.client_type is not context.request.client_type:
            raise ValueError("pinned resolve client type does not match its Run request")
        expected_languages = (
            tuple(sorted(result.metadata.supported_languages))
            if context.request.selects_all_languages
            else context.request.languages
        )
        if result.languages != expected_languages:
            raise ValueError("pinned resolve languages do not match its Run request")

    return StageImplementation(
        implementation_version="wgus-resolve-v3",
        execute=execute,
        validate=validate,
        configuration={
            "parser": "defusedxml",
            "policy": cast(JsonValue, selected_policy.model_dump(mode="json")),
            "target": cast(JsonValue, target.model_dump(mode="json")),
        },
    )

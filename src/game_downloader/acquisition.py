from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Mapping, Sequence
from typing import Protocol, cast
from urllib.parse import quote_from_bytes, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import ConfigDict, Field

from game_downloader._json import JsonValue, canonical_sha256_digest
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    AcquisitionPlan,
    BytesPath,
    ChainBasis,
    DiskSpaceEstimate,
    FrozenModel,
    IntegrityCheckDocument,
    IntegrityTorrent,
    PartAcquisition,
    PatchTransition,
    ProtocolWebSeed,
    RawProtocolResponse,
    ResolvedPart,
    ResolvedWebSeed,
    ResolveResult,
    SourceHash,
    SplitSegment,
    Stage,
    TorrentDescriptorRecord,
    TorrentFile,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation
from game_downloader.torrent import (
    TorrentFormatError,
    TorrentLimits,
    UnsafeTorrentPathError,
    bytes_path_from_text,
    decode_bytes_path,
    parse_torrent,
    torrent_source_components,
)
from game_downloader.wgus import (
    HttpTransport,
    HttpxTransport,
    ProtocolIncompatibleError,
    ResolvePolicy,
    ResponseLimitExceeded,
    SourceUnavailableError,
    TargetConfig,
    TransportFailure,
    TransportResponse,
    WgusIntegrityClient,
    parse_patches_chain_xml,
)
from game_downloader.workspace import (
    BlobStore,
    BlobValidationError,
    CasCorruptionError,
)

_SPLIT_SUFFIX = re.compile(rb"^(?P<base>.+)\.(?P<index>[0-9]{3})$")


class ArtifactCorruptError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("artifact_corrupt", message)


class UnsafeArchiveError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("unsafe_archive", message)


class AcquisitionPolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reserve_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=0)
    http_attempts: int = Field(default=3, ge=1, le=10)
    max_http_redirects: int = Field(default=5, ge=0, le=20)
    max_xml_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_torrent_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    read_timeout_seconds: float = Field(default=30.0, gt=0)
    max_torrent_depth: int = Field(default=64, ge=1)
    max_torrent_values: int = Field(default=1_000_000, ge=1)
    max_torrent_files: int = Field(default=200_000, ge=1)
    max_torrent_pieces: int = Field(default=2_000_000, ge=1)

    def torrent_limits(self) -> TorrentLimits:
        return TorrentLimits(
            max_metainfo_bytes=self.max_torrent_bytes,
            max_depth=self.max_torrent_depth,
            max_values=self.max_torrent_values,
            max_files=self.max_torrent_files,
            max_pieces=self.max_torrent_pieces,
        )


class DescriptorTransport(Protocol):
    def get(self, url: str) -> TransportResponse: ...


class HttpxDescriptorTransport:
    def __init__(self, policy: AcquisitionPolicy) -> None:
        self._policy = policy

    def get(self, url: str) -> TransportResponse:
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
                    headers={"User-Agent": "game-downloader/0.1 acquisition planner"},
                ) as client,
                client.stream("GET", url) as response,
            ):
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._policy.max_torrent_bytes:
                        raise ResponseLimitExceeded(
                            "torrent descriptor exceeds the configured byte limit"
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
            raise TransportFailure(
                f"torrent descriptor request failed: {type(exc).__name__}: {exc}"
            ) from exc


class IntegritySource(Protocol):
    def check(
        self,
        resolved: ResolveResult,
        part: ResolvedPart,
        language: str,
    ) -> tuple[IntegrityCheckDocument, RawProtocolResponse]: ...


def _validate_download_url(url: str, *, allow_http: bool) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ProtocolIncompatibleError("source URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ProtocolIncompatibleError("source URL must not contain credentials")
    if parsed.fragment:
        raise ProtocolIncompatibleError("source URL must not contain a fragment")
    if parsed.scheme == "http" and not allow_http:
        raise ProtocolIncompatibleError("plain HTTP source URL is not explicitly allowed")
    return url


def _resolved_web_seeds(
    seeds: Sequence[ProtocolWebSeed],
    descriptor_urls: Sequence[str],
    *,
    allow_http: bool,
) -> tuple[ResolvedWebSeed, ...]:
    secure_hosts = {
        parsed.hostname
        for url in descriptor_urls
        if (parsed := urlsplit(url)).scheme == "https" and parsed.hostname is not None
    }
    result: list[ResolvedWebSeed] = []
    seen: set[str] = set()
    for seed in seeds:
        parsed = urlsplit(seed.url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ProtocolIncompatibleError("web seed must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ProtocolIncompatibleError("web seed must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ProtocolIncompatibleError("web seed query/fragment semantics are unsupported")
        effective_scheme = parsed.scheme
        if parsed.scheme == "http":
            if parsed.hostname in secure_hosts:
                effective_scheme = "https"
            elif not allow_http:
                raise ProtocolIncompatibleError(
                    "plain HTTP web seed has no same-host HTTPS descriptor proof"
                )
        path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
        effective = urlunsplit((effective_scheme, parsed.netloc, path, "", ""))
        if effective in seen:
            continue
        seen.add(effective)
        result.append(
            ResolvedWebSeed(
                declared_url=seed.url,
                effective_url=effective,
                threads=seed.threads,
            )
        )
    return tuple(result)


def _artifact_urls(
    seeds: Sequence[ResolvedWebSeed],
    components: Sequence[bytes],
) -> tuple[str, ...]:
    relative = "/".join(quote_from_bytes(component, safe="") for component in components)
    return tuple(urljoin(seed.effective_url, relative) for seed in seeds)


def _artifact_id(
    *,
    role: str,
    part: ResolvedPart,
    path: BytesPath,
    size: int,
    source_hash: SourceHash | None,
    descriptor_sha256: str,
    transition: PatchTransition | None,
) -> str:
    identity = {
        "descriptor_sha256": descriptor_sha256,
        "language": part.language,
        "part": part.name.value,
        "part_version": part.version,
        "path": path.model_dump(mode="json"),
        "role": role,
        "size": size,
        "source_hash": source_hash.model_dump(mode="json") if source_hash else None,
        "transition_from": transition.version_from if transition else None,
        "transition_to": transition.version_to if transition else None,
    }
    return canonical_sha256_digest(identity)


def _source_hash(file: TorrentFile) -> SourceHash | None:
    if file.source_sha1 is None:
        return None
    return SourceHash(algorithm="sha1", value=file.source_sha1)


def _split_segments(
    artifacts: Sequence[AcquisitionArtifact],
    part: ResolvedPart,
    transition: PatchTransition,
) -> tuple[AcquisitionArtifact, ...]:
    groups: dict[tuple[tuple[bytes, ...], bytes], list[tuple[int, int]]] = {}
    for artifact_index, artifact in enumerate(artifacts):
        components = decode_bytes_path(artifact.path)
        matched = _SPLIT_SUFFIX.fullmatch(components[-1])
        if matched is None:
            continue
        index = int(matched.group("index"))
        if index == 0:
            raise ProtocolIncompatibleError("split segment numbering must start at .001")
        key = (components[:-1], matched.group("base"))
        groups.setdefault(key, []).append((index, artifact_index))

    updated = list(artifacts)
    for (parent, base), members in groups.items():
        ordered = sorted(members)
        indices = [index for index, _artifact_index in ordered]
        if indices != list(range(1, len(indices) + 1)):
            raise ProtocolIncompatibleError("split delivery bundle segments are not contiguous")
        group_identity = {
            "base": base.hex(),
            "language": part.language,
            "parent": [value.hex() for value in parent],
            "part": part.name.value,
            "transition_to": transition.version_to,
        }
        group_id = canonical_sha256_digest(group_identity)
        for index, artifact_index in ordered:
            updated[artifact_index] = updated[artifact_index].model_copy(
                update={
                    "split_segment": SplitSegment(
                        group_id=group_id,
                        index=index,
                        count=len(ordered),
                    )
                }
            )
    return tuple(updated)


class AcquisitionPlanner:
    def __init__(
        self,
        target: TargetConfig,
        integrity: IntegritySource,
        descriptor_transport: DescriptorTransport,
        policy: AcquisitionPolicy,
    ) -> None:
        self._target = target
        self._integrity = integrity
        self._descriptor_transport = descriptor_transport
        self._policy = policy
        self._descriptors: dict[str, TorrentDescriptorRecord] = {}

    def plan(
        self,
        resolved: ResolveResult,
        blobs: BlobStore,
        resolve_result_sha256: str,
    ) -> AcquisitionPlan:
        if resolved.resolved_target.target != self._target.target_id:
            raise ProtocolIncompatibleError("Acquisition TargetConfig does not match resolve pin")
        self._descriptors = {}
        patch_seeds = self._patch_web_seeds(resolved)
        raw_responses: list[RawProtocolResponse] = []
        part_plans: list[PartAcquisition] = []

        for part in resolved.version_vector:
            language = part.language or resolved.languages[0]
            integrity_document: IntegrityCheckDocument | None = None
            exact_torrent: IntegrityTorrent | None = None
            if part.integrity:
                integrity_document, raw = self._integrity.check(resolved, part, language)
                raw_responses.append(raw)
                matches = [
                    torrent
                    for torrent in integrity_document.torrents
                    if torrent.part is part.name and torrent.version == part.version
                ]
                if len(matches) == 1 and len(integrity_document.torrents) == 1:
                    exact_torrent = matches[0]
                elif len(matches) > 1 or (matches and len(integrity_document.torrents) != 1):
                    raise ProtocolIncompatibleError(
                        "integrity_check returned ambiguous torrents for an exact Part request"
                    )

            if exact_torrent is not None and integrity_document is not None:
                part_plan = self._reference_plan(
                    part,
                    exact_torrent,
                    integrity_document.web_seeds,
                    blobs,
                )
            else:
                part_plan = self._install_plan(
                    part,
                    patch_seeds.get(language, ()),
                    blobs,
                )
            part_plans.append(part_plan)

        descriptors = tuple(self._descriptors[digest] for digest in sorted(self._descriptors))
        all_artifacts = [artifact for part in part_plans for artifact in part.artifacts]
        descriptor_bytes = sum(descriptor.blob_size for descriptor in descriptors)
        download_bytes = sum(artifact.size for artifact in all_artifacts)
        assembled_bytes = self._assembled_bytes(part_plans)
        disk_space = DiskSpaceEstimate(
            descriptor_bytes=descriptor_bytes,
            download_bytes=download_bytes,
            assembled_bytes=assembled_bytes,
            reserve_bytes=self._policy.reserve_bytes,
            required_free_bytes=(
                descriptor_bytes + download_bytes + assembled_bytes + self._policy.reserve_bytes
            ),
        )
        return AcquisitionPlan(
            resolve_result_sha256=resolve_result_sha256,
            parts=tuple(part_plans),
            descriptors=descriptors,
            raw_responses=tuple(raw_responses),
            disk_space=disk_space,
        )

    def _patch_web_seeds(
        self,
        resolved: ResolveResult,
    ) -> dict[str, tuple[ProtocolWebSeed, ...]]:
        result: dict[str, tuple[ProtocolWebSeed, ...]] = {}
        for raw in resolved.raw_responses:
            if raw.kind != "patches_chain" or raw.language is None:
                continue
            document = parse_patches_chain_xml(raw.raw_xml)
            if not document.meta_need_update:
                result[raw.language] = document.web_seeds
        return result

    def _reference_plan(
        self,
        part: ResolvedPart,
        integrity_torrent: IntegrityTorrent,
        web_seeds: Sequence[ProtocolWebSeed],
        blobs: BlobStore,
    ) -> PartAcquisition:
        descriptor = self._descriptor(
            (integrity_torrent.descriptor_url,),
            integrity_torrent.descriptor_sha256,
            blobs,
        )
        seeds = _resolved_web_seeds(
            web_seeds,
            (integrity_torrent.descriptor_url,),
            allow_http=self._target.allow_http,
        )
        artifacts: list[AcquisitionArtifact] = []
        for torrent_file in descriptor.metainfo.files:
            if torrent_file.padding:
                continue
            source_hash = _source_hash(torrent_file)
            artifacts.append(
                AcquisitionArtifact(
                    artifact_id=_artifact_id(
                        role="client-file",
                        part=part,
                        path=torrent_file.path,
                        size=torrent_file.size,
                        source_hash=source_hash,
                        descriptor_sha256=descriptor.descriptor_sha256,
                        transition=None,
                    ),
                    role="client-file",
                    part=part.name,
                    language=part.language,
                    part_version=part.version,
                    acquisition_mode=AcquisitionMode.REFERENCE,
                    path=torrent_file.path,
                    size=torrent_file.size,
                    source_hash=source_hash,
                    source_urls=_artifact_urls(
                        seeds,
                        torrent_source_components(descriptor.metainfo, torrent_file),
                    ),
                    torrent_descriptor_sha256=descriptor.descriptor_sha256,
                )
            )
        if not artifacts:
            raise ArtifactCorruptError("reference torrent contains no physical files")
        return PartAcquisition(
            part=part.name,
            language=part.language,
            version=part.version,
            acquisition_mode=AcquisitionMode.REFERENCE,
            artifacts=tuple(artifacts),
            torrent_descriptor_sha256s=(descriptor.descriptor_sha256,),
        )

    def _install_plan(
        self,
        part: ResolvedPart,
        web_seeds: Sequence[ProtocolWebSeed],
        blobs: BlobStore,
    ) -> PartAcquisition:
        transitions = (
            (part.transitions[-1],)
            if part.chain_basis is ChainBasis.ORDERED_ZERO_STATE
            else part.transitions
        )
        if transitions[-1].version_to != part.version:
            raise ProtocolIncompatibleError(
                f"install chain for {part.name.value} does not reach the pinned version"
            )
        artifacts: list[AcquisitionArtifact] = []
        descriptor_ids: list[str] = []
        for transition in transitions:
            if not transition.files or transition.torrent is None:
                raise ProtocolIncompatibleError(
                    f"install transition for {part.name.value} has no files or torrent"
                )
            if transition.torrent.info_hash is None or not transition.torrent.urls:
                raise ProtocolIncompatibleError(
                    f"install transition for {part.name.value} lacks torrent identity"
                )
            descriptor = self._descriptor(
                transition.torrent.urls,
                transition.torrent.info_hash,
                blobs,
            )
            if descriptor.descriptor_sha256 not in descriptor_ids:
                descriptor_ids.append(descriptor.descriptor_sha256)
            seeds = _resolved_web_seeds(
                web_seeds,
                transition.torrent.urls,
                allow_http=self._target.allow_http,
            )
            transition_artifacts = self._install_transition_artifacts(
                part,
                transition,
                descriptor,
                seeds,
            )
            artifacts.extend(_split_segments(transition_artifacts, part, transition))
        return PartAcquisition(
            part=part.name,
            language=part.language,
            version=part.version,
            acquisition_mode=AcquisitionMode.INSTALL_BUNDLE,
            artifacts=tuple(artifacts),
            torrent_descriptor_sha256s=tuple(descriptor_ids),
        )

    def _install_transition_artifacts(
        self,
        part: ResolvedPart,
        transition: PatchTransition,
        descriptor: TorrentDescriptorRecord,
        seeds: Sequence[ResolvedWebSeed],
    ) -> tuple[AcquisitionArtifact, ...]:
        torrent_files = {
            torrent_source_components(descriptor.metainfo, file): file
            for file in descriptor.metainfo.files
            if not file.padding
        }
        artifacts: list[AcquisitionArtifact] = []
        for patch_file in transition.files:
            try:
                declared_path = bytes_path_from_text(patch_file.name)
            except UnsafeTorrentPathError as exc:
                raise UnsafeArchiveError(str(exc)) from exc
            components = decode_bytes_path(declared_path)
            torrent_file = torrent_files.get(components)
            if torrent_file is None:
                raise ArtifactCorruptError(
                    f"install torrent does not contain declared file {patch_file.name!r}"
                )
            if torrent_file.size != patch_file.size:
                raise ArtifactCorruptError(
                    f"install file size mismatch for {patch_file.name!r}: "
                    f"torrent={torrent_file.size}, XML={patch_file.size}"
                )
            source_hash = _source_hash(torrent_file)
            artifacts.append(
                AcquisitionArtifact(
                    artifact_id=_artifact_id(
                        role="delivery-bundle",
                        part=part,
                        path=declared_path,
                        size=patch_file.size,
                        source_hash=source_hash,
                        descriptor_sha256=descriptor.descriptor_sha256,
                        transition=transition,
                    ),
                    role="delivery-bundle",
                    part=part.name,
                    language=part.language,
                    part_version=part.version,
                    acquisition_mode=AcquisitionMode.INSTALL_BUNDLE,
                    path=declared_path,
                    size=patch_file.size,
                    source_hash=source_hash,
                    source_urls=_artifact_urls(seeds, components),
                    torrent_descriptor_sha256=descriptor.descriptor_sha256,
                    transition_from=transition.version_from,
                    transition_to=transition.version_to,
                    unpacked_size=patch_file.unpacked_size,
                )
            )
        return tuple(artifacts)

    def _descriptor(
        self,
        source_urls: Sequence[str],
        expected_sha256: str,
        blobs: BlobStore,
    ) -> TorrentDescriptorRecord:
        validated_urls = tuple(
            _validate_download_url(url, allow_http=self._target.allow_http) for url in source_urls
        )
        cached = self._descriptors.get(expected_sha256)
        if cached is not None:
            return cached
        last_error = "no descriptor URL was attempted"
        for source_url in validated_urls:
            for _attempt in range(self._policy.http_attempts):
                try:
                    response = self._descriptor_transport.get(source_url)
                except ResponseLimitExceeded as exc:
                    raise ArtifactCorruptError(str(exc)) from exc
                except TransportFailure as exc:
                    last_error = str(exc)
                    continue
                if response.status_code in {404, 429} or response.status_code >= 500:
                    last_error = f"descriptor source returned HTTP {response.status_code}"
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    last_error = f"descriptor source returned HTTP {response.status_code}"
                    break
                if len(response.body) > self._policy.max_torrent_bytes:
                    raise ArtifactCorruptError("torrent descriptor exceeds the byte limit")
                if len(response.redirect_urls) > self._policy.max_http_redirects + 1:
                    raise SourceUnavailableError("torrent descriptor exceeded redirect limit")
                final = urlsplit(response.final_url)
                if final.scheme == "http" and not self._target.allow_http:
                    raise ProtocolIncompatibleError(
                        "torrent descriptor redirected HTTPS traffic to HTTP"
                    )
                if final.scheme not in {"http", "https"}:
                    raise ProtocolIncompatibleError(
                        "torrent descriptor redirected to a non-HTTP URL"
                    )
                actual_sha256 = hashlib.sha256(response.body).hexdigest()
                if actual_sha256 != expected_sha256:
                    raise ArtifactCorruptError(
                        "torrent descriptor SHA-256 does not match integrity metadata"
                    )
                try:
                    metainfo = parse_torrent(response.body, self._policy.torrent_limits())
                    commit = blobs.put_bytes(
                        response.body,
                        expected_sha256=expected_sha256,
                        expected_size=len(response.body),
                    )
                except UnsafeTorrentPathError as exc:
                    raise UnsafeArchiveError(str(exc)) from exc
                except (TorrentFormatError, BlobValidationError, CasCorruptionError) as exc:
                    raise ArtifactCorruptError(str(exc)) from exc
                descriptor = TorrentDescriptorRecord(
                    descriptor_sha256=expected_sha256,
                    source_urls=validated_urls,
                    fetched_url=source_url,
                    final_url=response.final_url,
                    http_redirects=response.redirect_urls,
                    blob_sha256=commit.sha256,
                    blob_size=commit.size,
                    blob_path=commit.relative_path,
                    metainfo=metainfo,
                )
                self._descriptors[expected_sha256] = descriptor
                return descriptor
        raise SourceUnavailableError(
            f"torrent descriptor was unavailable after bounded retries: {last_error}"
        )

    @staticmethod
    def _assembled_bytes(parts: Sequence[PartAcquisition]) -> int:
        total = 0
        for part in parts:
            if part.acquisition_mode is AcquisitionMode.REFERENCE:
                total += sum(artifact.size for artifact in part.artifacts)
                continue
            transitions: dict[tuple[str | None, str | None], list[AcquisitionArtifact]] = {}
            for artifact in part.artifacts:
                transitions.setdefault(
                    (artifact.transition_from, artifact.transition_to), []
                ).append(artifact)
            for artifacts in transitions.values():
                packed = sum(artifact.size for artifact in artifacts)
                unpacked = sum(artifact.unpacked_size or 0 for artifact in artifacts)
                total += max(packed, unpacked)
        return total


def create_acquisition_implementation(
    target: TargetConfig,
    *,
    protocol_transport: HttpTransport | None = None,
    descriptor_transport: DescriptorTransport | None = None,
    policy: AcquisitionPolicy | None = None,
) -> StageImplementation:
    selected_policy = policy or AcquisitionPolicy()
    resolve_policy = ResolvePolicy(
        http_attempts=selected_policy.http_attempts,
        max_http_redirects=selected_policy.max_http_redirects,
        max_response_bytes=selected_policy.max_xml_response_bytes,
        connect_timeout_seconds=selected_policy.connect_timeout_seconds,
        read_timeout_seconds=selected_policy.read_timeout_seconds,
    )
    selected_protocol_transport = protocol_transport or HttpxTransport(resolve_policy)
    planner = AcquisitionPlanner(
        target,
        WgusIntegrityClient(target, selected_protocol_transport, resolve_policy),
        descriptor_transport or HttpxDescriptorTransport(selected_policy),
        selected_policy,
    )

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.PLAN_ACQUISITION or context.upstream is None:
            raise ProtocolIncompatibleError("plan-acquisition requires a resolve Stage Result")
        resolved = context.upstream_as(ResolveResult)
        plan = planner.plan(
            resolved,
            context.blobs,
            context.require_upstream_digest(),
        )
        return cast(Mapping[str, JsonValue], plan.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Acquisition Plan has no resolve Stage Result")
        plan = AcquisitionPlan.model_validate(payload)
        resolved = context.upstream_as(ResolveResult)
        expected_digest = context.require_upstream_digest()
        if plan.resolve_result_sha256 != expected_digest:
            raise ValueError("Acquisition Plan is not bound to its resolve Stage Result")
        expected_parts = [
            (part.name, part.language, part.version) for part in resolved.version_vector
        ]
        actual_parts = [(part.part, part.language, part.version) for part in plan.parts]
        if actual_parts != expected_parts:
            raise ValueError("Acquisition Plan Parts do not match the pinned Version Vector")
        for descriptor in plan.descriptors:
            path = context.blobs.path_for(descriptor.blob_sha256)
            expected_blob_path = (
                f"cache/blobs/sha256/{descriptor.blob_sha256[:2]}/{descriptor.blob_sha256}"
            )
            if descriptor.blob_path != expected_blob_path:
                raise ValueError("torrent descriptor CAS path is not canonical")
            path_stat = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("torrent descriptor CAS entry is not a regular file")
            if stat.S_IMODE(path_stat.st_mode) & 0o222:
                raise ValueError("torrent descriptor CAS entry is unexpectedly writable")
            data = path.read_bytes()
            if len(data) != descriptor.blob_size:
                raise ValueError("torrent descriptor CAS entry has the wrong size")
            if hashlib.sha256(data).hexdigest() != descriptor.blob_sha256:
                raise ValueError("torrent descriptor CAS entry has the wrong SHA-256")
            try:
                metainfo = parse_torrent(data, selected_policy.torrent_limits())
            except (TorrentFormatError, UnsafeTorrentPathError) as exc:
                raise ValueError(f"torrent descriptor CAS entry is invalid: {exc}") from exc
            if metainfo != descriptor.metainfo:
                raise ValueError("torrent descriptor model does not match its CAS bytes")

    return StageImplementation(
        implementation_version="acquisition-plan-v3",
        execute=execute,
        validate=validate,
        configuration={
            "policy": cast(JsonValue, selected_policy.model_dump(mode="json")),
            "target": cast(JsonValue, target.model_dump(mode="json")),
            "torrent_parser": "strict-bencode-v1",
        },
    )


__all__ = [
    "AcquisitionPlanner",
    "AcquisitionPolicy",
    "ArtifactCorruptError",
    "DescriptorTransport",
    "HttpxDescriptorTransport",
    "UnsafeArchiveError",
    "create_acquisition_implementation",
]

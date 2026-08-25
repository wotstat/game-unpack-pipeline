from __future__ import annotations

import base64
import binascii
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from game_downloader._json import JsonObject

Language = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}(?:_[A-Z]{2})?$")]
LanguageSelector = Annotated[
    str,
    StringConstraints(pattern=r"^(?:ALL|[A-Z]{2}(?:_[A-Z]{2})?)$"),
]
ReleaseName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[^\r\n]+$"),
]


def _validate_relative_path(value: str) -> str:
    if not value or value.startswith("/"):
        raise ValueError("path must be non-empty and relative")
    if "\\" in value or "\x00" in value:
        raise ValueError("path must contain only safe POSIX path characters")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("path must contain only canonical non-dot segments")
    return value


RelativePath = Annotated[
    str,
    StringConstraints(min_length=1),
    AfterValidator(_validate_relative_path),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Sha1 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
SnapshotId = Digest
RunId = Annotated[str, StringConstraints(pattern=r"^run-[a-f0-9]{32}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientType(StrEnum):
    SD = "sd"
    HD = "hd"


class AcquisitionMode(StrEnum):
    REFERENCE = "reference"
    INSTALL_BUNDLE = "install-bundle"


class DownloadMethod(StrEnum):
    WEB_SEED = "web-seed"
    TORRENT = "torrent"


class ContainerKind(StrEnum):
    OPAQUE = "opaque"
    ZIP = "zip"
    SEVEN_ZIP = "7z"
    SPLIT_SEGMENT = "split-segment"


class PartName(StrEnum):
    CLIENT = "client"
    SD_CONTENT = "sdcontent"
    HD_CONTENT = "hdcontent"
    LOCALE = "locale"


class Publisher(StrEnum):
    WARGAMING = "wargaming"
    LESTA = "lesta"
    QIHOO = "qihoo"


class ChainBasis(StrEnum):
    EXPLICIT = "explicit-version-graph"
    ORDERED_ZERO_STATE = "ordered-zero-state-install"


class ClientPartMetadata(FrozenModel):
    name: PartName
    integrity: bool
    language_specific: bool
    app_type: Annotated[str, StringConstraints(min_length=1)] | None = None


class ClientTypeMetadata(FrozenModel):
    client_type: ClientType
    architecture: Annotated[str, StringConstraints(min_length=1)] | None = None
    parts: tuple[ClientPartMetadata, ...] = Field(min_length=1)

    @field_validator("parts")
    @classmethod
    def parts_are_unique(
        cls, value: tuple[ClientPartMetadata, ...]
    ) -> tuple[ClientPartMetadata, ...]:
        names = [part.name for part in value]
        if len(names) != len(set(names)):
            raise ValueError("client type metadata contains duplicate Parts")
        return value


class ResolvedMetadata(FrozenModel):
    requested_protocol_version: Annotated[str, StringConstraints(min_length=1)]
    observed_protocol_version: Annotated[str, StringConstraints(min_length=1)]
    observed_publishers: Annotated[str, StringConstraints(min_length=1)] | None = None
    metadata_version: Annotated[str, StringConstraints(min_length=1)]
    app_id: Annotated[str, StringConstraints(min_length=1)]
    chain_id: Annotated[str, StringConstraints(min_length=1)]
    supported_languages: tuple[Language, ...] = Field(min_length=1)
    default_language: Language
    client_types: tuple[ClientTypeMetadata, ...] = Field(min_length=1)

    @field_validator("supported_languages")
    @classmethod
    def supported_languages_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("metadata supported languages must be unique")
        return value

    @field_validator("client_types")
    @classmethod
    def client_types_are_unique(
        cls, value: tuple[ClientTypeMetadata, ...]
    ) -> tuple[ClientTypeMetadata, ...]:
        names = [client.client_type for client in value]
        if len(names) != len(set(names)):
            raise ValueError("metadata contains duplicate client types")
        return value

    @model_validator(mode="after")
    def default_language_is_supported(self) -> ResolvedMetadata:
        if self.default_language not in self.supported_languages:
            raise ValueError("metadata default language is not supported")
        return self


class ApplicationRedirect(FrozenModel):
    from_host: Annotated[str, StringConstraints(min_length=1)]
    from_app_id: Annotated[str, StringConstraints(min_length=1)]
    to_host: Annotated[str, StringConstraints(min_length=1)]
    to_app_id: Annotated[str, StringConstraints(min_length=1)]


class ChangedGameInfo(FrozenModel):
    observed_protocol_version: Annotated[str, StringConstraints(min_length=1)] | None = None
    new_host: Annotated[str, StringConstraints(min_length=1)] | None = None
    new_app_id: Annotated[str, StringConstraints(min_length=1)] | None = None
    unknown_top_level_fields: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()

    @model_validator(mode="after")
    def changes_something(self) -> ChangedGameInfo:
        if self.new_host is None and self.new_app_id is None:
            raise ValueError("changed_game_info does not contain a new host or app ID")
        return self


class ResolvedTarget(FrozenModel):
    target: Annotated[str, StringConstraints(min_length=1)]
    publisher: Publisher
    api_host: Annotated[str, StringConstraints(min_length=1)]
    app_id: Annotated[str, StringConstraints(min_length=1)]
    application_redirects: tuple[ApplicationRedirect, ...] = ()


class PatchFile(FrozenModel):
    name: Annotated[str, StringConstraints(min_length=1)]
    size: Annotated[int, Field(ge=0)]
    unpacked_size: Annotated[int, Field(ge=0)] | None = None
    diff_size: Annotated[int, Field(ge=0)] | None = None


class PatchTorrent(FrozenModel):
    info_hash: Sha256 | None = None
    urls: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()


class ProtocolWebSeed(FrozenModel):
    url: Annotated[str, StringConstraints(min_length=1)]
    threads: Annotated[int, Field(ge=1)] = 1


class PatchTransition(FrozenModel):
    part: PartName
    version_from: Annotated[str, StringConstraints(min_length=1)] | None = None
    version_to: Annotated[str, StringConstraints(min_length=1)]
    files: tuple[PatchFile, ...] = ()
    torrent: PatchTorrent | None = None


class PatchesChainDocument(FrozenModel):
    observed_protocol_version: Annotated[str, StringConstraints(min_length=1)]
    observed_publishers: Annotated[str, StringConstraints(min_length=1)] | None = None
    meta_need_update: bool
    release_name: ReleaseName | None = None
    transitions: tuple[PatchTransition, ...] = ()
    web_seeds: tuple[ProtocolWebSeed, ...] = ()
    unknown_top_level_fields: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()

    @model_validator(mode="after")
    def complete_when_metadata_is_current(self) -> PatchesChainDocument:
        if not self.meta_need_update and (self.release_name is None or not self.transitions):
            raise ValueError("patches_chain requires release name and transitions")
        return self


class ResolvedPart(FrozenModel):
    name: PartName
    language: Language | None = None
    version: Annotated[str, StringConstraints(min_length=1)]
    integrity: bool
    chain_basis: ChainBasis
    transitions: tuple[PatchTransition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def language_matches_part(self) -> ResolvedPart:
        if self.name is PartName.LOCALE and self.language is None:
            raise ValueError("resolved locale Part requires language")
        if self.name is not PartName.LOCALE and self.language is not None:
            raise ValueError("only resolved locale Part may have language")
        if any(transition.part is not self.name for transition in self.transitions):
            raise ValueError("resolved Part contains a transition for another Part")
        return self


class RawProtocolResponse(FrozenModel):
    attempt: Annotated[int, Field(ge=1)]
    kind: Literal["metadata", "changed_game_info", "patches_chain", "integrity_check"]
    part: PartName | None = None
    language: Language | None = None
    request_url: Annotated[str, StringConstraints(min_length=1)]
    final_url: Annotated[str, StringConstraints(min_length=1)]
    http_redirects: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()
    observed_protocol_name: Annotated[str, StringConstraints(min_length=1)]
    observed_protocol_version: Annotated[str, StringConstraints(min_length=1)] | None = None
    unknown_top_level_fields: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()
    raw_xml: Annotated[str, StringConstraints(min_length=1)]


class IntegrityTorrent(FrozenModel):
    part: PartName
    version: Annotated[str, StringConstraints(min_length=1)]
    descriptor_url: Annotated[str, StringConstraints(min_length=1)]
    descriptor_sha256: Sha256
    blacklist_url: Annotated[str, StringConstraints(min_length=1)] | None = None


class IntegrityCheckDocument(FrozenModel):
    requested_protocol_version: Annotated[str, StringConstraints(min_length=1)]
    observed_protocol_version: Annotated[str, StringConstraints(min_length=1)]
    observed_publishers: Annotated[str, StringConstraints(min_length=1)] | None = None
    torrents: tuple[IntegrityTorrent, ...] = ()
    web_seeds: tuple[ProtocolWebSeed, ...] = ()
    unknown_top_level_fields: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()


class BytesPath(FrozenModel):
    """A path whose original byte components survive JSON serialization exactly."""

    components_base64: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(
        min_length=1
    )
    utf8: RelativePath | None = None

    @model_validator(mode="after")
    def components_are_safe_and_display_is_derived(self) -> BytesPath:
        decoded: list[bytes] = []
        for encoded in self.components_base64:
            try:
                component = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("path component is not canonical base64") from exc
            if base64.b64encode(component).decode("ascii") != encoded:
                raise ValueError("path component is not canonical base64")
            if (
                not component
                or component in {b".", b".."}
                or b"/" in component
                or b"\\" in component
                or b"\x00" in component
                or any(byte < 32 or byte == 127 for byte in component)
            ):
                raise ValueError("path contains an unsafe byte component")
            if len(component) > 255:
                raise ValueError("path component exceeds 255 bytes")
            decoded.append(component)
        if sum(len(component) + 1 for component in decoded) > 4096:
            raise ValueError("path exceeds 4096 bytes")
        try:
            expected_utf8 = "/".join(component.decode("utf-8") for component in decoded)
        except UnicodeDecodeError:
            expected_utf8 = None
        if self.utf8 != expected_utf8:
            raise ValueError("utf8 path does not match its byte components")
        return self


class SourceHash(FrozenModel):
    algorithm: Literal["sha1", "sha256"]
    value: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]+$")]

    @model_validator(mode="after")
    def digest_length_matches_algorithm(self) -> SourceHash:
        expected = 40 if self.algorithm == "sha1" else 64
        if len(self.value) != expected:
            raise ValueError(f"{self.algorithm} digest must contain {expected} hex characters")
        return self


class TorrentFile(FrozenModel):
    path: BytesPath
    size: Annotated[int, Field(ge=0)]
    source_sha1: Sha1 | None = None
    padding: bool = False


class TorrentMetainfo(FrozenModel):
    info_hash_sha1: Sha1
    name: BytesPath
    multi_file: bool
    piece_length: Annotated[int, Field(gt=0)]
    piece_count: Annotated[int, Field(ge=1)]
    pieces_sha256: Sha256
    files: tuple[TorrentFile, ...] = Field(min_length=1)
    total_size: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def file_and_piece_totals_are_consistent(self) -> TorrentMetainfo:
        if len(self.name.components_base64) != 1:
            raise ValueError("torrent name must be one byte-safe path component")
        if sum(file.size for file in self.files) != self.total_size:
            raise ValueError("torrent total size does not match its files")
        expected_pieces = (self.total_size + self.piece_length - 1) // self.piece_length
        if self.piece_count != expected_pieces:
            raise ValueError("torrent piece count does not cover its payload")
        paths = [file.path.components_base64 for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("torrent contains duplicate file paths")
        return self


class ResolvedWebSeed(FrozenModel):
    declared_url: Annotated[str, StringConstraints(min_length=1)]
    effective_url: Annotated[str, StringConstraints(min_length=1)]
    threads: Annotated[int, Field(ge=1)] = 1


class TorrentDescriptorRecord(FrozenModel):
    descriptor_sha256: Sha256
    source_urls: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(min_length=1)
    fetched_url: Annotated[str, StringConstraints(min_length=1)]
    final_url: Annotated[str, StringConstraints(min_length=1)]
    http_redirects: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()
    blob_sha256: Sha256
    blob_size: Annotated[int, Field(gt=0)]
    blob_path: RelativePath
    metainfo: TorrentMetainfo

    @model_validator(mode="after")
    def blob_is_the_declared_descriptor(self) -> TorrentDescriptorRecord:
        if self.blob_sha256 != self.descriptor_sha256:
            raise ValueError("torrent CAS blob does not match its declared SHA-256")
        return self


class SplitSegment(FrozenModel):
    group_id: Digest
    index: Annotated[int, Field(ge=1)]
    count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def index_is_in_range(self) -> SplitSegment:
        if self.index > self.count:
            raise ValueError("split segment index exceeds segment count")
        return self


class AcquisitionArtifact(FrozenModel):
    artifact_id: Digest
    role: Literal["client-file", "delivery-bundle"]
    part: PartName
    language: Language | None = None
    part_version: Annotated[str, StringConstraints(min_length=1)]
    acquisition_mode: AcquisitionMode
    path: BytesPath
    size: Annotated[int, Field(ge=0)]
    source_hash: SourceHash | None = None
    source_urls: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()
    torrent_descriptor_sha256: Sha256
    transition_from: Annotated[str, StringConstraints(min_length=1)] | None = None
    transition_to: Annotated[str, StringConstraints(min_length=1)] | None = None
    unpacked_size: Annotated[int, Field(ge=0)] | None = None
    split_segment: SplitSegment | None = None

    @model_validator(mode="after")
    def role_and_part_are_consistent(self) -> AcquisitionArtifact:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale Artifact requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale Artifact may have language")
        if self.acquisition_mode is AcquisitionMode.REFERENCE:
            if self.role != "client-file":
                raise ValueError("reference acquisition must produce client-file Artifacts")
            if self.transition_from is not None or self.transition_to is not None:
                raise ValueError("reference Artifact must not contain install transitions")
        else:
            if self.role != "delivery-bundle" or self.transition_to is None:
                raise ValueError("install-bundle Artifact requires a target transition")
        return self


class PartAcquisition(FrozenModel):
    part: PartName
    language: Language | None = None
    version: Annotated[str, StringConstraints(min_length=1)]
    acquisition_mode: AcquisitionMode
    artifacts: tuple[AcquisitionArtifact, ...] = ()
    torrent_descriptor_sha256s: tuple[Sha256, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def artifacts_match_part(self) -> PartAcquisition:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale acquisition requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale acquisition may have language")
        for artifact in self.artifacts:
            if (
                artifact.part is not self.part
                or artifact.language != self.language
                or artifact.part_version != self.version
                or artifact.acquisition_mode is not self.acquisition_mode
                or artifact.torrent_descriptor_sha256 not in self.torrent_descriptor_sha256s
            ):
                raise ValueError("Part acquisition contains an inconsistent Artifact")
        return self


class DiskSpaceEstimate(FrozenModel):
    descriptor_bytes: Annotated[int, Field(ge=0)]
    download_bytes: Annotated[int, Field(ge=0)]
    assembled_bytes: Annotated[int, Field(ge=0)]
    reserve_bytes: Annotated[int, Field(ge=0)]
    required_free_bytes: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def required_is_the_conservative_sum(self) -> DiskSpaceEstimate:
        expected = (
            self.descriptor_bytes + self.download_bytes + self.assembled_bytes + self.reserve_bytes
        )
        if self.required_free_bytes != expected:
            raise ValueError("required free space is not the conservative component sum")
        return self


class AcquisitionPlan(FrozenModel):
    schema_version: Literal[1] = 1
    resolve_result_sha256: Digest
    parts: tuple[PartAcquisition, ...] = Field(min_length=3)
    descriptors: tuple[TorrentDescriptorRecord, ...] = Field(min_length=1)
    raw_responses: tuple[RawProtocolResponse, ...] = ()
    disk_space: DiskSpaceEstimate

    @model_validator(mode="after")
    def graph_is_self_contained(self) -> AcquisitionPlan:
        part_keys = [(part.part, part.language) for part in self.parts]
        if len(part_keys) != len(set(part_keys)):
            raise ValueError("Acquisition Plan contains duplicate Parts")
        descriptor_ids = [descriptor.descriptor_sha256 for descriptor in self.descriptors]
        if len(descriptor_ids) != len(set(descriptor_ids)):
            raise ValueError("Acquisition Plan contains duplicate torrent descriptors")
        known_descriptors = set(descriptor_ids)
        artifacts = [artifact for part in self.parts for artifact in part.artifacts]
        artifact_ids = [artifact.artifact_id for artifact in artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("Acquisition Plan contains duplicate Artifact IDs")
        if any(
            artifact.torrent_descriptor_sha256 not in known_descriptors for artifact in artifacts
        ):
            raise ValueError("Artifact references an unknown torrent descriptor")
        if self.disk_space.descriptor_bytes != sum(
            descriptor.blob_size for descriptor in self.descriptors
        ):
            raise ValueError("descriptor byte estimate does not match the plan")
        if self.disk_space.download_bytes != sum(artifact.size for artifact in artifacts):
            raise ValueError("download byte estimate does not match the plan")
        assembled_bytes = 0
        for part in self.parts:
            if part.acquisition_mode is AcquisitionMode.REFERENCE:
                assembled_bytes += sum(artifact.size for artifact in part.artifacts)
                continue
            transitions: dict[tuple[str | None, str | None], list[AcquisitionArtifact]] = {}
            for artifact in part.artifacts:
                transitions.setdefault(
                    (artifact.transition_from, artifact.transition_to), []
                ).append(artifact)
            for transition_artifacts in transitions.values():
                packed = sum(artifact.size for artifact in transition_artifacts)
                unpacked = sum(artifact.unpacked_size or 0 for artifact in transition_artifacts)
                assembled_bytes += max(packed, unpacked)
        if self.disk_space.assembled_bytes != assembled_bytes:
            raise ValueError("assembled byte estimate does not match the plan")
        return self


class DownloadTrace(FrozenModel):
    method: DownloadMethod
    requested_url: Annotated[str, StringConstraints(min_length=1)] | None = None
    final_url: Annotated[str, StringConstraints(min_length=1)] | None = None
    http_redirects: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()
    etag: Annotated[str, StringConstraints(min_length=1)] | None = None
    last_modified: Annotated[str, StringConstraints(min_length=1)] | None = None
    resumed_from: Annotated[int, Field(ge=0)] = 0
    attempts: Annotated[int, Field(ge=1)] = 1
    parallel_segments: Annotated[int, Field(ge=1, le=32)] = 1

    @model_validator(mode="after")
    def web_seed_has_urls(self) -> DownloadTrace:
        if self.method is DownloadMethod.WEB_SEED and (
            self.requested_url is None or self.final_url is None
        ):
            raise ValueError("web-seed download trace requires request and final URLs")
        return self


class DownloadedArtifact(FrozenModel):
    artifact: AcquisitionArtifact
    blob_sha256: Sha256
    blob_size: Annotated[int, Field(ge=0)]
    blob_path: RelativePath
    source_hash_verified: bool
    reused: bool
    transport: DownloadTrace

    @model_validator(mode="after")
    def blob_matches_artifact(self) -> DownloadedArtifact:
        if self.blob_size != self.artifact.size:
            raise ValueError("downloaded blob size does not match its Artifact")
        expected_path = f"cache/blobs/sha256/{self.blob_sha256[:2]}/{self.blob_sha256}"
        if self.blob_path != expected_path:
            raise ValueError("downloaded blob CAS path is not canonical")
        if self.artifact.source_hash is not None and not self.source_hash_verified:
            raise ValueError("declared Artifact source hash was not verified")
        return self


class DownloadResult(FrozenModel):
    schema_version: Literal[1] = 1
    acquisition_plan_sha256: Digest
    artifacts: tuple[DownloadedArtifact, ...] = Field(min_length=1)
    downloaded_bytes: Annotated[int, Field(ge=0)]
    reused_artifacts: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def totals_match_artifacts(self) -> DownloadResult:
        artifact_ids = [item.artifact.artifact_id for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("Download Result contains duplicate Artifacts")
        if self.downloaded_bytes != sum(item.blob_size for item in self.artifacts):
            raise ValueError("Download Result byte total does not match its Artifacts")
        if self.reused_artifacts != sum(item.reused for item in self.artifacts):
            raise ValueError("Download Result reuse count does not match its Artifacts")
        return self


class ArtifactVerification(FrozenModel):
    download: DownloadedArtifact
    container: ContainerKind
    magic_hex: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{0,32}$")]
    container_verified: bool
    entries: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def archive_is_verified(self) -> ArtifactVerification:
        if self.container in {ContainerKind.ZIP, ContainerKind.SEVEN_ZIP} and (
            not self.container_verified or self.entries is None
        ):
            raise ValueError("archive Artifact must have a successful container verification")
        if self.container is ContainerKind.SPLIT_SEGMENT and self.container_verified:
            raise ValueError("individual split segments cannot be container-verified")
        return self


class SplitAssembly(FrozenModel):
    group_id: Digest
    artifact_ids: tuple[Digest, ...] = Field(min_length=1)
    blob_sha256: Sha256
    blob_size: Annotated[int, Field(gt=0)]
    blob_path: RelativePath
    container: ContainerKind
    entries: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def assembled_blob_is_canonical_archive(self) -> SplitAssembly:
        expected_path = f"cache/blobs/sha256/{self.blob_sha256[:2]}/{self.blob_sha256}"
        if self.blob_path != expected_path:
            raise ValueError("split assembly CAS path is not canonical")
        if self.container not in {ContainerKind.ZIP, ContainerKind.SEVEN_ZIP}:
            raise ValueError("split assembly must be a verified archive")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("split assembly contains duplicate segments")
        return self


class VerificationResult(FrozenModel):
    schema_version: Literal[1] = 1
    download_result_sha256: Digest
    artifacts: tuple[ArtifactVerification, ...] = Field(min_length=1)
    split_assemblies: tuple[SplitAssembly, ...] = ()

    @model_validator(mode="after")
    def verification_is_complete(self) -> VerificationResult:
        artifact_ids = [item.download.artifact.artifact_id for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("Verification Result contains duplicate Artifacts")
        known_ids = set(artifact_ids)
        assembly_groups: set[str] = set()
        for assembly in self.split_assemblies:
            if assembly.group_id in assembly_groups:
                raise ValueError("Verification Result contains duplicate split groups")
            assembly_groups.add(assembly.group_id)
            if not set(assembly.artifact_ids).issubset(known_ids):
                raise ValueError("split assembly references an unknown Artifact")
        return self


class ClientTreeFile(FrozenModel):
    path: RelativePath
    part: PartName
    language: Language | None = None
    part_version: Annotated[str, StringConstraints(min_length=1)]
    source_artifact_id: Digest
    source_blob_sha256: Sha256
    source_entry_path: RelativePath | None = None
    blob_sha256: Sha256
    blob_size: Annotated[int, Field(ge=0)]
    blob_path: RelativePath
    link_method: Literal["hardlink", "copy"]

    @model_validator(mode="after")
    def layer_and_blob_are_consistent(self) -> ClientTreeFile:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale Client Tree file requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale Client Tree files may have language")
        expected_path = f"cache/blobs/sha256/{self.blob_sha256[:2]}/{self.blob_sha256}"
        if self.blob_path != expected_path:
            raise ValueError("Client Tree file CAS path is not canonical")
        return self


class ClientTreeResult(FrozenModel):
    schema_version: Literal[1] = 1
    verification_result_sha256: Digest
    base_root: RelativePath
    locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    files: tuple[ClientTreeFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def tree_manifest_is_complete(self) -> ClientTreeResult:
        keys = [
            ("locale" if item.language is not None else "base", item.language, item.path)
            for item in self.files
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Client Tree contains duplicate layer paths")
        locale_languages = {item.language for item in self.files if item.language is not None}
        if not locale_languages.issubset(self.locale_roots):
            raise ValueError("Client Tree file references an unknown locale root")
        return self


class VfsSourceKind(StrEnum):
    GAME_PACKAGE = "game-package"
    LOOSE_FILE = "loose-file"


class IndexedPackage(FrozenModel):
    path: RelativePath
    blob_sha256: Sha256
    blob_size: Annotated[int, Field(ge=0)]
    blob_path: RelativePath
    part: PartName
    language: Language | None = None
    part_version: Annotated[str, StringConstraints(min_length=1)]
    precedence: Annotated[int, Field(ge=0)]
    entries: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def package_layer_and_blob_are_consistent(self) -> IndexedPackage:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale package requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale packages may have language")
        expected_path = f"cache/blobs/sha256/{self.blob_sha256[:2]}/{self.blob_sha256}"
        if self.blob_path != expected_path:
            raise ValueError("indexed package CAS path is not canonical")
        return self


class VfsCandidate(FrozenModel):
    source_kind: VfsSourceKind
    canonical_path: RelativePath
    original_path: RelativePath
    part: PartName
    language: Language | None = None
    part_version: Annotated[str, StringConstraints(min_length=1)]
    source_path: RelativePath
    source_sha256: Sha256
    precedence: Annotated[int, Field(ge=0)]
    zip_entry_index: Annotated[int, Field(ge=0)] | None = None
    compressed_size: Annotated[int, Field(ge=0)] | None = None
    uncompressed_size: Annotated[int, Field(ge=0)]
    crc32: Annotated[str, StringConstraints(pattern=r"^[A-F0-9]{8}$")] | None = None

    @model_validator(mode="after")
    def source_and_layer_are_consistent(self) -> VfsCandidate:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale VFS candidate requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale VFS candidates may have language")
        if self.source_kind is VfsSourceKind.GAME_PACKAGE:
            if self.zip_entry_index is None or self.compressed_size is None or self.crc32 is None:
                raise ValueError("Game Package candidate requires ZIP metadata")
        elif any(
            value is not None for value in (self.zip_entry_index, self.compressed_size, self.crc32)
        ):
            raise ValueError("loose VFS candidate must not contain ZIP metadata")
        return self


class VfsIndexedEntry(FrozenModel):
    lookup_key: RelativePath
    layer: Annotated[str, StringConstraints(pattern=r"^(base|locale:[A-Z]{2}(?:_[A-Z]{2})?)$")]
    candidates: tuple[VfsCandidate, ...] = Field(min_length=1)
    winner: VfsCandidate
    resolution_rule: Annotated[str, StringConstraints(min_length=1)]

    @model_validator(mode="after")
    def winner_is_a_unique_candidate(self) -> VfsIndexedEntry:
        if self.winner not in self.candidates:
            raise ValueError("VFS winner is not one of its candidates")
        if self.candidates.count(self.winner) != 1:
            raise ValueError("VFS winner is not unique")
        if any(
            candidate.canonical_path.casefold() != self.lookup_key for candidate in self.candidates
        ):
            raise ValueError("VFS candidate does not match its lookup key")
        return self


class VfsIndexResult(FrozenModel):
    schema_version: Literal[1] = 1
    client_tree_result_sha256: Digest
    policy_name: Annotated[str, StringConstraints(min_length=1)]
    policy_version: Annotated[str, StringConstraints(min_length=1)]
    policy_sha256: Sha256
    locale_languages: tuple[Language, ...] = Field(min_length=1)
    packages: tuple[IndexedPackage, ...]
    entries: tuple[VfsIndexedEntry, ...]

    @model_validator(mode="after")
    def index_has_unique_packages_and_entries(self) -> VfsIndexResult:
        if len(self.locale_languages) != len(set(self.locale_languages)):
            raise ValueError("VFS Index locale languages must be unique")
        package_keys = [(item.language, item.path) for item in self.packages]
        if len(package_keys) != len(set(package_keys)):
            raise ValueError("VFS index contains duplicate packages")
        entry_keys = [(item.layer, item.lookup_key) for item in self.entries]
        if len(entry_keys) != len(set(entry_keys)):
            raise ValueError("VFS index contains duplicate lookup keys")
        indexed_languages = {
            candidate.language
            for entry in self.entries
            for candidate in entry.candidates
            if candidate.language is not None
        }
        indexed_languages.update(
            package.language for package in self.packages if package.language is not None
        )
        if not indexed_languages.issubset(self.locale_languages):
            raise ValueError("VFS Index references an unknown locale language")
        return self


class MaterializedFile(FrozenModel):
    path: RelativePath
    language: Language | None = None
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    source: VfsCandidate

    @model_validator(mode="after")
    def layer_matches_source(self) -> MaterializedFile:
        if self.language != self.source.language:
            raise ValueError("materialized layer does not match VFS source")
        if self.path != self.source.canonical_path:
            raise ValueError("materialized path does not match the winning VFS path")
        if self.size != self.source.uncompressed_size:
            raise ValueError("materialized size does not match the winning VFS entry")
        return self


class MaterializationResult(FrozenModel):
    schema_version: Literal[1] = 1
    vfs_index_result_sha256: Digest
    base_root: RelativePath
    locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    files: tuple[MaterializedFile, ...]

    @model_validator(mode="after")
    def materialized_paths_are_unique(self) -> MaterializationResult:
        keys = [(item.language, item.path) for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("materialized VFS contains duplicate output paths")
        return self


class ResolveResult(FrozenModel):
    schema_version: Literal[1] = 1
    resolved_target: ResolvedTarget
    chain_id: Annotated[str, StringConstraints(min_length=1)]
    client_type: ClientType
    languages: tuple[Language, ...] = Field(min_length=1)
    metadata_version: Annotated[str, StringConstraints(min_length=1)]
    release_name: ReleaseName
    metadata: ResolvedMetadata
    version_vector: tuple[ResolvedPart, ...] = Field(min_length=3)
    raw_responses: tuple[RawProtocolResponse, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def version_vector_matches_request(self) -> ResolveResult:
        keys = [(part.name, part.language) for part in self.version_vector]
        if len(keys) != len(set(keys)):
            raise ValueError("Version Vector contains duplicate Parts")
        locale_languages = tuple(
            sorted(
                part.language
                for part in self.version_vector
                if part.name is PartName.LOCALE and part.language is not None
            )
        )
        if locale_languages != tuple(sorted(self.languages)):
            raise ValueError("Version Vector locale Parts do not match requested languages")
        if self.metadata.metadata_version != self.metadata_version:
            raise ValueError("metadata version does not match the pinned result")
        if self.metadata.chain_id != self.chain_id:
            raise ValueError("metadata chain ID does not match the pinned result")
        if self.metadata.app_id != self.resolved_target.app_id:
            raise ValueError("metadata app ID does not match the Resolved Target")
        return self


class PartVersion(FrozenModel):
    name: PartName
    language: Language | None = None
    version: Annotated[str, StringConstraints(min_length=1)]
    acquisition_mode: AcquisitionMode

    @model_validator(mode="after")
    def language_matches_part(self) -> PartVersion:
        if self.name is PartName.LOCALE and self.language is None:
            raise ValueError("locale Part requires language")
        if self.name is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale Part may have language")
        return self


class SnapshotSource(FrozenModel):
    target: Annotated[str, StringConstraints(min_length=1)]
    publisher: Annotated[str, StringConstraints(min_length=1)]
    api_host: AnyUrl
    resolved_app_id: Annotated[str, StringConstraints(min_length=1)]
    chain_id: Annotated[str, StringConstraints(min_length=1)]
    client_type: ClientType
    languages: tuple[Language, ...] = Field(min_length=1)
    metadata_version: Annotated[str, StringConstraints(min_length=1)]
    release_name: ReleaseName
    version_vector: tuple[PartVersion, ...] = Field(min_length=3)

    @field_validator("languages")
    @classmethod
    def languages_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("languages must be unique")
        return value


class SnapshotPayload(FrozenModel):
    base_root: RelativePath
    locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    actionscript_root: RelativePath
    stubs_root: RelativePath
    overlay_order: tuple[Literal["base"], Literal["locale:{language}"]]


class ManifestReference(FrozenModel):
    path: RelativePath
    sha256: Sha256
    records: Annotated[int, Field(ge=0)]


class SnapshotManifests(FrozenModel):
    files: ManifestReference
    actionscript: ManifestReference
    stubs: ManifestReference
    packages: ManifestReference
    conflicts: ManifestReference


class PolicyReference(FrozenModel):
    name: Annotated[str, StringConstraints(min_length=1)]
    version: Annotated[str, StringConstraints(min_length=1)]
    sha256: Sha256


class SnapshotPolicies(FrozenModel):
    vfs: PolicyReference
    readable: PolicyReference
    source_tree: PolicyReference


class ToolIdentity(FrozenModel):
    name: Annotated[str, StringConstraints(min_length=1)]
    version: Annotated[str, StringConstraints(min_length=1)]
    source: AnyUrl | None = None


class SnapshotQuality(FrozenModel):
    unresolved_conflicts: Literal[0]
    required_transform_failures: Literal[0]
    unmanifested_payload_files: Literal[0]


class GameSnapshotV1(FrozenModel):
    contract: Literal["game-snapshot"]
    contract_version: Literal["1.1.0"]
    snapshot_id: SnapshotId
    created_at: datetime
    source: SnapshotSource
    payload: SnapshotPayload
    manifests: SnapshotManifests
    policies: SnapshotPolicies
    tools: tuple[ToolIdentity, ...] = Field(min_length=1)
    quality: SnapshotQuality

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value


class BaseLayer(FrozenModel):
    kind: Literal["base"]


class LocaleLayer(FrozenModel):
    kind: Literal["locale"]
    language: Language


FileLayer = Annotated[BaseLayer | LocaleLayer, Field(discriminator="kind")]


class RepresentationKind(StrEnum):
    PASSTHROUGH = "passthrough"
    PYC_TO_PY = "pyc-to-py"
    PACKED_XML_TO_XML = "packed-xml-to-xml"
    MO_TO_PO = "mo-to-po"
    SWC_TO_AS = "swc-to-as"


class FileRepresentation(FrozenModel):
    kind: RepresentationKind
    source_path: RelativePath
    source_sha256: Sha256
    tool: Annotated[str, StringConstraints(min_length=1)] | None = None
    tool_version: Annotated[str, StringConstraints(min_length=1)] | None = None

    @model_validator(mode="after")
    def transformer_has_tool_identity(self) -> FileRepresentation:
        if self.kind is not RepresentationKind.PASSTHROUGH and (
            self.tool is None or self.tool_version is None
        ):
            raise ValueError("transformed representation requires tool and tool_version")
        return self


class GamePackageFileSourceV1(FrozenModel):
    kind: Literal["game-package"]
    part: PartName
    part_version: Annotated[str, StringConstraints(min_length=1)]
    language: Language | None = None
    game_package_path: RelativePath
    game_package_sha256: Sha256
    entry_path: RelativePath
    entry_sha256: Sha256

    @model_validator(mode="after")
    def language_matches_part(self) -> GamePackageFileSourceV1:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale package source requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale package source may have language")
        return self


class LooseFileSourceV1(FrozenModel):
    kind: Literal["loose-file"]
    part: PartName
    part_version: Annotated[str, StringConstraints(min_length=1)]
    language: Language | None = None
    client_tree_path: RelativePath
    client_tree_sha256: Sha256
    entry_path: RelativePath
    entry_sha256: Sha256

    @model_validator(mode="after")
    def language_matches_part(self) -> LooseFileSourceV1:
        if self.part is PartName.LOCALE and self.language is None:
            raise ValueError("locale loose-file source requires language")
        if self.part is not PartName.LOCALE and self.language is not None:
            raise ValueError("only locale loose-file source may have language")
        return self


FileSourceV1 = Annotated[
    GamePackageFileSourceV1 | LooseFileSourceV1,
    Field(discriminator="kind"),
]


class FileRepresentationV1(FileRepresentation):
    diagnostics: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()


class FileManifestEntryV1(FrozenModel):
    path: RelativePath
    layer: FileLayer
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    source: FileSourceV1
    representation: FileRepresentationV1

    @model_validator(mode="after")
    def representation_belongs_in_file_manifest(self) -> FileManifestEntryV1:
        if self.representation.kind is RepresentationKind.SWC_TO_AS:
            raise ValueError("SWC-to-AS representations belong in the ActionScript manifest")
        return self


class ActionScriptManifestEntryV1(FrozenModel):
    path: RelativePath
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    source: FileSourceV1
    representation: FileRepresentationV1

    @model_validator(mode="after")
    def source_is_a_base_swc(self) -> ActionScriptManifestEntryV1:
        if self.source.language is not None:
            raise ValueError("ActionScript source must come from the base layer")
        if not self.source.entry_path.lower().endswith(".swc"):
            raise ValueError("ActionScript source entry must be an SWC")
        if self.representation.kind is not RepresentationKind.SWC_TO_AS:
            raise ValueError("ActionScript representation must be swc-to-as")
        if self.representation.source_sha256 != self.source.entry_sha256:
            raise ValueError("ActionScript representation source digest does not match")
        if not self.path.lower().endswith(".as"):
            raise ValueError("ActionScript output path must end in .as")
        return self


class StubManifestEntryV1(FrozenModel):
    path: RelativePath
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def path_is_a_stub_payload(cls, value: str) -> str:
        if value == "manifest.json" or value == "py.typed" or value.lower().endswith(".pyi"):
            return value
        raise ValueError("stub output must be a .pyi, manifest.json, or py.typed file")


class ReadableFile(FrozenModel):
    path: RelativePath
    language: Language | None = None
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    source: MaterializedFile
    representation: FileRepresentation
    diagnostics: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()

    @model_validator(mode="after")
    def readable_output_matches_source_and_representation(self) -> ReadableFile:
        if self.language != self.source.language:
            raise ValueError("readable layer does not match its materialized source")
        if self.representation.source_path != self.source.path:
            raise ValueError("readable representation source path does not match")
        if self.representation.source_sha256 != self.source.sha256:
            raise ValueError("readable representation source digest does not match")
        if self.representation.kind is RepresentationKind.PASSTHROUGH:
            if self.path != self.source.path:
                raise ValueError("passthrough representation must preserve its path")
            if self.size != self.source.size or self.sha256 != self.source.sha256:
                raise ValueError("passthrough representation must preserve its bytes")
        elif self.representation.kind is RepresentationKind.PYC_TO_PY:
            if not self.source.path.lower().endswith(".pyc") or not self.path.lower().endswith(
                ".py"
            ):
                raise ValueError("PYC representation must map .pyc to .py")
        elif self.representation.kind is RepresentationKind.MO_TO_PO:
            if not self.source.path.lower().endswith(".mo") or not self.path.lower().endswith(
                ".po"
            ):
                raise ValueError("MO representation must map .mo to .po")
        elif self.representation.kind is RepresentationKind.PACKED_XML_TO_XML:
            if self.path != self.source.path or not self.path.lower().endswith(".xml"):
                raise ValueError("packed XML representation must preserve the .xml path")
        elif self.representation.kind is RepresentationKind.SWC_TO_AS:
            raise ValueError("SWC representations belong in ActionScript outputs")
        return self


class ActionScriptFile(FrozenModel):
    path: RelativePath
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    source: MaterializedFile
    representation: FileRepresentation
    diagnostics: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = ()

    @model_validator(mode="after")
    def output_is_bound_to_base_swc(self) -> ActionScriptFile:
        if self.source.language is not None:
            raise ValueError("ActionScript output must come from the base layer")
        if not self.source.path.lower().endswith(".swc"):
            raise ValueError("ActionScript source must be an SWC")
        if not self.path.lower().endswith(".as"):
            raise ValueError("ActionScript output path must end in .as")
        if self.representation.kind is not RepresentationKind.SWC_TO_AS:
            raise ValueError("ActionScript output requires swc-to-as representation")
        if self.representation.source_path != self.source.path:
            raise ValueError("ActionScript representation source path does not match")
        if self.representation.source_sha256 != self.source.sha256:
            raise ValueError("ActionScript representation source digest does not match")
        return self


class StubFile(FrozenModel):
    path: RelativePath
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def path_is_a_stub_payload(cls, value: str) -> str:
        if value == "manifest.json" or value == "py.typed" or value.lower().endswith(".pyi"):
            return value
        raise ValueError("stub output must be a .pyi, manifest.json, or py.typed file")


class ReadablePlanEntry(FrozenModel):
    source: MaterializedFile
    output_path: RelativePath
    representation: RepresentationKind
    actionscript_bundle: RelativePath | None = None

    @model_validator(mode="after")
    def planned_output_matches_representation(self) -> ReadablePlanEntry:
        if self.representation is RepresentationKind.PASSTHROUGH:
            if self.output_path != self.source.path:
                raise ValueError("planned passthrough output must preserve its path")
        elif self.actionscript_bundle is not None:
            raise ValueError("only passthrough SWC files may declare an ActionScript bundle")
        if self.actionscript_bundle is not None and (
            self.source.language is not None or not self.source.path.lower().endswith(".swc")
        ):
            raise ValueError("ActionScript bundle must reference a base-layer SWC")
        return self


class ReadablePlanResult(FrozenModel):
    schema_version: Literal[1] = 1
    materialization_result_sha256: Digest
    policy_name: Annotated[str, StringConstraints(min_length=1)]
    policy_version: Annotated[str, StringConstraints(min_length=1)]
    policy_sha256: Sha256
    materialized_base_root: RelativePath
    materialized_locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    entries: tuple[ReadablePlanEntry, ...]

    @model_validator(mode="after")
    def plan_is_collision_free(self) -> ReadablePlanResult:
        source_keys = [(item.source.language, item.source.path) for item in self.entries]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("readable plan contains duplicate materialized sources")
        output_keys = [(item.source.language, item.output_path.casefold()) for item in self.entries]
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("readable plan contains colliding output paths")
        bundles = [
            item.actionscript_bundle.casefold()
            for item in self.entries
            if item.actionscript_bundle is not None
        ]
        if len(bundles) != len(set(bundles)):
            raise ValueError("readable plan contains colliding ActionScript bundles")
        languages = {
            item.source.language for item in self.entries if item.source.language is not None
        }
        if not languages.issubset(self.materialized_locale_roots):
            raise ValueError("readable plan references an unknown locale layer")
        return self


class ReadableTransformResult(FrozenModel):
    schema_version: Literal[1] = 1
    readable_plan_result_sha256: Digest
    base_root: RelativePath
    locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    files: tuple[ReadableFile, ...]

    @model_validator(mode="after")
    def transformed_files_are_unique(self) -> ReadableTransformResult:
        if any(item.representation.kind is RepresentationKind.PASSTHROUGH for item in self.files):
            raise ValueError("transform result must not contain passthrough files")
        keys = [(item.language, item.path.casefold()) for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("transform result contains colliding paths")
        return self


class ActionScriptResult(FrozenModel):
    schema_version: Literal[1] = 1
    readable_transform_result_sha256: Digest
    root: RelativePath
    files: tuple[ActionScriptFile, ...]

    @model_validator(mode="after")
    def actionscript_paths_are_unique(self) -> ActionScriptResult:
        keys = [item.path.casefold() for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("ActionScript result contains colliding paths")
        return self


class ReadableAssemblyResult(FrozenModel):
    schema_version: Literal[2] = 2
    actionscript_result_sha256: Digest
    materialization_result_sha256: Digest
    policy_name: Annotated[str, StringConstraints(min_length=1)]
    policy_version: Annotated[str, StringConstraints(min_length=1)]
    policy_sha256: Sha256
    base_root: RelativePath
    locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    actionscript_root: RelativePath
    files: tuple[ReadableFile, ...]
    actionscript_files: tuple[ActionScriptFile, ...]

    @model_validator(mode="after")
    def assembly_paths_are_unique(self) -> ReadableAssemblyResult:
        keys = [(item.language, item.path.casefold()) for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("readable assembly contains colliding layer paths")
        actionscript_keys = [item.path.casefold() for item in self.actionscript_files]
        if len(actionscript_keys) != len(set(actionscript_keys)):
            raise ValueError("readable assembly contains colliding ActionScript paths")
        return self


class EngineStubsResult(FrozenModel):
    schema_version: Literal[1] = 1
    readable_assembly_result_sha256: Digest
    root: RelativePath
    files: tuple[StubFile, ...]

    @model_validator(mode="after")
    def stub_paths_are_unique(self) -> EngineStubsResult:
        keys = [item.path.casefold() for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("engine stub result contains colliding paths")
        return self


class ReadableResult(FrozenModel):
    schema_version: Literal[1] = 1
    materialization_result_sha256: Digest
    policy_name: Annotated[str, StringConstraints(min_length=1)]
    policy_version: Annotated[str, StringConstraints(min_length=1)]
    policy_sha256: Sha256
    base_root: RelativePath
    locale_roots: dict[Language, RelativePath] = Field(min_length=1)
    actionscript_root: RelativePath
    stubs_root: RelativePath
    tools: tuple[ToolIdentity, ...] = Field(min_length=1)
    files: tuple[ReadableFile, ...]
    actionscript_files: tuple[ActionScriptFile, ...] = ()
    stub_files: tuple[StubFile, ...] = ()

    @model_validator(mode="after")
    def readable_manifest_is_complete(self) -> ReadableResult:
        keys = [(item.language, item.path.casefold()) for item in self.files]
        if len(keys) != len(set(keys)):
            raise ValueError("readable output contains colliding layer paths")
        actionscript_keys = [item.path.casefold() for item in self.actionscript_files]
        if len(actionscript_keys) != len(set(actionscript_keys)):
            raise ValueError("readable output contains colliding ActionScript paths")
        stub_keys = [item.path.casefold() for item in self.stub_files]
        if len(stub_keys) != len(set(stub_keys)):
            raise ValueError("readable output contains colliding stub paths")
        tool_keys = [(item.name, item.version) for item in self.tools]
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError("readable output contains duplicate tool identities")
        known_tools = set(tool_keys)
        representations = tuple(item.representation for item in self.files) + tuple(
            item.representation for item in self.actionscript_files
        )
        for representation in representations:
            if (
                representation.kind is not RepresentationKind.PASSTHROUGH
                and (
                    representation.tool,
                    representation.tool_version,
                )
                not in known_tools
            ):
                raise ValueError("readable representation references an unknown tool")
        return self


class PackageManifestEntry(FrozenModel):
    path: RelativePath
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    part: PartName
    part_version: Annotated[str, StringConstraints(min_length=1)]
    language: Language | None = None
    container: Literal["zip"]
    precedence: Annotated[int, Field(ge=0)]
    entries: Annotated[int, Field(ge=0)]


class ConflictCandidateV1(FrozenModel):
    source_kind: VfsSourceKind
    source_path: RelativePath
    source_sha256: Sha256
    entry_path: RelativePath
    precedence: Annotated[int, Field(ge=0)]


class ConflictManifestEntryV1(FrozenModel):
    canonical_path: RelativePath
    layer: Annotated[str, StringConstraints(pattern=r"^(base|locale:[A-Z]{2}(?:_[A-Z]{2})?)$")]
    candidates: tuple[ConflictCandidateV1, ...] = Field(min_length=2)
    winner: ConflictCandidateV1
    resolution_rule: Annotated[str, StringConstraints(min_length=1)]
    resolved: Literal[True]

    @model_validator(mode="after")
    def winner_is_a_candidate(self) -> ConflictManifestEntryV1:
        if self.winner not in self.candidates:
            raise ValueError("conflict winner is not one of its candidates")
        return self


class SnapshotTimings(FrozenModel):
    populate_seconds: Annotated[float, Field(ge=0)] = 0.0
    seal_seconds: Annotated[float, Field(ge=0)] = 0.0
    verify_descriptor_seconds: Annotated[float, Field(ge=0)] = 0.0
    verify_manifests_seconds: Annotated[float, Field(ge=0)] = 0.0
    verify_payload_seconds: Annotated[float, Field(ge=0)] = 0.0
    publish_seconds: Annotated[float, Field(ge=0)] = 0.0


class SnapshotResult(FrozenModel):
    schema_version: Literal[1] = 1
    readable_result_sha256: Digest
    snapshot_id: SnapshotId
    version_name: ReleaseName
    snapshot_path: RelativePath
    descriptor_sha256: Sha256
    file_records: Annotated[int, Field(ge=0)]
    actionscript_records: Annotated[int, Field(ge=0)]
    stub_records: Annotated[int, Field(ge=0)]
    package_records: Annotated[int, Field(ge=0)]
    conflict_records: Annotated[int, Field(ge=0)]
    timings: SnapshotTimings = Field(default_factory=SnapshotTimings)


class Stage(StrEnum):
    RESOLVE = "resolve"
    PLAN_ACQUISITION = "plan-acquisition"
    DOWNLOAD = "download"
    VERIFY = "verify"
    ASSEMBLE_CLIENT = "assemble-client"
    INDEX_VFS = "index-vfs"
    MATERIALIZE_VFS = "materialize-vfs"
    PLAN_READABLE = "plan-readable"
    TRANSFORM_READABLE = "transform-readable"
    DECOMPILE_ACTIONSCRIPT = "decompile-actionscript"
    ASSEMBLE_READABLE = "assemble-readable"
    GENERATE_ENGINE_STUBS = "generate-engine-stubs"
    FINALIZE_READABLE = "finalize-readable"
    SNAPSHOT = "snapshot"

    @property
    def number(self) -> int:
        return STAGE_ORDER.index(self) + 1

    @property
    def directory_name(self) -> str:
        return f"{self.number * 10:03d}-{self.value}"

    @property
    def predecessor(self) -> Stage | None:
        index = STAGE_ORDER.index(self)
        return None if index == 0 else STAGE_ORDER[index - 1]


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


class RunRequest(FrozenModel):
    target: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    client_type: ClientType
    languages: tuple[LanguageSelector, ...] = Field(min_length=1)

    @field_validator("languages", mode="before")
    @classmethod
    def canonicalize_languages(cls, value: object) -> object:
        if isinstance(value, str):
            raise ValueError("languages must be a sequence, not a comma-separated string")
        if isinstance(value, (list, tuple, set, frozenset)):
            normalized = [str(language).strip().upper() for language in value]
            if any(not language for language in normalized):
                raise ValueError("languages must not contain empty values")
            if len(normalized) != len(set(normalized)):
                raise ValueError("languages must be unique")
            return tuple(sorted(normalized))
        return value

    @model_validator(mode="after")
    def selectors_are_consistent(self) -> RunRequest:
        if "ALL" in self.languages and self.languages != ("ALL",):
            raise ValueError("ALL cannot be combined with explicit languages")
        return self

    @property
    def selects_all_languages(self) -> bool:
        return self.languages == ("ALL",)


class RunRecord(FrozenModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    request: RunRequest
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value


class UpstreamReference(FrozenModel):
    stage: Stage
    result_sha256: Digest


class StageInputDocument(FrozenModel):
    schema_version: Literal[1] = 1
    stage: Stage
    implementation_version: Annotated[str, StringConstraints(min_length=1)]
    run_request: RunRequest
    upstream: UpstreamReference | None
    configuration: JsonObject


class StageInputRecord(FrozenModel):
    digest: Digest
    document: StageInputDocument


class StageResult(FrozenModel):
    schema_version: Literal[1] = 1
    stage: Stage
    input_digest: Digest
    implementation_version: Annotated[str, StringConstraints(min_length=1)]
    payload: JsonObject


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ErrorInfo(FrozenModel):
    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
    message: Annotated[str, StringConstraints(min_length=1)]
    exception_type: Annotated[str, StringConstraints(min_length=1)] | None = None


class StageStatus(FrozenModel):
    schema_version: Literal[1] = 1
    stage: Stage
    state: StageState
    attempt: Annotated[int, Field(ge=0)] = 0
    input_digest: Digest | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_sha256: Digest | None = None
    statistics: JsonObject = Field(default_factory=dict)
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def fields_match_state(self) -> StageStatus:
        if self.state is StageState.PENDING:
            if self.attempt != 0 or any(
                value is not None
                for value in (
                    self.input_digest,
                    self.started_at,
                    self.finished_at,
                    self.result_sha256,
                    self.statistics or None,
                    self.error,
                )
            ):
                raise ValueError("pending status must not describe an attempt")
            return self

        if self.attempt < 1 or self.input_digest is None or self.started_at is None:
            raise ValueError("non-pending status requires attempt, input_digest, and started_at")
        if self.state is StageState.RUNNING:
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.result_sha256,
                    self.statistics or None,
                    self.error,
                )
            ):
                raise ValueError("running status must not contain terminal fields")
            return self

        if self.finished_at is None:
            raise ValueError("terminal status requires finished_at")
        if self.state is StageState.SUCCEEDED:
            if self.result_sha256 is None or self.error is not None:
                raise ValueError("succeeded status requires only result_sha256")
            return self
        if self.result_sha256 is not None or self.statistics or self.error is None:
            raise ValueError("failed or interrupted status requires only error")
        return self


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StageSummary(FrozenModel):
    stage: Stage
    state: StageState
    attempt: int
    input_digest: Digest | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    result_sha256: Digest | None = None
    statistics: JsonObject = Field(default_factory=dict)
    error: ErrorInfo | None = None


class RunReport(FrozenModel):
    run_id: RunId
    request: RunRequest
    created_at: datetime
    state: RunState
    completed_until: Stage | None
    current_stage: Stage | None
    locked: bool
    active_duration_seconds: Annotated[float, Field(ge=0)]
    stages: tuple[StageSummary, ...]

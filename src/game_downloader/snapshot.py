from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Annotated, BinaryIO, Protocol, cast

import ijson  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from game_downloader import __version__
from game_downloader._json import (
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
    canonical_sha256_digest,
)
from game_downloader.contracts import ContractName, ContractRegistry, ContractValidationError
from game_downloader.models import (
    AcquisitionPlan,
    ActionScriptFile,
    ActionScriptManifestEntryV1,
    BaseLayer,
    ClientTreeFile,
    ClientTreeResult,
    ConflictCandidateV1,
    ConflictManifestEntryV1,
    FileManifestEntryV1,
    FileRepresentationV1,
    FrozenModel,
    GamePackageFileSourceV1,
    GameSnapshotV1,
    IndexedPackage,
    LocaleLayer,
    LooseFileSourceV1,
    ManifestReference,
    PackageManifestEntry,
    PartVersion,
    PolicyReference,
    ReadableFile,
    ReadableResult,
    RelativePath,
    RepresentationKind,
    ResolveResult,
    SnapshotManifests,
    SnapshotPayload,
    SnapshotPolicies,
    SnapshotQuality,
    SnapshotResult,
    SnapshotSource,
    SnapshotTimings,
    Stage,
    StageInputRecord,
    StageState,
    StubFile,
    StubManifestEntryV1,
    ToolIdentity,
    VfsCandidate,
    VfsIndexedEntry,
    VfsSourceKind,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation
from game_downloader.vfs import VfsPolicy
from game_downloader.workspace import Workspace, sha256_digest

CONTRACT_VERSION = "1.1.0"
GAME_DOWNLOADER_TOOL = ToolIdentity(
    name="game-downloader",
    version=__version__,
    source="https://github.com/wotstat/game-unpack-pipeline",
)
PACKED_SECTION_MAGIC = b"EN\xa1b"


class SourceTreePolicy(FrozenModel):
    name: str = "wot-source-tree"
    version: str = "1"
    vfs_mount: RelativePath = "res"
    root_files: tuple[str, ...] = (
        "Licenses.txt",
        "loc_version.xml",
        "paths.xml",
        "version.xml",
    )
    mounted_directories: tuple[str, ...] = ("mods", "res_mods")
    mounted_file_suffixes: tuple[str, ...] = (
        ".cfg",
        ".ini",
        ".json",
        ".md",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class SnapshotVerificationPolicy(FrozenModel):
    max_workers: Annotated[int, Field(ge=1, le=32)] = 4
    hash_batch_files: Annotated[int, Field(ge=1, le=4096)] = 64
    hash_batch_bytes: Annotated[int, Field(ge=1)] = 32 * 1024 * 1024
    parallel_hash_minimum_bytes: Annotated[int, Field(ge=0)] = 64 * 1024 * 1024


class SnapshotVerificationError(ValueError):
    pass


class SnapshotSealError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("snapshot_invalid", message)


class VfsSnapshotSource(Protocol):
    @property
    def policy_name(self) -> str: ...

    @property
    def policy_version(self) -> str: ...

    @property
    def policy_sha256(self) -> str: ...

    @property
    def packages(self) -> Sequence[IndexedPackage]: ...

    @property
    def entries(self) -> Sequence[VfsIndexedEntry]: ...


@dataclass(frozen=True, slots=True)
class _VfsSnapshotData:
    policy_name: str
    policy_version: str
    policy_sha256: str
    packages: tuple[IndexedPackage, ...]
    entries: tuple[VfsIndexedEntry, ...]


@dataclass(frozen=True, slots=True)
class _HashRequest:
    path: Path
    expected_size: int
    expected_sha256: str
    mismatch_message: str
    reject_packed_xml: bool = False


type _HashResult = tuple[_HashRequest, str, int, bytes]


@dataclass(frozen=True, slots=True)
class _PopulatedSnapshot:
    descriptor: GameSnapshotV1
    populate_seconds: float
    seal_seconds: float


@dataclass(frozen=True, slots=True)
class _VerifiedSnapshot:
    snapshot: Snapshot
    descriptor_seconds: float
    manifests_seconds: float
    payload_seconds: float


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    descriptor: GameSnapshotV1

    @classmethod
    def open_and_verify(cls, path: Path) -> Snapshot:
        return SnapshotVerifier().verify(path)


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: BinaryIO | None = None
        self._digest = hashlib.sha256()
        self.records = 0

    def __enter__(self) -> _JsonlWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("xb")
        return self

    def write(self, value: BaseModel) -> None:
        if self._stream is None:
            raise RuntimeError("JSONL writer is not open")
        encoded = canonical_json_bytes(value.model_dump(mode="json", exclude_none=True))
        self._stream.write(encoded)
        self._digest.update(encoded)
        self.records += 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            if exc_type is None:
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
        finally:
            stream.close()
            self._stream = None

    def reference(self, relative_path: str) -> ManifestReference:
        if self._stream is not None:
            raise RuntimeError("JSONL writer must be closed before creating its reference")
        return ManifestReference(
            path=relative_path,
            sha256=self._digest.hexdigest(),
            records=self.records,
        )


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or path.as_posix() != value
    ):
        raise SnapshotVerificationError(f"unsafe or non-canonical relative path: {value!r}")
    return path


def _elapsed_seconds(started_at: float) -> float:
    return round(max(0.0, perf_counter() - started_at), 6)


def _hash_file_details(path: Path) -> tuple[str, int, bytes]:
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if len(prefix) < 4:
                prefix = (prefix + chunk)[:4]
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, prefix


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size, _prefix = _hash_file_details(path)
    return digest, size


def _hash_batch(requests: tuple[_HashRequest, ...]) -> tuple[_HashResult, ...]:
    return tuple((request, *_hash_file_details(request.path)) for request in requests)


def _link_or_copy_into_partial(
    source: Path,
    destination: Path,
    *,
    known_directories: set[Path],
) -> str:
    """Publish into a private partial tree without redundant per-file atomic renames."""

    source_stat = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
        raise SnapshotSealError(f"snapshot source is not a regular file: {source}")
    parent = destination.parent
    if parent not in known_directories:
        parent.mkdir(parents=True, exist_ok=True)
        known_directories.add(parent)
    method = "hardlink"
    created = False
    try:
        try:
            os.link(source, destination, follow_symlinks=False)
            created = True
        except OSError as exc:
            if exc.errno not in {
                errno.EXDEV,
                errno.EPERM,
                errno.EACCES,
                errno.ENOTSUP,
                errno.EMLINK,
            }:
                raise
            method = "copy"
            with source.open("rb") as input_file:
                output_file = destination.open("xb")
                created = True
                with output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                    output_file.flush()
                    os.fchmod(output_file.fileno(), 0o444)
                    os.fsync(output_file.fileno())
        if method == "hardlink" and stat.S_IMODE(source_stat.st_mode) != 0o444:
            destination.chmod(0o444)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return method


def _identity_document(descriptor: GameSnapshotV1) -> dict[str, JsonValue]:
    return {
        "contract": descriptor.contract,
        "contract_version": descriptor.contract_version,
        "policies": cast(JsonValue, descriptor.policies.model_dump(mode="json", exclude_none=True)),
        "source": cast(JsonValue, descriptor.source.model_dump(mode="json", exclude_none=True)),
        "tools": cast(
            JsonValue,
            [tool.model_dump(mode="json", exclude_none=True) for tool in descriptor.tools],
        ),
    }


def _snapshot_id(
    source: SnapshotSource,
    policies: SnapshotPolicies,
    tools: Sequence[ToolIdentity],
) -> str:
    document = {
        "contract": "game-snapshot",
        "contract_version": CONTRACT_VERSION,
        "policies": policies.model_dump(mode="json", exclude_none=True),
        "source": source.model_dump(mode="json", exclude_none=True),
        "tools": [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
    }
    return canonical_sha256_digest(document)


class SnapshotAssembler:
    def __init__(
        self,
        registry: ContractRegistry | None = None,
        source_tree_policy: SourceTreePolicy | None = None,
        verification_policy: SnapshotVerificationPolicy | None = None,
    ) -> None:
        self._registry = registry or ContractRegistry()
        self._source_tree_policy = source_tree_policy or SourceTreePolicy()
        self._verification_policy = verification_policy or SnapshotVerificationPolicy()

    def build(
        self,
        readable: ReadableResult,
        client_tree: ClientTreeResult,
        index: VfsSnapshotSource,
        resolve: ResolveResult,
        acquisition: AcquisitionPlan,
        workspace: Workspace,
    ) -> SnapshotResult:
        source = self._source(resolve, acquisition)
        policies = SnapshotPolicies(
            vfs=PolicyReference(
                name=index.policy_name,
                version=index.policy_version,
                sha256=index.policy_sha256,
            ),
            readable=PolicyReference(
                name=readable.policy_name,
                version=readable.policy_version,
                sha256=readable.policy_sha256,
            ),
            source_tree=PolicyReference(
                name=self._source_tree_policy.name,
                version=self._source_tree_policy.version,
                sha256=self._source_tree_policy.sha256,
            ),
        )
        tools = self._tools(readable.tools)
        snapshot_id = _snapshot_id(source, policies, tools)
        final_root = workspace.snapshots_root / snapshot_id
        partial_root = workspace.snapshots_root / f"{snapshot_id}.partial"
        if final_root.exists():
            try:
                verified = SnapshotVerifier(
                    self._registry,
                    self._verification_policy,
                )._verify_with_timings(final_root)
            except SnapshotVerificationError as exc:
                raise SnapshotSealError(
                    f"immutable snapshot path already exists but is invalid: {exc}"
                ) from exc
            return self._result_from_descriptor(
                verified.snapshot.descriptor,
                workspace,
                final_root,
                timings=SnapshotTimings(
                    verify_descriptor_seconds=verified.descriptor_seconds,
                    verify_manifests_seconds=verified.manifests_seconds,
                    verify_payload_seconds=verified.payload_seconds,
                ),
            )
        self._remove_partial(partial_root)
        partial_root.mkdir(mode=0o700)
        try:
            populated = self._populate(
                partial_root,
                snapshot_id,
                readable,
                client_tree,
                index,
                source,
                policies,
                tools,
                workspace,
            )
            verified = SnapshotVerifier(
                self._registry,
                self._verification_policy,
            )._verify_with_timings(partial_root)
            publish_started = perf_counter()
            os.replace(partial_root, final_root)
            workspace.fsync_directory(workspace.snapshots_root)
            publish_seconds = _elapsed_seconds(publish_started)
            return self._result_from_descriptor(
                populated.descriptor,
                workspace,
                final_root,
                timings=SnapshotTimings(
                    populate_seconds=populated.populate_seconds,
                    seal_seconds=populated.seal_seconds,
                    verify_descriptor_seconds=verified.descriptor_seconds,
                    verify_manifests_seconds=verified.manifests_seconds,
                    verify_payload_seconds=verified.payload_seconds,
                    publish_seconds=publish_seconds,
                ),
            )
        except SnapshotSealError:
            raise
        except Exception as exc:
            raise SnapshotSealError(str(exc) or type(exc).__name__) from exc

    def _populate(
        self,
        root: Path,
        snapshot_id: str,
        readable: ReadableResult,
        client_tree: ClientTreeResult,
        index: VfsSnapshotSource,
        source: SnapshotSource,
        policies: SnapshotPolicies,
        tools: tuple[ToolIdentity, ...],
        workspace: Workspace,
    ) -> _PopulatedSnapshot:
        populate_started = perf_counter()
        base = root / "sources/base"
        (base / self._source_tree_policy.vfs_mount).mkdir(parents=True)
        for locale_language in sorted(readable.locale_roots):
            (root / "sources/locales" / locale_language / self._source_tree_policy.vfs_mount).mkdir(
                parents=True
            )
        (root / "sources-as3").mkdir()
        (root / "stubs").mkdir()
        manifests_root = root / "manifests"
        manifests_root.mkdir()
        known_directories: set[Path] = {
            base,
            root / "sources-as3",
            root / "stubs",
            manifests_root,
        }

        files_writer = _JsonlWriter(manifests_root / "files.jsonl")
        with files_writer:
            published = self._source_tree_entries(readable, client_tree, workspace)
            for language, path, source_path, entry in published:
                destination_root = base
                if language is not None:
                    destination_root = root / "sources/locales" / language
                destination = destination_root / path
                _link_or_copy_into_partial(
                    source_path,
                    destination,
                    known_directories=known_directories,
                )
                files_writer.write(entry)

        actionscript_writer = _JsonlWriter(manifests_root / "actionscript.jsonl")
        with actionscript_writer:
            for item in sorted(
                readable.actionscript_files, key=lambda value: value.path.encode("utf-8")
            ):
                source_path = self._actionscript_source_path(item, readable, workspace)
                _link_or_copy_into_partial(
                    source_path,
                    root / "sources-as3" / item.path,
                    known_directories=known_directories,
                )
                actionscript_writer.write(self._actionscript_manifest_entry(item))

        stubs_writer = _JsonlWriter(manifests_root / "stubs.jsonl")
        with stubs_writer:
            for stub_item in sorted(
                readable.stub_files, key=lambda value: value.path.encode("utf-8")
            ):
                source_path = self._stub_source_path(stub_item, readable, workspace)
                _link_or_copy_into_partial(
                    source_path,
                    root / "stubs" / stub_item.path,
                    known_directories=known_directories,
                )
                stubs_writer.write(
                    StubManifestEntryV1(
                        path=stub_item.path,
                        size=stub_item.size,
                        sha256=stub_item.sha256,
                    )
                )

        packages_writer = _JsonlWriter(manifests_root / "packages.jsonl")
        with packages_writer:
            for package in sorted(
                index.packages,
                key=lambda value: (
                    value.language is not None,
                    value.language or "",
                    value.precedence,
                    value.path.encode("utf-8"),
                ),
            ):
                packages_writer.write(
                    PackageManifestEntry(
                        path=package.path,
                        size=package.blob_size,
                        sha256=package.blob_sha256,
                        part=package.part,
                        part_version=package.part_version,
                        language=package.language,
                        container="zip",
                        precedence=package.precedence,
                        entries=package.entries,
                    )
                )

        conflicts_writer = _JsonlWriter(manifests_root / "conflicts.jsonl")
        with conflicts_writer:
            for conflict in sorted(
                (entry for entry in index.entries if len(entry.candidates) > 1),
                key=lambda value: (value.layer, value.winner.canonical_path.encode("utf-8")),
            ):
                conflicts_writer.write(
                    ConflictManifestEntryV1(
                        canonical_path=conflict.winner.canonical_path,
                        layer=conflict.layer,
                        candidates=tuple(
                            self._conflict_candidate(candidate) for candidate in conflict.candidates
                        ),
                        winner=self._conflict_candidate(conflict.winner),
                        resolution_rule=conflict.resolution_rule,
                        resolved=True,
                    )
                )

        populate_seconds = _elapsed_seconds(populate_started)
        seal_started = perf_counter()
        descriptor = GameSnapshotV1(
            contract="game-snapshot",
            contract_version=CONTRACT_VERSION,
            snapshot_id=snapshot_id,
            created_at=datetime.now(UTC),
            source=source,
            payload=SnapshotPayload(
                base_root="sources/base",
                locale_roots={
                    language: f"sources/locales/{language}"
                    for language in sorted(readable.locale_roots)
                },
                actionscript_root="sources-as3",
                stubs_root="stubs",
                overlay_order=("base", "locale:{language}"),
            ),
            manifests=SnapshotManifests(
                files=files_writer.reference("manifests/files.jsonl"),
                actionscript=actionscript_writer.reference("manifests/actionscript.jsonl"),
                stubs=stubs_writer.reference("manifests/stubs.jsonl"),
                packages=packages_writer.reference("manifests/packages.jsonl"),
                conflicts=conflicts_writer.reference("manifests/conflicts.jsonl"),
            ),
            policies=policies,
            tools=tools,
            quality=SnapshotQuality(
                unresolved_conflicts=0,
                required_transform_failures=0,
                unmanifested_payload_files=0,
            ),
        )
        descriptor_document = descriptor.model_dump(mode="json", exclude_none=True)
        self._registry.validate_game_snapshot(descriptor_document)
        descriptor_bytes = canonical_json_bytes(descriptor_document)
        self._write_readonly(root / "snapshot.json", descriptor_bytes)
        ready = sha256_digest(descriptor_bytes).encode("ascii") + b"\n"
        self._write_readonly(root / "READY", ready)
        self._seal_directories(root)
        return _PopulatedSnapshot(
            descriptor=descriptor,
            populate_seconds=populate_seconds,
            seal_seconds=_elapsed_seconds(seal_started),
        )

    @staticmethod
    def _source(
        resolve: ResolveResult,
        acquisition: AcquisitionPlan,
    ) -> SnapshotSource:
        planned = {(item.part, item.language): item for item in acquisition.parts}
        resolved_keys = {(item.name, item.language) for item in resolve.version_vector}
        if set(planned) != resolved_keys:
            raise SnapshotSealError("Acquisition Plan Parts do not match the pinned Version Vector")
        versions: list[PartVersion] = []
        for item in resolve.version_vector:
            plan = planned[(item.name, item.language)]
            if plan.version != item.version:
                raise SnapshotSealError("Acquisition Plan version changed after resolve")
            versions.append(
                PartVersion(
                    name=item.name,
                    language=item.language,
                    version=item.version,
                    acquisition_mode=plan.acquisition_mode,
                )
            )
        return SnapshotSource(
            target=resolve.resolved_target.target,
            publisher=resolve.resolved_target.publisher.value,
            api_host=resolve.resolved_target.api_host,
            resolved_app_id=resolve.resolved_target.app_id,
            chain_id=resolve.chain_id,
            client_type=resolve.client_type,
            languages=resolve.languages,
            metadata_version=resolve.metadata_version,
            release_name=resolve.release_name,
            version_vector=tuple(versions),
        )

    @staticmethod
    def _tools(readable_tools: Sequence[ToolIdentity]) -> tuple[ToolIdentity, ...]:
        values = {
            (item.name, item.version): item for item in (*readable_tools, GAME_DOWNLOADER_TOOL)
        }
        return tuple(values[key] for key in sorted(values))

    def _source_tree_entries(
        self,
        readable: ReadableResult,
        client_tree: ClientTreeResult,
        workspace: Workspace,
    ) -> tuple[tuple[str | None, str, Path, FileManifestEntryV1], ...]:
        values: list[tuple[str | None, str, Path, FileManifestEntryV1]] = []
        for readable_file in readable.files:
            published_path = f"{self._source_tree_policy.vfs_mount}/{readable_file.path}"
            values.append(
                (
                    readable_file.language,
                    published_path,
                    self._readable_source_path(readable_file, readable, workspace),
                    self._file_manifest_entry(readable_file, published_path),
                )
            )
        for client_file in client_tree.files:
            if not self._include_client_tree_file(client_file.path):
                continue
            values.append(
                (
                    client_file.language,
                    client_file.path,
                    self._client_tree_source_path(client_file, client_tree, workspace),
                    self._client_tree_manifest_entry(client_file),
                )
            )
        ordered = tuple(
            sorted(
                values,
                key=lambda value: (
                    value[0] is not None,
                    value[0] or "",
                    value[1].encode("utf-8"),
                ),
            )
        )
        seen: set[tuple[str | None, str]] = set()
        for language, path, _source, _entry in ordered:
            key = language, path.casefold()
            if key in seen:
                raise SnapshotSealError(f"source tree path collision: {path!r}")
            seen.add(key)
        return ordered

    def _include_client_tree_file(self, path: str) -> bool:
        lowered = path.casefold()
        if "/" not in path:
            return lowered in {value.casefold() for value in self._source_tree_policy.root_files}
        for directory in self._source_tree_policy.mounted_directories:
            prefix = f"{directory.casefold()}/"
            if lowered.startswith(prefix) and lowered.endswith(
                tuple(value.casefold() for value in self._source_tree_policy.mounted_file_suffixes)
            ):
                return True
        return False

    @staticmethod
    def _readable_source_path(
        item: ReadableFile,
        readable: ReadableResult,
        workspace: Workspace,
    ) -> Path:
        root = (
            workspace.root / readable.locale_roots[item.language]
            if item.language is not None
            else workspace.root / readable.base_root
        )
        path = root / item.path
        path_stat = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_size != item.size
        ):
            raise SnapshotSealError(f"Readable Result source changed: {path}")
        return path

    @staticmethod
    def _actionscript_source_path(
        item: ActionScriptFile,
        readable: ReadableResult,
        workspace: Workspace,
    ) -> Path:
        path = workspace.root / readable.actionscript_root / item.path
        path_stat = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_size != item.size
        ):
            raise SnapshotSealError(f"ActionScript Result source changed: {path}")
        return path

    @staticmethod
    def _stub_source_path(
        item: StubFile,
        readable: ReadableResult,
        workspace: Workspace,
    ) -> Path:
        path = workspace.root / readable.stubs_root / item.path
        path_stat = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_size != item.size
        ):
            raise SnapshotSealError(f"engine stub Result source changed: {path}")
        return path

    @staticmethod
    def _client_tree_source_path(
        item: ClientTreeFile,
        client_tree: ClientTreeResult,
        workspace: Workspace,
    ) -> Path:
        layer_root = (
            workspace.root / client_tree.locale_roots[item.language]
            if item.language is not None
            else workspace.root / client_tree.base_root
        )
        path = layer_root / item.path
        path_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
            raise SnapshotSealError(f"Client Tree source is not regular: {path}")
        digest, size = _hash_file(path)
        if size != item.blob_size or digest != item.blob_sha256:
            raise SnapshotSealError(f"Client Tree source changed: {path}")
        return path

    @staticmethod
    def _file_manifest_entry(item: ReadableFile, published_path: str) -> FileManifestEntryV1:
        candidate = item.source.source
        common = {
            "part": candidate.part,
            "part_version": candidate.part_version,
            "language": candidate.language,
            "entry_path": candidate.original_path,
            "entry_sha256": item.source.sha256,
        }
        provenance: GamePackageFileSourceV1 | LooseFileSourceV1
        if candidate.source_kind is VfsSourceKind.GAME_PACKAGE:
            provenance = GamePackageFileSourceV1(
                kind="game-package",
                game_package_path=candidate.source_path,
                game_package_sha256=candidate.source_sha256,
                **common,
            )
        else:
            provenance = LooseFileSourceV1(
                kind="loose-file",
                client_tree_path=candidate.source_path,
                client_tree_sha256=candidate.source_sha256,
                **common,
            )
        layer = (
            LocaleLayer(kind="locale", language=item.language)
            if item.language is not None
            else BaseLayer(kind="base")
        )
        return FileManifestEntryV1(
            path=published_path,
            layer=layer,
            size=item.size,
            sha256=item.sha256,
            source=provenance,
            representation=FileRepresentationV1(
                **item.representation.model_dump(mode="python"),
                diagnostics=item.diagnostics,
            ),
        )

    @staticmethod
    def _client_tree_manifest_entry(item: ClientTreeFile) -> FileManifestEntryV1:
        layer = (
            LocaleLayer(kind="locale", language=item.language)
            if item.language is not None
            else BaseLayer(kind="base")
        )
        return FileManifestEntryV1(
            path=item.path,
            layer=layer,
            size=item.blob_size,
            sha256=item.blob_sha256,
            source=LooseFileSourceV1(
                kind="loose-file",
                part=item.part,
                part_version=item.part_version,
                language=item.language,
                client_tree_path=item.path,
                client_tree_sha256=item.blob_sha256,
                entry_path=item.source_entry_path or item.path,
                entry_sha256=item.blob_sha256,
            ),
            representation=FileRepresentationV1(
                kind=RepresentationKind.PASSTHROUGH,
                source_path=item.path,
                source_sha256=item.blob_sha256,
                diagnostics=(),
            ),
        )

    @classmethod
    def _actionscript_manifest_entry(cls, item: ActionScriptFile) -> ActionScriptManifestEntryV1:
        candidate = item.source.source
        common = {
            "part": candidate.part,
            "part_version": candidate.part_version,
            "entry_path": candidate.original_path,
            "entry_sha256": item.source.sha256,
        }
        provenance: GamePackageFileSourceV1 | LooseFileSourceV1
        if candidate.source_kind is VfsSourceKind.GAME_PACKAGE:
            provenance = GamePackageFileSourceV1(
                kind="game-package",
                game_package_path=candidate.source_path,
                game_package_sha256=candidate.source_sha256,
                **common,
            )
        else:
            provenance = LooseFileSourceV1(
                kind="loose-file",
                client_tree_path=candidate.source_path,
                client_tree_sha256=candidate.source_sha256,
                **common,
            )
        return ActionScriptManifestEntryV1(
            path=item.path,
            size=item.size,
            sha256=item.sha256,
            source=provenance,
            representation=FileRepresentationV1(
                **item.representation.model_dump(mode="python"),
                diagnostics=item.diagnostics,
            ),
        )

    @staticmethod
    def _conflict_candidate(candidate: VfsCandidate) -> ConflictCandidateV1:
        return ConflictCandidateV1(
            source_kind=candidate.source_kind,
            source_path=candidate.source_path,
            source_sha256=candidate.source_sha256,
            entry_path=candidate.original_path,
            precedence=candidate.precedence,
        )

    @staticmethod
    def _write_readonly(path: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _seal_directories(root: Path) -> None:
        directories = [Path(current) for current, _dirs, _files in os.walk(root)]
        for directory in reversed(directories):
            os.chmod(directory, 0o555)

    @staticmethod
    def _remove_partial(path: Path) -> None:
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise SnapshotSealError(f"snapshot partial path is not a real directory: {path}")
        for current, directory_names, _file_names in os.walk(path, topdown=False):
            for name in directory_names:
                with suppress(OSError):
                    os.chmod(Path(current) / name, 0o700)
            with suppress(OSError):
                os.chmod(current, 0o700)
        shutil.rmtree(path)

    @staticmethod
    def _result_from_descriptor(
        descriptor: GameSnapshotV1,
        workspace: Workspace,
        root: Path,
        *,
        timings: SnapshotTimings,
    ) -> SnapshotResult:
        descriptor_digest, _size = _hash_file(root / "snapshot.json")
        return SnapshotResult(
            readable_result_sha256="sha256:" + "0" * 64,
            snapshot_id=descriptor.snapshot_id,
            version_name=descriptor.source.release_name,
            snapshot_path=root.relative_to(workspace.root).as_posix(),
            descriptor_sha256=descriptor_digest,
            file_records=descriptor.manifests.files.records,
            actionscript_records=descriptor.manifests.actionscript.records,
            stub_records=descriptor.manifests.stubs.records,
            package_records=descriptor.manifests.packages.records,
            conflict_records=descriptor.manifests.conflicts.records,
            timings=timings,
        )


class SnapshotVerifier:
    def __init__(
        self,
        registry: ContractRegistry | None = None,
        policy: SnapshotVerificationPolicy | None = None,
    ) -> None:
        self._registry = registry or ContractRegistry()
        self._policy = policy or SnapshotVerificationPolicy()

    def verify(self, path: Path) -> Snapshot:
        return self._verify_with_timings(path).snapshot

    def _verify_with_timings(self, path: Path) -> _VerifiedSnapshot:
        descriptor_started = perf_counter()
        root = path.absolute()
        if root.is_symlink() or not root.is_dir():
            raise SnapshotVerificationError(f"snapshot root is not a real directory: {root}")
        self._verify_layout(root)
        descriptor_path = self._regular_readonly_file(root / "snapshot.json")
        descriptor_bytes = descriptor_path.read_bytes()
        try:
            raw_descriptor = json.loads(descriptor_bytes)
        except json.JSONDecodeError as exc:
            raise SnapshotVerificationError(f"snapshot.json is invalid JSON: {exc}") from exc
        if descriptor_bytes != canonical_json_bytes(raw_descriptor):
            raise SnapshotVerificationError("snapshot.json is not canonical JSON")
        try:
            descriptor = self._registry.validate_game_snapshot(raw_descriptor)
        except ContractValidationError as exc:
            raise SnapshotVerificationError(str(exc)) from exc
        ready = self._regular_readonly_file(root / "READY").read_bytes()
        expected_ready = sha256_digest(descriptor_bytes).encode("ascii") + b"\n"
        if ready != expected_ready:
            raise SnapshotVerificationError("READY does not match canonical snapshot.json")
        expected_id = canonical_sha256_digest(_identity_document(descriptor))
        if descriptor.snapshot_id != expected_id:
            raise SnapshotVerificationError("snapshot_id does not match its identity document")
        descriptor_seconds = _elapsed_seconds(descriptor_started)

        manifests_started = perf_counter()
        packages = self._verify_packages(root, descriptor)
        self._verify_conflicts(root, descriptor)
        expected_files = self._verify_files_manifest(root, descriptor, packages)
        expected_actionscript = self._verify_actionscript_manifest(
            root, descriptor, packages, expected_files
        )
        expected_stubs = self._verify_stubs_manifest(root, descriptor)
        manifests_seconds = _elapsed_seconds(manifests_started)

        payload_started = perf_counter()
        self._verify_payload(root, descriptor, expected_files)
        self._verify_actionscript_payload(root, descriptor, expected_actionscript)
        self._verify_stubs_payload(root, descriptor, expected_stubs)
        return _VerifiedSnapshot(
            snapshot=Snapshot(path=root, descriptor=descriptor),
            descriptor_seconds=descriptor_seconds,
            manifests_seconds=manifests_seconds,
            payload_seconds=_elapsed_seconds(payload_started),
        )

    @staticmethod
    def _verify_layout(root: Path) -> None:
        SnapshotVerifier._readonly_directory(root)
        if {item.name for item in root.iterdir()} != {
            "READY",
            "manifests",
            "snapshot.json",
            "sources",
            "sources-as3",
            "stubs",
        }:
            raise SnapshotVerificationError(
                "snapshot root layout contains missing or extra entries"
            )
        manifests = root / "manifests"
        sources = root / "sources"
        actionscript = root / "sources-as3"
        stubs = root / "stubs"
        if (
            manifests.is_symlink()
            or not manifests.is_dir()
            or sources.is_symlink()
            or not sources.is_dir()
            or actionscript.is_symlink()
            or not actionscript.is_dir()
            or stubs.is_symlink()
            or not stubs.is_dir()
        ):
            raise SnapshotVerificationError("snapshot layout directories are invalid")
        SnapshotVerifier._readonly_directory(manifests)
        SnapshotVerifier._readonly_directory(sources)
        SnapshotVerifier._readonly_directory(actionscript)
        SnapshotVerifier._readonly_directory(stubs)
        if {item.name for item in manifests.iterdir()} != {
            "actionscript.jsonl",
            "conflicts.jsonl",
            "files.jsonl",
            "packages.jsonl",
            "stubs.jsonl",
        }:
            raise SnapshotVerificationError("snapshot manifests layout is invalid")

    @staticmethod
    def _readonly_directory(path: Path) -> None:
        path_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
            raise SnapshotVerificationError(f"snapshot directory is invalid: {path}")
        if stat.S_IMODE(path_stat.st_mode) & 0o222:
            raise SnapshotVerificationError(f"sealed snapshot directory is writable: {path}")

    @staticmethod
    def _regular_readonly_file(path: Path) -> Path:
        path_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
            raise SnapshotVerificationError(f"snapshot file is not regular: {path}")
        if stat.S_IMODE(path_stat.st_mode) & 0o222:
            raise SnapshotVerificationError(f"sealed snapshot file is writable: {path}")
        return path

    @staticmethod
    def _accept_hash_results(results: tuple[_HashResult, ...]) -> None:
        for request, digest, size, prefix in results:
            if (size, digest) != (request.expected_size, request.expected_sha256):
                raise SnapshotVerificationError(request.mismatch_message)
            if request.reject_packed_xml and prefix == PACKED_SECTION_MAGIC:
                raise SnapshotVerificationError("payload retained packed XML")

    def _verify_hash_requests(
        self,
        requests: Iterator[_HashRequest],
        *,
        total_bytes: int,
    ) -> None:
        if self._policy.max_workers == 1 or total_bytes < self._policy.parallel_hash_minimum_bytes:
            for request in requests:
                self._accept_hash_results(_hash_batch((request,)))
            return

        pending: deque[Future[tuple[_HashResult, ...]]] = deque()
        batch: list[_HashRequest] = []
        batch_bytes = 0
        maximum_pending = self._policy.max_workers * 2
        with ThreadPoolExecutor(
            max_workers=self._policy.max_workers,
            thread_name_prefix="snapshot-hash",
        ) as executor:
            for request in requests:
                if batch and (
                    len(batch) >= self._policy.hash_batch_files
                    or batch_bytes + request.expected_size > self._policy.hash_batch_bytes
                ):
                    pending.append(executor.submit(_hash_batch, tuple(batch)))
                    batch = []
                    batch_bytes = 0
                batch.append(request)
                batch_bytes += request.expected_size
                if len(pending) >= maximum_pending:
                    self._accept_hash_results(pending.popleft().result())
            if batch:
                pending.append(executor.submit(_hash_batch, tuple(batch)))
            while pending:
                self._accept_hash_results(pending.popleft().result())

    def _manifest_documents(
        self,
        root: Path,
        reference: ManifestReference,
        contract_name: ContractName,
    ) -> Iterator[BaseModel]:
        relative = _safe_relative_path(reference.path)
        path = self._regular_readonly_file(root.joinpath(*relative.parts))
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as source:
            for line in source:
                digest.update(line)
                if not line.endswith(b"\n") or line == b"\n":
                    raise SnapshotVerificationError(f"{reference.path} has a malformed JSONL line")
                try:
                    document = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SnapshotVerificationError(
                        f"{reference.path} contains invalid JSON: {exc}"
                    ) from exc
                if line != canonical_json_bytes(document):
                    raise SnapshotVerificationError(f"{reference.path} is not canonical JSONL")
                try:
                    yield self._registry.validate(contract_name, document)
                except ContractValidationError as exc:
                    raise SnapshotVerificationError(str(exc)) from exc
                count += 1
        if digest.hexdigest() != reference.sha256 or count != reference.records:
            raise SnapshotVerificationError(
                f"{reference.path} digest or record count does not match snapshot.json"
            )

    def _verify_packages(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
    ) -> set[tuple[str | None, str, str]]:
        result: set[tuple[str | None, str, str]] = set()
        previous: tuple[bool, str, int, bytes] | None = None
        for raw in self._manifest_documents(
            root, descriptor.manifests.packages, "package-manifest-entry"
        ):
            if not isinstance(raw, PackageManifestEntry):
                raise AssertionError("package contract returned the wrong model")
            key = (
                raw.language is not None,
                raw.language or "",
                raw.precedence,
                raw.path.encode("utf-8"),
            )
            if previous is not None and key <= previous:
                raise SnapshotVerificationError("packages manifest order is not deterministic")
            previous = key
            identity = (raw.language, raw.path, raw.sha256)
            if identity in result:
                raise SnapshotVerificationError("packages manifest contains a duplicate")
            result.add(identity)
        return result

    def _verify_conflicts(self, root: Path, descriptor: GameSnapshotV1) -> None:
        previous: tuple[str, bytes] | None = None
        seen: set[tuple[str, str]] = set()
        for raw in self._manifest_documents(
            root, descriptor.manifests.conflicts, "conflict-manifest-entry"
        ):
            if not isinstance(raw, ConflictManifestEntryV1):
                raise AssertionError("conflict contract returned the wrong model")
            key = (raw.layer, raw.canonical_path.encode("utf-8"))
            if previous is not None and key <= previous:
                raise SnapshotVerificationError("conflicts manifest order is not deterministic")
            previous = key
            identity = (raw.layer, raw.canonical_path.casefold())
            if identity in seen:
                raise SnapshotVerificationError("conflicts manifest contains a duplicate")
            seen.add(identity)

    def _verify_files_manifest(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
        packages: set[tuple[str | None, str, str]],
    ) -> dict[tuple[str | None, str], tuple[int, str]]:
        tools = {(item.name, item.version) for item in descriptor.tools}
        result: dict[tuple[str | None, str], tuple[int, str]] = {}
        previous: tuple[bool, str, bytes] | None = None
        for raw in self._manifest_documents(
            root, descriptor.manifests.files, "file-manifest-entry"
        ):
            if not isinstance(raw, FileManifestEntryV1):
                raise AssertionError("file contract returned the wrong model")
            language = raw.layer.language if isinstance(raw.layer, LocaleLayer) else None
            key = (language is not None, language or "", raw.path.encode("utf-8"))
            if previous is not None and key <= previous:
                raise SnapshotVerificationError("files manifest order is not deterministic")
            previous = key
            identity = (language, raw.path)
            if identity in result:
                raise SnapshotVerificationError("files manifest contains a duplicate path")
            if raw.source.language != language:
                raise SnapshotVerificationError("file source language differs from its layer")
            if raw.representation.source_sha256 != raw.source.entry_sha256:
                raise SnapshotVerificationError("representation is not bound to source entry bytes")
            if (
                raw.representation.kind is not RepresentationKind.PASSTHROUGH
                and (
                    raw.representation.tool,
                    raw.representation.tool_version,
                )
                not in tools
            ):
                raise SnapshotVerificationError("file representation references an unknown tool")
            if isinstance(raw.source, GamePackageFileSourceV1):
                package = (
                    raw.source.language,
                    raw.source.game_package_path,
                    raw.source.game_package_sha256,
                )
                if package not in packages:
                    raise SnapshotVerificationError(
                        "file source references an unknown Game Package"
                    )
            elif raw.source.client_tree_sha256 != raw.source.entry_sha256:
                raise SnapshotVerificationError(
                    "loose-file source digest differs from entry digest"
                )
            if raw.path.lower().endswith((".pyc", ".mo")):
                raise SnapshotVerificationError("files manifest retained a compiled source path")
            result[identity] = (raw.size, raw.sha256)
        return result

    def _verify_payload(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
        expected: dict[tuple[str | None, str], tuple[int, str]],
    ) -> None:
        if descriptor.payload.base_root != "sources/base":
            raise SnapshotVerificationError("payload base root is not canonical")
        if descriptor.payload.actionscript_root != "sources-as3":
            raise SnapshotVerificationError("ActionScript root is not canonical")
        expected_locale_roots = {
            language: f"sources/locales/{language}" for language in descriptor.source.languages
        }
        if descriptor.payload.locale_roots != expected_locale_roots:
            raise SnapshotVerificationError("payload locale roots do not match source languages")
        roots: dict[str | None, Path] = {
            None: root.joinpath(*_safe_relative_path(descriptor.payload.base_root).parts)
        }
        roots.update(
            {
                language: root.joinpath(*_safe_relative_path(relative).parts)
                for language, relative in descriptor.payload.locale_roots.items()
            }
        )
        if len({path.resolve() for path in roots.values()}) != len(roots):
            raise SnapshotVerificationError("payload layer roots overlap")

        def requests() -> Iterator[_HashRequest]:
            actual: set[tuple[str | None, str]] = set()
            for language, layer_root in roots.items():
                self._readonly_directory(layer_root)
                for current, directory_names, file_names in os.walk(
                    layer_root,
                    followlinks=False,
                ):
                    current_path = Path(current)
                    self._readonly_directory(current_path)
                    for name in directory_names:
                        self._readonly_directory(current_path / name)
                    for name in file_names:
                        path = self._regular_readonly_file(current_path / name)
                        relative = path.relative_to(layer_root).as_posix()
                        identity = (language, relative)
                        metadata = expected.get(identity)
                        if metadata is None:
                            raise SnapshotVerificationError("payload contains an unmanifested file")
                        actual.add(identity)
                        yield _HashRequest(
                            path=path,
                            expected_size=metadata[0],
                            expected_sha256=metadata[1],
                            mismatch_message="payload file differs from its manifest",
                            reject_packed_xml=relative.lower().endswith(".xml"),
                        )
            if actual != set(expected):
                raise SnapshotVerificationError("files manifest references missing payload files")

        self._verify_hash_requests(
            requests(),
            total_bytes=sum(size for size, _digest in expected.values()),
        )

    def _verify_actionscript_manifest(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
        packages: set[tuple[str | None, str, str]],
        source_files: dict[tuple[str | None, str], tuple[int, str]],
    ) -> dict[str, tuple[int, str]]:
        tools = {(item.name, item.version) for item in descriptor.tools}
        result: dict[str, tuple[int, str]] = {}
        seen: set[str] = set()
        previous: bytes | None = None
        for raw in self._manifest_documents(
            root,
            descriptor.manifests.actionscript,
            "actionscript-manifest-entry",
        ):
            if not isinstance(raw, ActionScriptManifestEntryV1):
                raise AssertionError("ActionScript contract returned the wrong model")
            key = raw.path.encode("utf-8")
            if previous is not None and key <= previous:
                raise SnapshotVerificationError("ActionScript manifest order is not deterministic")
            previous = key
            lookup = raw.path.casefold()
            if lookup in seen:
                raise SnapshotVerificationError("ActionScript manifest contains a duplicate path")
            seen.add(lookup)
            representation = raw.representation
            if (
                representation.tool,
                representation.tool_version,
            ) not in tools:
                raise SnapshotVerificationError(
                    "ActionScript representation references an unknown tool"
                )
            if representation.source_sha256 != raw.source.entry_sha256:
                raise SnapshotVerificationError(
                    "ActionScript representation is not bound to source SWC bytes"
                )
            if isinstance(raw.source, GamePackageFileSourceV1):
                package = (
                    None,
                    raw.source.game_package_path,
                    raw.source.game_package_sha256,
                )
                if package not in packages:
                    raise SnapshotVerificationError(
                        "ActionScript source references an unknown Game Package"
                    )
            elif raw.source.client_tree_sha256 != raw.source.entry_sha256:
                raise SnapshotVerificationError(
                    "ActionScript loose source digest differs from entry digest"
                )
            published_swc = f"res/{representation.source_path}"
            source_metadata = source_files.get((None, published_swc))
            if source_metadata is None or source_metadata[1] != raw.source.entry_sha256:
                raise SnapshotVerificationError(
                    "ActionScript source SWC is missing from the source tree"
                )
            result[raw.path] = (raw.size, raw.sha256)
        return result

    def _verify_actionscript_payload(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
        expected: dict[str, tuple[int, str]],
    ) -> None:
        relative_root = _safe_relative_path(descriptor.payload.actionscript_root)
        if relative_root.as_posix() != "sources-as3":
            raise SnapshotVerificationError("ActionScript payload root is not canonical")
        actionscript_root = root.joinpath(*relative_root.parts)

        def requests() -> Iterator[_HashRequest]:
            self._readonly_directory(actionscript_root)
            actual: set[str] = set()
            for current, directory_names, file_names in os.walk(
                actionscript_root,
                followlinks=False,
            ):
                current_path = Path(current)
                self._readonly_directory(current_path)
                for name in directory_names:
                    self._readonly_directory(current_path / name)
                for name in file_names:
                    path = self._regular_readonly_file(current_path / name)
                    relative = path.relative_to(actionscript_root).as_posix()
                    metadata = expected.get(relative)
                    if metadata is None:
                        raise SnapshotVerificationError(
                            "ActionScript payload contains an unmanifested file"
                        )
                    if not relative.lower().endswith(".as"):
                        raise SnapshotVerificationError(
                            "ActionScript payload contains a non-AS file"
                        )
                    actual.add(relative)
                    yield _HashRequest(
                        path=path,
                        expected_size=metadata[0],
                        expected_sha256=metadata[1],
                        mismatch_message="ActionScript payload differs from its manifest",
                    )
            if actual != set(expected):
                raise SnapshotVerificationError(
                    "ActionScript manifest references missing payload files"
                )

        self._verify_hash_requests(
            requests(),
            total_bytes=sum(size for size, _digest in expected.values()),
        )

    def _verify_stubs_manifest(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
    ) -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        seen: set[str] = set()
        previous: bytes | None = None
        for raw in self._manifest_documents(
            root,
            descriptor.manifests.stubs,
            "stub-manifest-entry",
        ):
            if not isinstance(raw, StubManifestEntryV1):
                raise AssertionError("stub contract returned the wrong model")
            key = raw.path.encode("utf-8")
            if previous is not None and key <= previous:
                raise SnapshotVerificationError("stubs manifest order is not deterministic")
            previous = key
            lookup = raw.path.casefold()
            if lookup in seen:
                raise SnapshotVerificationError("stubs manifest contains a duplicate path")
            seen.add(lookup)
            result[raw.path] = (raw.size, raw.sha256)
        return result

    def _verify_stubs_payload(
        self,
        root: Path,
        descriptor: GameSnapshotV1,
        expected: dict[str, tuple[int, str]],
    ) -> None:
        relative_root = _safe_relative_path(descriptor.payload.stubs_root)
        if relative_root.as_posix() != "stubs":
            raise SnapshotVerificationError("engine stubs payload root is not canonical")
        stubs_root = root.joinpath(*relative_root.parts)

        def requests() -> Iterator[_HashRequest]:
            self._readonly_directory(stubs_root)
            actual: set[str] = set()
            for current, directory_names, file_names in os.walk(
                stubs_root,
                followlinks=False,
            ):
                current_path = Path(current)
                self._readonly_directory(current_path)
                for name in directory_names:
                    self._readonly_directory(current_path / name)
                for name in file_names:
                    path = self._regular_readonly_file(current_path / name)
                    relative = path.relative_to(stubs_root).as_posix()
                    metadata = expected.get(relative)
                    if metadata is None:
                        raise SnapshotVerificationError(
                            "engine stubs payload contains an unmanifested file"
                        )
                    actual.add(relative)
                    yield _HashRequest(
                        path=path,
                        expected_size=metadata[0],
                        expected_sha256=metadata[1],
                        mismatch_message="engine stubs payload differs from its manifest",
                    )
            if actual != set(expected):
                raise SnapshotVerificationError("stubs manifest references missing payload files")

        self._verify_hash_requests(
            requests(),
            total_bytes=sum(size for size, _digest in expected.values()),
        )


def _load_vfs_snapshot_data(
    workspace: Workspace,
    run_id: str,
) -> _VfsSnapshotData:
    stage = Stage.INDEX_VFS
    status = workspace.load_stage_status(run_id, stage)
    if status.state is not StageState.SUCCEEDED or status.result_sha256 is None:
        raise SnapshotSealError("required Stage is not committed: index-vfs")
    result_path = workspace.stage_path(run_id, stage) / "result.json"
    digest = hashlib.sha256()
    with result_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if f"sha256:{digest.hexdigest()}" != status.result_sha256:
        raise SnapshotSealError("committed Stage digest changed: index-vfs")

    input_path = workspace.stage_path(run_id, stage) / "input.json"
    input_bytes = workspace.read_bytes(input_path)
    stage_input = StageInputRecord.model_validate_json(input_bytes)
    canonical_input = canonical_json_bytes(stage_input.model_dump(mode="json"))
    calculated_input_digest = canonical_sha256_digest(stage_input.document.model_dump(mode="json"))
    if (
        input_bytes != canonical_input
        or stage_input.digest != calculated_input_digest
        or stage_input.digest != status.input_digest
        or stage_input.document.stage is not stage
    ):
        raise SnapshotSealError("index-vfs input document is not a committed canonical input")
    policy = VfsPolicy.model_validate(stage_input.document.configuration)

    packages: list[IndexedPackage] = []
    with result_path.open("rb") as source:
        for raw in ijson.items(source, "payload.packages.item"):
            packages.append(IndexedPackage.model_validate(raw))
    conflicts: list[VfsIndexedEntry] = []
    with result_path.open("rb") as source:
        for raw in ijson.items(source, "payload.entries.item"):
            if isinstance(raw, dict) and len(raw.get("candidates", ())) > 1:
                conflicts.append(VfsIndexedEntry.model_validate(raw))
    return _VfsSnapshotData(
        policy_name=policy.name,
        policy_version=policy.version,
        policy_sha256=policy.sha256,
        packages=tuple(packages),
        entries=tuple(conflicts),
    )


def create_snapshot_implementation(
    source_tree_policy: SourceTreePolicy | None = None,
    verification_policy: SnapshotVerificationPolicy | None = None,
) -> StageImplementation:
    selected = source_tree_policy or SourceTreePolicy()
    selected_verification = verification_policy or SnapshotVerificationPolicy()

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.SNAPSHOT or context.upstream is None:
            raise SnapshotSealError("snapshot requires a Readable Result")
        readable = context.upstream_as(ReadableResult)
        client_tree = context.committed_as(Stage.ASSEMBLE_CLIENT, ClientTreeResult)
        index = _load_vfs_snapshot_data(context.workspace, context.run_id)
        resolve = context.committed_as(Stage.RESOLVE, ResolveResult)
        acquisition = context.committed_as(Stage.PLAN_ACQUISITION, AcquisitionPlan)
        result = SnapshotAssembler(
            source_tree_policy=selected,
            verification_policy=selected_verification,
        ).build(
            readable,
            client_tree,
            index,
            resolve,
            acquisition,
            context.workspace,
        )
        result = result.model_copy(
            update={"readable_result_sha256": context.require_upstream_digest()}
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Snapshot Result has no Readable Result")
        result = SnapshotResult.model_validate(payload)
        expected_upstream = context.require_upstream_digest()
        if result.readable_result_sha256 != expected_upstream:
            raise ValueError("Snapshot Result is not bound to Readable Result")
        expected_path = f"snapshots/{result.snapshot_id}"
        if result.snapshot_path != expected_path:
            raise ValueError("Snapshot Result path is not canonical")
        descriptor_path = context.workspace.root / result.snapshot_path / "snapshot.json"
        descriptor_digest, _size = _hash_file(descriptor_path)
        if descriptor_digest != result.descriptor_sha256:
            raise ValueError("Snapshot Result descriptor digest does not match")
        try:
            descriptor = ContractRegistry().validate_game_snapshot(
                json.loads(descriptor_path.read_bytes())
            )
        except (ContractValidationError, json.JSONDecodeError) as exc:
            raise ValueError(f"Snapshot Result descriptor is invalid: {exc}") from exc
        if (
            descriptor.snapshot_id != result.snapshot_id
            or descriptor.manifests.files.records != result.file_records
            or descriptor.manifests.actionscript.records != result.actionscript_records
            or descriptor.manifests.stubs.records != result.stub_records
            or descriptor.manifests.packages.records != result.package_records
            or descriptor.manifests.conflicts.records != result.conflict_records
        ):
            raise ValueError("Snapshot Result counters or descriptor digest do not match")

    return StageImplementation(
        implementation_version="snapshot-v4",
        execute=execute,
        validate=validate,
        configuration={
            "contract_version": CONTRACT_VERSION,
            "source_tree": selected.model_dump(mode="json"),
            "verification": selected_verification.model_dump(mode="json"),
        },
    )


__all__ = [
    "CONTRACT_VERSION",
    "Snapshot",
    "SnapshotAssembler",
    "SnapshotSealError",
    "SnapshotVerificationError",
    "SnapshotVerificationPolicy",
    "SnapshotVerifier",
    "SourceTreePolicy",
    "create_snapshot_implementation",
]

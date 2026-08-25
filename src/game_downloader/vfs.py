from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
import unicodedata
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, cast

from defusedxml import ElementTree
from pydantic import ConfigDict, Field

from game_downloader._json import JsonValue, canonical_sha256
from game_downloader.acquisition import ArtifactCorruptError, UnsafeArchiveError
from game_downloader.client_tree import link_or_copy
from game_downloader.delivery import safe_archive_name
from game_downloader.models import (
    ClientTreeFile,
    ClientTreeResult,
    ClientType,
    FrozenModel,
    IndexedPackage,
    Language,
    MaterializationResult,
    MaterializedFile,
    Stage,
    VfsCandidate,
    VfsIndexedEntry,
    VfsIndexResult,
    VfsSourceKind,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation
from game_downloader.workspace import Workspace


class VfsOrderUnknownError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("vfs_order_unknown", message)


class VfsPolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "wot-paths-xml"
    version: str = "1"
    package_precedence: str = "paths.xml-first-listed-wins"
    loose_res_precedence: str = "after-packages"
    unicode_normalization: str = "NFC"
    case_policy: str = "case-insensitive-conflicts"
    max_paths_xml_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_package_entries: int = Field(default=2_000_000, ge=1)
    max_package_unpacked_bytes: int = Field(default=64 * 1024 * 1024 * 1024, ge=1)
    materialize_workers: int = Field(default=6, ge=1, le=32)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def _canonical_entry_path(value: str) -> str:
    safe = safe_archive_name(value.rstrip("/"))
    return unicodedata.normalize("NFC", safe)


def _layer(language: str | None) -> str:
    return f"locale:{language}" if language is not None else "base"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _physical_tree_file(
    workspace: Workspace,
    tree: ClientTreeResult,
    item: ClientTreeFile,
    *,
    verify_digest: bool = True,
) -> Path:
    root = (
        workspace.root / tree.locale_roots[item.language]
        if item.language is not None
        else workspace.root / tree.base_root
    )
    path = root / item.path
    path_stat = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        raise ArtifactCorruptError(f"Client Tree file is not regular: {path}")
    if path_stat.st_size != item.blob_size:
        raise ArtifactCorruptError(f"Client Tree file does not match its manifest: {path}")
    if verify_digest and _hash_file(path) != item.blob_sha256:
        raise ArtifactCorruptError(f"Client Tree file does not match its manifest: {path}")
    return path


class VfsIndexer:
    def __init__(self, policy: VfsPolicy | None = None) -> None:
        self._policy = policy or VfsPolicy()

    def index(
        self,
        tree: ClientTreeResult,
        workspace: Workspace,
        client_type: ClientType,
        *,
        verify_file_digests: bool = True,
    ) -> VfsIndexResult:
        paths_item = next(
            (item for item in tree.files if item.language is None and item.path == "paths.xml"),
            None,
        )
        if paths_item is None:
            raise VfsOrderUnknownError("Client Tree has no base paths.xml")
        paths_file = _physical_tree_file(
            workspace,
            tree,
            paths_item,
            verify_digest=verify_file_digests,
        )
        if paths_file.stat().st_size > self._policy.max_paths_xml_bytes:
            raise VfsOrderUnknownError("paths.xml exceeds the configured size limit")
        package_order = self._parse_package_order(paths_file, client_type)
        precedence_by_path = {path: index for index, path in enumerate(package_order)}

        packages: list[IndexedPackage] = []
        candidates: dict[tuple[str, str], list[VfsCandidate]] = defaultdict(list)
        physical_packages = [item for item in tree.files if item.path.lower().endswith(".pkg")]
        for item in physical_packages:
            precedence = precedence_by_path.get(item.path)
            if precedence is None:
                raise VfsOrderUnknownError(
                    f"Game Package {item.path!r} is not ordered by paths.xml"
                )
            package_path = _physical_tree_file(
                workspace,
                tree,
                item,
                verify_digest=verify_file_digests,
            )
            indexed, package_candidates = self._index_package(package_path, item, precedence)
            packages.append(indexed)
            for candidate in package_candidates:
                candidates[
                    (_layer(candidate.language), candidate.canonical_path.casefold())
                ].append(candidate)

        loose_precedence = len(package_order)
        for item in tree.files:
            if item.path.lower().endswith(".pkg") or not item.path.startswith("res/"):
                continue
            relative = _canonical_entry_path(item.path.removeprefix("res/"))
            candidate = VfsCandidate(
                source_kind=VfsSourceKind.LOOSE_FILE,
                canonical_path=relative,
                original_path=item.path.removeprefix("res/"),
                part=item.part,
                language=item.language,
                part_version=item.part_version,
                source_path=item.path,
                source_sha256=item.blob_sha256,
                precedence=loose_precedence,
                uncompressed_size=item.blob_size,
            )
            candidates[(_layer(item.language), relative.casefold())].append(candidate)

        entries = tuple(
            self._resolve_candidates(layer, lookup_key, values)
            for (layer, lookup_key), values in sorted(
                candidates.items(), key=lambda pair: (pair[0][0], pair[0][1].encode("utf-8"))
            )
        )
        return VfsIndexResult(
            client_tree_result_sha256="sha256:" + "0" * 64,
            policy_name=self._policy.name,
            policy_version=self._policy.version,
            policy_sha256=self._policy.sha256,
            locale_languages=tuple(sorted(tree.locale_roots)),
            packages=tuple(
                sorted(packages, key=lambda item: (item.language or "", item.precedence))
            ),
            entries=entries,
        )

    @staticmethod
    def _parse_package_order(path: Path, client_type: ClientType) -> tuple[str, ...]:
        try:
            root = ElementTree.fromstring(path.read_bytes())
        except Exception as exc:
            raise VfsOrderUnknownError(f"paths.xml is invalid: {exc}") from exc
        packages_element = root.find("./Paths/Packages")
        if packages_element is None:
            raise VfsOrderUnknownError("paths.xml has no Paths/Packages order")
        result: list[str] = []
        for element in packages_element.findall("Package"):
            if element.text is None or not element.text.strip():
                raise VfsOrderUnknownError("paths.xml contains an empty Package entry")
            allowed = {
                value.strip().lower()
                for value in element.attrib.get("type", "").split(",")
                if value.strip()
            }
            if allowed and client_type.value not in allowed:
                continue
            raw = element.text.strip().removeprefix("./")
            package_path = safe_archive_name(raw)
            if not package_path.startswith("res/packages/") or not package_path.endswith(".pkg"):
                raise VfsOrderUnknownError(
                    f"paths.xml Package has unsupported location {package_path!r}"
                )
            if package_path in result:
                raise VfsOrderUnknownError("paths.xml contains duplicate Package entries")
            result.append(package_path)
        if not result:
            raise VfsOrderUnknownError("paths.xml has no packages for selected client type")
        return tuple(result)

    def _index_package(
        self,
        path: Path,
        item: ClientTreeFile,
        precedence: int,
    ) -> tuple[IndexedPackage, tuple[VfsCandidate, ...]]:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > self._policy.max_package_entries:
                    raise UnsafeArchiveError("Game Package entry count exceeds policy")
                total = 0
                result: list[VfsCandidate] = []
                for index, info in enumerate(infos):
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0xFFFF
                    kind = stat.S_IFMT(mode)
                    if kind not in {0, stat.S_IFREG}:
                        raise UnsafeArchiveError("Game Package contains a link or special entry")
                    original = safe_archive_name(info.filename)
                    canonical = _canonical_entry_path(original)
                    total += info.file_size
                    if total > self._policy.max_package_unpacked_bytes:
                        raise UnsafeArchiveError("Game Package expanded size exceeds policy")
                    result.append(
                        VfsCandidate(
                            source_kind=VfsSourceKind.GAME_PACKAGE,
                            canonical_path=canonical,
                            original_path=original,
                            part=item.part,
                            language=item.language,
                            part_version=item.part_version,
                            source_path=item.path,
                            source_sha256=item.blob_sha256,
                            precedence=precedence,
                            zip_entry_index=index,
                            compressed_size=info.compress_size,
                            uncompressed_size=info.file_size,
                            crc32=f"{info.CRC:08X}",
                        )
                    )
                return (
                    IndexedPackage(
                        path=item.path,
                        blob_sha256=item.blob_sha256,
                        blob_size=item.blob_size,
                        blob_path=item.blob_path,
                        part=item.part,
                        language=item.language,
                        part_version=item.part_version,
                        precedence=precedence,
                        entries=len(result),
                    ),
                    tuple(result),
                )
        except UnsafeArchiveError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise ArtifactCorruptError(f"cannot index Game Package {item.path!r}: {exc}") from exc

    @staticmethod
    def _resolve_candidates(
        layer: str,
        lookup_key: str,
        values: Sequence[VfsCandidate],
    ) -> VfsIndexedEntry:
        ordered = tuple(
            sorted(
                values,
                key=lambda item: (
                    item.precedence,
                    item.source_path.encode("utf-8"),
                    item.zip_entry_index if item.zip_entry_index is not None else -1,
                ),
            )
        )
        best_precedence = ordered[0].precedence
        winners = [item for item in ordered if item.precedence == best_precedence]
        if len(winners) != 1:
            raise VfsOrderUnknownError(
                f"VFS path {lookup_key!r} has ambiguous candidates at precedence {best_precedence}"
            )
        if len(ordered) == 1:
            rule = "only-candidate"
        elif winners[0].source_kind is VfsSourceKind.GAME_PACKAGE:
            rule = "paths.xml:first-listed-package-wins"
        else:
            rule = "paths.xml:loose-res-fallback"
        return VfsIndexedEntry(
            lookup_key=lookup_key,
            layer=layer,
            candidates=ordered,
            winner=winners[0],
            resolution_rule=rule,
        )


class VfsMaterializer:
    def __init__(self, policy: VfsPolicy | None = None) -> None:
        self._policy = policy or VfsPolicy()

    def materialize(
        self,
        index: VfsIndexResult,
        workspace: Workspace,
        work_directory: Path,
        *,
        locale_languages: Sequence[Language],
        progress: Callable[[str], None] | None = None,
    ) -> MaterializationResult:
        report_progress = progress or (lambda _message: None)
        partial_root = work_directory / "materialized.partial"
        final_root = work_directory / "materialized"
        if partial_root.exists():
            shutil.rmtree(partial_root)
        if final_root.exists():
            shutil.rmtree(final_root)
        (partial_root / "base").mkdir(parents=True)
        indexed_languages = {
            entry.winner.language for entry in index.entries if entry.winner.language is not None
        }
        selected_languages = set(locale_languages)
        if not indexed_languages.issubset(selected_languages):
            raise ArtifactCorruptError(
                "requested locale roots omit a language present in the VFS Index"
            )
        for language in sorted(selected_languages):
            (partial_root / "locales" / language).mkdir(parents=True)

        loose: list[VfsCandidate] = []
        package_groups: dict[tuple[str | None, str, str], list[VfsCandidate]] = defaultdict(list)
        for entry in index.entries:
            winner = entry.winner
            if winner.source_kind is VfsSourceKind.LOOSE_FILE:
                loose.append(winner)
            else:
                package_groups[(winner.language, winner.source_path, winner.source_sha256)].append(
                    winner
                )

        materialized: list[MaterializedFile] = []
        phase_started = time.monotonic()
        for candidate in loose:
            source = workspace.blobs.path_for(candidate.source_sha256)
            destination = self._destination(partial_root, candidate)
            link_or_copy(source, destination)
            materialized.append(
                MaterializedFile(
                    path=candidate.canonical_path,
                    language=candidate.language,
                    size=candidate.uncompressed_size,
                    sha256=candidate.source_sha256,
                    source=candidate,
                )
            )
        report_progress(
            f"materialized {len(loose)} loose files in {time.monotonic() - phase_started:.1f}s"
        )

        groups = tuple(
            (key, tuple(sorted(values, key=lambda item: item.zip_entry_index or 0)))
            for key, values in sorted(
                package_groups.items(),
                key=lambda item: (
                    item[0][0] is not None,
                    item[0][0] or "",
                    item[0][1].encode("utf-8"),
                ),
            )
        )
        phase_started = time.monotonic()
        if self._policy.materialize_workers == 1:
            package_results = [
                self._materialize_package(key, values, workspace, partial_root)
                for key, values in groups
            ]
        else:
            with ThreadPoolExecutor(
                max_workers=self._policy.materialize_workers,
                thread_name_prefix="vfs-package",
            ) as executor:
                package_results = list(
                    executor.map(
                        lambda group: self._materialize_package(
                            group[0], group[1], workspace, partial_root
                        ),
                        groups,
                    )
                )
        for values in package_results:
            materialized.extend(values)
        report_progress(
            f"materialized {sum(len(values) for values in package_results)} files from "
            f"{len(groups)} packages in {time.monotonic() - phase_started:.1f}s"
        )

        os.replace(partial_root, final_root)
        phase_started = time.monotonic()
        os.sync()
        workspace.fsync_directory(work_directory)
        report_progress(
            f"durably synced materialized VFS in {time.monotonic() - phase_started:.1f}s"
        )
        return MaterializationResult(
            vfs_index_result_sha256="sha256:" + "0" * 64,
            base_root=(final_root / "base").relative_to(workspace.root).as_posix(),
            locale_roots={
                language: (final_root / "locales" / language).relative_to(workspace.root).as_posix()
                for language in sorted(selected_languages)
            },
            files=tuple(
                sorted(
                    materialized,
                    key=lambda item: (
                        item.language is not None,
                        item.language or "",
                        item.path.encode("utf-8"),
                    ),
                )
            ),
        )

    def _materialize_package(
        self,
        key: tuple[str | None, str, str],
        candidates: Sequence[VfsCandidate],
        workspace: Workspace,
        root: Path,
    ) -> list[MaterializedFile]:
        _language, _source_path, source_sha256 = key
        package_path = workspace.blobs.path_for(source_sha256)
        result: list[MaterializedFile] = []
        try:
            with zipfile.ZipFile(package_path) as archive:
                infos = archive.infolist()
                for candidate in candidates:
                    assert candidate.zip_entry_index is not None
                    info = infos[candidate.zip_entry_index]
                    if (
                        _canonical_entry_path(info.filename) != candidate.canonical_path
                        or info.file_size != candidate.uncompressed_size
                        or f"{info.CRC:08X}" != candidate.crc32
                    ):
                        raise ArtifactCorruptError(
                            "Game Package central directory changed after VFS indexing"
                        )
                    destination = self._destination(root, candidate)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temporary_name = tempfile.mkstemp(
                        dir=destination.parent,
                        prefix=f".{destination.name}.",
                        suffix=".tmp",
                    )
                    temporary = Path(temporary_name)
                    digest = hashlib.sha256()
                    crc = 0
                    size = 0
                    try:
                        with (
                            archive.open(info) as source,
                            os.fdopen(descriptor, "wb") as output,
                        ):
                            size, crc = self._copy_entry(
                                cast(BinaryIO, source),
                                cast(BinaryIO, output),
                                digest,
                            )
                            os.fchmod(output.fileno(), 0o444)
                        if size != candidate.uncompressed_size or f"{crc:08X}" != candidate.crc32:
                            raise ArtifactCorruptError(
                                "Game Package entry failed size/CRC verification"
                            )
                        os.replace(temporary, destination)
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        with suppress(OSError):
                            os.close(descriptor)
                        raise
                    result.append(
                        MaterializedFile(
                            path=candidate.canonical_path,
                            language=candidate.language,
                            size=size,
                            sha256=digest.hexdigest(),
                            source=candidate,
                        )
                    )
        except ArtifactCorruptError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise ArtifactCorruptError(
                f"failed to materialize Game Package {_source_path!r}: {exc}"
            ) from exc
        return result

    @staticmethod
    def _copy_entry(
        source: BinaryIO,
        destination: BinaryIO,
        digest: hashlib._Hash,
    ) -> tuple[int, int]:
        size = 0
        crc = 0
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
        return size, crc & 0xFFFFFFFF

    @staticmethod
    def _destination(root: Path, candidate: VfsCandidate) -> Path:
        layer = (
            root / "locales" / candidate.language
            if candidate.language is not None
            else root / "base"
        )
        return layer / candidate.canonical_path


def _validate_materialized_structure(
    result: MaterializationResult,
    workspace: Workspace,
) -> None:
    roots: dict[str | None, Path] = {None: workspace.root / result.base_root}
    roots.update(
        {language: workspace.root / path for language, path in result.locale_roots.items()}
    )
    expected = {(item.language, item.path): item for item in result.files}
    actual: set[tuple[str | None, str]] = set()
    for language, root in roots.items():
        if root.is_symlink() or not root.is_dir():
            raise ValueError("materialized VFS layer root is invalid")
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink() for name in directory_names):
                raise ValueError("materialized VFS contains a symlink directory")
            for name in file_names:
                path = current_path / name
                key = (language, path.relative_to(root).as_posix())
                item = expected.get(key)
                if item is None:
                    raise ValueError("materialized VFS contains an unmanifested file")
                path_stat = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                    raise ValueError("materialized VFS contains a non-regular file")
                if stat.S_IMODE(path_stat.st_mode) & 0o222:
                    raise ValueError("materialized VFS file is writable")
                if path_stat.st_size != item.size:
                    raise ValueError("materialized VFS file size does not match manifest")
                actual.add(key)
    if actual != set(expected):
        raise ValueError("materialized VFS manifest references missing files")


def _audit_materialized(result: MaterializationResult, workspace: Workspace) -> None:
    _validate_materialized_structure(result, workspace)
    roots: dict[str | None, Path] = {None: workspace.root / result.base_root}
    roots.update(
        {language: workspace.root / path for language, path in result.locale_roots.items()}
    )
    for item in result.files:
        if _hash_file(roots[item.language] / item.path) != item.sha256:
            raise ValueError("materialized VFS file content does not match manifest")


def create_index_vfs_implementation(policy: VfsPolicy | None = None) -> StageImplementation:
    selected = policy or VfsPolicy()

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.INDEX_VFS or context.upstream is None:
            raise VfsOrderUnknownError("index-vfs requires a Client Tree Result")
        tree = context.upstream_as(ClientTreeResult)
        result = VfsIndexer(selected).index(
            tree,
            context.workspace,
            context.request.client_type,
            verify_file_digests=False,
        )
        result = result.model_copy(
            update={"client_tree_result_sha256": context.require_upstream_digest()}
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("VFS Index Result has no Client Tree Result")
        result = VfsIndexResult.model_validate(payload)
        if result.client_tree_result_sha256 != context.require_upstream_digest():
            raise ValueError("VFS Index Result is not bound to Client Tree Result")
        if (
            result.policy_name != selected.name
            or result.policy_version != selected.version
            or result.policy_sha256 != selected.sha256
        ):
            raise ValueError("VFS Index Result policy does not match implementation")
        known_packages = {(item.language, item.path, item.blob_sha256) for item in result.packages}
        for entry in result.entries:
            for candidate in entry.candidates:
                if (
                    candidate.source_kind is VfsSourceKind.GAME_PACKAGE
                    and (
                        candidate.language,
                        candidate.source_path,
                        candidate.source_sha256,
                    )
                    not in known_packages
                ):
                    raise ValueError("VFS candidate references an unknown Game Package")

    def audit(context: StageContext, _payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("VFS Index Result has no Client Tree Result")
        tree = context.upstream_as(ClientTreeResult)
        for item in tree.files:
            if item.path == "paths.xml" or item.path.lower().endswith(".pkg"):
                _physical_tree_file(context.workspace, tree, item, verify_digest=True)

    return StageImplementation(
        implementation_version="vfs-index-v3",
        execute=execute,
        validate=validate,
        audit=audit,
        configuration=cast(Mapping[str, JsonValue], selected.model_dump(mode="json")),
    )


def create_materialize_vfs_implementation(
    policy: VfsPolicy | None = None,
) -> StageImplementation:
    selected = policy or VfsPolicy()

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.MATERIALIZE_VFS or context.upstream is None:
            raise ArtifactCorruptError("materialize-vfs requires a VFS Index Result")
        index = context.upstream_as(VfsIndexResult)
        result = VfsMaterializer(selected).materialize(
            index,
            context.workspace,
            context.work_directory,
            locale_languages=index.locale_languages,
            progress=context.progress,
        )
        result = result.model_copy(
            update={"vfs_index_result_sha256": context.require_upstream_digest()}
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Materialization Result has no VFS Index Result")
        result = MaterializationResult.model_validate(payload)
        index = context.upstream_as(VfsIndexResult)
        if result.vfs_index_result_sha256 != context.require_upstream_digest():
            raise ValueError("Materialization Result is not bound to VFS Index Result")
        expected_base = (
            (context.work_directory / "materialized" / "base")
            .relative_to(context.workspace.root)
            .as_posix()
        )
        if result.base_root != expected_base:
            raise ValueError("materialized VFS base root is not canonical for this Stage")
        if set(result.locale_roots) != set(index.locale_languages):
            raise ValueError("materialized VFS locale roots do not match the VFS Index")
        expected = {
            (entry.winner.language, entry.winner.canonical_path): entry.winner
            for entry in index.entries
        }
        actual = {(item.language, item.path): item.source for item in result.files}
        if actual != expected:
            raise ValueError("Materialization Result does not cover exactly the VFS winners")
        roots = (result.base_root, *result.locale_roots.values())
        for relative_root in roots:
            root = context.workspace.root / relative_root
            if root.is_symlink() or not root.is_dir():
                raise ValueError("materialized VFS layer root is invalid")

    def audit(context: StageContext, payload: dict[str, JsonValue]) -> None:
        _audit_materialized(MaterializationResult.model_validate(payload), context.workspace)

    return StageImplementation(
        implementation_version="vfs-materialize-v5",
        execute=execute,
        validate=validate,
        audit=audit,
        configuration=cast(Mapping[str, JsonValue], selected.model_dump(mode="json")),
    )


__all__ = [
    "VfsIndexer",
    "VfsMaterializer",
    "VfsOrderUnknownError",
    "VfsPolicy",
    "create_index_vfs_implementation",
    "create_materialize_vfs_implementation",
]

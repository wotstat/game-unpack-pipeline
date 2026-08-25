from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import quote_from_bytes

from defusedxml import ElementTree
from pydantic import ConfigDict, Field

from game_downloader._json import JsonValue
from game_downloader.acquisition import ArtifactCorruptError, UnsafeArchiveError
from game_downloader.delivery import (
    safe_archive_name,
    validate_downloaded_artifact_structure,
)
from game_downloader.models import (
    ArtifactVerification,
    BytesPath,
    ClientTreeFile,
    ClientTreeResult,
    ContainerKind,
    FrozenModel,
    PartName,
    SplitAssembly,
    Stage,
    VerificationResult,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation
from game_downloader.torrent import decode_bytes_path
from game_downloader.workspace import (
    BlobValidationError,
    CasCorruptionError,
    Workspace,
)

_SAFE_DISK_BYTES = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
_DIFF_SUFFIXES = (".rdiff", ".xdiff", ".wdsfc")


class UnsupportedInstallBundleError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("unsupported_install_bundle", message)


class AssemblyPolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_entries: int = Field(default=1_000_000, ge=1)
    max_expanded_bytes: int = Field(default=32 * 1024 * 1024 * 1024, ge=1)
    archive_timeout_seconds: int = Field(default=4 * 60 * 60, ge=10)
    archive_executables: tuple[str, ...] = ("7zz", "7z", "bsdtar")


def bytes_path_to_disk(path: BytesPath) -> str:
    components = decode_bytes_path(path)
    return "/".join(quote_from_bytes(component, safe=_SAFE_DISK_BYTES) for component in components)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_regular(path: Path) -> os.stat_result:
    path_stat = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        raise UnsafeArchiveError(f"Client Tree source is not a regular file: {path}")
    return path_stat


def link_or_copy(source: Path, destination: Path) -> str:
    _ensure_regular(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    method = "hardlink"
    try:
        try:
            os.link(source, temporary, follow_symlinks=False)
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
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return method


class ClientTreeAssembler:
    def __init__(self, policy: AssemblyPolicy | None = None) -> None:
        self._policy = policy or AssemblyPolicy()

    def assemble(
        self,
        verified: VerificationResult,
        workspace: Workspace,
        work_directory: Path,
    ) -> ClientTreeResult:
        partial_root = work_directory / "client-tree.partial"
        final_root = work_directory / "client-tree"
        if partial_root.exists():
            shutil.rmtree(partial_root)
        if final_root.exists():
            shutil.rmtree(final_root)
        base_root = partial_root / "base"
        locale_root = partial_root / "locales"
        base_root.mkdir(parents=True)
        locale_root.mkdir(parents=True)

        locale_languages = sorted(
            {
                item.download.artifact.language
                for item in verified.artifacts
                if item.download.artifact.part is PartName.LOCALE
                and item.download.artifact.language is not None
            }
        )
        for language in locale_languages:
            (locale_root / language).mkdir(parents=True)

        assembly_by_group = {item.group_id: item for item in verified.split_assemblies}
        handled_groups: set[str] = set()
        files: list[ClientTreeFile] = []
        occupied: dict[tuple[str, str | None], dict[str, str]] = {}

        for item in verified.artifacts:
            artifact = item.download.artifact
            if artifact.split_segment is not None:
                group_id = artifact.split_segment.group_id
                if group_id in handled_groups:
                    continue
                assembly = assembly_by_group.get(group_id)
                if assembly is None:
                    raise ArtifactCorruptError("verified split segment has no assembled bundle")
                group_items = [
                    candidate
                    for candidate in verified.artifacts
                    if candidate.download.artifact.split_segment is not None
                    and candidate.download.artifact.split_segment.group_id == group_id
                ]
                self._validate_install_group(group_items)
                files.extend(
                    self._install_bundle(
                        group_items[0],
                        assembly,
                        workspace,
                        partial_root,
                        occupied,
                    )
                )
                handled_groups.add(group_id)
                continue
            if artifact.acquisition_mode.value == "reference":
                files.append(self._install_reference(item, workspace, partial_root, occupied))
            else:
                self._validate_zero_state(item)
                files.extend(self._install_bundle(item, None, workspace, partial_root, occupied))

        if not files:
            raise ArtifactCorruptError("Client Tree assembly produced no files")
        os.replace(partial_root, final_root)
        workspace.fsync_directory(work_directory)
        locale_roots = {
            language: (final_root / "locales" / language).relative_to(workspace.root).as_posix()
            for language in locale_languages
        }
        return ClientTreeResult(
            verification_result_sha256="sha256:" + "0" * 64,
            base_root=(final_root / "base").relative_to(workspace.root).as_posix(),
            locale_roots=locale_roots,
            files=tuple(
                sorted(
                    files,
                    key=lambda item: (
                        item.language is not None,
                        item.language or "",
                        item.path.encode("utf-8"),
                    ),
                )
            ),
        )

    @staticmethod
    def _validate_install_group(items: Sequence[ArtifactVerification]) -> None:
        if not items:
            raise ArtifactCorruptError("split bundle group is empty")
        signatures = {
            (
                item.download.artifact.part,
                item.download.artifact.language,
                item.download.artifact.part_version,
                item.download.artifact.transition_from,
                item.download.artifact.transition_to,
            )
            for item in items
        }
        if len(signatures) != 1:
            raise ArtifactCorruptError("split bundle segments have inconsistent provenance")
        ClientTreeAssembler._validate_zero_state(items[0])

    @staticmethod
    def _validate_zero_state(item: ArtifactVerification) -> None:
        artifact = item.download.artifact
        if artifact.transition_from not in {None, "0"}:
            raise UnsupportedInstallBundleError(
                "v1 cannot apply a Delivery Bundle that requires a non-zero prior state"
            )

    def _install_reference(
        self,
        item: ArtifactVerification,
        workspace: Workspace,
        tree_root: Path,
        occupied: dict[tuple[str, str | None], dict[str, str]],
    ) -> ClientTreeFile:
        validate_downloaded_artifact_structure(workspace, item.download)
        artifact = item.download.artifact
        relative = bytes_path_to_disk(artifact.path)
        layer_root = self._layer_root(tree_root, artifact.part, artifact.language)
        self._claim_path(relative, artifact.language, occupied)
        source = workspace.blobs.path_for(item.download.blob_sha256)
        method = link_or_copy(source, layer_root / relative)
        return ClientTreeFile(
            path=relative,
            part=artifact.part,
            language=artifact.language,
            part_version=artifact.part_version,
            source_artifact_id=artifact.artifact_id,
            source_blob_sha256=item.download.blob_sha256,
            blob_sha256=item.download.blob_sha256,
            blob_size=item.download.blob_size,
            blob_path=item.download.blob_path,
            link_method=method,
        )

    def _install_bundle(
        self,
        item: ArtifactVerification,
        assembly: SplitAssembly | None,
        workspace: Workspace,
        tree_root: Path,
        occupied: dict[tuple[str, str | None], dict[str, str]],
    ) -> list[ClientTreeFile]:
        artifact = item.download.artifact
        if assembly is None:
            source_blob_sha256 = item.download.blob_sha256
            source_blob_path = workspace.blobs.path_for(source_blob_sha256)
            container = item.container
            source_artifact_id = artifact.artifact_id
        else:
            source_blob_sha256 = assembly.blob_sha256
            source_blob_path = workspace.blobs.path_for(source_blob_sha256)
            container = assembly.container
            source_artifact_id = assembly.group_id
        extraction_parent = tree_root.parent / "delivery-extract"
        extraction_parent.mkdir(parents=True, exist_ok=True)
        extraction_root = Path(tempfile.mkdtemp(prefix="bundle-", dir=extraction_parent))
        try:
            if container is ContainerKind.ZIP:
                self._extract_zip(source_blob_path, extraction_root)
            elif container is ContainerKind.SEVEN_ZIP:
                self._extract_native(source_blob_path, extraction_root)
            else:
                raise ArtifactCorruptError("Delivery Bundle is not a verified archive")
            extracted = self._audit_extraction(extraction_root)
            deletion_only_noop = self._process_service(extraction_root, extracted)
            extracted = self._audit_extraction(extraction_root)
            layer_root = self._layer_root(tree_root, artifact.part, artifact.language)
            result: list[ClientTreeFile] = []
            for source, relative in extracted:
                self._claim_path(relative, artifact.language, occupied)
                source_size = source.stat().st_size
                source_sha256 = _hash_file(source)
                try:
                    commit = workspace.blobs.adopt_verified_file(
                        source,
                        verified_sha256=source_sha256,
                        expected_size=source_size,
                    )
                except (BlobValidationError, CasCorruptionError) as exc:
                    raise ArtifactCorruptError(str(exc)) from exc
                method = link_or_copy(commit.path, layer_root / relative)
                result.append(
                    ClientTreeFile(
                        path=relative,
                        part=artifact.part,
                        language=artifact.language,
                        part_version=artifact.part_version,
                        source_artifact_id=source_artifact_id,
                        source_blob_sha256=source_blob_sha256,
                        source_entry_path=relative,
                        blob_sha256=commit.sha256,
                        blob_size=commit.size,
                        blob_path=commit.relative_path,
                        link_method=method,
                    )
                )
            if not result and not deletion_only_noop:
                raise ArtifactCorruptError("Delivery Bundle contains no installable files")
            return result
        finally:
            shutil.rmtree(extraction_root, ignore_errors=True)

    def _extract_zip(self, archive_path: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > self._policy.max_entries:
                    raise UnsafeArchiveError("Delivery Bundle entry count exceeds policy")
                seen: set[str] = set()
                total = 0
                for info in infos:
                    raw_name = info.filename.rstrip("/")
                    if not raw_name:
                        continue
                    relative = safe_archive_name(raw_name)
                    folded = relative.casefold()
                    if folded in seen:
                        raise UnsafeArchiveError("Delivery Bundle contains duplicate/case paths")
                    seen.add(folded)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    kind = stat.S_IFMT(mode)
                    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise UnsafeArchiveError("Delivery Bundle contains a link or special entry")
                    if info.is_dir():
                        (destination / relative).mkdir(parents=True, exist_ok=True)
                        continue
                    total += info.file_size
                    if total > self._policy.max_expanded_bytes:
                        raise UnsafeArchiveError("Delivery Bundle expanded size exceeds policy")
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
            raise ArtifactCorruptError(f"failed to extract ZIP Delivery Bundle: {exc}") from exc

    def _extract_native(self, archive_path: Path, destination: Path) -> None:
        executable = next(
            (value for value in self._policy.archive_executables if shutil.which(value)),
            None,
        )
        if executable is None:
            raise StageExecutionError(
                "stage_not_implemented", "7z extraction requires 7zz, 7z, or bsdtar"
            )
        if Path(executable).name in {"7z", "7zz"}:
            command = [executable, "x", "-bd", "-y", f"-o{destination}", str(archive_path)]
        else:
            command = [
                executable,
                "-x",
                "--no-same-owner",
                "--no-same-permissions",
                "-f",
                str(archive_path),
                "-C",
                str(destination),
            ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._policy.archive_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactCorruptError(f"Delivery Bundle extraction failed: {exc}") from exc
        if completed.returncode != 0:
            diagnostic = completed.stderr[-2048:].decode("utf-8", errors="replace")
            raise ArtifactCorruptError(f"Delivery Bundle extraction failed: {diagnostic}")

    def _audit_extraction(self, root: Path) -> list[tuple[Path, str]]:
        files: list[tuple[Path, str]] = []
        total = 0
        seen: set[str] = set()
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directory_names:
                directory = current_path / name
                directory_stat = directory.lstat()
                if directory.is_symlink() or not stat.S_ISDIR(directory_stat.st_mode):
                    raise UnsafeArchiveError("Delivery Bundle extracted an unsafe directory")
            for name in file_names:
                path = current_path / name
                path_stat = _ensure_regular(path)
                if path_stat.st_nlink != 1:
                    raise UnsafeArchiveError("Delivery Bundle extracted a hardlink")
                relative = path.relative_to(root).as_posix()
                relative = safe_archive_name(relative)
                folded = relative.casefold()
                if folded in seen:
                    raise UnsafeArchiveError("Delivery Bundle has a case-insensitive collision")
                seen.add(folded)
                total += path_stat.st_size
                if len(files) + 1 > self._policy.max_entries:
                    raise UnsafeArchiveError("Delivery Bundle entry count exceeds policy")
                if total > self._policy.max_expanded_bytes:
                    raise UnsafeArchiveError("Delivery Bundle expanded size exceeds policy")
                files.append((path, relative))
        return sorted(files, key=lambda item: item[1].encode("utf-8"))

    @staticmethod
    def _process_service(root: Path, files: Sequence[tuple[Path, str]]) -> bool:
        service = next(
            (path for path, relative in files if relative == "_service/service.xml"), None
        )
        if service is None:
            if any(relative.lower().endswith(_DIFF_SUFFIXES) for _path, relative in files):
                raise UnsupportedInstallBundleError(
                    "Delivery Bundle contains delta files without zero-state service semantics"
                )
            return False
        if service.stat().st_size > 8 * 1024 * 1024:
            raise UnsafeArchiveError("Delivery Bundle service.xml exceeds the size limit")
        try:
            root_element = ElementTree.fromstring(service.read_bytes())
        except Exception as exc:
            raise ArtifactCorruptError(f"invalid Delivery Bundle service.xml: {exc}") from exc
        patch_info = root_element.find("patch_service_info")
        if patch_info is None:
            raise ArtifactCorruptError("Delivery Bundle service.xml lacks patch_service_info")
        apply_diff = patch_info.find("files_to_apply_diff")
        if apply_diff is not None and list(apply_diff.iter("file")):
            raise UnsupportedInstallBundleError(
                "Delivery Bundle service.xml requires delta application against prior state"
            )
        delete_files = patch_info.find("files_to_delete")
        deletion_targets = 0
        if delete_files is not None:
            for element in list(delete_files):
                if element.tag not in {"file", "directory"}:
                    continue
                if element.text is None or not element.text.strip():
                    raise ArtifactCorruptError(
                        "Delivery Bundle service.xml contains an empty deletion target"
                    )
                safe_archive_name(element.text.strip().replace("\\", "/"))
                deletion_targets += 1

        delete_at_beginning = patch_info.findtext("delete_at_the_beginning", default="")
        service_operations = {element.tag for element in list(patch_info)}
        deletion_only_noop = (
            deletion_targets > 0
            and delete_at_beginning.strip().lower() in {"1", "true"}
            and service_operations <= {"delete_at_the_beginning", "files_to_delete"}
        )
        service_root = root / "_service"
        if service_root.exists():
            shutil.rmtree(service_root)
        return deletion_only_noop

    @staticmethod
    def _layer_root(tree_root: Path, part: PartName, language: str | None) -> Path:
        if part is PartName.LOCALE:
            if language is None:
                raise ArtifactCorruptError("locale Artifact has no language")
            root = tree_root / "locales" / language
        else:
            root = tree_root / "base"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _claim_path(
        relative: str,
        language: str | None,
        occupied: dict[tuple[str, str | None], dict[str, str]],
    ) -> None:
        key = ("locale" if language is not None else "base", language)
        paths = occupied.setdefault(key, {})
        folded = relative.casefold()
        previous = paths.get(folded)
        if previous is not None:
            raise UnsafeArchiveError(
                f"Client Tree path collision between {previous!r} and {relative!r}"
            )
        paths[folded] = relative


def _validate_tree_structure(result: ClientTreeResult, workspace: Workspace) -> None:
    roots: dict[str | None, Path] = {None: workspace.root / result.base_root}
    roots.update(
        {language: workspace.root / path for language, path in result.locale_roots.items()}
    )
    expected: dict[tuple[str | None, str], ClientTreeFile] = {
        (item.language, item.path): item for item in result.files
    }
    actual: set[tuple[str | None, str]] = set()
    for language, root in roots.items():
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"Client Tree layer root is invalid: {root}")
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink() for name in directory_names):
                raise ValueError("Client Tree contains a symlink directory")
            for name in file_names:
                path = current_path / name
                item_key = (language, path.relative_to(root).as_posix())
                item = expected.get(item_key)
                if item is None:
                    raise ValueError(f"Client Tree contains an unmanifested file: {path}")
                path_stat = _ensure_regular(path)
                if stat.S_IMODE(path_stat.st_mode) & 0o222:
                    raise ValueError(f"Client Tree file is writable: {path}")
                if path_stat.st_size != item.blob_size:
                    raise ValueError(f"Client Tree file size does not match manifest: {path}")
                blob = workspace.blobs.path_for(item.blob_sha256)
                expected_blob_path = f"cache/blobs/sha256/{item.blob_sha256[:2]}/{item.blob_sha256}"
                if item.blob_path != expected_blob_path:
                    raise ValueError("Client Tree file references a non-canonical CAS path")
                blob_stat = _ensure_regular(blob)
                if stat.S_IMODE(blob_stat.st_mode) & 0o222 or blob_stat.st_size != item.blob_size:
                    raise ValueError("Client Tree file references an invalid CAS blob")
                if item.link_method == "hardlink":
                    if (path_stat.st_dev, path_stat.st_ino) != (
                        blob_stat.st_dev,
                        blob_stat.st_ino,
                    ):
                        raise ValueError("Client Tree hardlink is not linked to its CAS blob")
                elif _hash_file(path) != item.blob_sha256:
                    # Copy fallback is exceptional (normally every path is on the workspace
                    # filesystem), so verify it immediately rather than deferring its safety.
                    raise ValueError(f"Client Tree copied file does not match manifest: {path}")
                actual.add(item_key)
    if actual != set(expected):
        raise ValueError("Client Tree manifest references missing physical files")


def _audit_tree(result: ClientTreeResult, workspace: Workspace) -> None:
    _validate_tree_structure(result, workspace)
    roots: dict[str | None, Path] = {None: workspace.root / result.base_root}
    roots.update(
        {language: workspace.root / path for language, path in result.locale_roots.items()}
    )
    for item in result.files:
        root = roots[item.language]
        path = root / item.path
        if _hash_file(path) != item.blob_sha256:
            raise ValueError(f"Client Tree file content does not match manifest: {path}")
        blob = workspace.blobs.path_for(item.blob_sha256)
        if _hash_file(blob) != item.blob_sha256:
            raise ValueError("Client Tree file references an invalid CAS blob")


def create_assemble_client_implementation(
    policy: AssemblyPolicy | None = None,
) -> StageImplementation:
    selected = policy or AssemblyPolicy()

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.ASSEMBLE_CLIENT or context.upstream is None:
            raise ArtifactCorruptError("assemble-client requires a Verification Result")
        verified = context.upstream_as(VerificationResult)
        result = ClientTreeAssembler(selected).assemble(
            verified, context.workspace, context.work_directory
        )
        result = result.model_copy(
            update={"verification_result_sha256": context.require_upstream_digest()}
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Client Tree Result has no Verification Result")
        result = ClientTreeResult.model_validate(payload)
        verified = context.upstream_as(VerificationResult)
        expected_digest = context.require_upstream_digest()
        if result.verification_result_sha256 != expected_digest:
            raise ValueError("Client Tree Result is not bound to Verification Result")
        expected_base = (
            (context.work_directory / "client-tree" / "base")
            .relative_to(context.workspace.root)
            .as_posix()
        )
        if result.base_root != expected_base:
            raise ValueError("Client Tree base root is not canonical for this Stage")
        expected_languages = {
            item.download.artifact.language
            for item in verified.artifacts
            if item.download.artifact.part is PartName.LOCALE
            and item.download.artifact.language is not None
        }
        if set(result.locale_roots) != expected_languages:
            raise ValueError("Client Tree locale roots do not match verified locale Artifacts")
        _validate_tree_structure(result, context.workspace)

    def audit(context: StageContext, payload: dict[str, JsonValue]) -> None:
        _audit_tree(ClientTreeResult.model_validate(payload), context.workspace)

    return StageImplementation(
        implementation_version="client-tree-v4",
        execute=execute,
        validate=validate,
        audit=audit,
        configuration=cast(Mapping[str, JsonValue], selected.model_dump(mode="json")),
    )


__all__ = [
    "AssemblyPolicy",
    "ClientTreeAssembler",
    "UnsupportedInstallBundleError",
    "bytes_path_to_disk",
    "create_assemble_client_implementation",
    "link_or_copy",
]

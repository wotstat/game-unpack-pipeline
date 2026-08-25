from __future__ import annotations

import base64
import codecs
import gc
import hashlib
import heapq
import json
import math
import os
import re
import resource
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from importlib.metadata import version as distribution_version
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree
from fissix import pygram, pytree  # type: ignore[import-untyped]
from fissix.pgen2.driver import Driver  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field

from game_downloader._json import (
    JsonValue,
    canonical_sha256,
)
from game_downloader.client_tree import link_or_copy
from game_downloader.engine_stubs import (
    EngineStubError,
    EngineStubGenerator,
    analyze_engine_stubs,
    find_main_binaries,
)
from game_downloader.models import (
    ActionScriptFile,
    ActionScriptResult,
    ClientTreeResult,
    EngineStubsResult,
    FileRepresentation,
    FrozenModel,
    MaterializationResult,
    MaterializedFile,
    ReadableAssemblyResult,
    ReadableFile,
    ReadablePlanEntry,
    ReadablePlanResult,
    ReadableResult,
    ReadableTransformResult,
    RepresentationKind,
    Stage,
    StubFile,
    ToolIdentity,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation
from game_downloader.workspace import Workspace

PACKED_SECTION_MAGIC = b"EN\xa1b"
PACKED_SECTION_VERSION = 0
PYTHON_27_MAGIC = b"\x03\xf3\r\n"

PACKED_XML_TOOL = ToolIdentity(name="game-downloader-packed-section", version="1")
MO_TOOL = ToolIdentity(name="game-downloader-mo-catalogue", version="1")
PYTHON27_SYNTAX_TOOL = ToolIdentity(
    name="fissix",
    version="24.4.24",
    source="https://github.com/amyreese/fissix",
)
ENGINE_STUBS_TOOL = ToolIdentity(name="game-downloader-engine-stubs", version="2")


class TransformFailedError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("transform_failed", message)


class ReadablePolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "wot-readable"
    version: str = "11"
    pyc_tool_name: str = "game-downloader-pyc"
    pyc_tool_version: str = "4+uncompyle6-3.9.3"
    pyc_tool_source: str = "https://github.com/rocky/python-uncompyle6"
    strip_pyc_metadata_headers: bool = True
    pyc_syntax_tool_name: str = PYTHON27_SYNTAX_TOOL.name
    pyc_syntax_tool_version: str = PYTHON27_SYNTAX_TOOL.version
    pyc_syntax_tool_source: str = str(PYTHON27_SYNTAX_TOOL.source)
    accepted_pyc_magics: tuple[str, ...] = (PYTHON_27_MAGIC.hex(),)
    actionscript_tool_name: str = "ffdec"
    actionscript_tool_version: str = "26.2.1"
    actionscript_tool_source: str = "https://github.com/jindrapetrik/jpexs-decompiler"
    transform_workers: int = Field(default=6, ge=1, le=32)
    pyc_batch_size: int = Field(default=32, ge=1, le=256)
    actionscript_workers: int = Field(default=1, ge=1, le=4)
    engine_stub_workers: int = Field(default=4, ge=1, le=16)
    subprocess_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    actionscript_timeout_seconds: int = Field(default=7200, ge=1, le=7200)
    actionscript_method_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    actionscript_heap_megabytes: int = Field(default=2048, ge=512, le=8192)
    max_actionscript_files_per_library: int = Field(default=100_000, ge=1)
    max_actionscript_output_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_actionscript_log_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    max_swc_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_swc_library_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_transform_output_bytes: int = Field(default=128 * 1024 * 1024, ge=1024)
    max_mo_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_packed_xml_bytes: int = Field(default=1024 * 1024 * 1024, ge=1024)
    max_packed_xml_nodes: int = Field(default=2_000_000, ge=1)
    max_packed_xml_depth: int = Field(default=128, ge=1, le=1024)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class PycTransformer(Protocol):
    @property
    def identity(self) -> ToolIdentity: ...

    def transform(
        self,
        source: Path,
        magic: bytes,
        scratch_directory: Path,
    ) -> tuple[bytes, tuple[str, ...]]: ...


@dataclass(frozen=True, slots=True)
class ActionScriptOutput:
    path: str
    data: bytes
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _StagedPycOutput:
    source: Path
    magic: bytes
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _StagedPycOutputs:
    root: Path
    outputs: tuple[_StagedPycOutput, ...]


class ActionScriptTransformer(Protocol):
    @property
    def identity(self) -> ToolIdentity: ...

    def transform(
        self,
        source: Path,
        scratch_directory: Path,
    ) -> tuple[ActionScriptOutput, ...]: ...


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _set_resource_limit(kind: int, maximum: int) -> None:
    soft, hard = resource.getrlimit(kind)
    selected = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
    if soft == resource.RLIM_INFINITY or soft > selected:
        resource.setrlimit(kind, (selected, hard))


def _limit_decompiler(
    maximum_cpu_seconds: int,
    maximum_file_bytes: int = 128 * 1024 * 1024,
) -> None:
    with suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with suppress(OSError, ValueError):
        _set_resource_limit(resource.RLIMIT_CPU, maximum_cpu_seconds)
    with suppress(OSError, ValueError):
        _set_resource_limit(resource.RLIMIT_FSIZE, maximum_file_bytes)
    with suppress(OSError, ValueError):
        _set_resource_limit(resource.RLIMIT_NOFILE, 64)


def _pyc_wall_timeout_seconds(
    cpu_timeout_seconds: int,
    *,
    workers: int,
    logical_cpus: int | None = None,
) -> int:
    available_cpus = logical_cpus if logical_cpus is not None else (os.cpu_count() or 1)
    contention_waves = math.ceil(max(1, workers) / max(1, available_cpus))
    return cpu_timeout_seconds * max(1, contention_waves)


def _pyc_batch_wall_timeout_seconds(
    item_timeout_seconds: int,
    *,
    items: int,
    workers: int,
    logical_cpus: int | None = None,
) -> int:
    return (
        _pyc_wall_timeout_seconds(
            item_timeout_seconds * max(1, items),
            workers=workers,
            logical_cpus=logical_cpus,
        )
        + 30
    )


def _balanced_pyc_batches(
    values: Sequence[tuple[int, Path, bytes]],
    *,
    batch_size: int,
) -> tuple[tuple[tuple[int, Path, bytes], ...], ...]:
    """Place large PYC inputs first while keeping every subprocess batch balanced."""
    if not values:
        return ()
    batch_count = math.ceil(len(values) / batch_size)
    buckets: list[list[tuple[int, Path, bytes]]] = [[] for _ in range(batch_count)]
    loads = [0] * batch_count
    available = [(0, index) for index in range(batch_count)]
    heapq.heapify(available)
    weighted = sorted(
        ((source.stat().st_size, index, (index, source, magic)) for index, source, magic in values),
        key=lambda item: (-item[0], item[1]),
    )
    for size, _index, value in weighted:
        load, bucket_index = heapq.heappop(available)
        buckets[bucket_index].append(value)
        loads[bucket_index] = load + size
        if len(buckets[bucket_index]) < batch_size:
            heapq.heappush(available, (loads[bucket_index], bucket_index))
    ordered = sorted(
        ((loads[index], index, tuple(bucket)) for index, bucket in enumerate(buckets)),
        key=lambda item: (-item[0], item[1]),
    )
    return tuple(batch for _load, _index, batch in ordered)


def _ffdec_cpu_limit_seconds(
    wall_timeout_seconds: int,
    *,
    logical_cpus: int | None = None,
) -> int:
    available_cpus = logical_cpus if logical_cpus is not None else (os.cpu_count() or 1)
    return wall_timeout_seconds * max(1, available_cpus) + 60


def _limit_ffdec(maximum_cpu_seconds: int) -> None:
    with suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with suppress(OSError, ValueError):
        _set_resource_limit(resource.RLIMIT_CPU, maximum_cpu_seconds)
    with suppress(OSError, ValueError):
        _set_resource_limit(resource.RLIMIT_FSIZE, 128 * 1024 * 1024)
    with suppress(OSError, ValueError):
        _set_resource_limit(resource.RLIMIT_NOFILE, 256)


class FfdecTransformer:
    _failure_markers = (
        "decompilation error",
        "not decompiled due to error",
        "outofmemoryerror",
    )

    def __init__(self, policy: ReadablePolicy, executable: Path | None = None) -> None:
        self._policy = policy
        self._configured_executable = executable
        self._resolved_executable: Path | None = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> ToolIdentity:
        self._ensure_healthy()
        return ToolIdentity(
            name=self._policy.actionscript_tool_name,
            version=self._policy.actionscript_tool_version,
            source=self._policy.actionscript_tool_source,
        )

    def _ensure_healthy(self) -> Path:
        with self._lock:
            if self._resolved_executable is not None:
                return self._resolved_executable
            configured = self._configured_executable
            if configured is None:
                configured_value = os.environ.get("GAME_DOWNLOADER_FFDEC")
                discovered = configured_value or shutil.which(self._policy.actionscript_tool_name)
                if discovered is None:
                    raise TransformFailedError(
                        f"{self._policy.actionscript_tool_name} "
                        f"{self._policy.actionscript_tool_version} is not installed"
                    )
                configured = Path(discovered)
            if not configured.is_file():
                raise TransformFailedError(f"ActionScript decompiler does not exist: {configured}")
            try:
                completed = subprocess.run(
                    [str(configured), "-help"],
                    check=False,
                    capture_output=True,
                    timeout=30,
                    env={
                        "HOME": os.environ.get("HOME", "/nonexistent"),
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": os.environ.get("PATH", ""),
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TransformFailedError(f"cannot run ActionScript decompiler: {exc}") from exc
            observed = (completed.stdout + completed.stderr).decode("utf-8", "replace")
            marker = f"JPEXS Free Flash Decompiler v.{self._policy.actionscript_tool_version}"
            if completed.returncode != 0 or marker not in observed:
                raise TransformFailedError(
                    "ActionScript decompiler health check did not report the pinned version "
                    f"{self._policy.actionscript_tool_version}: {observed.strip()[:512]}"
                )
            self._resolved_executable = configured.resolve()
            return self._resolved_executable

    def transform(
        self,
        source: Path,
        scratch_directory: Path,
    ) -> tuple[ActionScriptOutput, ...]:
        executable = self._ensure_healthy()
        work = Path(tempfile.mkdtemp(prefix="ffdec-", dir=scratch_directory))
        export_root = work / "export"
        home = work / "home"
        export_root.mkdir()
        home.mkdir()
        environment = {
            "HOME": str(home),
            "FFDEC_MEMORY": f"{self._policy.actionscript_heap_megabytes}m",
            "JAVA_TOOL_OPTIONS": "-Djava.awt.headless=true",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(work),
        }
        try:
            library, library_sha256 = self._read_swc_library(source)
            library_path = work / "library.swf"
            with library_path.open("xb") as stream:
                stream.write(library)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            try:
                completed = subprocess.run(
                    [
                        str(executable),
                        "-format",
                        "script:as",
                        "-onerror",
                        "abort",
                        "-timeout",
                        str(self._policy.actionscript_method_timeout_seconds),
                        "-export",
                        "script",
                        str(export_root),
                        str(library_path.resolve()),
                    ],
                    check=False,
                    capture_output=True,
                    cwd=work,
                    env=environment,
                    timeout=self._policy.actionscript_timeout_seconds,
                    start_new_session=True,
                    preexec_fn=partial(
                        _limit_ffdec,
                        _ffdec_cpu_limit_seconds(self._policy.actionscript_timeout_seconds),
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                raise TransformFailedError(
                    f"ActionScript decompiler timed out for {source}"
                ) from exc
            except OSError as exc:
                raise TransformFailedError(
                    f"cannot run ActionScript decompiler for {source}: {exc}"
                ) from exc
            log = completed.stdout + completed.stderr
            if len(log) > self._policy.max_actionscript_log_bytes:
                raise TransformFailedError(f"ActionScript decompiler log exceeds policy: {source}")
            if completed.returncode != 0:
                detail = log.decode("utf-8", "replace").strip()
                status = (
                    f"signal {-completed.returncode}"
                    if completed.returncode < 0
                    else f"exit status {completed.returncode}"
                )
                raise TransformFailedError(
                    f"ActionScript decompilation failed for {source} ({status}): "
                    f"{detail[-2048:] or 'no diagnostics'}"
                )
            return self._read_outputs(
                export_root,
                source,
                (
                    "container=swc",
                    "swc-member=library.swf",
                    f"swc-member-sha256={library_sha256}",
                ),
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _read_swc_library(self, source: Path) -> tuple[bytes, str]:
        if source.stat().st_size > self._policy.max_swc_bytes:
            raise TransformFailedError(f"SWC exceeds policy: {source}")
        try:
            with zipfile.ZipFile(source) as archive:
                matches = [
                    item for item in archive.infolist() if item.filename.casefold() == "library.swf"
                ]
                if len(matches) != 1 or matches[0].filename != "library.swf":
                    raise TransformFailedError(
                        f"SWC must contain one canonical library.swf member: {source}"
                    )
                member = matches[0]
                if member.is_dir() or member.flag_bits & 0x1:
                    raise TransformFailedError(f"SWC library.swf member is invalid: {source}")
                if member.file_size > self._policy.max_swc_library_bytes:
                    raise TransformFailedError(f"SWC library.swf exceeds policy: {source}")
                with archive.open(member) as stream:
                    library = stream.read(self._policy.max_swc_library_bytes + 1)
        except TransformFailedError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise TransformFailedError(f"invalid SWC archive {source}: {exc}") from exc
        if len(library) != member.file_size or len(library) > self._policy.max_swc_library_bytes:
            raise TransformFailedError(f"SWC library.swf size is invalid: {source}")
        if library[:3] not in {b"FWS", b"CWS", b"ZWS"}:
            raise TransformFailedError(f"invalid SWF signature in SWC library.swf: {source}")
        return library, hashlib.sha256(library).hexdigest()

    def _read_outputs(
        self,
        root: Path,
        source: Path,
        source_diagnostics: tuple[str, ...] = (),
    ) -> tuple[ActionScriptOutput, ...]:
        files: list[ActionScriptOutput] = []
        total = 0
        seen: set[str] = set()
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink() for name in directory_names):
                raise TransformFailedError(f"ActionScript export contains a symlink: {source}")
            for name in sorted(file_names, key=lambda value: value.encode("utf-8")):
                path = current_path / name
                path_stat = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                    raise TransformFailedError(
                        f"ActionScript export contains a non-regular file: {source}"
                    )
                relative = path.relative_to(root).as_posix()
                pure = PurePosixPath(relative)
                if (
                    pure.as_posix() != relative
                    or "\\" in relative
                    or "\x00" in relative
                    or any(part in {"", ".", ".."} for part in relative.split("/"))
                    or not relative.lower().endswith(".as")
                ):
                    raise TransformFailedError(
                        f"ActionScript export contains an invalid path {relative!r}: {source}"
                    )
                lookup = relative.casefold()
                if lookup in seen:
                    raise TransformFailedError(
                        f"ActionScript export contains a case collision {relative!r}: {source}"
                    )
                seen.add(lookup)
                if len(files) >= self._policy.max_actionscript_files_per_library:
                    raise TransformFailedError(
                        f"ActionScript export file count exceeds policy: {source}"
                    )
                encoded = path.read_bytes()
                try:
                    text = encoded.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise TransformFailedError(
                        f"ActionScript export is not UTF-8 ({relative}): {source}"
                    ) from exc
                normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                lowered = normalized.lower()
                marker = next(
                    (value for value in self._failure_markers if value in lowered),
                    None,
                )
                if marker is not None:
                    raise TransformFailedError(
                        f"ActionScript decompiler emitted {marker!r} in {relative}: {source}"
                    )
                if normalized and not normalized.endswith("\n"):
                    normalized += "\n"
                data = normalized.encode("utf-8")
                total += len(data)
                if total > self._policy.max_actionscript_output_bytes:
                    raise TransformFailedError(f"ActionScript export size exceeds policy: {source}")
                files.append(
                    ActionScriptOutput(
                        path=relative,
                        data=data,
                        diagnostics=(
                            "language=actionscript-3",
                            f"backend=ffdec-{self._policy.actionscript_tool_version}",
                            *source_diagnostics,
                        ),
                    )
                )
        return tuple(sorted(files, key=lambda item: item.path.encode("utf-8")))


class Uncompyle6Transformer:
    _failure_markers = (
        "parse error",
        "unsupported opcode",
        "unsupported python version",
        "decompilation failed",
        "decompyle incomplete",
    )

    def __init__(self, policy: ReadablePolicy, executable: Path | None = None) -> None:
        self._policy = policy
        self._configured_executable = executable
        self._resolved_executable: Path | None = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> ToolIdentity:
        self._ensure_healthy()
        return ToolIdentity(
            name=self._policy.pyc_tool_name,
            version=self._policy.pyc_tool_version,
            source=self._policy.pyc_tool_source,
        )

    def _ensure_healthy(self) -> Path:
        with self._lock:
            if self._resolved_executable is not None:
                return self._resolved_executable
            configured = self._configured_executable
            if configured is None:
                discovered = shutil.which(self._policy.pyc_tool_name)
                if discovered is None:
                    raise TransformFailedError(
                        f"{self._policy.pyc_tool_name} {self._policy.pyc_tool_version} "
                        "is not installed"
                    )
                configured = Path(discovered)
            if not configured.is_file():
                raise TransformFailedError(f"PYC decompiler does not exist: {configured}")
            try:
                completed = subprocess.run(
                    [str(configured), "--version"],
                    check=False,
                    capture_output=True,
                    timeout=15,
                    env={
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONHASHSEED": "0",
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TransformFailedError(f"cannot run PYC decompiler: {exc}") from exc
            observed = (completed.stdout + completed.stderr).decode("utf-8", "replace")
            if completed.returncode != 0 or self._policy.pyc_tool_version not in observed:
                raise TransformFailedError(
                    "PYC decompiler health check did not report the pinned version "
                    f"{self._policy.pyc_tool_version}: {observed.strip()[:512]}"
                )
            self._resolved_executable = configured.resolve()
            return self._resolved_executable

    def transform(
        self,
        source: Path,
        magic: bytes,
        scratch_directory: Path,
    ) -> tuple[bytes, tuple[str, ...]]:
        executable = self._ensure_healthy()
        if magic.hex() not in self._policy.accepted_pyc_magics:
            raise TransformFailedError(f"unsupported PYC magic {magic.hex()} in {source}")
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONHASHSEED": "0",
            "TMPDIR": str(scratch_directory),
        }
        try:
            completed = subprocess.run(
                [str(executable), str(source.resolve())],
                check=False,
                capture_output=True,
                cwd=scratch_directory,
                env=environment,
                timeout=_pyc_wall_timeout_seconds(
                    self._policy.subprocess_timeout_seconds,
                    workers=self._policy.transform_workers,
                ),
                start_new_session=True,
                preexec_fn=partial(
                    _limit_decompiler,
                    self._policy.subprocess_timeout_seconds,
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise TransformFailedError(f"PYC decompiler timed out for {source}") from exc
        except OSError as exc:
            raise TransformFailedError(f"cannot run PYC decompiler for {source}: {exc}") from exc
        if len(completed.stdout) > self._policy.max_transform_output_bytes:
            raise TransformFailedError(f"PYC output exceeds policy for {source}")
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        if completed.returncode != 0 or stderr:
            detail = stderr or f"exit status {completed.returncode}"
            raise TransformFailedError(f"PYC decompilation failed for {source}: {detail[:2048]}")
        return self._decode_output(completed.stdout, source=source, magic=magic)

    def transform_many(
        self,
        sources: Sequence[tuple[Path, bytes]],
        scratch_directory: Path,
    ) -> tuple[tuple[bytes, tuple[str, ...]], ...]:
        if not sources:
            return ()
        staged = self.stage_many(sources, scratch_directory)
        try:
            return tuple(self.decode_staged(output) for output in staged.outputs)
        finally:
            shutil.rmtree(staged.root, ignore_errors=True)

    def stage_many(
        self,
        sources: Sequence[tuple[Path, bytes]],
        scratch_directory: Path,
    ) -> _StagedPycOutputs:
        if not sources:
            raise ValueError("cannot stage an empty PYC input set")
        executable = self._ensure_healthy()
        indexed = tuple((index, source, magic) for index, (source, magic) in enumerate(sources))
        for _index, source, magic in indexed:
            if magic.hex() not in self._policy.accepted_pyc_magics:
                raise TransformFailedError(f"unsupported PYC magic {magic.hex()} in {source}")
        batches = _balanced_pyc_batches(indexed, batch_size=self._policy.pyc_batch_size)
        root = Path(tempfile.mkdtemp(prefix="pyc-many-", dir=scratch_directory))
        transformed: dict[int, _StagedPycOutput] = {}
        run_batch = partial(
            self._stage_batch,
            executable=executable,
            root=root,
        )
        try:
            with ThreadPoolExecutor(
                max_workers=min(self._policy.transform_workers, len(batches)),
                thread_name_prefix="pyc-batch",
            ) as executor:
                for values in executor.map(run_batch, enumerate(batches)):
                    transformed.update(values)
            if set(transformed) != set(range(len(indexed))):
                raise TransformFailedError("PYC batch decompiler returned incomplete output")
            return _StagedPycOutputs(
                root=root,
                outputs=tuple(transformed[index] for index in range(len(indexed))),
            )
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def _stage_batch(
        self,
        indexed_batch: tuple[int, Sequence[tuple[int, Path, bytes]]],
        *,
        executable: Path,
        root: Path,
    ) -> dict[int, _StagedPycOutput]:
        batch_index, batch = indexed_batch
        work = root / f"{batch_index:08d}"
        work.mkdir()
        output_root = work / "outputs"
        output_root.mkdir()
        manifest_path = work / "manifest.json"
        identifiers = {
            index: f"{position:08d}" for position, (index, _path, _magic) in enumerate(batch)
        }
        manifest_path.write_text(
            json.dumps(
                {
                    "items": [
                        {"id": identifiers[index], "source": str(source.resolve())}
                        for index, source, _magic in batch
                    ],
                    "max_output_bytes": self._policy.max_transform_output_bytes,
                    "timeout_seconds": self._policy.subprocess_timeout_seconds,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONHASHSEED": "0",
            "TMPDIR": str(work),
        }
        try:
            try:
                completed = subprocess.run(
                    [str(executable), "--batch", str(manifest_path), str(output_root)],
                    check=False,
                    capture_output=True,
                    cwd=work,
                    env=environment,
                    timeout=_pyc_batch_wall_timeout_seconds(
                        self._policy.subprocess_timeout_seconds,
                        items=len(batch),
                        workers=self._policy.transform_workers,
                    ),
                    start_new_session=True,
                    preexec_fn=partial(
                        _limit_decompiler,
                        self._policy.subprocess_timeout_seconds * len(batch),
                        self._policy.max_transform_output_bytes,
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                raise TransformFailedError(
                    f"PYC batch decompiler timed out for {batch[0][1]}"
                ) from exc
            except OSError as exc:
                raise TransformFailedError(f"cannot run PYC batch decompiler: {exc}") from exc
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            if completed.returncode != 0 or stderr:
                detail = stderr or f"exit status {completed.returncode}"
                raise TransformFailedError(f"PYC batch decompilation failed: {detail[-2048:]}")
            if len(completed.stdout) > 1024 * 1024:
                raise TransformFailedError("PYC batch report exceeds policy")
            try:
                raw_report = json.loads(completed.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TransformFailedError("PYC batch report is malformed") from exc
            if not isinstance(raw_report, dict) or not isinstance(raw_report.get("outputs"), list):
                raise TransformFailedError("PYC batch report is malformed")
            reported: dict[str, tuple[int, str]] = {}
            for raw_output in raw_report["outputs"]:
                if not isinstance(raw_output, dict):
                    raise TransformFailedError("PYC batch report contains an invalid output")
                item_id = raw_output.get("id")
                size = raw_output.get("size")
                sha256 = raw_output.get("sha256")
                if (
                    not isinstance(item_id, str)
                    or item_id in reported
                    or not isinstance(size, int)
                    or not 0 <= size <= self._policy.max_transform_output_bytes
                    or not isinstance(sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                ):
                    raise TransformFailedError("PYC batch report contains invalid metadata")
                reported[item_id] = (size, sha256)
            expected_ids = set(identifiers.values())
            if set(reported) != expected_ids or {path.name for path in output_root.iterdir()} != {
                f"{item_id}.py" for item_id in expected_ids
            }:
                raise TransformFailedError("PYC batch output set does not match its manifest")
            results: dict[int, _StagedPycOutput] = {}
            for index, source, magic in batch:
                item_id = identifiers[index]
                output_path = output_root / f"{item_id}.py"
                output_stat = output_path.lstat()
                if output_path.is_symlink() or not stat.S_ISREG(output_stat.st_mode):
                    raise TransformFailedError("PYC batch emitted a non-regular output")
                expected_size, expected_sha256 = reported[item_id]
                if output_stat.st_size != expected_size:
                    raise TransformFailedError("PYC batch output does not match its report")
                results[index] = _StagedPycOutput(
                    source=source,
                    magic=magic,
                    path=output_path,
                    size=expected_size,
                    sha256=expected_sha256,
                )
            return results
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

    def decode_staged(
        self,
        output: _StagedPycOutput,
    ) -> tuple[bytes, tuple[str, ...]]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(output.path, flags)
        except OSError as exc:
            raise TransformFailedError("cannot open staged PYC output") from exc
        try:
            output_stat = os.fstat(descriptor)
            if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_size != output.size:
                raise TransformFailedError("PYC batch output changed before validation")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(self._policy.max_transform_output_bytes + 1)
        finally:
            os.close(descriptor)
        if len(encoded) != output.size or hashlib.sha256(encoded).hexdigest() != output.sha256:
            raise TransformFailedError("PYC batch output does not match its report")
        return self._decode_output(encoded, source=output.source, magic=output.magic)

    def _decode_output(
        self,
        encoded: bytes,
        *,
        source: Path,
        magic: bytes,
    ) -> tuple[bytes, tuple[str, ...]]:
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransformFailedError(f"PYC output is not UTF-8 for {source}") from exc
        lowered = text.lower()
        marker = next((value for value in self._failure_markers if value in lowered), None)
        if marker is not None:
            raise TransformFailedError(f"PYC decompiler emitted {marker!r} for {source}")
        normalized, removed_module_return, empty_module = self._normalize_output(text, magic)
        diagnostics = [
            "bytecode=python-2.7",
            f"magic={magic.hex()}",
            f"adapter=game-downloader-pyc-{self._policy.pyc_tool_version.split('+', 1)[0]}",
            "backend=uncompyle6-3.9.3",
        ]
        if removed_module_return:
            diagnostics.append("removed-decompiler-module-return")
        if empty_module:
            diagnostics.append("empty-module")
        return normalized.encode("utf-8"), tuple(diagnostics)

    def _normalize_output(self, value: str, magic: bytes) -> tuple[str, bool, bool]:
        lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and (not lines[0].strip() or lines[0].startswith("#")):
            lines.pop(0)
        while lines and (not lines[-1].strip() or lines[-1].startswith("# okay decompiling ")):
            lines.pop()
        removed_module_return = False
        if lines and lines[-1] == "return":
            lines.pop()
            removed_module_return = True
            while lines and not lines[-1].strip():
                lines.pop()
        empty_module = not lines
        if empty_module:
            lines.append("pass")
        body = "\n".join(lines) + "\n"
        if self._policy.strip_pyc_metadata_headers:
            return body, removed_module_return, empty_module
        header = (
            "# -*- coding: utf-8 -*-\n"
            f"# Decompiled by {self._policy.pyc_tool_name} "
            f"{self._policy.pyc_tool_version}\n"
            f"# Python bytecode 2.7; magic={magic.hex()}\n\n"
        )
        return header + body, removed_module_return, empty_module


class Python27SourceValidator:
    _future_print = re.compile(
        r"^from\s+__future__\s+import[^\n]*\bprint_function\b",
        re.MULTILINE,
    )

    def __init__(self, policy: ReadablePolicy) -> None:
        self._policy = policy
        observed = distribution_version(policy.pyc_syntax_tool_name)
        if observed != policy.pyc_syntax_tool_version:
            raise TransformFailedError(
                f"Python 2 syntax validator requires {policy.pyc_syntax_tool_name} "
                f"{policy.pyc_syntax_tool_version}, found {observed}"
            )
        self._local = threading.local()

    def validate(self, output: bytes, source_name: str) -> tuple[str, ...]:
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransformFailedError(f"PYC output is not UTF-8 for {source_name}") from exc
        normal, print_function = self._drivers()
        selected = print_function if self._future_print.search(text) else normal
        try:
            selected.parse_string(text)
        except Exception as exc:
            raise TransformFailedError(
                f"decompiled Python 2.7 source is invalid for {source_name}: {exc}"
            ) from exc
        return (
            "syntax=python-2.7",
            f"syntax-validator={self._policy.pyc_syntax_tool_name}-"
            f"{self._policy.pyc_syntax_tool_version}",
        )

    def _drivers(self) -> tuple[Driver, Driver]:
        existing = getattr(self._local, "drivers", None)
        if existing is None:
            existing = (
                Driver(pygram.python_grammar, convert=pytree.convert),
                Driver(pygram.python_grammar_no_print_statement, convert=pytree.convert),
            )
            self._local.drivers = existing
        return cast(tuple[Driver, Driver], existing)


@dataclass(frozen=True)
class _PackedNode:
    name: str
    value: str
    children: tuple[_PackedNode, ...]


class PackedXmlDecoder:
    _data_pos_mask = 0x0FFFFFFF
    _xml_name = re.compile(r"[A-Za-z_][A-Za-z0-9._-]*\Z")

    def __init__(self, policy: ReadablePolicy) -> None:
        self._policy = policy
        self._node_count = 0
        self._namespace_declarations = 0
        self._duplicate_namespace_declarations = 0

    def decode(self, data: bytes, root_name: str) -> tuple[bytes, tuple[str, ...]]:
        if len(data) > self._policy.max_packed_xml_bytes:
            raise TransformFailedError("packed XML exceeds the configured size limit")
        if len(data) < 6 or data[:4] != PACKED_SECTION_MAGIC:
            raise TransformFailedError("packed XML magic is missing")
        version = data[4]
        if version != PACKED_SECTION_VERSION:
            raise TransformFailedError(f"unsupported packed XML version {version}")
        strings, section_start = self._read_string_table(data, 5)
        self._node_count = 0
        self._namespace_declarations = 0
        self._duplicate_namespace_declarations = 0
        node = self._read_node(
            self._safe_tag(root_name, root=True),
            memoryview(data)[section_start:],
            0,
            strings,
            0,
        )
        rendered = ('<?xml version="1.0" encoding="utf-8"?>\n' + self._render_node(node, 0)).encode(
            "utf-8"
        )
        if len(rendered) > self._policy.max_transform_output_bytes:
            raise TransformFailedError("packed XML output exceeds the configured size limit")
        try:
            ElementTree.fromstring(rendered)
        except Exception as exc:
            raise TransformFailedError(f"packed XML output verification failed: {exc}") from exc
        return rendered, (
            f"packed-section-version={version}",
            f"nodes={self._node_count}",
            f"namespace-declarations={self._namespace_declarations}",
            f"duplicate-namespace-declarations={self._duplicate_namespace_declarations}",
        )

    @staticmethod
    def _read_string_table(data: bytes, start: int) -> tuple[tuple[str, ...], int]:
        values: list[str] = []
        cursor = start
        while True:
            end = data.find(b"\0", cursor)
            if end < 0:
                raise TransformFailedError("packed XML string table is not terminated")
            if end == cursor:
                return tuple(values), cursor + 1
            try:
                value = data[cursor:end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TransformFailedError("packed XML tag is not UTF-8") from exc
            values.append(value)
            if len(values) > 32767:
                raise TransformFailedError("packed XML string table is too large")
            cursor = end + 1

    def _read_node(
        self,
        name: str,
        section: memoryview,
        section_type: int,
        strings: Sequence[str],
        depth: int,
    ) -> _PackedNode:
        self._node_count += 1
        if self._node_count > self._policy.max_packed_xml_nodes:
            raise TransformFailedError("packed XML node count exceeds policy")
        if depth > self._policy.max_packed_xml_depth:
            raise TransformFailedError("packed XML nesting exceeds policy")
        if section_type != 0:
            return _PackedNode(name, self._decode_value(section_type, section), ())
        if len(section) < 6:
            raise TransformFailedError("packed XML section header is truncated")
        child_count = struct.unpack_from("<h", section, 0)[0]
        if child_count < 0:
            raise TransformFailedError("packed XML section has a negative child count")
        header_size = 2 + child_count * 6 + 4
        if header_size > len(section):
            raise TransformFailedError("packed XML child records are truncated")
        positions: list[int] = []
        types: list[int] = []
        keys: list[int] = []
        for index in range(child_count):
            raw_position = struct.unpack_from("<I", section, 2 + index * 6)[0]
            key = struct.unpack_from("<h", section, 2 + index * 6 + 4)[0]
            positions.append(raw_position & self._data_pos_mask)
            types.append(raw_position >> 28)
            keys.append(key)
        final_position = struct.unpack_from("<I", section, 2 + child_count * 6)[0]
        positions.append(final_position & self._data_pos_mask)
        types.append(final_position >> 28)
        data_block = section[header_size:]
        if any(position > len(data_block) for position in positions):
            raise TransformFailedError("packed XML data position is out of bounds")
        if positions != sorted(positions):
            raise TransformFailedError("packed XML data positions are not monotonic")
        own_end = positions[0]
        own_type = types[0]
        own_data = data_block[:own_end]
        value = self._decode_value(own_type, own_data)
        children: list[_PackedNode] = []
        for index in range(child_count):
            key = keys[index]
            if key < 0 or key >= len(strings):
                raise TransformFailedError("packed XML child references an unknown tag")
            child_name = self._safe_tag(strings[key], root=False)
            children.append(
                self._read_node(
                    child_name,
                    data_block[positions[index] : positions[index + 1]],
                    types[index + 1],
                    strings,
                    depth + 1,
                )
            )
        if own_type == 3 and len(own_data) == 48 and not children:
            floats = struct.unpack("<12f", own_data)
            value = ""
            for row in range(4):
                row_value = " ".join(
                    self._format_float(item) for item in floats[row * 3 : row * 3 + 3]
                )
                children.append(_PackedNode(f"row{row}", row_value, ()))
                self._node_count += 1
                if self._node_count > self._policy.max_packed_xml_nodes:
                    raise TransformFailedError("packed XML node count exceeds policy")
        return _PackedNode(name, value, tuple(children))

    def _decode_value(self, section_type: int, data: memoryview) -> str:
        raw = data.tobytes()
        if section_type == 0:
            if raw:
                raise TransformFailedError("nested packed XML section was decoded as a scalar")
            return ""
        if section_type == 1:
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TransformFailedError("packed XML string value is not UTF-8") from exc
            self._validate_xml_text(value)
            return value
        if section_type == 2:
            if len(raw) == 0:
                return "0"
            if len(raw) not in {1, 2, 4, 8}:
                raise TransformFailedError("packed XML integer has an invalid width")
            return str(int.from_bytes(raw, "little", signed=True))
        if section_type == 3:
            if len(raw) % 4 != 0 or len(raw) > 48:
                raise TransformFailedError("packed XML float/vector has an invalid width")
            return " ".join(
                self._format_float(value) for value in struct.unpack(f"<{len(raw) // 4}f", raw)
            )
        if section_type == 4:
            return "true" if raw else "false"
        if section_type in {5, 6}:
            if section_type == 6:
                if not raw or raw[0] not in {0, 1}:
                    raise TransformFailedError("packed XML encrypted blob marker is invalid")
                raw = raw[1:] if raw[0] == 0 else bytes(value ^ 0x9C for value in raw[1:])
            return base64.b64encode(raw).decode("ascii")
        raise TransformFailedError(f"packed XML contains unsupported section type {section_type}")

    @staticmethod
    def _format_float(value: float) -> str:
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "-inf" if value < 0 else "inf"
        return format(value, ".9g")

    @staticmethod
    def _validate_xml_text(value: str) -> None:
        if any(
            ord(character) < 0x20 and character not in {"\t", "\n", "\r"} for character in value
        ):
            raise TransformFailedError("packed XML string contains an XML control character")

    @classmethod
    def _safe_tag(cls, value: str, *, root: bool) -> str:
        candidate = value.replace(" ", "..")
        if candidate and candidate[0].isdigit():
            candidate = "id." + candidate
        if not candidate:
            if root:
                return "root"
            raise TransformFailedError("packed XML contains an empty tag")
        parts = candidate.split(":")
        if len(parts) > 2 or any(cls._xml_name.fullmatch(part) is None for part in parts):
            if root:
                return "root"
            raise TransformFailedError(f"packed XML contains an invalid tag {value!r}") from None
        return candidate

    def _render_node(self, node: _PackedNode, depth: int) -> str:
        indentation = "\t" * depth
        value = escape(node.value)
        namespace_children = tuple(
            child
            for child in node.children
            if child.name == "xmlns" or child.name.startswith("xmlns:")
        )
        ordinary_children = tuple(
            child
            for child in node.children
            if child.name != "xmlns" and not child.name.startswith("xmlns:")
        )
        seen_declarations: dict[str, str] = {}
        attributes: list[str] = []
        for declaration in namespace_children:
            if declaration.children:
                raise TransformFailedError("packed XML namespace declaration has children")
            previous = seen_declarations.get(declaration.name)
            if previous is not None:
                if previous != declaration.value:
                    raise TransformFailedError(
                        "packed XML repeats a namespace prefix with a different URI"
                    )
                self._duplicate_namespace_declarations += 1
                continue
            seen_declarations[declaration.name] = declaration.value
            attributes.append(f" {declaration.name}={quoteattr(declaration.value)}")
            self._namespace_declarations += 1
        opening = f"{indentation}<{node.name}{''.join(attributes)}>"
        if not ordinary_children:
            return f"{opening}{value}</{node.name}>\n"
        if value:
            opening += value
        opening += "\n"
        children = "".join(self._render_node(child, depth + 1) for child in ordinary_children)
        return opening + children + f"{indentation}</{node.name}>\n"


@dataclass(frozen=True)
class _MoEntry:
    context: str | None
    msgid: str
    plural: str | None
    translations: tuple[str, ...]


class MoCatalogueConverter:
    _charset_pattern = re.compile(rb"charset\s*=\s*([A-Za-z0-9._-]+)", re.IGNORECASE)

    def __init__(self, policy: ReadablePolicy) -> None:
        self._policy = policy

    def convert(self, data: bytes) -> tuple[bytes, tuple[str, ...]]:
        if len(data) > self._policy.max_mo_bytes:
            raise TransformFailedError("MO catalogue exceeds the configured size limit")
        raw_entries, endian, revision = self._parse_raw(data)
        header = next(
            (translation for original, translation in raw_entries if original == b""), b""
        )
        match = self._charset_pattern.search(header)
        charset = match.group(1).decode("ascii") if match is not None else "UTF-8"
        try:
            codec = codecs.lookup(charset).name
        except LookupError as exc:
            raise TransformFailedError(
                f"MO catalogue declares unknown charset {charset!r}"
            ) from exc
        entries = tuple(
            self._decode_entry(original, translation, codec)
            for original, translation in raw_entries
        )
        rendered = self._render(entries)
        if self._parse_rendered(rendered.decode("utf-8")) != entries:
            raise TransformFailedError("PO catalogue round-trip verification failed")
        return rendered, (
            f"mo-endian={endian}",
            f"mo-revision={revision}",
            f"charset={codec}",
            f"messages={len(entries)}",
        )

    @staticmethod
    def _parse_raw(data: bytes) -> tuple[tuple[tuple[bytes, bytes], ...], str, int]:
        if len(data) < 28:
            raise TransformFailedError("MO catalogue header is truncated")
        magic = data[:4]
        if magic == b"\xde\x12\x04\x95":
            prefix = "<"
            endian = "little"
        elif magic == b"\x95\x04\x12\xde":
            prefix = ">"
            endian = "big"
        else:
            raise TransformFailedError("MO catalogue magic is invalid")
        revision, count, original_offset, translation_offset = struct.unpack_from(
            f"{prefix}4I", data, 4
        )
        if revision >> 16 > 1:
            raise TransformFailedError(f"unsupported MO major revision {revision >> 16}")
        if count > 10_000_000:
            raise TransformFailedError("MO message count exceeds policy")
        table_size = count * 8
        if original_offset + table_size > len(data) or translation_offset + table_size > len(data):
            raise TransformFailedError("MO string table is out of bounds")
        entries: list[tuple[bytes, bytes]] = []
        for index in range(count):
            original_length, original_position = struct.unpack_from(
                f"{prefix}2I", data, original_offset + index * 8
            )
            translation_length, translation_position = struct.unpack_from(
                f"{prefix}2I", data, translation_offset + index * 8
            )
            if original_position + original_length > len(
                data
            ) or translation_position + translation_length > len(data):
                raise TransformFailedError("MO message string is out of bounds")
            entries.append(
                (
                    data[original_position : original_position + original_length],
                    data[translation_position : translation_position + translation_length],
                )
            )
        return tuple(entries), endian, revision

    @staticmethod
    def _decode_entry(original: bytes, translation: bytes, codec: str) -> _MoEntry:
        original_parts = original.split(b"\0")
        if len(original_parts) > 2:
            raise TransformFailedError("MO message contains more than one plural msgid")
        singular = original_parts[0]
        context_bytes: bytes | None = None
        if b"\x04" in singular:
            context_bytes, singular = singular.split(b"\x04", 1)
        try:
            context = context_bytes.decode(codec) if context_bytes is not None else None
            msgid = singular.decode(codec)
            plural = original_parts[1].decode(codec) if len(original_parts) == 2 else None
            translations = tuple(value.decode(codec) for value in translation.split(b"\0"))
        except UnicodeDecodeError as exc:
            raise TransformFailedError(f"MO message is not valid {codec}") from exc
        if plural is None and len(translations) != 1:
            raise TransformFailedError("singular MO message contains plural translations")
        return _MoEntry(context, msgid, plural, translations)

    @staticmethod
    def _quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _render(self, entries: Sequence[_MoEntry]) -> bytes:
        groups: list[str] = []
        for entry in entries:
            lines: list[str] = []
            if entry.context is not None:
                lines.append(f"msgctxt {self._quote(entry.context)}")
            lines.append(f"msgid {self._quote(entry.msgid)}")
            if entry.plural is None:
                lines.append(f"msgstr {self._quote(entry.translations[0])}")
            else:
                lines.append(f"msgid_plural {self._quote(entry.plural)}")
                lines.extend(
                    f"msgstr[{index}] {self._quote(value)}"
                    for index, value in enumerate(entry.translations)
                )
            groups.append("\n".join(lines))
        output = "\n\n".join(groups) + "\n"
        encoded = output.encode("utf-8")
        if len(encoded) > self._policy.max_transform_output_bytes:
            raise TransformFailedError("PO catalogue output exceeds the configured size limit")
        return encoded

    @staticmethod
    def _parse_rendered(value: str) -> tuple[_MoEntry, ...]:
        result: list[_MoEntry] = []
        for group in value.rstrip("\n").split("\n\n"):
            fields: dict[str, str] = {}
            translations: dict[int, str] = {}
            for line in group.splitlines():
                key, separator, raw_value = line.partition(" ")
                if not separator:
                    raise TransformFailedError("generated PO directive is malformed")
                try:
                    decoded = json.loads(raw_value)
                except json.JSONDecodeError as exc:
                    raise TransformFailedError("generated PO string is malformed") from exc
                if not isinstance(decoded, str):
                    raise TransformFailedError("generated PO value is not a string")
                if key.startswith("msgstr[") and key.endswith("]"):
                    translations[int(key[7:-1])] = decoded
                elif key in fields:
                    raise TransformFailedError("generated PO contains a duplicate directive")
                else:
                    fields[key] = decoded
            if "msgid" not in fields:
                raise TransformFailedError("generated PO entry has no msgid")
            plural = fields.get("msgid_plural")
            if plural is None:
                if "msgstr" not in fields or translations:
                    raise TransformFailedError("generated PO singular translation is malformed")
                translated: tuple[str, ...] = (fields["msgstr"],)
            else:
                if "msgstr" in fields or tuple(sorted(translations)) != tuple(
                    range(len(translations))
                ):
                    raise TransformFailedError("generated PO plural translation is malformed")
                translated = tuple(translations[index] for index in range(len(translations)))
            result.append(_MoEntry(fields.get("msgctxt"), fields["msgid"], plural, translated))
        return tuple(result)


@dataclass(frozen=True)
class _ReadablePlan:
    source: MaterializedFile
    source_path: Path
    output_path: str
    representation: RepresentationKind


class ReadableAssembler:
    def __init__(
        self,
        policy: ReadablePolicy | None = None,
        pyc_transformer: PycTransformer | None = None,
        actionscript_transformer: ActionScriptTransformer | None = None,
    ) -> None:
        self._policy = policy or ReadablePolicy()
        self._pyc = pyc_transformer or Uncompyle6Transformer(self._policy)
        self._actionscript = actionscript_transformer or FfdecTransformer(self._policy)
        self._pyc_syntax = Python27SourceValidator(self._policy)
        self._mo = MoCatalogueConverter(self._policy)

    def build(
        self,
        materialized: MaterializationResult,
        workspace: Workspace,
        work_directory: Path,
        *,
        client_tree: ClientTreeResult | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ReadableResult:
        report_progress = progress or (lambda _message: None)
        report_progress("preparing readable workspace")
        partial_root = work_directory / "readable.partial"
        final_root = work_directory / "readable"
        if partial_root.exists():
            shutil.rmtree(partial_root)
        if final_root.exists():
            shutil.rmtree(final_root)
        (partial_root / "base").mkdir(parents=True)
        for language in sorted(materialized.locale_roots):
            (partial_root / "locales" / language).mkdir(parents=True)
        (partial_root / "sources-as3").mkdir()
        scratch = partial_root / ".transform-tmp"
        scratch.mkdir()

        report_progress(f"planning {len(materialized.files)} files")
        plans = tuple(self._plan(item, materialized, workspace) for item in materialized.files)
        self._check_collisions(plans)
        actionscript_plans = tuple(
            plan
            for plan in plans
            if plan.source.language is None and self._is_actionscript_library(plan.source.path)
        )
        self._check_actionscript_bundle_collisions(actionscript_plans)
        results: list[ReadableFile] = []
        actionscript_results: list[ActionScriptFile] = []
        transformed = tuple(
            plan for plan in plans if plan.representation is not RepresentationKind.PASSTHROUGH
        )
        passthrough_total = len(plans) - len(transformed)
        report_progress(
            f"planned {len(transformed)} transformations, "
            f"{len(actionscript_plans)} AS3 libraries and {passthrough_total} passthrough files"
        )
        with ThreadPoolExecutor(
            max_workers=self._policy.transform_workers,
            thread_name_prefix="readable",
        ) as executor:
            transform_interval = max(1, len(transformed) // 20)
            for completed, transformed_file in enumerate(
                executor.map(
                    lambda plan: self._transform(plan, partial_root, scratch),
                    transformed,
                ),
                start=1,
            ):
                results.append(transformed_file)
                if completed % transform_interval == 0 or completed == len(transformed):
                    report_progress(f"transformed {completed}/{len(transformed)} files")
        gc.collect()
        with ThreadPoolExecutor(
            max_workers=self._policy.actionscript_workers,
            thread_name_prefix="actionscript",
        ) as executor:
            actionscript_interval = max(1, len(actionscript_plans) // 20)
            for completed, exported in enumerate(
                executor.map(
                    lambda plan: self._transform_actionscript(plan, partial_root, scratch),
                    actionscript_plans,
                ),
                start=1,
            ):
                actionscript_results.extend(exported)
                if completed % actionscript_interval == 0 or completed == len(actionscript_plans):
                    report_progress(
                        f"decompiled {completed}/{len(actionscript_plans)} AS3 libraries"
                    )
        passthrough_completed = 0
        passthrough_interval = max(1, passthrough_total // 20)
        for plan in plans:
            if plan.representation is not RepresentationKind.PASSTHROUGH:
                continue
            destination = self._destination(partial_root, plan.source.language, plan.output_path)
            link_or_copy(plan.source_path, destination)
            results.append(
                ReadableFile(
                    path=plan.output_path,
                    language=plan.source.language,
                    size=plan.source.size,
                    sha256=plan.source.sha256,
                    source=plan.source,
                    representation=FileRepresentation(
                        kind=RepresentationKind.PASSTHROUGH,
                        source_path=plan.source.path,
                        source_sha256=plan.source.sha256,
                    ),
                    diagnostics=(
                        ("content-sniff=text-xml",)
                        if plan.source.path.lower().endswith(".xml")
                        else ()
                    ),
                )
            )
            passthrough_completed += 1
            if (
                passthrough_completed % passthrough_interval == 0
                or passthrough_completed == passthrough_total
            ):
                report_progress(
                    f"linked {passthrough_completed}/{passthrough_total} passthrough files"
                )

        shutil.rmtree(scratch)
        if client_tree is None:
            (partial_root / "stubs").mkdir()
        else:
            report_progress("analyzing native PE metadata")
            self._generate_engine_stubs(
                partial_root,
                client_tree,
                workspace,
                progress=report_progress,
            )
        stub_results = self._stub_files(partial_root / "stubs")
        report_progress(f"generated {len(stub_results)} stub artifacts; finalizing result")
        os.replace(partial_root, final_root)
        os.sync()
        workspace.fsync_directory(work_directory)
        tools = self._tool_identities(
            plans,
            has_actionscript=bool(actionscript_plans),
            has_engine_stubs=client_tree is not None,
        )
        return ReadableResult(
            materialization_result_sha256="sha256:" + "0" * 64,
            policy_name=self._policy.name,
            policy_version=self._policy.version,
            policy_sha256=self._policy.sha256,
            base_root=(final_root / "base").relative_to(workspace.root).as_posix(),
            locale_roots={
                language: (final_root / "locales" / language).relative_to(workspace.root).as_posix()
                for language in sorted(materialized.locale_roots)
            },
            actionscript_root=(final_root / "sources-as3").relative_to(workspace.root).as_posix(),
            stubs_root=(final_root / "stubs").relative_to(workspace.root).as_posix(),
            tools=tools,
            files=tuple(
                sorted(
                    results,
                    key=lambda item: (
                        item.language is not None,
                        item.language or "",
                        item.path.encode("utf-8"),
                    ),
                )
            ),
            actionscript_files=tuple(
                sorted(actionscript_results, key=lambda item: item.path.encode("utf-8"))
            ),
            stub_files=stub_results,
        )

    def _generate_engine_stubs(
        self,
        root: Path,
        client_tree: ClientTreeResult,
        workspace: Workspace,
        *,
        source_roots: Sequence[Path] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        client_root = workspace.root / client_tree.base_root
        selected_source_roots = source_roots or (
            root / "base",
            *(root / "locales" / language for language in sorted(client_tree.locale_roots)),
        )
        try:
            binaries = find_main_binaries(client_root)
            usage, bindings, enums, constants = analyze_engine_stubs(
                binaries,
                selected_source_roots,
                max_workers=self._policy.engine_stub_workers,
                progress=progress,
            )
            EngineStubGenerator().write(
                root / "stubs",
                usage=usage,
                bindings=bindings,
                binaries=binaries,
                binary_root=client_root,
                enums=enums,
                constants=constants,
            )
        except (EngineStubError, OSError, ValueError) as exc:
            raise TransformFailedError(f"engine stub generation failed: {exc}") from exc

    @staticmethod
    def _stub_files(root: Path) -> tuple[StubFile, ...]:
        values: list[StubFile] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().encode("utf-8")):
            if not path.is_file():
                continue
            path_stat = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                raise TransformFailedError(f"engine stub output is not regular: {path}")
            values.append(
                StubFile(
                    path=path.relative_to(root).as_posix(),
                    size=path_stat.st_size,
                    sha256=_hash_file(path),
                )
            )
        return tuple(values)

    def _plan(
        self,
        item: MaterializedFile,
        materialized: MaterializationResult,
        workspace: Workspace,
    ) -> _ReadablePlan:
        root = (
            workspace.root / materialized.locale_roots[item.language]
            if item.language is not None
            else workspace.root / materialized.base_root
        )
        source = root / item.path
        source_stat = source.lstat()
        if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
            raise TransformFailedError(f"materialized source is not regular: {source}")
        if source_stat.st_size != item.size:
            raise TransformFailedError(f"materialized source size changed: {source}")
        lowered = item.path.lower()
        if lowered.endswith(".pyc"):
            representation = RepresentationKind.PYC_TO_PY
            output = item.path[:-4] + ".py"
        elif lowered.endswith(".mo"):
            if item.size > self._policy.max_mo_bytes:
                raise TransformFailedError(f"MO catalogue exceeds policy: {item.path}")
            representation = RepresentationKind.MO_TO_PO
            output = item.path[:-3] + ".po"
        elif lowered.endswith(".xml"):
            with source.open("rb") as stream:
                head = stream.read(65536)
            if head.startswith(PACKED_SECTION_MAGIC):
                if item.size > self._policy.max_packed_xml_bytes:
                    raise TransformFailedError(f"packed XML exceeds policy: {item.path}")
                representation = RepresentationKind.PACKED_XML_TO_XML
            elif self._looks_like_text_xml(head):
                representation = RepresentationKind.PASSTHROUGH
            else:
                raise TransformFailedError(f"unknown XML encoding/format: {item.path}")
            output = item.path
        else:
            representation = RepresentationKind.PASSTHROUGH
            output = item.path
        return _ReadablePlan(item, source, output, representation)

    @staticmethod
    def _looks_like_text_xml(head: bytes) -> bool:
        if not head:
            return False
        encodings = (
            ("utf-8-sig", "utf-16")
            if head.startswith((b"\xff\xfe", b"\xfe\xff"))
            else ("utf-8-sig",)
        )
        for encoding in encodings:
            try:
                text = head.decode(encoding)
            except UnicodeDecodeError:
                continue
            if text.lstrip().startswith("<"):
                return True
        return False

    @staticmethod
    def _check_collisions(plans: Sequence[_ReadablePlan]) -> None:
        seen: dict[tuple[str | None, str], _ReadablePlan] = {}
        for plan in plans:
            key = (plan.source.language, plan.output_path.casefold())
            previous = seen.get(key)
            if previous is not None:
                raise TransformFailedError(
                    "readable path collision between "
                    f"{previous.source.path!r} and {plan.source.path!r}"
                )
            seen[key] = plan

    @classmethod
    def _check_actionscript_bundle_collisions(cls, plans: Sequence[_ReadablePlan]) -> None:
        seen: dict[str, _ReadablePlan] = {}
        for plan in plans:
            bundle = cls._actionscript_bundle_path(plan.source.path)
            previous = seen.get(bundle.casefold())
            if previous is not None:
                raise TransformFailedError(
                    "ActionScript bundle collision between "
                    f"{previous.source.path!r} and {plan.source.path!r}"
                )
            seen[bundle.casefold()] = plan

    @staticmethod
    def _actionscript_bundle_path(source_path: str) -> str:
        stem = PurePosixPath(source_path).stem
        return re.sub(r"-\d+(?:\.\d+)*-SNAPSHOT\Z", "", stem, flags=re.IGNORECASE)

    @staticmethod
    def _is_actionscript_library(source_path: str) -> bool:
        path = PurePosixPath(source_path)
        parts = tuple(part.casefold() for part in path.parts)
        return path.suffix.casefold() == ".swc" and parts[-4:-1] == (
            "gui",
            "flash",
            "swc",
        )

    def _transform(
        self,
        plan: _ReadablePlan,
        root: Path,
        scratch: Path,
    ) -> ReadableFile:
        self._verify_transform_source(plan)
        if plan.representation is RepresentationKind.PYC_TO_PY:
            with plan.source_path.open("rb") as source:
                magic = source.read(4)
            output, diagnostics = self._pyc.transform(plan.source_path, magic, scratch)
            return self._finish_pyc_transform(plan, root, output, diagnostics)
        elif plan.representation is RepresentationKind.PACKED_XML_TO_XML:
            output, diagnostics = PackedXmlDecoder(self._policy).decode(
                plan.source_path.read_bytes(), Path(plan.source.path).name
            )
            tool = PACKED_XML_TOOL
        elif plan.representation is RepresentationKind.MO_TO_PO:
            output, diagnostics = self._mo.convert(plan.source_path.read_bytes())
            tool = MO_TOOL
        else:
            raise AssertionError("passthrough plan reached transformer")
        return self._commit_transform(plan, root, output, diagnostics, tool)

    def _finish_pyc_transform(
        self,
        plan: _ReadablePlan,
        root: Path,
        output: bytes,
        diagnostics: tuple[str, ...],
    ) -> ReadableFile:
        diagnostics += self._pyc_syntax.validate(output, plan.source.path)
        return self._commit_transform(plan, root, output, diagnostics, self._pyc.identity)

    def _commit_transform(
        self,
        plan: _ReadablePlan,
        root: Path,
        output: bytes,
        diagnostics: tuple[str, ...],
        tool: ToolIdentity,
    ) -> ReadableFile:
        if len(output) > self._policy.max_transform_output_bytes:
            raise TransformFailedError(f"transform output exceeds policy: {plan.source.path}")
        destination = self._destination(root, plan.source.language, plan.output_path)
        self._write_output(destination, output)
        return ReadableFile(
            path=plan.output_path,
            language=plan.source.language,
            size=len(output),
            sha256=hashlib.sha256(output).hexdigest(),
            source=plan.source,
            representation=FileRepresentation(
                kind=plan.representation,
                source_path=plan.source.path,
                source_sha256=plan.source.sha256,
                tool=tool.name,
                tool_version=tool.version,
            ),
            diagnostics=diagnostics,
        )

    def _transform_actionscript(
        self,
        plan: _ReadablePlan,
        root: Path,
        scratch: Path,
    ) -> tuple[ActionScriptFile, ...]:
        self._verify_transform_source(plan)
        outputs = self._actionscript.transform(plan.source_path, scratch)
        bundle = self._actionscript_bundle_path(plan.source.path)
        tool = self._actionscript.identity
        result: list[ActionScriptFile] = []
        for output in outputs:
            relative = f"{bundle}/{output.path}"
            destination = root / "sources-as3" / relative
            self._write_output(destination, output.data)
            result.append(
                ActionScriptFile(
                    path=relative,
                    size=len(output.data),
                    sha256=hashlib.sha256(output.data).hexdigest(),
                    source=plan.source,
                    representation=FileRepresentation(
                        kind=RepresentationKind.SWC_TO_AS,
                        source_path=plan.source.path,
                        source_sha256=plan.source.sha256,
                        tool=tool.name,
                        tool_version=tool.version,
                    ),
                    diagnostics=output.diagnostics,
                )
            )
        return tuple(result)

    @staticmethod
    def _verify_transform_source(plan: _ReadablePlan) -> None:
        # Transformed source bytes are not published verbatim in the final snapshot. Verify
        # their materialization digest at the consumption boundary; passthrough files are
        # independently hashed by the final SnapshotVerifier.
        if _hash_file(plan.source_path) != plan.source.sha256:
            raise TransformFailedError(f"materialized source digest changed: {plan.source.path}")

    @staticmethod
    def _write_output(destination: Path, output: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(output)
                os.fchmod(stream.fileno(), 0o444)
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _tool_identities(
        self,
        plans: Sequence[_ReadablePlan],
        *,
        has_actionscript: bool,
        has_engine_stubs: bool,
    ) -> tuple[ToolIdentity, ...]:
        return self._tool_identities_for_representations(
            {plan.representation for plan in plans},
            has_actionscript=has_actionscript,
            has_engine_stubs=has_engine_stubs,
        )

    def _tool_identities_for_representations(
        self,
        representations: set[RepresentationKind],
        *,
        has_actionscript: bool,
        has_engine_stubs: bool,
    ) -> tuple[ToolIdentity, ...]:
        policy_tool = ToolIdentity(name="game-downloader-readable", version=self._policy.version)
        identities = {(policy_tool.name, policy_tool.version): policy_tool}
        if RepresentationKind.PYC_TO_PY in representations:
            identity = self._pyc.identity
            identities[(identity.name, identity.version)] = identity
            identities[(PYTHON27_SYNTAX_TOOL.name, PYTHON27_SYNTAX_TOOL.version)] = (
                PYTHON27_SYNTAX_TOOL
            )
        if RepresentationKind.PACKED_XML_TO_XML in representations:
            identities[(PACKED_XML_TOOL.name, PACKED_XML_TOOL.version)] = PACKED_XML_TOOL
        if RepresentationKind.MO_TO_PO in representations:
            identities[(MO_TOOL.name, MO_TOOL.version)] = MO_TOOL
        if has_actionscript:
            identity = self._actionscript.identity
            identities[(identity.name, identity.version)] = identity
        if has_engine_stubs:
            identities[(ENGINE_STUBS_TOOL.name, ENGINE_STUBS_TOOL.version)] = ENGINE_STUBS_TOOL
        return tuple(identities[key] for key in sorted(identities))

    @staticmethod
    def _destination(root: Path, language: str | None, relative: str) -> Path:
        layer = root / "locales" / language if language is not None else root / "base"
        return layer / relative


def _audit_readable(result: ReadableResult, workspace: Workspace) -> None:
    roots: dict[str | None, Path] = {None: workspace.root / result.base_root}
    roots.update(
        {language: workspace.root / path for language, path in result.locale_roots.items()}
    )
    expected = {(item.language, item.path): item for item in result.files}
    actual: set[tuple[str | None, str]] = set()
    for language, root in roots.items():
        if root.is_symlink() or not root.is_dir():
            raise ValueError("readable layer root is invalid")
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink() for name in directory_names):
                raise ValueError("readable output contains a symlink directory")
            for name in file_names:
                path = current_path / name
                key = (language, path.relative_to(root).as_posix())
                item = expected.get(key)
                if item is None:
                    raise ValueError("readable output contains an unmanifested file")
                path_stat = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                    raise ValueError("readable output contains a non-regular file")
                if stat.S_IMODE(path_stat.st_mode) & 0o222:
                    raise ValueError("readable output file is writable")
                if path_stat.st_size != item.size or _hash_file(path) != item.sha256:
                    raise ValueError("readable output does not match its manifest")
                lowered = item.path.lower()
                if lowered.endswith((".pyc", ".mo")):
                    raise ValueError("readable output retained a compiled source format")
                if lowered.endswith(".xml"):
                    with path.open("rb") as source:
                        if source.read(4) == PACKED_SECTION_MAGIC:
                            raise ValueError("readable output retained packed XML")
                actual.add(key)
    if actual != set(expected):
        raise ValueError("readable manifest references missing files")

    actionscript_root = workspace.root / result.actionscript_root
    if actionscript_root.is_symlink() or not actionscript_root.is_dir():
        raise ValueError("ActionScript output root is invalid")
    expected_actionscript = {item.path: item for item in result.actionscript_files}
    actual_actionscript: set[str] = set()
    for current, directory_names, file_names in os.walk(actionscript_root, followlinks=False):
        current_path = Path(current)
        if any((current_path / name).is_symlink() for name in directory_names):
            raise ValueError("ActionScript output contains a symlink directory")
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(actionscript_root).as_posix()
            actionscript_item = expected_actionscript.get(relative)
            if actionscript_item is None:
                raise ValueError("ActionScript output contains an unmanifested file")
            path_stat = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("ActionScript output contains a non-regular file")
            if stat.S_IMODE(path_stat.st_mode) & 0o222:
                raise ValueError("ActionScript output file is writable")
            if (
                path_stat.st_size != actionscript_item.size
                or _hash_file(path) != actionscript_item.sha256
            ):
                raise ValueError("ActionScript output does not match its manifest")
            if not relative.lower().endswith(".as"):
                raise ValueError("ActionScript output has an unexpected extension")
            actual_actionscript.add(relative)
    if actual_actionscript != set(expected_actionscript):
        raise ValueError("ActionScript manifest references missing files")

    stubs_root = workspace.root / result.stubs_root
    if stubs_root.is_symlink() or not stubs_root.is_dir():
        raise ValueError("engine stubs output root is invalid")
    expected_stubs = {item.path: item for item in result.stub_files}
    actual_stubs: set[str] = set()
    for current, directory_names, file_names in os.walk(stubs_root, followlinks=False):
        current_path = Path(current)
        if any((current_path / name).is_symlink() for name in directory_names):
            raise ValueError("engine stubs output contains a symlink directory")
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(stubs_root).as_posix()
            stub_item = expected_stubs.get(relative)
            if stub_item is None:
                raise ValueError("engine stubs output contains an unmanifested file")
            path_stat = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("engine stubs output contains a non-regular file")
            if stat.S_IMODE(path_stat.st_mode) & 0o222:
                raise ValueError("engine stubs output file is writable")
            if path_stat.st_size != stub_item.size or _hash_file(path) != stub_item.sha256:
                raise ValueError("engine stubs output does not match its manifest")
            actual_stubs.add(relative)
    if actual_stubs != set(expected_stubs):
        raise ValueError("engine stubs manifest references missing files")


def _source_path(
    entry: ReadablePlanEntry,
    plan: ReadablePlanResult,
    workspace: Workspace,
) -> Path:
    root = (
        workspace.root / plan.materialized_locale_roots[entry.source.language]
        if entry.source.language is not None
        else workspace.root / plan.materialized_base_root
    )
    return root / entry.source.path


def _runtime_plan(
    entry: ReadablePlanEntry,
    plan: ReadablePlanResult,
    workspace: Workspace,
) -> _ReadablePlan:
    return _ReadablePlan(
        source=entry.source,
        source_path=_source_path(entry, plan, workspace),
        output_path=entry.output_path,
        representation=entry.representation,
    )


def _assert_stage_root(context: StageContext, actual: str, *parts: str) -> None:
    expected = (
        (context.work_directory.joinpath(*parts)).relative_to(context.workspace.root).as_posix()
    )
    if actual != expected:
        raise ValueError(f"Stage output root is not canonical: expected {expected!r}")


def _audit_manifest_file(path: Path, *, size: int, sha256: str, label: str) -> None:
    path_stat = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"{label} output contains a non-regular file")
    if stat.S_IMODE(path_stat.st_mode) & 0o222:
        raise ValueError(f"{label} output file is writable")
    if path_stat.st_size != size or _hash_file(path) != sha256:
        raise ValueError(f"{label} output does not match its manifest")


def _audit_layer_entries(
    files: Sequence[ReadableFile],
    *,
    base_root: Path,
    locale_roots: Mapping[str, Path],
    label: str,
) -> None:
    for item in files:
        root = locale_roots[item.language] if item.language is not None else base_root
        _audit_manifest_file(root / item.path, size=item.size, sha256=item.sha256, label=label)


def _audit_actionscript_entries(
    files: Sequence[ActionScriptFile],
    *,
    root: Path,
) -> None:
    for item in files:
        _audit_manifest_file(
            root / item.path,
            size=item.size,
            sha256=item.sha256,
            label="ActionScript",
        )


def _audit_stub_entries(files: Sequence[StubFile], *, root: Path) -> None:
    for item in files:
        _audit_manifest_file(
            root / item.path,
            size=item.size,
            sha256=item.sha256,
            label="engine stubs",
        )


def _passthrough_file(entry: ReadablePlanEntry) -> ReadableFile:
    return ReadableFile(
        path=entry.output_path,
        language=entry.source.language,
        size=entry.source.size,
        sha256=entry.source.sha256,
        source=entry.source,
        representation=FileRepresentation(
            kind=RepresentationKind.PASSTHROUGH,
            source_path=entry.source.path,
            source_sha256=entry.source.sha256,
        ),
        diagnostics=(
            ("content-sniff=text-xml",) if entry.source.path.lower().endswith(".xml") else ()
        ),
    )


_READABLE_PROCESS_ASSEMBLER: ReadableAssembler | None = None


def _chunked_work[WorkItem](
    values: Sequence[WorkItem],
    *,
    workers: int,
) -> tuple[tuple[WorkItem, ...], ...]:
    if not values:
        return ()
    chunk_size = max(1, math.ceil(len(values) / max(1, workers * 4)))
    return tuple(
        tuple(values[offset : offset + chunk_size]) for offset in range(0, len(values), chunk_size)
    )


def _balanced_staged_pyc_work(
    values: Sequence[tuple[_ReadablePlan, Path, _StagedPycOutput]],
    *,
    workers: int,
) -> tuple[tuple[tuple[_ReadablePlan, Path, _StagedPycOutput], ...], ...]:
    if not values:
        return ()
    chunk_count = min(len(values), max(1, workers * 8))
    buckets: list[list[tuple[_ReadablePlan, Path, _StagedPycOutput]]] = [
        [] for _ in range(chunk_count)
    ]
    loads = [0] * chunk_count
    available = [(0, index) for index in range(chunk_count)]
    heapq.heapify(available)
    ordered = sorted(
        enumerate(values),
        key=lambda item: (-item[1][2].size, item[0]),
    )
    for _index, value in ordered:
        load, bucket_index = heapq.heappop(available)
        buckets[bucket_index].append(value)
        loads[bucket_index] = load + value[2].size
        heapq.heappush(available, (loads[bucket_index], bucket_index))
    return tuple(
        tuple(buckets[index])
        for index in sorted(range(chunk_count), key=lambda index: (-loads[index], index))
    )


def _initialize_readable_process(policy: ReadablePolicy) -> None:
    global _READABLE_PROCESS_ASSEMBLER
    _READABLE_PROCESS_ASSEMBLER = ReadableAssembler(policy)


def _process_assembler() -> ReadableAssembler:
    if _READABLE_PROCESS_ASSEMBLER is None:
        raise RuntimeError("readable transform process was not initialized")
    return _READABLE_PROCESS_ASSEMBLER


def _finish_pyc_chunk(
    values: Sequence[tuple[_ReadablePlan, Path, _StagedPycOutput]],
) -> tuple[ReadableFile, ...]:
    assembler = _process_assembler()
    if not isinstance(assembler._pyc, Uncompyle6Transformer):
        raise RuntimeError("staged PYC output requires the pinned batch transformer")
    result: list[ReadableFile] = []
    for plan, root, staged in values:
        if plan.source_path != staged.source:
            raise TransformFailedError("staged PYC output does not match its source")
        output, diagnostics = assembler._pyc.decode_staged(staged)
        result.append(assembler._finish_pyc_transform(plan, root, output, diagnostics))
    return tuple(result)


def _transform_readable_chunk(
    values: Sequence[tuple[_ReadablePlan, Path, Path]],
) -> tuple[ReadableFile, ...]:
    assembler = _process_assembler()
    return tuple(assembler._transform(plan, root, scratch) for plan, root, scratch in values)


def create_readable_implementations(
    policy: ReadablePolicy | None = None,
    pyc_transformer: PycTransformer | None = None,
    actionscript_transformer: ActionScriptTransformer | None = None,
) -> Mapping[Stage, StageImplementation]:
    selected = policy or ReadablePolicy()
    assembler = ReadableAssembler(selected, pyc_transformer, actionscript_transformer)
    configuration = cast(Mapping[str, JsonValue], selected.model_dump(mode="json"))

    def execute_plan(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.PLAN_READABLE or context.upstream is None:
            raise TransformFailedError("plan-readable requires a Materialization Result")
        materialized = context.upstream_as(MaterializationResult)
        context.progress(f"planning {len(materialized.files)} files")
        runtime_plans = tuple(
            assembler._plan(item, materialized, context.workspace) for item in materialized.files
        )
        assembler._check_collisions(runtime_plans)
        actionscript_plans = tuple(
            item for item in runtime_plans if assembler._is_actionscript_library(item.source.path)
        )
        assembler._check_actionscript_bundle_collisions(actionscript_plans)
        actionscript_paths = {item.source.path for item in actionscript_plans}
        entries = tuple(
            ReadablePlanEntry(
                source=item.source,
                output_path=item.output_path,
                representation=item.representation,
                actionscript_bundle=(
                    assembler._actionscript_bundle_path(item.source.path)
                    if item.source.path in actionscript_paths
                    else None
                ),
            )
            for item in runtime_plans
        )
        transformed = sum(
            item.representation is not RepresentationKind.PASSTHROUGH for item in entries
        )
        context.progress(
            f"planned {transformed} transformations, {len(actionscript_plans)} AS3 libraries "
            f"and {len(entries) - transformed} passthrough files"
        )
        result = ReadablePlanResult(
            materialization_result_sha256=context.require_upstream_digest(),
            policy_name=selected.name,
            policy_version=selected.version,
            policy_sha256=selected.sha256,
            materialized_base_root=materialized.base_root,
            materialized_locale_roots=materialized.locale_roots,
            entries=entries,
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate_plan(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Readable Plan Result has no Materialization Result")
        result = ReadablePlanResult.model_validate(payload)
        materialized = context.upstream_as(MaterializationResult)
        if result.materialization_result_sha256 != context.require_upstream_digest():
            raise ValueError("Readable Plan Result is not bound to Materialization Result")
        if (
            result.policy_name != selected.name
            or result.policy_version != selected.version
            or result.policy_sha256 != selected.sha256
        ):
            raise ValueError("Readable Plan Result policy does not match implementation")
        if (
            result.materialized_base_root != materialized.base_root
            or result.materialized_locale_roots != materialized.locale_roots
        ):
            raise ValueError("Readable Plan Result roots do not match Materialization Result")

    def execute_transforms(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.TRANSFORM_READABLE or context.upstream is None:
            raise TransformFailedError("transform-readable requires a Readable Plan Result")
        phase_started = time.monotonic()
        plan = context.upstream_as(ReadablePlanResult)
        context.progress(
            f"loaded readable plan with {len(plan.entries)} entries in "
            f"{time.monotonic() - phase_started:.1f}s"
        )
        root = context.work_directory / "transformed"
        if root.exists():
            shutil.rmtree(root)
        (root / "base").mkdir(parents=True)
        for language in sorted(plan.materialized_locale_roots):
            (root / "locales" / language).mkdir(parents=True)
        scratch = context.work_directory / ".transform-tmp"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir()
        runtime_plans = tuple(
            _runtime_plan(item, plan, context.workspace)
            for item in plan.entries
            if item.representation is not RepresentationKind.PASSTHROUGH
        )
        files: list[ReadableFile] = []
        interval = max(1, len(runtime_plans) // 20)
        completed = 0
        batch_transformer = (
            assembler._pyc if isinstance(assembler._pyc, Uncompyle6Transformer) else None
        )
        batched_pyc = (
            tuple(
                item
                for item in runtime_plans
                if item.representation is RepresentationKind.PYC_TO_PY
            )
            if batch_transformer is not None
            else ()
        )
        remaining = tuple(
            item
            for item in runtime_plans
            if not batched_pyc or item.representation is not RepresentationKind.PYC_TO_PY
        )
        packed_xml_count = sum(
            item.representation is RepresentationKind.PACKED_XML_TO_XML for item in remaining
        )
        mo_count = sum(item.representation is RepresentationKind.MO_TO_PO for item in remaining)
        context.progress(
            f"scheduled {len(batched_pyc)} PYC, "
            f"{packed_xml_count} packed XML and {mo_count} MO transformations"
        )
        if batched_pyc:
            assert batch_transformer is not None

            def prepare_pyc(item: _ReadablePlan) -> tuple[Path, bytes]:
                assembler._verify_transform_source(item)
                with item.source_path.open("rb") as source:
                    return item.source_path, source.read(4)

            phase_started = time.monotonic()
            with ThreadPoolExecutor(
                max_workers=selected.transform_workers,
                thread_name_prefix="pyc-prepare",
            ) as prepare_executor:
                pyc_inputs = tuple(prepare_executor.map(prepare_pyc, batched_pyc))
            context.progress(
                f"verified {len(pyc_inputs)} PYC inputs in {time.monotonic() - phase_started:.1f}s"
            )
            phase_started = time.monotonic()
            staged_pyc = batch_transformer.stage_many(pyc_inputs, scratch)
            staged_pyc_bytes = sum(output.size for output in staged_pyc.outputs)
            context.progress(
                f"decompiled {len(staged_pyc.outputs)} PYC inputs into "
                f"{staged_pyc_bytes} staged bytes in {time.monotonic() - phase_started:.1f}s"
            )

            process_workers = min(selected.transform_workers, len(runtime_plans))
            try:
                with ProcessPoolExecutor(
                    max_workers=process_workers,
                    initializer=_initialize_readable_process,
                    initargs=(selected,),
                ) as process_executor:
                    phase_started = time.monotonic()
                    pyc_work = tuple(
                        (item, root, output)
                        for item, output in zip(
                            batched_pyc,
                            staged_pyc.outputs,
                            strict=True,
                        )
                    )
                    for transformed_chunk in process_executor.map(
                        _finish_pyc_chunk,
                        _balanced_staged_pyc_work(pyc_work, workers=process_workers),
                    ):
                        for transformed_file in transformed_chunk:
                            files.append(transformed_file)
                            completed += 1
                            if completed % interval == 0 or completed == len(runtime_plans):
                                context.progress(
                                    f"transformed {completed}/{len(runtime_plans)} files"
                                )
                    context.progress(
                        f"validated and committed {len(pyc_work)} PYC outputs in "
                        f"{time.monotonic() - phase_started:.1f}s"
                    )

                    phase_started = time.monotonic()
                    remaining_work = tuple((item, root, scratch) for item in remaining)
                    for transformed_chunk in process_executor.map(
                        _transform_readable_chunk,
                        _chunked_work(remaining_work, workers=process_workers),
                    ):
                        for transformed_file in transformed_chunk:
                            files.append(transformed_file)
                            completed += 1
                            if completed % interval == 0 or completed == len(runtime_plans):
                                context.progress(
                                    f"transformed {completed}/{len(runtime_plans)} files"
                                )
                    context.progress(
                        f"converted {len(remaining_work)} XML/MO inputs in "
                        f"{time.monotonic() - phase_started:.1f}s"
                    )
            finally:
                shutil.rmtree(staged_pyc.root, ignore_errors=True)
        else:
            phase_started = time.monotonic()
            with ThreadPoolExecutor(
                max_workers=selected.transform_workers,
                thread_name_prefix="readable",
            ) as thread_executor:
                for transformed_file in thread_executor.map(
                    lambda item: assembler._transform(item, root, scratch),
                    remaining,
                ):
                    files.append(transformed_file)
                    completed += 1
                    if completed % interval == 0 or completed == len(runtime_plans):
                        context.progress(f"transformed {completed}/{len(runtime_plans)} files")
            context.progress(
                f"converted {len(remaining)} custom-adapter inputs in "
                f"{time.monotonic() - phase_started:.1f}s"
            )
        shutil.rmtree(scratch)
        phase_started = time.monotonic()
        os.sync()
        context.progress(
            f"durably synced transformed outputs in {time.monotonic() - phase_started:.1f}s"
        )
        result = ReadableTransformResult(
            readable_plan_result_sha256=context.require_upstream_digest(),
            base_root=(root / "base").relative_to(context.workspace.root).as_posix(),
            locale_roots={
                language: (root / "locales" / language)
                .relative_to(context.workspace.root)
                .as_posix()
                for language in sorted(plan.materialized_locale_roots)
            },
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
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate_transforms(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Readable Transform Result has no Readable Plan Result")
        result = ReadableTransformResult.model_validate(payload)
        plan = context.upstream_as(ReadablePlanResult)
        if result.readable_plan_result_sha256 != context.require_upstream_digest():
            raise ValueError("Readable Transform Result is not bound to Readable Plan Result")
        _assert_stage_root(context, result.base_root, "transformed", "base")
        if set(result.locale_roots) != set(plan.materialized_locale_roots):
            raise ValueError("transformed locale roots do not match Readable Plan Result")
        for language, root in result.locale_roots.items():
            _assert_stage_root(context, root, "transformed", "locales", language)

    def audit_transforms(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = ReadableTransformResult.model_validate(payload)
        _audit_layer_entries(
            result.files,
            base_root=context.workspace.root / result.base_root,
            locale_roots={
                language: context.workspace.root / root
                for language, root in result.locale_roots.items()
            },
            label="transformed readable",
        )

    def execute_actionscript(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.DECOMPILE_ACTIONSCRIPT or context.upstream is None:
            raise TransformFailedError(
                "decompile-actionscript requires a Readable Transform Result"
            )
        context.upstream_as(ReadableTransformResult)
        plan = context.committed_as(Stage.PLAN_READABLE, ReadablePlanResult)
        root = context.work_directory / "readable"
        if root.exists():
            shutil.rmtree(root)
        (root / "sources-as3").mkdir(parents=True)
        scratch = context.work_directory / ".actionscript-tmp"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir()
        runtime_plans = tuple(
            _runtime_plan(item, plan, context.workspace)
            for item in plan.entries
            if item.actionscript_bundle is not None
        )
        files: list[ActionScriptFile] = []
        with ThreadPoolExecutor(
            max_workers=selected.actionscript_workers,
            thread_name_prefix="actionscript",
        ) as executor:
            interval = max(1, len(runtime_plans) // 20)
            for completed, exported in enumerate(
                executor.map(
                    lambda item: assembler._transform_actionscript(item, root, scratch),
                    runtime_plans,
                ),
                start=1,
            ):
                files.extend(exported)
                if completed % interval == 0 or completed == len(runtime_plans):
                    context.progress(f"decompiled {completed}/{len(runtime_plans)} AS3 libraries")
        shutil.rmtree(scratch)
        phase_started = time.monotonic()
        os.sync()
        context.progress(
            f"durably synced ActionScript outputs in {time.monotonic() - phase_started:.1f}s"
        )
        result = ActionScriptResult(
            readable_transform_result_sha256=context.require_upstream_digest(),
            root=(root / "sources-as3").relative_to(context.workspace.root).as_posix(),
            files=tuple(sorted(files, key=lambda item: item.path.encode("utf-8"))),
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate_actionscript(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("ActionScript Result has no Readable Transform Result")
        result = ActionScriptResult.model_validate(payload)
        if result.readable_transform_result_sha256 != context.require_upstream_digest():
            raise ValueError("ActionScript Result is not bound to Readable Transform Result")
        _assert_stage_root(context, result.root, "readable", "sources-as3")

    def audit_actionscript(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = ActionScriptResult.model_validate(payload)
        _audit_actionscript_entries(result.files, root=context.workspace.root / result.root)

    def execute_assembly(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.ASSEMBLE_READABLE or context.upstream is None:
            raise TransformFailedError("assemble-readable requires an ActionScript Result")
        actionscript = context.upstream_as(ActionScriptResult)
        plan = context.committed_as(Stage.PLAN_READABLE, ReadablePlanResult)
        transforms = context.committed_as(Stage.TRANSFORM_READABLE, ReadableTransformResult)
        transformed = {(item.language, item.path): item for item in transforms.files}
        root = context.work_directory / "readable"
        if root.exists():
            shutil.rmtree(root)
        (root / "base").mkdir(parents=True)
        for language in sorted(plan.materialized_locale_roots):
            (root / "locales" / language).mkdir(parents=True)
        (root / "sources-as3").mkdir()
        files: list[ReadableFile] = []
        passthrough_total = sum(
            item.representation is RepresentationKind.PASSTHROUGH for item in plan.entries
        )
        passthrough_completed = 0
        passthrough_interval = max(1, passthrough_total // 20)
        for entry in plan.entries:
            destination = assembler._destination(
                root,
                entry.source.language,
                entry.output_path,
            )
            if entry.representation is RepresentationKind.PASSTHROUGH:
                link_or_copy(_source_path(entry, plan, context.workspace), destination)
                files.append(_passthrough_file(entry))
                passthrough_completed += 1
                if (
                    passthrough_completed % passthrough_interval == 0
                    or passthrough_completed == passthrough_total
                ):
                    context.progress(
                        f"linked {passthrough_completed}/{passthrough_total} passthrough files"
                    )
                continue
            transformed_file = transformed.get((entry.source.language, entry.output_path))
            if transformed_file is None:
                raise TransformFailedError(
                    f"transformed output is missing from checkpoint: {entry.output_path}"
                )
            source_root = (
                context.workspace.root / transforms.locale_roots[entry.source.language]
                if entry.source.language is not None
                else context.workspace.root / transforms.base_root
            )
            link_or_copy(source_root / transformed_file.path, destination)
            files.append(transformed_file)
        for item in actionscript.files:
            link_or_copy(
                context.workspace.root / actionscript.root / item.path,
                root / "sources-as3" / item.path,
            )
        result = ReadableAssemblyResult(
            actionscript_result_sha256=context.require_upstream_digest(),
            materialization_result_sha256=plan.materialization_result_sha256,
            policy_name=plan.policy_name,
            policy_version=plan.policy_version,
            policy_sha256=plan.policy_sha256,
            base_root=(root / "base").relative_to(context.workspace.root).as_posix(),
            locale_roots={
                language: (root / "locales" / language)
                .relative_to(context.workspace.root)
                .as_posix()
                for language in sorted(plan.materialized_locale_roots)
            },
            actionscript_root=(root / "sources-as3").relative_to(context.workspace.root).as_posix(),
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
            actionscript_files=actionscript.files,
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate_assembly(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Readable Assembly Result has no ActionScript Result")
        result = ReadableAssemblyResult.model_validate(payload)
        if result.actionscript_result_sha256 != context.require_upstream_digest():
            raise ValueError("Readable Assembly Result is not bound to ActionScript Result")
        if (
            result.policy_name != selected.name
            or result.policy_version != selected.version
            or result.policy_sha256 != selected.sha256
        ):
            raise ValueError("Readable Assembly Result policy does not match implementation")
        _assert_stage_root(context, result.base_root, "readable", "base")
        for language, root in result.locale_roots.items():
            _assert_stage_root(context, root, "readable", "locales", language)
        _assert_stage_root(context, result.actionscript_root, "readable", "sources-as3")

    def audit_assembly(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = ReadableAssemblyResult.model_validate(payload)
        _audit_layer_entries(
            result.files,
            base_root=context.workspace.root / result.base_root,
            locale_roots={
                language: context.workspace.root / root
                for language, root in result.locale_roots.items()
            },
            label="readable assembly",
        )
        _audit_actionscript_entries(
            result.actionscript_files,
            root=context.workspace.root / result.actionscript_root,
        )

    def execute_stubs(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.GENERATE_ENGINE_STUBS or context.upstream is None:
            raise TransformFailedError("generate-engine-stubs requires a Readable Assembly Result")
        assembly = context.upstream_as(ReadableAssemblyResult)
        client_tree = context.committed_as(Stage.ASSEMBLE_CLIENT, ClientTreeResult)
        root = context.work_directory / "readable-source"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source_roots = (
            context.workspace.root / assembly.base_root,
            *(
                context.workspace.root / assembly.locale_roots[language]
                for language in sorted(assembly.locale_roots)
            ),
        )
        context.progress("analyzing native PE metadata from committed readable roots")
        assembler._generate_engine_stubs(
            root,
            client_tree,
            context.workspace,
            source_roots=source_roots,
            progress=context.progress,
        )
        files = assembler._stub_files(root / "stubs")
        context.progress(f"generated {len(files)} stub artifacts")
        result = EngineStubsResult(
            readable_assembly_result_sha256=context.require_upstream_digest(),
            root=(root / "stubs").relative_to(context.workspace.root).as_posix(),
            files=files,
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate_stubs(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Engine Stubs Result has no Readable Assembly Result")
        result = EngineStubsResult.model_validate(payload)
        if result.readable_assembly_result_sha256 != context.require_upstream_digest():
            raise ValueError("Engine Stubs Result is not bound to Readable Assembly Result")
        _assert_stage_root(context, result.root, "readable-source", "stubs")

    def audit_stubs(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = EngineStubsResult.model_validate(payload)
        _audit_stub_entries(result.files, root=context.workspace.root / result.root)

    def execute_finalize(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.FINALIZE_READABLE or context.upstream is None:
            raise TransformFailedError("finalize-readable requires an Engine Stubs Result")
        stubs = context.upstream_as(EngineStubsResult)
        assembly = context.committed_as(Stage.ASSEMBLE_READABLE, ReadableAssemblyResult)
        tools = assembler._tool_identities_for_representations(
            {item.representation.kind for item in assembly.files},
            has_actionscript=bool(assembly.actionscript_files),
            has_engine_stubs=True,
        )
        result = ReadableResult(
            materialization_result_sha256=assembly.materialization_result_sha256,
            policy_name=assembly.policy_name,
            policy_version=assembly.policy_version,
            policy_sha256=assembly.policy_sha256,
            base_root=assembly.base_root,
            locale_roots=assembly.locale_roots,
            actionscript_root=assembly.actionscript_root,
            stubs_root=stubs.root,
            tools=tools,
            files=assembly.files,
            actionscript_files=assembly.actionscript_files,
            stub_files=stubs.files,
        )
        context.progress("finalized Readable Result references; Snapshot will verify the payload")
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate_finalize(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Readable Result has no Engine Stubs Result")
        result = ReadableResult.model_validate(payload)
        stubs = context.upstream_as(EngineStubsResult)
        assembly = context.committed_as(Stage.ASSEMBLE_READABLE, ReadableAssemblyResult)
        if result.materialization_result_sha256 != assembly.materialization_result_sha256:
            raise ValueError("Readable Result is not bound to Materialization Result")
        if (
            result.policy_name != selected.name
            or result.policy_version != selected.version
            or result.policy_sha256 != selected.sha256
        ):
            raise ValueError("Readable Result policy does not match implementation")
        if (
            result.base_root != assembly.base_root
            or result.locale_roots != assembly.locale_roots
            or result.actionscript_root != assembly.actionscript_root
        ):
            raise ValueError("Readable Result roots do not match Readable Assembly Result")
        if result.stubs_root != stubs.root:
            raise ValueError("Readable Result root does not match Engine Stubs Result")
        if result.stub_files != stubs.files:
            raise ValueError("Readable Result stubs do not match Engine Stubs Result")
        _audit_readable(result, context.workspace)

    return {
        Stage.PLAN_READABLE: StageImplementation(
            implementation_version="plan-readable-v1",
            execute=execute_plan,
            validate=validate_plan,
            configuration=configuration,
        ),
        Stage.TRANSFORM_READABLE: StageImplementation(
            implementation_version="transform-readable-v4",
            execute=execute_transforms,
            validate=validate_transforms,
            audit=audit_transforms,
            configuration=configuration,
        ),
        Stage.DECOMPILE_ACTIONSCRIPT: StageImplementation(
            implementation_version="decompile-actionscript-v1",
            execute=execute_actionscript,
            validate=validate_actionscript,
            audit=audit_actionscript,
            configuration=configuration,
        ),
        Stage.ASSEMBLE_READABLE: StageImplementation(
            implementation_version="assemble-readable-v2",
            execute=execute_assembly,
            validate=validate_assembly,
            audit=audit_assembly,
        ),
        Stage.GENERATE_ENGINE_STUBS: StageImplementation(
            implementation_version="generate-engine-stubs-v4",
            execute=execute_stubs,
            validate=validate_stubs,
            audit=audit_stubs,
            configuration=configuration,
        ),
        Stage.FINALIZE_READABLE: StageImplementation(
            implementation_version="finalize-readable-v2",
            execute=execute_finalize,
            audit=validate_finalize,
            configuration=configuration,
        ),
    }


__all__ = [
    "ActionScriptOutput",
    "ActionScriptTransformer",
    "FfdecTransformer",
    "MoCatalogueConverter",
    "PackedXmlDecoder",
    "PycTransformer",
    "ReadableAssembler",
    "ReadablePolicy",
    "TransformFailedError",
    "Uncompyle6Transformer",
    "create_readable_implementations",
]

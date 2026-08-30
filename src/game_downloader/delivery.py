from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast
from urllib.parse import urlsplit

import httpx
from pydantic import ConfigDict, Field, ValidationError

from game_downloader._json import JsonValue
from game_downloader.acquisition import ArtifactCorruptError, UnsafeArchiveError
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionPlan,
    ArtifactVerification,
    ContainerKind,
    DownloadedArtifact,
    DownloadMethod,
    DownloadResult,
    DownloadTrace,
    FrozenModel,
    ParallelRangeFallback,
    ParallelRangeFallbackReason,
    SourceHash,
    SplitAssembly,
    SplitSegment,
    Stage,
    VerificationResult,
)
from game_downloader.pipeline import StageContext, StageExecutionError, StageImplementation
from game_downloader.torrent import torrent_source_components
from game_downloader.workspace import (
    AdvisoryFileLock,
    BlobValidationError,
    CasCorruptionError,
    Workspace,
    WorkspaceCorruptError,
)

_CONTENT_RANGE = re.compile(r"^bytes (?P<start>[0-9]+)-(?P<end>[0-9]+)/(?P<total>[0-9]+)$")
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_SEVEN_ZIP_MAGIC = b"7z\xbc\xaf'\x1c"
MINIMUM_DOWNLOAD_THROUGHPUT_BYTES_PER_SECOND = 1024 * 1024


class InsufficientDiskError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("insufficient_disk", message)


class DownloadTooSlowError(StageExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__("download_too_slow", message)


class _ParallelRangeUnavailable(Exception):
    def __init__(self, fallback: ParallelRangeFallback) -> None:
        self.fallback = fallback
        super().__init__(fallback.reason.value)


def _parallel_range_unavailable(
    reason: ParallelRangeFallbackReason,
    url: str,
    *,
    response: httpx.Response | None = None,
    response_status: int | None = None,
    range_index: int | None = None,
    attempts: int = 1,
) -> _ParallelRangeUnavailable:
    effective_url = str(response.url) if response is not None else url
    return _ParallelRangeUnavailable(
        ParallelRangeFallback(
            reason=reason,
            source_host=(urlsplit(effective_url).hostname or urlsplit(url).hostname or "unknown"),
            response_status=(response.status_code if response is not None else response_status),
            range_index=range_index,
            attempts=attempts,
        )
    )


class DownloadPolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_workers: int = Field(default=6, ge=1, le=32)
    attempts_per_url: int = Field(default=4, ge=1, le=20)
    chunk_bytes: int = Field(default=1024 * 1024, ge=64 * 1024, le=16 * 1024 * 1024)
    connect_timeout_seconds: float = Field(default=15.0, gt=0)
    read_timeout_seconds: float = Field(default=60.0, gt=0)
    max_redirects: int = Field(default=5, ge=0, le=20)
    progress_interval_seconds: float = Field(default=60.0, gt=0)
    progress_percent_step: int = Field(default=10, ge=1, le=100)
    minimum_throughput_bytes_per_second: int = Field(
        default=MINIMUM_DOWNLOAD_THROUGHPUT_BYTES_PER_SECOND,
        ge=0,
    )
    minimum_throughput_window_seconds: float = Field(default=300.0, ge=10.0, le=900.0)
    parallel_range_minimum_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    parallel_range_target_bytes: int = Field(default=128 * 1024 * 1024, ge=64 * 1024)
    parallel_range_max_segments: int = Field(default=16, ge=2, le=32)
    aria2_executable: str = "aria2c"
    aria2_timeout_seconds: int = Field(default=24 * 60 * 60, ge=60)


class VerificationPolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_archive_entries: int = Field(default=1_000_000, ge=1)
    max_archive_unpacked_bytes: int = Field(default=32 * 1024 * 1024 * 1024, ge=1)
    max_compression_ratio: float = Field(default=10_000.0, ge=1.0)
    archive_timeout_seconds: int = Field(default=4 * 60 * 60, ge=10)
    seven_zip_executables: tuple[str, ...] = ("7zz", "7z", "bsdtar")
    max_workers: int = Field(default=4, ge=1, le=8)


class _PartialState(FrozenModel):
    artifact_id: str
    url: str
    bytes_written: int = Field(ge=0)
    etag: str | None = None
    last_modified: str | None = None


class _ParallelRangeState(FrozenModel):
    artifact_id: str
    url: str
    final_url: str
    size: int = Field(ge=1)
    segment_count: int = Field(ge=2, le=32)
    etag: str | None = None
    last_modified: str | None = None
    completed_bytes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ByteRange:
    index: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def _format_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    selected = float(value)
    for unit in units:
        if abs(selected) < 1024 or unit == units[-1]:
            return f"{selected:.0f} {unit}" if unit == "B" else f"{selected:.1f} {unit}"
        selected /= 1024
    raise AssertionError("unreachable")


def _format_rate(value: float) -> str:
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s")
    selected = float(value)
    for unit in units:
        if abs(selected) < 1024 or unit == units[-1]:
            return f"{selected:.3f} {unit}"
        selected /= 1024
    raise AssertionError("unreachable")


class _DownloadProgress:
    def __init__(
        self,
        artifact_sizes: Mapping[str, int],
        initial_bytes: Mapping[str, int],
        observer: Callable[[str], None],
        *,
        interval_seconds: float,
        percent_step: int,
        minimum_throughput_bytes_per_second: int = 0,
        minimum_throughput_window_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._artifact_sizes = dict(artifact_sizes)
        self._current_bytes = {
            artifact_id: initial_bytes.get(artifact_id, 0) for artifact_id in self._artifact_sizes
        }
        self._total_bytes = sum(self._artifact_sizes.values())
        self._observer = observer
        self._interval_seconds = interval_seconds
        self._percent_step = percent_step
        self._minimum_throughput_bytes_per_second = minimum_throughput_bytes_per_second
        self._minimum_throughput_window_seconds = minimum_throughput_window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        started_at = clock()
        self._last_report_at = started_at
        self._last_report_downloaded = -1
        self._interval_transferred = 0
        self._total_transferred = 0
        self._throughput_samples: deque[tuple[float, int]] = deque([(started_at, 0)])
        self._too_slow_message: str | None = None
        percentage = self._percentage(sum(self._current_bytes.values()))
        self._next_percent = percent_step
        while self._next_percent <= percentage:
            self._next_percent += percent_step

    def started(self, artifact_count: int) -> None:
        with self._lock:
            downloaded = sum(self._current_bytes.values())
            self._observer(
                f"Downloading {artifact_count} artifacts: "
                f"{_format_bytes(downloaded)} / {_format_bytes(self._total_bytes)} available"
            )

    def update(
        self,
        artifact_id: str,
        current_bytes: int,
        transferred_bytes: int = 0,
        *,
        source_host: str | None = None,
    ) -> None:
        expected_size = self._artifact_sizes[artifact_id]
        if not 0 <= current_bytes <= expected_size:
            raise ValueError(
                f"invalid progress for Artifact {artifact_id}: "
                f"{current_bytes} not in [0, {expected_size}]"
            )
        if transferred_bytes < 0:
            raise ValueError("transferred progress cannot be negative")
        with self._lock:
            if self._too_slow_message is not None:
                raise DownloadTooSlowError(self._too_slow_message)
            self._current_bytes[artifact_id] = current_bytes
            self._interval_transferred += transferred_bytes
            self._total_transferred += transferred_bytes
            now = self._clock()
            downloaded = sum(self._current_bytes.values())
            percentage = self._percentage(downloaded)
            if (
                now - self._last_report_at >= self._interval_seconds
                or percentage >= self._next_percent
            ):
                self._report(now, downloaded)
            self._check_minimum_throughput(
                now,
                downloaded,
                artifact_id=artifact_id,
                source_host=source_host,
            )

    def completed(self) -> None:
        with self._lock:
            now = self._clock()
            downloaded = sum(self._current_bytes.values())
            if downloaded != self._total_bytes:
                raise ValueError(
                    f"download progress ended at {downloaded} of {self._total_bytes} bytes"
                )
            if self._last_report_downloaded != downloaded:
                self._report(now, downloaded)

    def _report(self, now: float, downloaded: int) -> None:
        elapsed = max(0.0, now - self._last_report_at)
        speed = self._interval_transferred / elapsed if elapsed > 0 else 0.0
        percentage = self._percentage(downloaded)
        self._observer(
            f"Download progress: {_format_bytes(downloaded)} / "
            f"{_format_bytes(self._total_bytes)} ({percentage:.1f}%); interval: "
            f"{_format_bytes(self._interval_transferred)} in {elapsed:.1f}s "
            f"({_format_bytes(speed)}/s)"
        )
        self._last_report_at = now
        self._last_report_downloaded = downloaded
        self._interval_transferred = 0
        while self._next_percent <= percentage:
            self._next_percent += self._percent_step

    def _percentage(self, downloaded: int) -> float:
        return 100.0 if self._total_bytes == 0 else downloaded * 100 / self._total_bytes

    def _check_minimum_throughput(
        self,
        now: float,
        downloaded: int,
        *,
        artifact_id: str,
        source_host: str | None,
    ) -> None:
        minimum = self._minimum_throughput_bytes_per_second
        if minimum <= 0 or downloaded >= self._total_bytes:
            return
        samples = self._throughput_samples
        samples.append((now, self._total_transferred))
        cutoff = now - self._minimum_throughput_window_seconds
        while len(samples) > 1 and samples[1][0] <= cutoff:
            samples.popleft()
        window_started_at, window_started_bytes = samples[0]
        elapsed = now - window_started_at
        if elapsed < self._minimum_throughput_window_seconds:
            return
        speed = (self._total_transferred - window_started_bytes) / elapsed
        if speed >= minimum:
            return
        remaining = self._total_bytes - downloaded
        source = f" from {source_host}" if source_host is not None else ""
        self._too_slow_message = (
            f"aggregate download throughput averaged {_format_rate(speed)} over "
            f"{elapsed:.1f}s, below the configured {_format_rate(minimum)} minimum; "
            f"{_format_bytes(remaining)} remain; current Artifact {artifact_id}{source}"
        )
        raise DownloadTooSlowError(self._too_slow_message)


def _split_byte_ranges(
    size: int,
    *,
    target_bytes: int,
    max_segments: int,
) -> tuple[_ByteRange, ...]:
    if size < 2:
        return ()
    segment_count = min(max_segments, size, max(2, (size + target_bytes - 1) // target_bytes))
    base_size, extra = divmod(size, segment_count)
    ranges: list[_ByteRange] = []
    start = 0
    for index in range(segment_count):
        segment_size = base_size + (1 if index < extra else 0)
        end = start + segment_size - 1
        ranges.append(_ByteRange(index=index, start=start, end=end))
        start = end + 1
    assert start == size
    return tuple(ranges)


class _ParallelArtifactProgress:
    def __init__(
        self,
        artifact_id: str,
        ranges: Sequence[_ByteRange],
        initial_bytes: Sequence[int],
        progress: _DownloadProgress | None,
        state: _ParallelRangeState,
        state_path: Path,
        workspace: Workspace,
    ) -> None:
        if len(ranges) != len(initial_bytes):
            raise ValueError("parallel range progress does not match its ranges")
        self._artifact_id = artifact_id
        self._range_sizes = tuple(item.size for item in ranges)
        self._current_bytes = list(initial_bytes)
        self._progress = progress
        self._state = state
        self._state_path = state_path
        self._workspace = workspace
        self._lock = threading.Lock()
        if progress is not None:
            progress.update(artifact_id, sum(initial_bytes))

    def update(
        self,
        index: int,
        current_bytes: int,
        transferred_bytes: int,
        *,
        source_host: str | None,
    ) -> None:
        if not 0 <= current_bytes <= self._range_sizes[index]:
            raise ValueError("parallel range progress exceeds its declared size")
        with self._lock:
            if current_bytes < self._current_bytes[index]:
                raise ValueError("parallel range progress cannot move backwards")
            self._current_bytes[index] = current_bytes
            if self._progress is not None:
                self._progress.update(
                    self._artifact_id,
                    sum(self._current_bytes),
                    transferred_bytes,
                    source_host=source_host,
                )

    def current(self, index: int) -> int:
        with self._lock:
            return self._current_bytes[index]

    def persist(self) -> None:
        with self._lock:
            current_state = self._state.model_copy(
                update={"completed_bytes": tuple(self._current_bytes)}
            )
            self._workspace.atomic_write_json(
                self._state_path,
                current_state.model_dump(mode="json"),
            )


def _artifact_cache_key(artifact: AcquisitionArtifact) -> str:
    return artifact.artifact_id.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class _PayloadIntegrity:
    sha256: str
    source_hash_verified: bool


class _PayloadHasher:
    def __init__(self, source_hash: SourceHash | None) -> None:
        self._source_hash = source_hash
        self._own = hashlib.sha256()
        self._declared = hashlib.new(source_hash.algorithm) if source_hash is not None else None

    def update(self, chunk: bytes) -> None:
        self._own.update(chunk)
        if self._declared is not None:
            self._declared.update(chunk)

    def update_file(self, path: Path) -> None:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.update(chunk)

    def finish(self) -> _PayloadIntegrity:
        source_verified = self._source_hash is None or (
            self._declared is not None and self._declared.hexdigest() == self._source_hash.value
        )
        return _PayloadIntegrity(
            sha256=self._own.hexdigest(),
            source_hash_verified=source_verified,
        )


def _hash_file(path: Path, source_hash: SourceHash | None) -> tuple[str, bool]:
    hasher = _PayloadHasher(source_hash)
    hasher.update_file(path)
    integrity = hasher.finish()
    return integrity.sha256, integrity.source_hash_verified


def _validate_regular_file(path: Path, *, expected_size: int, writable: bool) -> None:
    path_stat = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        raise ArtifactCorruptError(f"Artifact path is not a regular file: {path}")
    if not writable and stat.S_IMODE(path_stat.st_mode) & 0o222:
        raise ArtifactCorruptError(f"committed Artifact is unexpectedly writable: {path}")
    if path_stat.st_size != expected_size:
        raise ArtifactCorruptError(
            f"Artifact size mismatch for {path}: {path_stat.st_size} != {expected_size}"
        )


def validate_downloaded_artifact_structure(
    workspace: Workspace,
    item: DownloadedArtifact,
) -> Path:
    path = workspace.blobs.path_for(item.blob_sha256)
    expected_path = f"cache/blobs/sha256/{item.blob_sha256[:2]}/{item.blob_sha256}"
    if item.blob_path != expected_path:
        raise ArtifactCorruptError("Artifact receipt contains a non-canonical CAS path")
    _validate_regular_file(path, expected_size=item.blob_size, writable=False)
    return path


def validate_downloaded_artifact(workspace: Workspace, item: DownloadedArtifact) -> None:
    path = validate_downloaded_artifact_structure(workspace, item)
    own_sha256, source_verified = _hash_file(path, item.artifact.source_hash)
    if own_sha256 != item.blob_sha256:
        raise ArtifactCorruptError("Artifact CAS content does not match its SHA-256 key")
    if item.artifact.source_hash is not None and not source_verified:
        raise ArtifactCorruptError("Artifact CAS content does not match its declared source hash")


class _Aria2Adapter:
    def __init__(self, policy: DownloadPolicy) -> None:
        self._policy = policy

    def download(
        self,
        artifact: AcquisitionArtifact,
        plan: AcquisitionPlan,
        workspace: Workspace,
        partial_directory: Path,
        payload_path: Path,
    ) -> DownloadTrace:
        executable = shutil.which(self._policy.aria2_executable)
        if executable is None:
            raise StageExecutionError(
                "source_unavailable",
                "Artifact has no direct web seed and aria2c is unavailable",
            )
        descriptor = next(
            (
                item
                for item in plan.descriptors
                if item.descriptor_sha256 == artifact.torrent_descriptor_sha256
            ),
            None,
        )
        if descriptor is None:
            raise ArtifactCorruptError("Artifact references a missing torrent descriptor")
        matching_indices = [
            index
            for index, torrent_file in enumerate(descriptor.metainfo.files, start=1)
            if torrent_file.path == artifact.path and not torrent_file.padding
        ]
        if len(matching_indices) != 1:
            raise ArtifactCorruptError("Artifact path is ambiguous in its torrent descriptor")
        descriptor_path = workspace.blobs.path_for(descriptor.blob_sha256)
        torrent_directory = partial_directory / "torrent"
        torrent_directory.mkdir(parents=True, exist_ok=True)
        command = [
            executable,
            "--no-conf=true",
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--check-integrity=true",
            "--seed-time=0",
            "--enable-dht=false",
            "--enable-dht6=false",
            "--enable-peer-exchange=false",
            "--bt-enable-lpd=false",
            "--bt-exclude-tracker=*",
            f"--select-file={matching_indices[0]}",
            f"--dir={torrent_directory}",
            str(descriptor_path),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=self._policy.aria2_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StageExecutionError(
                "source_unavailable", f"aria2 torrent fallback failed: {exc}"
            ) from exc
        if completed.returncode != 0:
            diagnostic = completed.stdout[-4096:].decode("utf-8", errors="replace")
            raise StageExecutionError(
                "source_unavailable",
                f"aria2 torrent fallback exited {completed.returncode}: {diagnostic}",
            )
        torrent_file = descriptor.metainfo.files[matching_indices[0] - 1]
        components = torrent_source_components(descriptor.metainfo, torrent_file)
        produced = torrent_directory.joinpath(*(os.fsdecode(component) for component in components))
        _validate_regular_file(produced, expected_size=artifact.size, writable=True)
        os.replace(produced, payload_path)
        return DownloadTrace(method=DownloadMethod.TORRENT, attempts=1)


class ArtifactDownloader:
    def __init__(
        self,
        policy: DownloadPolicy | None = None,
        progress_observer: Callable[[str], None] | None = None,
    ) -> None:
        self._policy = policy or DownloadPolicy()
        self._aria2 = _Aria2Adapter(self._policy)
        self._progress_observer = progress_observer or (lambda _message: None)

    def download(self, plan: AcquisitionPlan, workspace: Workspace) -> DownloadResult:
        workspace.initialize()
        artifacts = tuple(artifact for part in plan.parts for artifact in part.artifacts)
        cached = self._validated_cached_artifacts(artifacts, workspace)
        cached_bytes = sum(item.blob_size for item in cached.values())
        progress = _DownloadProgress(
            {artifact.artifact_id: artifact.size for artifact in artifacts},
            {artifact_id: item.blob_size for artifact_id, item in cached.items()},
            self._progress_observer,
            interval_seconds=self._policy.progress_interval_seconds,
            percent_step=self._policy.progress_percent_step,
            minimum_throughput_bytes_per_second=(self._policy.minimum_throughput_bytes_per_second),
            minimum_throughput_window_seconds=(self._policy.minimum_throughput_window_seconds),
        )
        progress.started(len(artifacts))
        remaining_required_bytes = max(
            0,
            plan.disk_space.required_free_bytes - cached_bytes,
        )
        free_bytes = shutil.disk_usage(workspace.root).free
        if free_bytes < remaining_required_bytes:
            raise InsufficientDiskError(
                "remaining Acquisition Plan requires "
                f"{remaining_required_bytes} free bytes after validating "
                f"{len(cached)} cached Artifacts, only {free_bytes} available"
            )
        timeout = httpx.Timeout(
            connect=self._policy.connect_timeout_seconds,
            read=self._policy.read_timeout_seconds,
            write=self._policy.read_timeout_seconds,
            pool=self._policy.connect_timeout_seconds,
        )
        range_workers = min(64, self._policy.max_workers * 3)
        scheduled_artifacts = tuple(
            sorted(
                artifacts,
                key=lambda item: (-item.size, item.artifact_id.encode("ascii")),
            )
        )
        with (
            httpx.Client(
                follow_redirects=True,
                max_redirects=self._policy.max_redirects,
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=self._policy.max_workers + range_workers,
                    max_keepalive_connections=self._policy.max_workers + range_workers,
                ),
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "game-downloader/0.1 resumable downloader",
                },
            ) as client,
            ThreadPoolExecutor(
                max_workers=range_workers,
                thread_name_prefix="artifact-range",
            ) as range_executor,
        ):
            if self._policy.max_workers == 1:
                scheduled_downloads = tuple(
                    self._download_one(
                        artifact,
                        plan,
                        workspace,
                        client,
                        cached.get(artifact.artifact_id),
                        progress,
                        range_executor,
                    )
                    for artifact in scheduled_artifacts
                )
            else:
                with ThreadPoolExecutor(
                    max_workers=self._policy.max_workers,
                    thread_name_prefix="artifact-download",
                ) as executor:
                    scheduled_downloads = tuple(
                        executor.map(
                            lambda artifact: self._download_one(
                                artifact,
                                plan,
                                workspace,
                                client,
                                cached.get(artifact.artifact_id),
                                progress,
                                range_executor,
                            ),
                            scheduled_artifacts,
                        )
                    )
        downloads_by_id = {item.artifact.artifact_id: item for item in scheduled_downloads}
        downloaded = tuple(downloads_by_id[artifact.artifact_id] for artifact in artifacts)
        progress.completed()
        return DownloadResult(
            acquisition_plan_sha256="sha256:" + "0" * 64,
            artifacts=downloaded,
            downloaded_bytes=sum(item.blob_size for item in downloaded),
            reused_artifacts=sum(item.reused for item in downloaded),
        )

    @staticmethod
    def _validated_cached_artifacts(
        artifacts: Sequence[AcquisitionArtifact],
        workspace: Workspace,
    ) -> dict[str, DownloadedArtifact]:
        cached: dict[str, DownloadedArtifact] = {}
        for artifact in artifacts:
            partial_directory = workspace.partial_root / _artifact_cache_key(artifact)
            if not partial_directory.exists():
                continue
            with AdvisoryFileLock(
                partial_directory / "lock",
                label=f"Artifact {artifact.artifact_id}",
            ).acquire(blocking=True):
                receipt = ArtifactDownloader._load_receipt(
                    partial_directory,
                    artifact,
                    workspace,
                )
            if receipt is not None:
                cached[artifact.artifact_id] = receipt
        return cached

    def _download_one(
        self,
        artifact: AcquisitionArtifact,
        plan: AcquisitionPlan,
        workspace: Workspace,
        client: httpx.Client,
        cached: DownloadedArtifact | None = None,
        progress: _DownloadProgress | None = None,
        range_executor: ThreadPoolExecutor | None = None,
    ) -> DownloadedArtifact:
        partial_directory = workspace.partial_root / _artifact_cache_key(artifact)
        partial_directory.mkdir(parents=True, exist_ok=True)
        with AdvisoryFileLock(
            partial_directory / "lock", label=f"Artifact {artifact.artifact_id}"
        ).acquire(blocking=True):
            receipt = cached or self._load_receipt(partial_directory, artifact, workspace)
            if receipt is not None:
                if progress is not None:
                    progress.update(artifact.artifact_id, artifact.size)
                return receipt
            payload_path = partial_directory / "payload.part"
            state_path = partial_directory / "state.json"
            if payload_path.exists():
                _validate_regular_file(
                    payload_path, expected_size=payload_path.stat().st_size, writable=True
                )
                if payload_path.stat().st_size > artifact.size:
                    payload_path.unlink()
                    state_path.unlink(missing_ok=True)
            if artifact.source_urls:
                trace, integrity = self._download_web_seed(
                    artifact,
                    client,
                    workspace,
                    payload_path,
                    state_path,
                    progress,
                    range_executor,
                )
            else:
                trace = self._aria2.download(
                    artifact, plan, workspace, partial_directory, payload_path
                )
                if progress is not None:
                    progress.update(artifact.artifact_id, artifact.size, artifact.size)
                own_sha256, source_verified = _hash_file(payload_path, artifact.source_hash)
                integrity = _PayloadIntegrity(
                    sha256=own_sha256,
                    source_hash_verified=source_verified,
                )
            _validate_regular_file(payload_path, expected_size=artifact.size, writable=True)
            if artifact.source_hash is not None and not integrity.source_hash_verified:
                payload_path.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                raise ArtifactCorruptError(
                    f"source hash mismatch for Artifact {artifact.artifact_id}"
                )
            try:
                commit = workspace.blobs.adopt_verified_file(
                    payload_path,
                    verified_sha256=integrity.sha256,
                    expected_size=artifact.size,
                )
            except (BlobValidationError, CasCorruptionError) as exc:
                raise ArtifactCorruptError(str(exc)) from exc
            receipt = DownloadedArtifact(
                artifact=artifact,
                blob_sha256=commit.sha256,
                blob_size=commit.size,
                blob_path=commit.relative_path,
                source_hash_verified=integrity.source_hash_verified,
                reused=False,
                transport=trace,
            )
            workspace.atomic_write_json(
                partial_directory / "receipt.json", receipt.model_dump(mode="json")
            )
            state_path.unlink(missing_ok=True)
            if progress is not None:
                progress.update(artifact.artifact_id, artifact.size)
            return receipt

    @staticmethod
    def _load_receipt(
        partial_directory: Path,
        artifact: AcquisitionArtifact,
        workspace: Workspace,
    ) -> DownloadedArtifact | None:
        receipt_path = partial_directory / "receipt.json"
        try:
            receipt = DownloadedArtifact.model_validate_json(workspace.read_bytes(receipt_path))
        except FileNotFoundError:
            return None
        except (ValidationError, WorkspaceCorruptError) as exc:
            raise ArtifactCorruptError(f"invalid Artifact receipt: {exc}") from exc
        if (
            receipt.artifact.artifact_id != artifact.artifact_id
            or receipt.artifact.size != artifact.size
            or receipt.artifact.source_hash != artifact.source_hash
            or receipt.artifact.torrent_descriptor_sha256 != artifact.torrent_descriptor_sha256
        ):
            raise ArtifactCorruptError("Artifact receipt does not match the Acquisition Plan")
        current = receipt.model_copy(update={"artifact": artifact, "reused": True})
        validate_downloaded_artifact(workspace, current)
        return current

    def _download_web_seed(
        self,
        artifact: AcquisitionArtifact,
        client: httpx.Client,
        workspace: Workspace,
        payload_path: Path,
        state_path: Path,
        progress: _DownloadProgress | None,
        range_executor: ThreadPoolExecutor | None,
    ) -> tuple[DownloadTrace, _PayloadIntegrity]:
        attempts = 0
        range_state_path = payload_path.parent / "range-state.json"
        initial_size = payload_path.stat().st_size if payload_path.exists() else 0
        last_error = "no web seed was attempted"
        saw_corrupt_payload = False
        parallel_fallbacks: list[ParallelRangeFallback] = []
        if (
            payload_path.exists()
            and initial_size == artifact.size
            and not range_state_path.exists()
        ):
            own_sha256, source_verified = _hash_file(payload_path, artifact.source_hash)
            if artifact.source_hash is None or source_verified:
                if progress is not None:
                    progress.update(artifact.artifact_id, artifact.size)
                state = self._load_state(state_path, workspace)
                completed_url = state.url if state is not None else artifact.source_urls[0]
                return (
                    DownloadTrace(
                        method=DownloadMethod.WEB_SEED,
                        requested_url=completed_url,
                        final_url=completed_url,
                        etag=state.etag if state is not None else None,
                        last_modified=state.last_modified if state is not None else None,
                        resumed_from=initial_size,
                        attempts=1,
                    ),
                    _PayloadIntegrity(
                        sha256=own_sha256,
                        source_hash_verified=source_verified,
                    ),
                )
            payload_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            initial_size = 0
            saw_corrupt_payload = True
            if progress is not None:
                progress.update(artifact.artifact_id, 0)
        if (
            range_executor is not None
            and (
                artifact.size >= self._policy.parallel_range_minimum_bytes
                or range_state_path.exists()
            )
            and (not payload_path.exists() or range_state_path.exists())
        ):
            for url in artifact.source_urls:
                self._validate_web_seed_url(url)
                try:
                    ranged = self._try_parallel_range_download(
                        artifact,
                        url,
                        client,
                        workspace,
                        payload_path,
                        progress,
                        range_executor,
                    )
                except _ParallelRangeUnavailable as exc:
                    last_error = str(exc)
                    parallel_fallbacks.append(exc.fallback)
                    self._clear_parallel_range_state(payload_path.parent)
                    payload_path.unlink(missing_ok=True)
                    state_path.unlink(missing_ok=True)
                    initial_size = 0
                    if progress is not None:
                        progress.update(artifact.artifact_id, 0)
                    self._progress_observer(
                        f"Artifact {artifact.artifact_id}: parallel Range unavailable from "
                        f"{exc.fallback.source_host} ({exc.fallback.reason.value}); "
                        "single-stream HTTP fallback remains enabled"
                    )
                    continue
                if ranged is None:
                    continue
                range_trace, range_integrity = ranged
                if artifact.source_hash is not None and not range_integrity.source_hash_verified:
                    last_error = "parallel web seed payload failed its declared source hash"
                    fallback = ParallelRangeFallback(
                        reason=ParallelRangeFallbackReason.PAYLOAD_HASH_MISMATCH,
                        source_host=(urlsplit(range_trace.final_url or url).hostname or "unknown"),
                        attempts=range_trace.attempts,
                        discarded_bytes=artifact.size,
                    )
                    parallel_fallbacks.append(fallback)
                    payload_path.unlink(missing_ok=True)
                    state_path.unlink(missing_ok=True)
                    initial_size = 0
                    if progress is not None:
                        progress.update(artifact.artifact_id, 0)
                    self._progress_observer(
                        f"Artifact {artifact.artifact_id}: parallel Range unavailable from "
                        f"{fallback.source_host} ({fallback.reason.value}); "
                        "single-stream HTTP fallback remains enabled"
                    )
                    continue
                return (
                    range_trace.model_copy(
                        update={"parallel_range_fallbacks": tuple(parallel_fallbacks)}
                    ),
                    range_integrity,
                )
        for url in artifact.source_urls:
            self._validate_web_seed_url(url)
            for _attempt in range(self._policy.attempts_per_url):
                attempts += 1
                try:
                    complete, trace, integrity = self._http_attempt(
                        artifact,
                        url,
                        client,
                        workspace,
                        payload_path,
                        state_path,
                        progress,
                    )
                except (httpx.HTTPError, OSError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if not complete:
                    last_error = "web seed response ended before the declared Artifact size"
                    continue
                assert integrity is not None
                if artifact.source_hash is not None and not integrity.source_hash_verified:
                    saw_corrupt_payload = True
                    last_error = "web seed payload failed its declared source hash"
                    payload_path.unlink(missing_ok=True)
                    state_path.unlink(missing_ok=True)
                    if progress is not None:
                        progress.update(artifact.artifact_id, 0)
                    continue
                return (
                    trace.model_copy(
                        update={
                            "attempts": attempts,
                            "resumed_from": initial_size,
                            "parallel_range_fallbacks": tuple(parallel_fallbacks),
                        }
                    ),
                    integrity,
                )
        if saw_corrupt_payload:
            raise ArtifactCorruptError(
                "all complete sources were corrupt for Artifact "
                f"{artifact.artifact_id}: {last_error}"
            )
        raise StageExecutionError(
            "source_unavailable",
            f"Artifact {artifact.artifact_id} was unavailable after {attempts} attempts: "
            f"{last_error}",
        )

    @staticmethod
    def _validate_web_seed_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ArtifactCorruptError("Acquisition Plan contains a non-HTTPS web seed URL")

    def _try_parallel_range_download(
        self,
        artifact: AcquisitionArtifact,
        url: str,
        client: httpx.Client,
        workspace: Workspace,
        payload_path: Path,
        progress: _DownloadProgress | None,
        executor: ThreadPoolExecutor,
    ) -> tuple[DownloadTrace, _PayloadIntegrity] | None:
        try:
            probe = client.head(url)
        except (httpx.HTTPError, OSError):
            raise _parallel_range_unavailable(
                ParallelRangeFallbackReason.PROBE_FAILED,
                url,
            ) from None
        if probe.status_code != 200:
            raise _parallel_range_unavailable(
                ParallelRangeFallbackReason.PROBE_STATUS,
                url,
                response=probe,
            )
        final_url = str(probe.url)
        if urlsplit(final_url).scheme != "https":
            raise ArtifactCorruptError("web seed redirected a range probe away from HTTPS")
        try:
            declared_size = int(probe.headers.get("content-length", ""))
        except ValueError:
            raise _parallel_range_unavailable(
                ParallelRangeFallbackReason.PROBE_LENGTH_UNAVAILABLE,
                url,
                response=probe,
            ) from None
        if declared_size != artifact.size:
            raise _parallel_range_unavailable(
                ParallelRangeFallbackReason.PROBE_SIZE_MISMATCH,
                url,
                response=probe,
            )
        if "bytes" not in probe.headers.get("accept-ranges", "").lower():
            raise _parallel_range_unavailable(
                ParallelRangeFallbackReason.RANGE_NOT_ADVERTISED,
                url,
                response=probe,
            )
        etag = probe.headers.get("etag")
        last_modified = probe.headers.get("last-modified")
        if etag is None and last_modified is None:
            raise _parallel_range_unavailable(
                ParallelRangeFallbackReason.VALIDATOR_UNAVAILABLE,
                url,
                response=probe,
            )
        ranges = _split_byte_ranges(
            artifact.size,
            target_bytes=self._policy.parallel_range_target_bytes,
            max_segments=self._policy.parallel_range_max_segments,
        )
        if len(ranges) < 2:
            return None

        state = _ParallelRangeState(
            artifact_id=artifact.artifact_id,
            url=url,
            final_url=final_url,
            size=artifact.size,
            segment_count=len(ranges),
            etag=etag,
            last_modified=last_modified,
            completed_bytes=(0,) * len(ranges),
        )
        state_path = payload_path.parent / "range-state.json"
        previous_state = self._load_parallel_range_state(state_path, workspace)
        previous_identity = (
            previous_state.model_copy(update={"completed_bytes": state.completed_bytes})
            if previous_state is not None
            else None
        )
        if previous_identity != state:
            self._clear_parallel_range_state(payload_path.parent)
            payload_path.unlink(missing_ok=True)
            workspace.atomic_write_json(state_path, state.model_dump(mode="json"))
            previous_state = state
        assert previous_state is not None
        if len(previous_state.completed_bytes) != len(ranges) or any(
            not 0 <= current <= item.size
            for item, current in zip(ranges, previous_state.completed_bytes, strict=True)
        ):
            raise ArtifactCorruptError("parallel range state contains invalid progress")
        initial_bytes = previous_state.completed_bytes
        if payload_path.exists():
            _validate_regular_file(
                payload_path,
                expected_size=artifact.size,
                writable=True,
            )
        else:
            with payload_path.open("w+b") as output:
                output.truncate(artifact.size)
                output.flush()
                os.fsync(output.fileno())
        resumed_from = sum(initial_bytes)
        range_progress = _ParallelArtifactProgress(
            artifact.artifact_id,
            ranges,
            initial_bytes,
            progress,
            previous_state,
            state_path,
            workspace,
        )
        self._progress_observer(
            f"Artifact {artifact.artifact_id}: downloading {_format_bytes(artifact.size)} "
            f"in {len(ranges)} validated HTTP ranges from {urlsplit(final_url).hostname}"
        )
        with payload_path.open("r+b", buffering=0) as output:
            futures = [
                executor.submit(
                    self._download_range,
                    artifact,
                    url,
                    client,
                    item,
                    output.fileno(),
                    previous_state,
                    range_progress,
                )
                for item in ranges
            ]
            try:
                range_attempts = [future.result() for future in futures]
            except _ParallelRangeUnavailable as exc:
                for future in futures:
                    future.cancel()
                wait(futures)
                range_progress.persist()
                discarded_bytes = sum(range_progress.current(item.index) for item in ranges)
                raise _ParallelRangeUnavailable(
                    exc.fallback.model_copy(update={"discarded_bytes": discarded_bytes})
                ) from exc
            except BaseException:
                for future in futures:
                    future.cancel()
                wait(futures)
                raise
            os.fsync(output.fileno())
        range_progress.persist()
        own_sha256, source_verified = _hash_file(payload_path, artifact.source_hash)
        integrity = _PayloadIntegrity(
            sha256=own_sha256,
            source_hash_verified=source_verified,
        )
        self._clear_parallel_range_state(payload_path.parent)
        trace = self._trace(probe, url).model_copy(
            update={
                "attempts": max(range_attempts),
                "parallel_segments": len(ranges),
                "resumed_from": resumed_from,
            }
        )
        return trace, integrity

    def _download_range(
        self,
        artifact: AcquisitionArtifact,
        url: str,
        client: httpx.Client,
        byte_range: _ByteRange,
        output_fd: int,
        state: _ParallelRangeState,
        progress: _ParallelArtifactProgress,
    ) -> int:
        attempts = 0
        last_response_status: int | None = None
        try:
            while attempts < self._policy.attempts_per_url:
                attempts += 1
                current_size = progress.current(byte_range.index)
                if current_size == byte_range.size:
                    return attempts
                headers = {
                    "Range": (f"bytes={byte_range.start + current_size}-{byte_range.end}"),
                    "If-Range": state.etag or cast(str, state.last_modified),
                }
                try:
                    with client.stream("GET", url, headers=headers) as response:
                        last_response_status = response.status_code
                        if (
                            response.status_code in {404, 408, 425, 429}
                            or response.status_code >= 500
                        ):
                            continue
                        if response.status_code != 206:
                            raise _parallel_range_unavailable(
                                ParallelRangeFallbackReason.RANGE_RESPONSE_NOT_PARTIAL,
                                url,
                                response=response,
                                range_index=byte_range.index,
                                attempts=attempts,
                            )
                        raw_range = response.headers.get("content-range", "")
                        matched = _CONTENT_RANGE.fullmatch(raw_range)
                        expected_start = byte_range.start + current_size
                        if (
                            matched is None
                            or int(matched.group("start")) != expected_start
                            or int(matched.group("end")) != byte_range.end
                            or int(matched.group("total")) != artifact.size
                        ):
                            raise _parallel_range_unavailable(
                                ParallelRangeFallbackReason.INVALID_CONTENT_RANGE,
                                url,
                                response=response,
                                range_index=byte_range.index,
                                attempts=attempts,
                            )
                        if state.etag is not None:
                            validator_changed = response.headers.get("etag") != state.etag
                        else:
                            validator_changed = (
                                response.headers.get("last-modified") != state.last_modified
                            )
                        if validator_changed:
                            raise _parallel_range_unavailable(
                                ParallelRangeFallbackReason.VALIDATOR_CHANGED,
                                url,
                                response=response,
                                range_index=byte_range.index,
                                attempts=attempts,
                            )
                        source_host = urlsplit(str(response.url)).hostname
                        written = current_size
                        for received in response.iter_raw():
                            for offset in range(0, len(received), self._policy.chunk_bytes):
                                chunk = received[offset : offset + self._policy.chunk_bytes]
                                written += len(chunk)
                                if written > byte_range.size:
                                    raise _parallel_range_unavailable(
                                        ParallelRangeFallbackReason.RANGE_LENGTH_MISMATCH,
                                        url,
                                        response=response,
                                        range_index=byte_range.index,
                                        attempts=attempts,
                                    )
                                absolute_offset = byte_range.start + written - len(chunk)
                                remaining = memoryview(chunk)
                                while remaining:
                                    persisted = os.pwrite(output_fd, remaining, absolute_offset)
                                    if persisted <= 0:
                                        raise OSError("positional Artifact write made no progress")
                                    absolute_offset += persisted
                                    remaining = remaining[persisted:]
                                progress.update(
                                    byte_range.index,
                                    written,
                                    len(chunk),
                                    source_host=source_host,
                                )
                except (httpx.HTTPError, OSError):
                    continue
                current_size = progress.current(byte_range.index)
                if current_size == byte_range.size:
                    return attempts
        finally:
            progress.persist()
        raise _parallel_range_unavailable(
            ParallelRangeFallbackReason.RANGE_REQUEST_FAILED,
            url,
            response_status=last_response_status,
            range_index=byte_range.index,
            attempts=attempts,
        )

    @staticmethod
    def _clear_parallel_range_state(partial_directory: Path) -> None:
        for path in partial_directory.glob("range-[0-9][0-9][0-9][0-9].part"):
            path.unlink(missing_ok=True)
        (partial_directory / "range-state.json").unlink(missing_ok=True)

    @staticmethod
    def _load_parallel_range_state(
        state_path: Path,
        workspace: Workspace,
    ) -> _ParallelRangeState | None:
        try:
            return _ParallelRangeState.model_validate_json(workspace.read_bytes(state_path))
        except FileNotFoundError:
            return None
        except (ValidationError, WorkspaceCorruptError) as exc:
            raise ArtifactCorruptError(f"invalid parallel range state: {exc}") from exc

    def _http_attempt(
        self,
        artifact: AcquisitionArtifact,
        url: str,
        client: httpx.Client,
        workspace: Workspace,
        payload_path: Path,
        state_path: Path,
        progress: _DownloadProgress | None,
    ) -> tuple[bool, DownloadTrace, _PayloadIntegrity | None]:
        existing_size = payload_path.stat().st_size if payload_path.exists() else 0
        state = self._load_state(state_path, workspace)
        can_resume = (
            existing_size > 0
            and state is not None
            and state.artifact_id == artifact.artifact_id
            and state.url == url
            and state.bytes_written == existing_size
            and (state.etag is not None or state.last_modified is not None)
        )
        if existing_size and not can_resume:
            payload_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            existing_size = 0
            state = None
        if progress is not None:
            progress.update(artifact.artifact_id, existing_size)
        headers: dict[str, str] = {}
        if existing_size:
            headers["Range"] = f"bytes={existing_size}-"
            assert state is not None
            validator = state.etag or state.last_modified
            assert validator is not None
            headers["If-Range"] = validator

        with client.stream("GET", url, headers=headers) as response:
            if response.status_code in {404, 408, 425, 429} or response.status_code >= 500:
                return False, self._trace(response, url), None
            if response.status_code not in {200, 206}:
                raise httpx.HTTPStatusError(
                    f"unexpected HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response_etag = response.headers.get("etag")
            response_last_modified = response.headers.get("last-modified")
            append = existing_size > 0 and response.status_code == 206
            if append:
                raw_range = response.headers.get("content-range", "")
                matched = _CONTENT_RANGE.fullmatch(raw_range)
                if (
                    matched is None
                    or int(matched.group("start")) != existing_size
                    or int(matched.group("total")) != artifact.size
                ):
                    raise ArtifactCorruptError("web seed returned an invalid Content-Range")
                assert state is not None
                validator_changed = (
                    response_etag != state.etag
                    if state.etag is not None
                    else response_last_modified != state.last_modified
                )
                if validator_changed:
                    payload_path.unlink(missing_ok=True)
                    state_path.unlink(missing_ok=True)
                    if progress is not None:
                        progress.update(artifact.artifact_id, 0)
                    return False, self._trace(response, url), None
                mode = "ab"
            else:
                existing_size = 0
                mode = "wb"
                if response.status_code == 206:
                    raw_range = response.headers.get("content-range", "")
                    matched = _CONTENT_RANGE.fullmatch(raw_range)
                    if (
                        matched is None
                        or int(matched.group("start")) != 0
                        or int(matched.group("total")) != artifact.size
                    ):
                        raise ArtifactCorruptError("web seed returned an invalid initial range")

            workspace.atomic_write_json(
                state_path,
                _PartialState(
                    artifact_id=artifact.artifact_id,
                    url=url,
                    bytes_written=existing_size,
                    etag=response_etag,
                    last_modified=response_last_modified,
                ).model_dump(mode="json"),
            )
            hasher = _PayloadHasher(artifact.source_hash)
            if existing_size:
                hasher.update_file(payload_path)
            try:
                with payload_path.open(mode) as output:
                    self._stream_response(
                        response,
                        cast(BinaryIO, output),
                        existing_size,
                        artifact.size,
                        artifact.artifact_id,
                        progress,
                        hasher,
                        source_host=urlsplit(str(response.url)).hostname,
                    )
            finally:
                current_size = payload_path.stat().st_size if payload_path.exists() else 0
                workspace.atomic_write_json(
                    state_path,
                    _PartialState(
                        artifact_id=artifact.artifact_id,
                        url=url,
                        bytes_written=current_size,
                        etag=response_etag,
                        last_modified=response_last_modified,
                    ).model_dump(mode="json"),
                )
            complete = payload_path.stat().st_size == artifact.size
            integrity = hasher.finish() if complete else None
            return complete, self._trace(response, url), integrity

    def _stream_response(
        self,
        response: httpx.Response,
        output: BinaryIO,
        initial_size: int,
        expected_size: int,
        artifact_id: str,
        progress: _DownloadProgress | None,
        hasher: _PayloadHasher,
        *,
        source_host: str | None,
    ) -> None:
        written = initial_size
        for received in response.iter_raw():
            for offset in range(0, len(received), self._policy.chunk_bytes):
                chunk = received[offset : offset + self._policy.chunk_bytes]
                written += len(chunk)
                if written > expected_size:
                    raise ArtifactCorruptError("web seed sent more bytes than declared")
                output.write(chunk)
                hasher.update(chunk)
                if progress is not None:
                    progress.update(
                        artifact_id,
                        written,
                        len(chunk),
                        source_host=source_host,
                    )
        output.flush()
        os.fsync(output.fileno())

    @staticmethod
    def _trace(response: httpx.Response, requested_url: str) -> DownloadTrace:
        history = tuple(str(item.url) for item in response.history)
        redirects = history + ((str(response.url),) if history else ())
        return DownloadTrace(
            method=DownloadMethod.WEB_SEED,
            requested_url=requested_url,
            final_url=str(response.url),
            http_redirects=redirects,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    @staticmethod
    def _load_state(state_path: Path, workspace: Workspace) -> _PartialState | None:
        try:
            return _PartialState.model_validate_json(workspace.read_bytes(state_path))
        except FileNotFoundError:
            return None
        except (ValidationError, WorkspaceCorruptError) as exc:
            raise ArtifactCorruptError(f"invalid partial download state: {exc}") from exc


def safe_archive_name(value: str) -> str:
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise UnsafeArchiveError(f"archive contains an unsafe path {value!r}")
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise UnsafeArchiveError(f"archive contains an unsafe path {value!r}")
    path = PurePosixPath(value)
    if any(component in {"", ".", ".."} for component in path.parts):
        raise UnsafeArchiveError(f"archive contains an unsafe path {value!r}")
    if path.parts and re.fullmatch(r"[A-Za-z]:.*", path.parts[0]):
        raise UnsafeArchiveError(f"archive contains a drive-qualified path {value!r}")
    return path.as_posix()


class ArtifactVerifier:
    def __init__(self, policy: VerificationPolicy | None = None) -> None:
        self._policy = policy or VerificationPolicy()

    def verify(
        self,
        downloaded: DownloadResult,
        workspace: Workspace,
        *,
        verify_blob_digests: bool = True,
    ) -> VerificationResult:
        def verify_item(item: DownloadedArtifact) -> ArtifactVerification:
            if verify_blob_digests:
                validate_downloaded_artifact(workspace, item)
            else:
                validate_downloaded_artifact_structure(workspace, item)
            segment = item.artifact.split_segment
            if segment is not None:
                return ArtifactVerification(
                    download=item,
                    container=ContainerKind.SPLIT_SEGMENT,
                    magic_hex=self._magic(workspace.blobs.path_for(item.blob_sha256)).hex(),
                    container_verified=False,
                )
            container, entries = self._verify_one(item, workspace)
            return ArtifactVerification(
                download=item,
                container=container,
                magic_hex=self._magic(workspace.blobs.path_for(item.blob_sha256)).hex(),
                container_verified=container is not ContainerKind.OPAQUE,
                entries=entries,
            )

        if self._policy.max_workers == 1:
            verified = tuple(verify_item(item) for item in downloaded.artifacts)
        else:
            with ThreadPoolExecutor(
                max_workers=self._policy.max_workers,
                thread_name_prefix="artifact-verify",
            ) as executor:
                verified = tuple(executor.map(verify_item, downloaded.artifacts))

        split_groups: dict[str, list[DownloadedArtifact]] = defaultdict(list)
        for item in downloaded.artifacts:
            segment = item.artifact.split_segment
            if segment is not None:
                split_groups[segment.group_id].append(item)
        assemblies = tuple(
            self._assemble_split(group_id, members, workspace)
            for group_id, members in sorted(split_groups.items())
        )
        return VerificationResult(
            download_result_sha256="sha256:" + "0" * 64,
            artifacts=verified,
            split_assemblies=assemblies,
        )

    def _verify_one(
        self,
        item: DownloadedArtifact,
        workspace: Workspace,
    ) -> tuple[ContainerKind, int | None]:
        path = workspace.blobs.path_for(item.blob_sha256)
        magic = self._magic(path)
        display_path = item.artifact.path.utf8 or ""
        is_delivery = item.artifact.role == "delivery-bundle"
        if magic.startswith(_ZIP_MAGICS):
            return ContainerKind.ZIP, self._verify_zip(path)
        if magic.startswith(_SEVEN_ZIP_MAGIC):
            return ContainerKind.SEVEN_ZIP, self._verify_seven_zip(path)
        if display_path.lower().endswith(".pkg") or is_delivery:
            raise ArtifactCorruptError(
                f"declared package/bundle has unsupported magic bytes: {display_path!r}"
            )
        return ContainerKind.OPAQUE, None

    def _verify_zip(self, path: Path) -> int:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > self._policy.max_archive_entries:
                    raise UnsafeArchiveError("ZIP entry count exceeds the configured limit")
                total = 0
                for info in infos:
                    safe_archive_name(info.filename.rstrip("/"))
                    mode = (info.external_attr >> 16) & 0xFFFF
                    kind = stat.S_IFMT(mode)
                    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise UnsafeArchiveError("ZIP contains a link or special filesystem entry")
                    total += info.file_size
                    if total > self._policy.max_archive_unpacked_bytes:
                        raise UnsafeArchiveError("ZIP expanded size exceeds the configured limit")
                    ratio = info.file_size / max(1, info.compress_size)
                    if ratio > self._policy.max_compression_ratio:
                        raise UnsafeArchiveError(
                            "ZIP compression ratio exceeds the configured limit"
                        )
                corrupt = archive.testzip()
                if corrupt is not None:
                    raise ArtifactCorruptError(f"ZIP CRC failed for entry {corrupt!r}")
                return len(infos)
        except UnsafeArchiveError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise ArtifactCorruptError(f"invalid ZIP container: {exc}") from exc

    def _verify_seven_zip(self, path: Path) -> int:
        executable = next(
            (value for value in self._policy.seven_zip_executables if shutil.which(value)),
            None,
        )
        if executable is None:
            raise StageExecutionError(
                "stage_not_implemented", "7z verification requires 7zz, 7z, or bsdtar"
            )
        names_command = [executable, "-tf", str(path)]
        test_command = [executable, "-xOf", str(path)]
        if Path(executable).name in {"7z", "7zz"}:
            names_command = [executable, "l", "-ba", "-slt", str(path)]
            test_command = [executable, "t", "-bd", "-y", str(path)]
        try:
            names_result = subprocess.run(
                names_command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=self._policy.archive_timeout_seconds,
            )
            if names_result.returncode != 0:
                raise ArtifactCorruptError("7z container listing failed")
            if Path(executable).name in {"7z", "7zz"}:
                names = [
                    line.removeprefix("Path = ")
                    for line in names_result.stdout.decode("utf-8", errors="strict").splitlines()
                    if line.startswith("Path = ")
                ]
                if names and names[0] == str(path):
                    names = names[1:]
            else:
                names = names_result.stdout.decode("utf-8", errors="strict").splitlines()
            if len(names) > self._policy.max_archive_entries:
                raise UnsafeArchiveError("7z entry count exceeds the configured limit")
            for name in names:
                safe_archive_name(name.rstrip("/"))
            tested = subprocess.run(
                test_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._policy.archive_timeout_seconds,
            )
            if tested.returncode != 0:
                diagnostic = tested.stderr[-2048:].decode("utf-8", errors="replace")
                raise ArtifactCorruptError(f"7z container integrity test failed: {diagnostic}")
            return len(names)
        except UnicodeDecodeError as exc:
            raise UnsafeArchiveError("7z listing contains a non-UTF-8 path") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArtifactCorruptError("7z container verification timed out") from exc
        except OSError as exc:
            raise StageExecutionError("stage_not_implemented", str(exc)) from exc

    def _assemble_split(
        self,
        group_id: str,
        members: Sequence[DownloadedArtifact],
        workspace: Workspace,
    ) -> SplitAssembly:
        ordered = sorted(
            members,
            key=lambda item: (
                item.artifact.split_segment.index if item.artifact.split_segment is not None else 0
            ),
        )
        segments = [item.artifact.split_segment for item in ordered]
        if any(segment is None for segment in segments):
            raise ArtifactCorruptError("split group contains an unnumbered Artifact")
        complete_segments = cast(list[SplitSegment], segments)
        expected_count = complete_segments[0].count
        if (
            len(complete_segments) != expected_count
            or [segment.index for segment in complete_segments]
            != list(range(1, expected_count + 1))
            or any(
                segment.group_id != group_id or segment.count != expected_count
                for segment in complete_segments
            )
        ):
            raise ArtifactCorruptError("split delivery bundle is incomplete or inconsistent")
        work = workspace.tmp_root / "split-assemblies" / group_id.removeprefix("sha256:")
        work.parent.mkdir(parents=True, exist_ok=True)
        with work.open("wb") as output:
            for item in ordered:
                source = workspace.blobs.path_for(item.blob_sha256)
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        try:
            commit = workspace.blobs.put_file(
                work, expected_size=sum(item.blob_size for item in ordered)
            )
        except (BlobValidationError, CasCorruptionError) as exc:
            raise ArtifactCorruptError(str(exc)) from exc
        finally:
            work.unlink(missing_ok=True)
        synthetic = ordered[0].model_copy(
            update={
                "blob_sha256": commit.sha256,
                "blob_size": commit.size,
                "blob_path": commit.relative_path,
            }
        )
        container, entries = self._verify_one(synthetic, workspace)
        if container not in {ContainerKind.ZIP, ContainerKind.SEVEN_ZIP} or entries is None:
            raise ArtifactCorruptError("assembled split delivery bundle is not an archive")
        return SplitAssembly(
            group_id=group_id,
            artifact_ids=tuple(item.artifact.artifact_id for item in ordered),
            blob_sha256=commit.sha256,
            blob_size=commit.size,
            blob_path=commit.relative_path,
            container=container,
            entries=entries,
        )

    @staticmethod
    def _magic(path: Path) -> bytes:
        with path.open("rb") as stream:
            return stream.read(16)


def create_download_implementation(
    policy: DownloadPolicy | None = None,
) -> StageImplementation:
    selected = policy or DownloadPolicy()

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.DOWNLOAD or context.upstream is None:
            raise ArtifactCorruptError("download requires an Acquisition Plan")
        plan = context.upstream_as(AcquisitionPlan)
        result = ArtifactDownloader(selected, context.progress).download(plan, context.workspace)
        result = result.model_copy(
            update={"acquisition_plan_sha256": context.require_upstream_digest()}
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Download Result has no Acquisition Plan")
        plan = context.upstream_as(AcquisitionPlan)
        result = DownloadResult.model_validate(payload)
        expected_digest = context.require_upstream_digest()
        if result.acquisition_plan_sha256 != expected_digest:
            raise ValueError("Download Result is not bound to its Acquisition Plan")
        expected_artifacts = tuple(artifact for part in plan.parts for artifact in part.artifacts)
        if tuple(item.artifact for item in result.artifacts) != expected_artifacts:
            raise ValueError("Download Result Artifacts do not match the Acquisition Plan")
        for item in result.artifacts:
            validate_downloaded_artifact_structure(context.workspace, item)

    def audit(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = DownloadResult.model_validate(payload)
        for item in result.artifacts:
            validate_downloaded_artifact(context.workspace, item)

    return StageImplementation(
        implementation_version="resumable-download-v9",
        execute=execute,
        validate=validate,
        audit=audit,
        configuration=cast(Mapping[str, JsonValue], selected.model_dump(mode="json")),
    )


def create_verify_implementation(
    policy: VerificationPolicy | None = None,
) -> StageImplementation:
    selected = policy or VerificationPolicy()

    def execute(context: StageContext) -> Mapping[str, JsonValue]:
        if context.stage is not Stage.VERIFY or context.upstream is None:
            raise ArtifactCorruptError("verify requires a Download Result")
        downloaded = context.upstream_as(DownloadResult)
        result = ArtifactVerifier(selected).verify(
            downloaded,
            context.workspace,
            verify_blob_digests=False,
        )
        result = result.model_copy(
            update={"download_result_sha256": context.require_upstream_digest()}
        )
        return cast(Mapping[str, JsonValue], result.model_dump(mode="json"))

    def validate(context: StageContext, payload: dict[str, JsonValue]) -> None:
        if context.upstream is None:
            raise ValueError("Verification Result has no Download Result")
        downloaded = context.upstream_as(DownloadResult)
        result = VerificationResult.model_validate(payload)
        expected_digest = context.require_upstream_digest()
        if result.download_result_sha256 != expected_digest:
            raise ValueError("Verification Result is not bound to its Download Result")
        if tuple(item.download for item in result.artifacts) != downloaded.artifacts:
            raise ValueError("Verification Result does not cover exactly the downloaded Artifacts")
        for item in result.artifacts:
            validate_downloaded_artifact_structure(context.workspace, item.download)
        for assembly in result.split_assemblies:
            path = context.workspace.blobs.path_for(assembly.blob_sha256)
            _validate_regular_file(path, expected_size=assembly.blob_size, writable=False)

    def audit(context: StageContext, payload: dict[str, JsonValue]) -> None:
        result = VerificationResult.model_validate(payload)
        for item in result.artifacts:
            validate_downloaded_artifact(context.workspace, item.download)
        for assembly in result.split_assemblies:
            path = context.workspace.blobs.path_for(assembly.blob_sha256)
            _validate_regular_file(path, expected_size=assembly.blob_size, writable=False)
            actual, _verified = _hash_file(path, None)
            if actual != assembly.blob_sha256:
                raise ValueError("split assembly CAS content does not match its SHA-256 key")

    return StageImplementation(
        implementation_version="artifact-verification-v2",
        execute=execute,
        validate=validate,
        audit=audit,
        configuration=cast(Mapping[str, JsonValue], selected.model_dump(mode="json")),
    )


__all__ = [
    "ArtifactDownloader",
    "ArtifactVerifier",
    "DownloadPolicy",
    "DownloadTooSlowError",
    "InsufficientDiskError",
    "VerificationPolicy",
    "create_download_implementation",
    "create_verify_implementation",
    "safe_archive_name",
    "validate_downloaded_artifact",
    "validate_downloaded_artifact_structure",
]

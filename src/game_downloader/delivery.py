from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import ConfigDict, Field, ValidationError

from game_downloader._json import JsonValue
from game_downloader.acquisition import ArtifactCorruptError, UnsafeArchiveError
from game_downloader.aria2_rpc import Aria2ControlTransportError, Aria2Session
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


class DownloadPolicy(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_workers: int = Field(default=16, ge=1, le=32)
    http_attempts_per_url: int = Field(default=20, ge=1, le=20)
    http_retry_backoff_seconds: float = Field(default=1.0, ge=0.0, le=30.0)
    http_retry_max_backoff_seconds: float = Field(default=10.0, ge=0.0, le=30.0)
    http_no_progress_timeout_seconds: float = Field(default=300.0, gt=0, le=600.0)
    http_minimum_throughput_bytes_per_second: int = Field(default=256 * 1024, ge=0)
    http_throughput_window_seconds: float = Field(default=300.0, ge=10.0, le=900.0)
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
    minimum_throughput_tail_percent: float = Field(default=5.0, ge=0.0, le=100.0)
    minimum_throughput_tail_grace_seconds: float = Field(default=20 * 60, ge=0.0)
    parallel_range_minimum_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    parallel_range_target_bytes: int = Field(default=128 * 1024 * 1024, ge=64 * 1024)
    parallel_range_max_segments: int = Field(default=16, ge=2, le=32)
    aria2_executable: str = "aria2c"
    aria2_timeout_seconds: int = Field(default=24 * 60 * 60, ge=60)


class _HttpTransferProgress:
    """Track useful file progress across HTTP retries, restarts and source changes."""

    def __init__(
        self,
        artifact_id: str,
        size: int,
        initial_bytes: int,
        policy: DownloadPolicy,
        *,
        observer: Callable[[str], None] | None = None,
    ) -> None:
        self._artifact_id = artifact_id
        self._size = size
        self._policy = policy
        self._observer = observer or (lambda _message: None)
        self._slow_warning_active = False
        self._current_bytes = initial_bytes
        self._high_watermark = initial_bytes
        now = time.monotonic()
        self._last_progress_at = now
        self._samples: deque[tuple[float, int]] = deque([(now, initial_bytes)])

    def update(self, current_bytes: int) -> None:
        now = time.monotonic()
        self._current_bytes = current_bytes
        if current_bytes > self._high_watermark:
            self._high_watermark = current_bytes
            self._last_progress_at = now
        self._check(now)

    def remaining(self) -> float:
        return self._check(time.monotonic())

    def restart_after_discard(self) -> None:
        """Rebase a discarded layout without treating its rejection as progress."""
        self._current_bytes = self._high_watermark = 0
        self._samples.clear()
        self._samples.append((time.monotonic(), 0))
        self._slow_warning_active = False

    def _check(self, now: float) -> float:
        remaining = self._policy.http_no_progress_timeout_seconds - (now - self._last_progress_at)
        if remaining <= 0:
            raise StageExecutionError(
                "source_unavailable",
                f"Artifact {self._artifact_id}: {_format_bytes(self._current_bytes)} / "
                f"{_format_bytes(self._size)} available; HTTP made no forward progress for "
                f"{self._policy.http_no_progress_timeout_seconds:g}s",
            )
        self._samples.append((now, self._current_bytes))
        cutoff = now - self._policy.http_throughput_window_seconds
        while len(self._samples) > 1 and self._samples[1][0] <= cutoff:
            self._samples.popleft()
        started_at, started_bytes = self._samples[0]
        elapsed = now - started_at
        minimum_speed = self._policy.http_minimum_throughput_bytes_per_second
        if (
            self._current_bytes < self._size
            and minimum_speed > 0
            and elapsed >= self._policy.http_throughput_window_seconds
            and (speed := max(0, self._current_bytes - started_bytes) / elapsed) < minimum_speed
        ):
            if not self._slow_warning_active:
                self._observer(
                    f"Warning: Artifact {self._artifact_id}: "
                    f"{_format_bytes(self._current_bytes)} / "
                    f"{_format_bytes(self._size)} available; "
                    f"HTTP forward progress averaged {_format_rate(speed)} over "
                    f"{elapsed:.1f}s, below the configured {_format_rate(minimum_speed)} "
                    "warning threshold; continuing current stream"
                )
                self._slow_warning_active = True
        else:
            self._slow_warning_active = False
        return remaining

    def timeout(self) -> httpx.Timeout:
        remaining = self.remaining()
        return httpx.Timeout(
            connect=min(self._policy.connect_timeout_seconds, remaining),
            read=min(self._policy.read_timeout_seconds, remaining),
            write=min(self._policy.read_timeout_seconds, remaining),
            pool=min(self._policy.connect_timeout_seconds, remaining),
        )


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
    native_control: bool = False
    confirmed_complete: bool = False
    sequential_prefix: bool = False


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
        minimum_throughput_tail_percent: float = 0.0,
        minimum_throughput_tail_grace_seconds: float = 0.0,
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
        self._minimum_throughput_tail_percent = minimum_throughput_tail_percent
        self._minimum_throughput_tail_grace_seconds = minimum_throughput_tail_grace_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failure: tuple[str, str] | None = None
        self._failure_cause: BaseException | None = None
        started_at = clock()
        self._last_report_at = started_at
        self._last_report_downloaded = -1
        self._interval_transferred = 0
        self._total_transferred = 0
        self._throughput_samples: deque[tuple[float, int]] = deque([(started_at, 0)])
        self._slow_warning_active = False
        downloaded = sum(self._current_bytes.values())
        self._throughput_tail_started_at = (
            started_at if self._in_minimum_throughput_tail(downloaded) else None
        )
        percentage = self._percentage(downloaded)
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

    def fail(self, error: StageExecutionError, *, cause: BaseException | None = None) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = (error.error.code, str(error))
                self._failure_cause = cause if cause is not None else error

    def failure_cause(self) -> BaseException | None:
        with self._lock:
            return self._failure_cause

    def check_active(self) -> None:
        with self._lock:
            if self._failure is not None:
                raise StageExecutionError(*self._failure)

    def update(
        self,
        artifact_id: str,
        current_bytes: int,
        transferred_bytes: int = 0,
        *,
        source_host: str | None = None,
    ) -> None:
        self.check_active()
        expected_size = self._artifact_sizes[artifact_id]
        if not 0 <= current_bytes <= expected_size:
            raise ValueError(
                f"invalid progress for Artifact {artifact_id}: "
                f"{current_bytes} not in [0, {expected_size}]"
            )
        if transferred_bytes < 0:
            raise ValueError("transferred progress cannot be negative")
        with self._lock:
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

    def _in_minimum_throughput_tail(self, downloaded: int) -> bool:
        remaining = self._total_bytes - downloaded
        return (
            self._minimum_throughput_tail_percent > 0
            and remaining > 0
            and remaining * 100 <= self._total_bytes * self._minimum_throughput_tail_percent
        )

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
        in_throughput_tail = self._in_minimum_throughput_tail(downloaded)
        if not in_throughput_tail:
            self._throughput_tail_started_at = None
        elif self._throughput_tail_started_at is None:
            self._throughput_tail_started_at = now
        if (
            in_throughput_tail
            and self._throughput_tail_started_at is not None
            and now - self._throughput_tail_started_at
            <= self._minimum_throughput_tail_grace_seconds
        ):
            return
        window_started_at, window_started_bytes = samples[0]
        elapsed = now - window_started_at
        if elapsed < self._minimum_throughput_window_seconds:
            return
        speed = (self._total_transferred - window_started_bytes) / elapsed
        if speed >= minimum:
            self._slow_warning_active = False
            return
        if self._slow_warning_active:
            return
        remaining = self._total_bytes - downloaded
        source = f" from {source_host}" if source_host is not None else ""
        self._observer(
            f"Warning: aggregate download throughput averaged {_format_rate(speed)} over "
            f"{elapsed:.1f}s, below the configured {_format_rate(minimum)} warning threshold; "
            f"{_format_bytes(remaining)} remain; current Artifact {artifact_id}{source}; "
            "continuing download"
        )
        self._slow_warning_active = True


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


class _NativeHttpError(OSError):
    def __init__(
        self,
        message: str,
        code: int = 0,
        *,
        http_status: int | None = None,
        bound_response: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.bound_response = bound_response


class _Aria2HttpAdapter:
    def __init__(
        self,
        policy: DownloadPolicy,
        session: Aria2Session,
        client: httpx.Client,
        observer: Callable[[str], None],
    ) -> None:
        self.policy, self.session, self.client = policy, session, client
        self.observer = observer
        self.hash_slots = threading.Semaphore(min(4, policy.max_workers))

    def probe(
        self,
        url: str,
        artifact: AcquisitionArtifact,
        transfer: _HttpTransferProgress,
        *,
        use_get: bool = False,
    ) -> httpx.Response:
        response = None if use_get else self.client.head(url, timeout=transfer.timeout())
        if response is None or response.status_code in {405, 501}:
            with self.client.stream(
                "GET", url, headers={"Range": "bytes=0-0"}, timeout=transfer.timeout()
            ) as streamed:
                response = streamed
        if response.status_code not in {200, 206}:
            raise _NativeHttpError(f"HTTP metadata returned {response.status_code}")
        if urlsplit(str(response.url)).scheme != "https":
            raise ArtifactCorruptError("web seed redirected metadata away from HTTPS")
        length = response.headers.get("content-length")
        if response.status_code == 206:
            matched = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
            length = matched.group("total") if matched else None
        if length is not None and (not length.isdecimal() or int(length) != artifact.size):
            raise _NativeHttpError("HTTP metadata size does not match the Acquisition Plan")
        return response

    @staticmethod
    def validator(response: httpx.Response) -> tuple[str | None, str | None]:
        def clean(value: str | None) -> str | None:
            return value if value and all(32 <= ord(char) < 127 for char in value) else None

        etag = clean(response.headers.get("etag"))
        if etag and etag.startswith("W/"):
            etag = None
        return etag, clean(response.headers.get("last-modified"))

    @staticmethod
    def clear(payload: Path, state: Path) -> None:
        payload.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        Path(str(payload) + ".aria2").unlink(missing_ok=True)

    def attempt(
        self,
        artifact: AcquisitionArtifact,
        url: str,
        workspace: Workspace,
        payload: Path,
        state_path: Path,
        progress: _DownloadProgress,
        transfer: _HttpTransferProgress,
        *,
        single_stream: bool,
    ) -> tuple[DownloadTrace, _PayloadIntegrity]:
        previous = ArtifactDownloader._load_state(state_path, workspace)
        # Validate the GET representation before binding ranges, including the
        # first split request. Some CDNs return different validators for HEAD.
        metadata = self.probe(
            url,
            artifact,
            transfer,
            use_get=artifact.size > 0
            and artifact.source_hash is not None
            and (
                previous is not None
                or single_stream
                or artifact.size >= self.policy.parallel_range_minimum_bytes
            ),
        )
        final_url = str(metadata.url)
        etag, modified = self.validator(metadata)
        control = Path(str(payload) + ".aria2")
        if control.exists() or control.is_symlink():
            _validate_regular_file(control, expected_size=control.stat().st_size, writable=True)
        same_source = (
            previous is not None
            and previous.artifact_id == artifact.artifact_id
            and previous.bytes_written <= artifact.size
            and previous.url == url
            and (etag is not None or modified is not None)
            and (previous.etag == etag if etag is not None else previous.last_modified == modified)
        )
        if (
            same_source
            and previous is not None
            and previous.confirmed_complete
            and previous.bytes_written == artifact.size
            and payload.exists()
            and not control.exists()
        ):
            # Completion survives an interrupted hash/commit, but without a
            # declared source hash it is only reusable after this fresh probe.
            _validate_regular_file(payload, expected_size=artifact.size, writable=True)
            progress.check_active()
            progress.update(artifact.artifact_id, artifact.size)
            transfer.update(artifact.size)
            with self.hash_slots:
                own, verified = _hash_file(payload, artifact.source_hash)
            return DownloadTrace(
                method=DownloadMethod.WEB_SEED,
                requested_url=url,
                final_url=final_url,
                http_redirects=tuple(str(r.url) for r in metadata.history)
                + ((final_url,) if metadata.history else ()),
                etag=etag,
                last_modified=modified,
                resumed_from=artifact.size,
            ), _PayloadIntegrity(sha256=own, source_hash_verified=verified)
        native = (
            same_source and previous is not None and previous.native_control and control.exists()
        )
        if not native:
            self.clear(payload, state_path)
        resumed = previous.bytes_written if native and previous else 0
        progress.update(artifact.artifact_id, resumed)
        transfer.update(resumed)
        state = _PartialState(
            artifact_id=artifact.artifact_id,
            url=url,
            bytes_written=resumed,
            etag=etag,
            last_modified=modified,
            native_control=True,
        )
        workspace.atomic_write_json(state_path, state.model_dump(mode="json"))
        minimum_split = max(1024 * 1024, self.policy.parallel_range_target_bytes)
        segments = 1
        fallbacks: tuple[ParallelRangeFallback, ...] = ()
        if artifact.size >= self.policy.parallel_range_minimum_bytes and not single_stream:
            if metadata.request.method == "GET" and metadata.status_code != 206:
                reason = ParallelRangeFallbackReason.RANGE_RESPONSE_NOT_PARTIAL
            elif metadata.headers.get("accept-ranges", "").lower() != "bytes":
                reason = ParallelRangeFallbackReason.RANGE_NOT_ADVERTISED
            elif not (etag or modified):
                reason = ParallelRangeFallbackReason.VALIDATOR_UNAVAILABLE
            else:
                reason = None
                segments = max(
                    1,
                    min(
                        16, self.policy.parallel_range_max_segments, artifact.size // minimum_split
                    ),
                )
            if reason is not None:
                fallbacks = (
                    ParallelRangeFallback(
                        reason=reason, source_host=urlsplit(final_url).hostname or "unknown"
                    ),
                )
        headers = ["Accept-Encoding: identity"]
        bind_response = native or segments > 1 or artifact.source_hash is None
        # A fresh whole-file response is pinned by its declared source hash.
        # Partial resume and parallel ranges still require source validators.
        if etag and bind_response:
            headers.append("If-Match: " + etag)
        elif modified and bind_response:
            headers.append("If-Unmodified-Since: " + modified)
        options: dict[str, Any] = {
            "dir": str(payload.parent),
            "out": payload.name,
            "continue": "true" if native else "false",
            "use-head": "true"
            if (native or segments > 1) and metadata.request.method == "HEAD"
            else "false",
            "split": str(segments),
            "max-connection-per-server": str(segments),
            "min-split-size": str(minimum_split),
            "header": headers,
        }
        # With disk-cache disabled and one native writer, st_size is an exact
        # contiguous prefix even below aria2's 16 KiB RPC block granularity.
        contiguous = segments == 1 and (not native or bool(previous and previous.sequential_prefix))
        state = state.model_copy(update={"sequential_prefix": contiguous})
        workspace.atomic_write_json(state_path, state.model_dump(mode="json"))
        gid = self.session.call(
            "addUri",
            [final_url],
            options,
            timeout=transfer.remaining(),
        )
        if not isinstance(gid, str) or re.fullmatch(r"[a-f0-9]{16}", gid) is None:
            raise StageExecutionError("source_unavailable", "aria2 returned an invalid transfer ID")
        completed, observed, complete = resumed, resumed, False
        status_retry = False
        interval = min(0.25, self.policy.http_no_progress_timeout_seconds / 4)
        try:
            while True:
                progress.check_active()
                try:
                    status = self.session.call(
                        "tellStatus",
                        gid,
                        [
                            "status",
                            "completedLength",
                            "totalLength",
                            "errorCode",
                            "downloadSpeed",
                            "errorMessage",
                        ],
                        timeout=min(5, transfer.remaining()),
                    )
                except Aria2ControlTransportError as error:
                    # The native writer may still be healthy. Retrying this
                    # read cannot start a second download or discard its bitmap.
                    progress.check_active()
                    if contiguous and payload.exists():
                        completed = max(completed, payload.stat().st_size)
                        if completed > artifact.size:
                            raise _NativeHttpError(
                                "aria2 HTTP size does not match the Acquisition Plan"
                            ) from None
                        transfer.update(completed)
                        progress.update(
                            artifact.artifact_id, completed, max(0, completed - observed)
                        )
                        observed = completed
                    if not status_retry:
                        self.observer(
                            f"Artifact {artifact.artifact_id}: {error}; "
                            "retrying status of the existing HTTP transfer"
                        )
                        status_retry = True
                    time.sleep(min(interval, transfer.remaining()))
                    continue
                status_retry = False
                completed = int(status["completedLength"])
                if contiguous and payload.exists():
                    completed = max(completed, payload.stat().st_size)
                total = int(status["totalLength"])
                if not 0 <= completed <= artifact.size or total not in {0, artifact.size}:
                    raise _NativeHttpError("aria2 HTTP size does not match the Acquisition Plan")
                if (
                    segments > 1
                    and int(status.get("downloadSpeed", "0")) > 0
                    and completed <= observed
                    and transfer.remaining() < self.policy.http_no_progress_timeout_seconds / 2
                ):
                    raise _NativeHttpError(
                        "HTTP Range progress is below native block granularity", 8
                    )
                transfer.update(completed)
                progress.update(
                    artifact.artifact_id,
                    completed,
                    max(0, completed - observed),
                    source_host=urlsplit(final_url).hostname,
                )
                observed = completed
                if status["status"] == "complete":
                    if completed != artifact.size or total != artifact.size:
                        raise _NativeHttpError("aria2 completed an incomplete HTTP payload")
                    complete = True
                    break
                if status["status"] in {"error", "removed"}:
                    code = int(status.get("errorCode", "1"))
                    # Native diagnostics may contain credentials or full source URLs.
                    # Retain only aria2's numeric HTTP response status.
                    native_message = status.get("errorMessage", "")
                    matched = re.search(
                        r"The response status is not successful\. status=([1-5][0-9]{2})\b",
                        native_message if isinstance(native_message, str) else "",
                    )
                    http_status = int(matched[1]) if matched else None
                    detail = f" (HTTP {http_status})" if http_status is not None else ""
                    raise _NativeHttpError(
                        f"aria2 HTTP transfer failed with code {code}{detail}",
                        code,
                        http_status=http_status,
                        bound_response=bind_response and bool(etag or modified),
                    )
                time.sleep(min(interval, transfer.remaining()))
        finally:
            if not complete:
                already_failing = sys.exc_info()[0] is not None
                try:
                    stopped = self.session.remove(gid)
                    completed = int(stopped["completedLength"])
                    if contiguous and payload.exists():
                        completed = max(completed, payload.stat().st_size)
                except StageExecutionError:
                    self.session.close()
                    if not already_failing:
                        raise
            workspace.atomic_write_json(
                state_path,
                state.model_copy(update={"bytes_written": completed}).model_dump(mode="json"),
            )
        if not complete:
            raise _NativeHttpError("aria2 HTTP transfer did not complete")
        _validate_regular_file(payload, expected_size=artifact.size, writable=True)
        with payload.open("rb") as source:
            os.fsync(source.fileno())
        # A declared source hash pins the content independently of a CDN edge's
        # ETag. Re-probing such a complete file can discard correct gigabytes.
        # Partial resume still always requires an unchanged source validator.
        if artifact.source_hash is None and (etag or modified):
            current = self.probe(url, artifact, transfer)
            current_etag, current_modified = self.validator(current)
            if current_etag != etag if etag else current_modified != modified:
                raise _NativeHttpError("HTTP source validator changed during download")
        workspace.atomic_write_json(
            state_path,
            state.model_copy(
                update={"bytes_written": artifact.size, "confirmed_complete": True}
            ).model_dump(mode="json"),
        )
        progress.check_active()
        with self.hash_slots:
            own, verified = _hash_file(payload, artifact.source_hash)
        self.session.discard_completed(gid)
        return DownloadTrace(
            method=DownloadMethod.WEB_SEED,
            requested_url=url,
            final_url=final_url,
            http_redirects=tuple(str(r.url) for r in metadata.history)
            + ((final_url,) if metadata.history else ()),
            etag=etag,
            last_modified=modified,
            resumed_from=resumed,
            parallel_segments=segments,
            parallel_range_fallbacks=fallbacks,
        ), _PayloadIntegrity(sha256=own, source_hash_verified=verified)


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
            minimum_throughput_tail_percent=self._policy.minimum_throughput_tail_percent,
            minimum_throughput_tail_grace_seconds=(
                self._policy.minimum_throughput_tail_grace_seconds
            ),
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
        scheduled_artifacts = tuple(
            sorted(artifacts, key=lambda item: (-item.size, item.artifact_id))
        )
        pending_http = any(a.source_urls and a.artifact_id not in cached for a in artifacts)
        with ExitStack() as stack:
            client = stack.enter_context(
                httpx.Client(
                    follow_redirects=True,
                    max_redirects=self._policy.max_redirects,
                    timeout=timeout,
                    headers={"Accept-Encoding": "identity", "User-Agent": "game-downloader/0.1"},
                )
            )
            session = (
                stack.enter_context(
                    Aria2Session(
                        workspace.tmp_root,
                        executable=self._policy.aria2_executable,
                        workers=self._policy.max_workers,
                        connect_timeout=self._policy.connect_timeout_seconds,
                        read_timeout=self._policy.read_timeout_seconds,
                    )
                )
                if pending_http
                else None
            )
            http = (
                _Aria2HttpAdapter(self._policy, session, client, self._progress_observer)
                if session
                else None
            )

            def download_artifact(artifact: AcquisitionArtifact) -> DownloadedArtifact:
                progress.check_active()
                try:
                    return self._download_one(
                        artifact, plan, workspace, http, cached.get(artifact.artifact_id), progress
                    )
                except BaseException as exc:
                    progress.fail(
                        exc
                        if isinstance(exc, StageExecutionError)
                        else StageExecutionError("source_unavailable", "HTTP download interrupted"),
                        cause=exc,
                    )
                    raise

            with ThreadPoolExecutor(
                max_workers=self._policy.max_workers, thread_name_prefix="artifact-download"
            ) as executor:
                try:
                    pending = [
                        executor.submit(download_artifact, artifact)
                        for artifact in scheduled_artifacts
                    ]
                    scheduled_downloads = tuple(future.result() for future in as_completed(pending))
                except BaseException:
                    if session is not None:
                        session.close()
                    executor.shutdown(wait=True, cancel_futures=True)
                    if cause := progress.failure_cause():
                        raise cause from None
                    raise
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
        http: _Aria2HttpAdapter | None,
        cached: DownloadedArtifact | None = None,
        progress: _DownloadProgress | None = None,
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
                    http,
                    workspace,
                    payload_path,
                    state_path,
                    progress,
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
            Path(str(payload_path) + ".aria2").unlink(missing_ok=True)
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
        http: _Aria2HttpAdapter | None,
        workspace: Workspace,
        payload_path: Path,
        state_path: Path,
        progress: _DownloadProgress | None,
    ) -> tuple[DownloadTrace, _PayloadIntegrity]:
        assert http is not None and progress is not None
        previous = self._load_state(state_path, workspace)
        initial_size = payload_path.stat().st_size if payload_path.exists() else 0
        # A native sparse file can have its final length long before it is complete.
        # A declared source hash proves completeness without a metadata request.
        # Otherwise attempt() must revalidate even a previously completed payload.
        if (
            initial_size == artifact.size
            and payload_path.exists()
            and not Path(str(payload_path) + ".aria2").exists()
            and artifact.source_hash is not None
        ):
            progress.check_active()
            with http.hash_slots:
                own, verified = _hash_file(payload_path, artifact.source_hash)
            if verified:
                url = previous.url if previous else artifact.source_urls[0]
                return DownloadTrace(
                    method=DownloadMethod.WEB_SEED,
                    requested_url=url,
                    final_url=url,
                    resumed_from=initial_size,
                ), _PayloadIntegrity(sha256=own, source_hash_verified=verified)
            http.clear(payload_path, state_path)
        attempts = 0
        saw_corrupt = False
        single_stream = False
        fallbacks: list[ParallelRangeFallback] = []
        last_error = "no HTTP source was attempted"
        transfer = _HttpTransferProgress(
            artifact.artifact_id,
            artifact.size,
            previous.bytes_written if previous and not previous.confirmed_complete else 0,
            self._policy,
            observer=self._progress_observer,
        )
        for url in artifact.source_urls:
            self._validate_web_seed_url(url)
            for _attempt in range(self._policy.http_attempts_per_url):
                progress.check_active()
                if attempts:
                    delay = min(
                        self._policy.http_retry_backoff_seconds * 2 ** min(attempts - 1, 10),
                        self._policy.http_retry_max_backoff_seconds,
                        transfer.remaining(),
                    )
                    self._progress_observer(
                        f"Artifact {artifact.artifact_id}: {last_error}; "
                        f"retrying HTTP from {urlsplit(url).hostname} in {delay:g}s "
                        f"(attempt {_attempt + 1}/{self._policy.http_attempts_per_url})"
                    )
                    until = time.monotonic() + delay
                    while time.monotonic() < until:
                        progress.check_active()
                        time.sleep(min(0.1, until - time.monotonic()))
                transfer.remaining()
                attempts += 1
                try:
                    trace, integrity = http.attempt(
                        artifact,
                        url,
                        workspace,
                        payload_path,
                        state_path,
                        progress,
                        transfer,
                        single_stream=single_stream,
                    )
                except (httpx.HTTPError, OSError) as error:
                    last_error = (
                        str(error) if isinstance(error, _NativeHttpError) else type(error).__name__
                    )
                    fallback_reason = None
                    if isinstance(error, _NativeHttpError):
                        if error.code == 8:
                            fallback_reason = ParallelRangeFallbackReason.RANGE_RESPONSE_NOT_PARTIAL
                        elif (
                            error.code == 22
                            and error.bound_response
                            and artifact.source_hash is not None
                        ):
                            # aria2 may overwrite a rejected conditional response's
                            # diagnostic with "No URI available". A fresh pinned
                            # response can recover without reusing partial bytes.
                            fallback_reason = (
                                ParallelRangeFallbackReason.VALIDATOR_CHANGED
                                if error.http_status == 412
                                else ParallelRangeFallbackReason.RANGE_REQUEST_FAILED
                            )
                    if fallback_reason is not None:
                        http.clear(payload_path, state_path)
                        if not single_stream:
                            # Rebase once after discarding the layout. Only new
                            # payload bytes may renew the existing idle deadline.
                            transfer.restart_after_discard()
                        single_stream = True
                        fallbacks.append(
                            ParallelRangeFallback(
                                reason=fallback_reason,
                                source_host=urlsplit(url).hostname or "unknown",
                            )
                        )
                    continue
                if not integrity.source_hash_verified:
                    saw_corrupt = True
                    last_error = "HTTP payload failed its declared source hash"
                    http.clear(payload_path, state_path)
                    progress.update(artifact.artifact_id, 0)
                    transfer.update(0)
                    if trace.parallel_segments > 1:
                        single_stream = True
                        fallbacks.append(
                            ParallelRangeFallback(
                                reason=ParallelRangeFallbackReason.PAYLOAD_HASH_MISMATCH,
                                source_host=urlsplit(url).hostname or "unknown",
                                discarded_bytes=artifact.size,
                            )
                        )
                    continue
                return trace.model_copy(
                    update={
                        "attempts": attempts,
                        "parallel_range_fallbacks": tuple(fallbacks)
                        + trace.parallel_range_fallbacks,
                    }
                ), integrity
        if saw_corrupt:
            raise ArtifactCorruptError(
                f"all complete sources were corrupt for Artifact "
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
        implementation_version="aria2-http-download-v16",
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
    "InsufficientDiskError",
    "VerificationPolicy",
    "create_download_implementation",
    "create_verify_implementation",
    "safe_archive_name",
    "validate_downloaded_artifact",
    "validate_downloaded_artifact_structure",
]

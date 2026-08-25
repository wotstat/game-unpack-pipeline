from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from pydantic import ValidationError

from game_downloader._json import canonical_json_bytes
from game_downloader.models import (
    RunId,
    RunRecord,
    RunRequest,
    Stage,
    StageState,
    StageStatus,
)

_RUN_ID_PATTERN = re.compile(r"^run-[a-f0-9]{32}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def sha256_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class WorkspaceError(RuntimeError):
    pass


class RunNotFoundError(WorkspaceError):
    pass


class RunLockedError(WorkspaceError):
    pass


class WorkspaceCorruptError(WorkspaceError):
    pass


class CasCorruptionError(WorkspaceError):
    pass


class BlobValidationError(WorkspaceError):
    pass


class AdvisoryFileLock(AbstractContextManager["AdvisoryFileLock"]):
    """Unix advisory lock whose lifetime is tied to one open file descriptor."""

    def __init__(self, path: Path, *, label: str) -> None:
        self.path = path
        self.label = label
        self._file_descriptor: int | None = None

    def acquire(self, *, blocking: bool = False) -> AdvisoryFileLock:
        if self._file_descriptor is not None:
            raise RuntimeError(f"lock already acquired: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as exc:
                raise RunLockedError(f"{self.label} is already locked") from exc

            metadata = canonical_json_bytes(
                {
                    "acquired_at": utc_now().isoformat().replace("+00:00", "Z"),
                    "pid": os.getpid(),
                }
            )
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            remaining = memoryview(metadata)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
            os.fsync(descriptor)
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise
        self._file_descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._file_descriptor
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._file_descriptor = None

    def __enter__(self) -> AdvisoryFileLock:
        if self._file_descriptor is None:
            return self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    @staticmethod
    def is_held(path: Path) -> bool:
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class BlobCommit:
    sha256: str
    size: int
    path: Path
    relative_path: str
    reused: bool


class BlobStore:
    """Content-addressed blobs committed only after size and SHA-256 validation."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def put_bytes(
        self,
        data: bytes,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> BlobCommit:
        digest = hashlib.sha256(data).hexdigest()
        self._check_expectations(digest, len(data), expected_sha256, expected_size)
        target = self.path_for(digest)
        with self._blob_lock(digest):
            if target.is_symlink():
                raise CasCorruptionError(f"CAS path is a symlink: {target}")
            if target.exists():
                self._validate_existing(target, digest, len(data))
                return self._commit_descriptor(target, digest, len(data), reused=True)
            self._workspace.atomic_write_bytes(target, data, mode=0o444)
            return self._commit_descriptor(target, digest, len(data), reused=False)

    def put_file(
        self,
        source: Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> BlobCommit:
        source = source.absolute()
        source_stat = source.lstat()
        if not stat.S_ISREG(source_stat.st_mode) or source.is_symlink():
            raise BlobValidationError(f"CAS source must be a regular non-symlink file: {source}")
        digest, size = self._hash_file(source)
        self._check_expectations(digest, size, expected_sha256, expected_size)
        target = self.path_for(digest)
        with self._blob_lock(digest):
            if target.is_symlink():
                raise CasCorruptionError(f"CAS path is a symlink: {target}")
            if target.exists():
                self._validate_existing(target, digest, size)
                return self._commit_descriptor(target, digest, size, reused=True)
            self._copy_verified(source, target, digest, size)
            return self._commit_descriptor(target, digest, size, reused=False)

    def adopt_verified_file(
        self,
        source: Path,
        *,
        verified_sha256: str,
        expected_size: int,
    ) -> BlobCommit:
        """Atomically move an exclusively owned, already hashed file into the CAS.

        The caller must have calculated ``verified_sha256`` from the completed file while
        holding exclusive ownership of it.  This method still validates the path, file type,
        size and any pre-existing CAS entry, but deliberately avoids another full read and
        copy of a payload that was just verified by the producer.
        """

        self._validate_digest(verified_sha256)
        source = source.absolute()
        self._workspace._assert_destination(source)
        target = self.path_for(verified_sha256)
        if source == target:
            raise BlobValidationError("CAS adoption source is already the target path")
        source_stat = source.lstat()
        if not stat.S_ISREG(source_stat.st_mode) or source.is_symlink():
            raise BlobValidationError(f"CAS source must be a regular non-symlink file: {source}")
        if source_stat.st_size != expected_size:
            raise BlobValidationError(
                f"size mismatch: calculated {source_stat.st_size}, expected {expected_size}"
            )

        with self._blob_lock(verified_sha256):
            if target.is_symlink():
                raise CasCorruptionError(f"CAS path is a symlink: {target}")
            if target.exists():
                self._validate_existing(target, verified_sha256, expected_size)
                source.unlink()
                self._workspace.fsync_directory(source.parent)
                return self._commit_descriptor(
                    target,
                    verified_sha256,
                    expected_size,
                    reused=True,
                )

            self._workspace._assert_destination(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                source,
                os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                descriptor_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(descriptor_stat.st_mode)
                    or descriptor_stat.st_size != expected_size
                    or (descriptor_stat.st_dev, descriptor_stat.st_ino)
                    != (source_stat.st_dev, source_stat.st_ino)
                ):
                    raise BlobValidationError("CAS source changed after producer verification")
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.replace(source, target)
            except OSError as exc:
                if source.exists():
                    source.chmod(0o600)
                if exc.errno == errno.EXDEV:
                    raise BlobValidationError(
                        "CAS adoption requires source and target on the same filesystem"
                    ) from exc
                raise
            self._workspace.fsync_directory(target.parent)
            if source.parent != target.parent:
                self._workspace.fsync_directory(source.parent)
            return self._commit_descriptor(
                target,
                verified_sha256,
                expected_size,
                reused=False,
            )

    def path_for(self, digest: str) -> Path:
        self._validate_digest(digest)
        return self._workspace.blobs_root / digest[:2] / digest

    def _blob_lock(self, digest: str) -> AdvisoryFileLock:
        lock = AdvisoryFileLock(
            self._workspace.blobs_root / ".locks" / f"{digest}.lock",
            label=f"CAS blob {digest}",
        )
        return lock.acquire(blocking=True)

    def _copy_verified(self, source: Path, target: Path, digest: str, size: int) -> None:
        self._workspace._assert_destination(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        copied_hash = hashlib.sha256()
        copied_size = 0
        try:
            with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
                while chunk := source_file.read(1024 * 1024):
                    target_file.write(chunk)
                    copied_hash.update(chunk)
                    copied_size += len(chunk)
                target_file.flush()
                os.fchmod(target_file.fileno(), 0o444)
                os.fsync(target_file.fileno())
            if copied_size != size or copied_hash.hexdigest() != digest:
                raise BlobValidationError("CAS source changed while it was being copied")
            os.replace(temporary, target)
            self._workspace.fsync_directory(target.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            raise

    def _validate_existing(self, path: Path, digest: str, size: int) -> None:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path.is_symlink():
            raise CasCorruptionError(f"CAS path is not a regular file: {path}")
        if stat.S_IMODE(path_stat.st_mode) & 0o222:
            raise CasCorruptionError(f"CAS blob is unexpectedly writable: {path}")
        if path_stat.st_size != size:
            raise CasCorruptionError(
                f"CAS blob {digest} has size {path_stat.st_size}, expected {size}"
            )
        actual_digest, actual_size = self._hash_file(path)
        if actual_size != size or actual_digest != digest:
            raise CasCorruptionError(f"CAS blob content does not match its key: {path}")

    def _commit_descriptor(self, path: Path, digest: str, size: int, *, reused: bool) -> BlobCommit:
        return BlobCommit(
            sha256=digest,
            size=size,
            path=path,
            relative_path=path.relative_to(self._workspace.root).as_posix(),
            reused=reused,
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @classmethod
    def _check_expectations(
        cls,
        digest: str,
        size: int,
        expected_sha256: str | None,
        expected_size: int | None,
    ) -> None:
        if expected_sha256 is not None:
            cls._validate_digest(expected_sha256)
            if digest != expected_sha256:
                raise BlobValidationError(
                    f"SHA-256 mismatch: calculated {digest}, expected {expected_sha256}"
                )
        if expected_size is not None and size != expected_size:
            raise BlobValidationError(f"size mismatch: calculated {size}, expected {expected_size}")

    @staticmethod
    def _validate_digest(digest: str) -> None:
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")


class Workspace:
    """Own all mutable pipeline state beneath one explicit data root."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("workspace root must be an absolute path")
        self.root = root
        self.cache_root = root / "cache"
        self.partial_root = self.cache_root / "partial"
        self.blobs_root = self.cache_root / "blobs" / "sha256"
        self.runs_root = root / "runs"
        self.snapshots_root = root / "snapshots"
        self.tmp_root = root / "tmp"
        self.blobs = BlobStore(self)

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise WorkspaceError(f"workspace root must be a real directory: {self.root}")
        for directory in (
            self.partial_root,
            self.blobs_root,
            self.runs_root,
            self.snapshots_root,
            self.tmp_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def create_run(self, request: RunRequest) -> RunRecord:
        self.initialize()
        for _ in range(10):
            run_id = f"run-{uuid.uuid4().hex}"
            final = self.runs_root / run_id
            partial = self.runs_root / f".{run_id}.partial"
            try:
                partial.mkdir(mode=0o700)
            except FileExistsError:
                continue
            try:
                (partial / "stages").mkdir()
                (partial / "work").mkdir()
                (partial / "lock").touch(mode=0o600)
                record = RunRecord(run_id=run_id, request=request, created_at=utc_now())
                self.atomic_write_json(partial / "run.json", record.model_dump(mode="json"))
                os.replace(partial, final)
                self.fsync_directory(self.runs_root)
                return record
            except BaseException:
                shutil.rmtree(partial, ignore_errors=True)
                raise
        raise WorkspaceError("could not allocate a unique Run ID")

    def load_run(self, run_id: str) -> RunRecord:
        path = self.run_path(run_id) / "run.json"
        try:
            record = RunRecord.model_validate_json(self.read_bytes(path))
        except FileNotFoundError as exc:
            raise RunNotFoundError(f"Run does not exist: {run_id}") from exc
        except ValidationError as exc:
            raise WorkspaceCorruptError(f"invalid run.json for {run_id}: {exc}") from exc
        if record.run_id != run_id:
            raise WorkspaceCorruptError(f"run.json ID does not match directory: {run_id}")
        return record

    def run_path(self, run_id: str | RunId) -> Path:
        normalized = str(run_id)
        if _RUN_ID_PATTERN.fullmatch(normalized) is None:
            raise RunNotFoundError(f"invalid Run ID: {normalized}")
        path = self.runs_root / normalized
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise WorkspaceCorruptError(f"Run path is not a real directory: {path}")
        return path

    def stage_path(self, run_id: str | RunId, stage: Stage) -> Path:
        return self.run_path(run_id) / "stages" / stage.directory_name

    def stage_work_path(self, run_id: str | RunId, stage: Stage, input_digest: str) -> Path:
        digest_suffix = input_digest.removeprefix("sha256:")
        if _SHA256_PATTERN.fullmatch(digest_suffix) is None:
            raise ValueError("invalid stage input digest")
        return self.run_path(run_id) / "work" / stage.directory_name / digest_suffix

    def load_stage_status(self, run_id: str | RunId, stage: Stage) -> StageStatus:
        path = self.stage_path(run_id, stage) / "status.json"
        try:
            return StageStatus.model_validate_json(self.read_bytes(path))
        except FileNotFoundError:
            return StageStatus(stage=stage, state=StageState.PENDING)
        except ValidationError as exc:
            raise WorkspaceCorruptError(
                f"invalid status for Run {run_id}, stage {stage.value}: {exc}"
            ) from exc

    def write_stage_status(self, run_id: str | RunId, status: StageStatus) -> None:
        self.atomic_write_json(
            self.stage_path(run_id, status.stage) / "status.json",
            status.model_dump(mode="json"),
        )

    def run_lock(self, run_id: str | RunId) -> AdvisoryFileLock:
        run_path = self.run_path(run_id)
        if not run_path.is_dir():
            raise RunNotFoundError(f"Run does not exist: {run_id}")
        return AdvisoryFileLock(run_path / "lock", label=f"Run {run_id}")

    def is_run_locked(self, run_id: str | RunId) -> bool:
        return AdvisoryFileLock.is_held(self.run_path(run_id) / "lock")

    def atomic_write_json(self, destination: Path, value: object) -> None:
        self.atomic_write_bytes(destination, canonical_json_bytes(value))

    def atomic_write_bytes(self, destination: Path, data: bytes, *, mode: int = 0o600) -> None:
        self._assert_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(data)
                temporary_file.flush()
                os.fchmod(temporary_file.fileno(), mode)
                os.fsync(temporary_file.fileno())
            os.replace(temporary, destination)
            self.fsync_directory(destination.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            raise

    def read_bytes(self, path: Path) -> bytes:
        self._assert_destination(path)
        if path.is_symlink():
            raise WorkspaceCorruptError(f"workspace metadata must not be a symlink: {path}")
        return path.read_bytes()

    def _assert_destination(self, path: Path) -> None:
        if not path.is_absolute():
            raise WorkspaceError(f"workspace path must be absolute: {path}")
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"path escapes workspace root: {path}") from exc

        current = path.parent
        while current != self.root and current != current.parent:
            if current.exists() and current.is_symlink():
                raise WorkspaceError(f"workspace path traverses a symlink: {current}")
            current = current.parent

    @staticmethod
    def fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

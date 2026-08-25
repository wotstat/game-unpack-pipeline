from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from game_downloader.models import ClientType, RunRequest
from game_downloader.workspace import (
    BlobValidationError,
    CasCorruptionError,
    RunNotFoundError,
    Workspace,
)


def test_run_creation_is_atomic_and_request_is_canonical(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    request = RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("RU", "EN"))

    created = workspace.create_run(request)
    loaded = workspace.load_run(created.run_id)

    assert loaded == created
    assert loaded.request.languages == ("EN", "RU")
    assert not list(workspace.runs_root.glob(".*.partial"))


def test_all_language_selector_is_canonical_and_exclusive() -> None:
    request = RunRequest(target="wot-eu", client_type=ClientType.SD, languages=(" all ",))

    assert request.languages == ("ALL",)
    assert request.selects_all_languages
    with pytest.raises(ValidationError, match="cannot be combined"):
        RunRequest(target="wot-eu", client_type=ClientType.SD, languages=("all", "EN"))


def test_run_ids_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()

    with pytest.raises(RunNotFoundError, match="invalid Run ID"):
        workspace.load_run("../../outside")


def test_cas_blob_is_validated_then_reused(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    data = b"immutable artifact bytes"
    digest = hashlib.sha256(data).hexdigest()

    first = workspace.blobs.put_bytes(data, expected_sha256=digest, expected_size=len(data))
    second = workspace.blobs.put_bytes(data)

    assert first.sha256 == digest
    assert first.path.read_bytes() == data
    assert not first.reused
    assert second.reused
    assert second.path == first.path
    assert first.relative_path == f"cache/blobs/sha256/{digest[:2]}/{digest}"


def test_cas_commits_a_file_with_the_same_validation_rules(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact from transport")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    committed = workspace.blobs.put_file(
        source,
        expected_sha256=digest,
        expected_size=source.stat().st_size,
    )

    assert committed.path.read_bytes() == source.read_bytes()
    assert committed.path.stat().st_mode & 0o222 == 0


def test_cas_adopts_an_exclusively_owned_verified_file_without_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    source = workspace.tmp_root / "verified-payload.bin"
    source.write_bytes(b"already hashed by the producer")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    monkeypatch.setattr(
        workspace.blobs,
        "_hash_file",
        lambda _path: pytest.fail("adoption must not hash the producer-owned file again"),
    )
    committed = workspace.blobs.adopt_verified_file(
        source,
        verified_sha256=digest,
        expected_size=source.stat().st_size,
    )

    assert not source.exists()
    assert committed.path.read_bytes() == b"already hashed by the producer"
    assert committed.path.stat().st_mode & 0o222 == 0
    assert committed.sha256 == digest


def test_cas_adoption_rejects_a_size_changed_after_verification(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    source = workspace.tmp_root / "changed-payload.bin"
    source.write_bytes(b"changed")

    with pytest.raises(BlobValidationError, match="size mismatch"):
        workspace.blobs.adopt_verified_file(
            source,
            verified_sha256=hashlib.sha256(b"original").hexdigest(),
            expected_size=len(b"original"),
        )

    assert source.exists()


def test_cas_rejects_wrong_expectations_without_visible_blob(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()

    with pytest.raises(BlobValidationError, match="SHA-256 mismatch"):
        workspace.blobs.put_bytes(b"payload", expected_sha256="0" * 64)

    assert not workspace.blobs.path_for("0" * 64).exists()


def test_cas_detects_corruption_instead_of_reusing_it(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    committed = workspace.blobs.put_bytes(b"payload")
    committed.path.chmod(0o600)
    committed.path.write_bytes(b"corrupt")

    with pytest.raises(CasCorruptionError, match="CAS blob"):
        workspace.blobs.put_bytes(b"payload")


def test_concurrent_cas_writers_publish_one_blob(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    data = b"same artifact" * 100

    with ThreadPoolExecutor(max_workers=8) as executor:
        commits = list(executor.map(lambda _index: workspace.blobs.put_bytes(data), range(8)))

    assert sum(not commit.reused for commit in commits) == 1
    assert len({commit.path for commit in commits}) == 1

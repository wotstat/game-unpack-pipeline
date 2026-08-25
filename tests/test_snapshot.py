from __future__ import annotations

import errno
import hashlib
import os
import shutil
import threading
from pathlib import Path

import pytest

from game_downloader.models import (
    AcquisitionMode,
    AcquisitionPlan,
    ActionScriptFile,
    ChainBasis,
    ClientTreeFile,
    ClientTreeResult,
    ClientType,
    DiskSpaceEstimate,
    FileRepresentation,
    IndexedPackage,
    MaterializedFile,
    PartAcquisition,
    PartName,
    Publisher,
    ReadableFile,
    ReadableResult,
    RepresentationKind,
    ResolvedMetadata,
    ResolvedPart,
    ResolvedTarget,
    ResolveResult,
    StubFile,
    ToolIdentity,
    VfsCandidate,
    VfsIndexResult,
    VfsSourceKind,
)
from game_downloader.snapshot import (
    Snapshot,
    SnapshotAssembler,
    SnapshotVerificationError,
    SnapshotVerificationPolicy,
    SnapshotVerifier,
    _link_or_copy_into_partial,
)
from game_downloader.workspace import Workspace


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _readable_fixture(workspace: Workspace) -> tuple[ReadableResult, VfsIndexResult]:
    package_bytes = b"package-source"
    loose_bytes = b"catalog-source"
    package_candidate = VfsCandidate(
        source_kind=VfsSourceKind.GAME_PACKAGE,
        canonical_path="assets/value.bin",
        original_path="assets/value.bin",
        part=PartName.CLIENT,
        part_version="1.0",
        source_path="res/packages/base.pkg",
        source_sha256="a" * 64,
        precedence=0,
        zip_entry_index=0,
        compressed_size=len(package_bytes),
        uncompressed_size=len(package_bytes),
        crc32="00000000",
    )
    loose_candidate = VfsCandidate(
        source_kind=VfsSourceKind.LOOSE_FILE,
        canonical_path="text/messages.mo",
        original_path="text/messages.mo",
        part=PartName.LOCALE,
        language="EN",
        part_version="2.0",
        source_path="res/text/messages.mo",
        source_sha256=_digest(loose_bytes),
        precedence=1,
        uncompressed_size=len(loose_bytes),
    )
    swc_bytes = b"fixture-swc"
    swc_candidate = VfsCandidate(
        source_kind=VfsSourceKind.GAME_PACKAGE,
        canonical_path="gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
        original_path="gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
        part=PartName.CLIENT,
        part_version="1.0",
        source_path="res/packages/base.pkg",
        source_sha256="a" * 64,
        precedence=0,
        zip_entry_index=1,
        compressed_size=len(swc_bytes),
        uncompressed_size=len(swc_bytes),
        crc32="00000000",
    )
    materialized_package = MaterializedFile(
        path=package_candidate.canonical_path,
        size=len(package_bytes),
        sha256=_digest(package_bytes),
        source=package_candidate,
    )
    materialized_loose = MaterializedFile(
        path=loose_candidate.canonical_path,
        language="EN",
        size=len(loose_bytes),
        sha256=_digest(loose_bytes),
        source=loose_candidate,
    )
    materialized_swc = MaterializedFile(
        path=swc_candidate.canonical_path,
        size=len(swc_bytes),
        sha256=_digest(swc_bytes),
        source=swc_candidate,
    )
    readable_files = (
        ReadableFile(
            path="assets/value.bin",
            size=len(package_bytes),
            sha256=_digest(package_bytes),
            source=materialized_package,
            representation=FileRepresentation(
                kind=RepresentationKind.PASSTHROUGH,
                source_path="assets/value.bin",
                source_sha256=_digest(package_bytes),
            ),
        ),
        ReadableFile(
            path="text/messages.po",
            language="EN",
            size=len(b'msgid ""\nmsgstr ""\n'),
            sha256=_digest(b'msgid ""\nmsgstr ""\n'),
            source=materialized_loose,
            representation=FileRepresentation(
                kind=RepresentationKind.MO_TO_PO,
                source_path="text/messages.mo",
                source_sha256=_digest(loose_bytes),
                tool="fixture-mo",
                tool_version="1",
            ),
            diagnostics=("messages=1",),
        ),
        ReadableFile(
            path="gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
            size=len(swc_bytes),
            sha256=_digest(swc_bytes),
            source=materialized_swc,
            representation=FileRepresentation(
                kind=RepresentationKind.PASSTHROUGH,
                source_path="gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
                source_sha256=_digest(swc_bytes),
            ),
        ),
    )
    actionscript_bytes = b"package net.wg { public class App {} }\n"
    stub_bytes = b"def time() -> float: ...\n"
    actionscript_files = (
        ActionScriptFile(
            path="base_app/scripts/net/wg/App.as",
            size=len(actionscript_bytes),
            sha256=_digest(actionscript_bytes),
            source=materialized_swc,
            representation=FileRepresentation(
                kind=RepresentationKind.SWC_TO_AS,
                source_path="gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
                source_sha256=_digest(swc_bytes),
                tool="fixture-ffdec",
                tool_version="1",
            ),
            diagnostics=("language=actionscript-3",),
        ),
    )
    base_root = workspace.root / "readable/base"
    locale_root = workspace.root / "readable/locales/EN"
    actionscript_root = workspace.root / "readable/sources-as3"
    stubs_root = workspace.root / "readable/stubs"
    (base_root / "assets").mkdir(parents=True)
    (base_root / "gui/flash/swc").mkdir(parents=True)
    (locale_root / "text").mkdir(parents=True)
    (actionscript_root / "base_app/scripts/net/wg").mkdir(parents=True)
    stubs_root.mkdir(parents=True)
    (base_root / "assets/value.bin").write_bytes(package_bytes)
    (base_root / "gui/flash/swc/base_app-1.0-SNAPSHOT.swc").write_bytes(swc_bytes)
    (locale_root / "text/messages.po").write_bytes(b'msgid ""\nmsgstr ""\n')
    (actionscript_root / "base_app/scripts/net/wg/App.as").write_bytes(actionscript_bytes)
    (stubs_root / "BigWorld.pyi").write_bytes(stub_bytes)
    (base_root / "assets/value.bin").chmod(0o444)
    (base_root / "gui/flash/swc/base_app-1.0-SNAPSHOT.swc").chmod(0o444)
    (locale_root / "text/messages.po").chmod(0o444)
    (actionscript_root / "base_app/scripts/net/wg/App.as").chmod(0o444)
    (stubs_root / "BigWorld.pyi").chmod(0o444)
    readable = ReadableResult(
        materialization_result_sha256="sha256:" + "1" * 64,
        policy_name="fixture-readable",
        policy_version="1",
        policy_sha256="b" * 64,
        base_root="readable/base",
        locale_roots={"EN": "readable/locales/EN"},
        actionscript_root="readable/sources-as3",
        stubs_root="readable/stubs",
        tools=(
            ToolIdentity(name="fixture-mo", version="1"),
            ToolIdentity(name="fixture-ffdec", version="1"),
            ToolIdentity(name="game-downloader-readable", version="1"),
        ),
        files=readable_files,
        actionscript_files=actionscript_files,
        stub_files=(
            StubFile(
                path="BigWorld.pyi",
                size=len(stub_bytes),
                sha256=_digest(stub_bytes),
            ),
        ),
    )
    index = VfsIndexResult(
        client_tree_result_sha256="sha256:" + "2" * 64,
        policy_name="fixture-vfs",
        policy_version="1",
        policy_sha256="c" * 64,
        locale_languages=("EN",),
        packages=(
            IndexedPackage(
                path="res/packages/base.pkg",
                blob_sha256="a" * 64,
                blob_size=100,
                blob_path=f"cache/blobs/sha256/aa/{'a' * 64}",
                part=PartName.CLIENT,
                part_version="1.0",
                precedence=0,
                entries=1,
            ),
        ),
        entries=(),
    )
    return readable, index


def _client_tree_fixture(workspace: Workspace) -> ClientTreeResult:
    base_root = workspace.root / "client-tree/base"
    locale_root = workspace.root / "client-tree/locales/EN"
    base_root.mkdir(parents=True)
    locale_root.mkdir(parents=True)
    values = (
        (None, "Licenses.txt", b"licenses\n", PartName.CLIENT, "1.0"),
        (None, "mods/1.0/readme.txt", b"mods\n", PartName.CLIENT, "1.0"),
        (None, "paths.xml", b"<root/>\n", PartName.CLIENT, "1.0"),
        (None, "version.xml", b"<version/>\n", PartName.CLIENT, "1.0"),
        ("EN", "loc_version.xml", b"<locale/>\n", PartName.LOCALE, "2.0"),
        (None, "WorldOfTanks.exe", b"binary", PartName.CLIENT, "1.0"),
    )
    files: list[ClientTreeFile] = []
    for language, relative, data, part, part_version in values:
        root = locale_root if language is not None else base_root
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o444)
        digest = _digest(data)
        files.append(
            ClientTreeFile(
                path=relative,
                part=part,
                language=language,
                part_version=part_version,
                source_artifact_id="sha256:" + digest,
                source_blob_sha256=digest,
                source_entry_path=relative,
                blob_sha256=digest,
                blob_size=len(data),
                blob_path=f"cache/blobs/sha256/{digest[:2]}/{digest}",
                link_method="copy",
            )
        )
    return ClientTreeResult(
        verification_result_sha256="sha256:" + "3" * 64,
        base_root="client-tree/base",
        locale_roots={"EN": "client-tree/locales/EN"},
        files=tuple(files),
    )


def _source_fixture() -> tuple[ResolveResult, AcquisitionPlan]:
    parts = (
        ResolvedPart.model_construct(
            name=PartName.CLIENT,
            language=None,
            version="1.0",
            integrity=True,
            chain_basis=ChainBasis.EXPLICIT,
            transitions=(),
        ),
        ResolvedPart.model_construct(
            name=PartName.SD_CONTENT,
            language=None,
            version="1.0",
            integrity=True,
            chain_basis=ChainBasis.EXPLICIT,
            transitions=(),
        ),
        ResolvedPart.model_construct(
            name=PartName.LOCALE,
            language="EN",
            version="2.0",
            integrity=False,
            chain_basis=ChainBasis.ORDERED_ZERO_STATE,
            transitions=(),
        ),
    )
    resolve = ResolveResult.model_construct(
        resolved_target=ResolvedTarget(
            target="fixture",
            publisher=Publisher.WARGAMING,
            api_host="https://example.test",
            app_id="FIXTURE.APP",
        ),
        chain_id="fixture-chain",
        client_type=ClientType.SD,
        languages=("EN",),
        metadata_version="metadata-1",
        release_name="release-1",
        metadata=ResolvedMetadata.model_construct(
            requested_protocol_version="1",
            observed_protocol_version="1",
            observed_publishers=None,
            metadata_version="metadata-1",
            app_id="FIXTURE.APP",
            chain_id="fixture-chain",
            supported_languages=("EN",),
            default_language="EN",
            client_types=(),
        ),
        version_vector=parts,
        raw_responses=(),
    )
    acquisition_parts = tuple(
        PartAcquisition.model_construct(
            part=item.name,
            language=item.language,
            version=item.version,
            acquisition_mode=(
                AcquisitionMode.INSTALL_BUNDLE
                if item.name is PartName.LOCALE
                else AcquisitionMode.REFERENCE
            ),
            artifacts=(),
            torrent_descriptor_sha256s=(),
        )
        for item in parts
    )
    acquisition = AcquisitionPlan.model_construct(
        resolve_result_sha256="sha256:" + "0" * 64,
        parts=acquisition_parts,
        descriptors=(),
        raw_responses=(),
        disk_space=DiskSpaceEstimate(
            descriptor_bytes=0,
            download_bytes=0,
            assembled_bytes=0,
            reserve_bytes=0,
            required_free_bytes=0,
        ),
    )
    return resolve, acquisition


def _build_snapshot(tmp_path: Path) -> Snapshot:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    readable, index = _readable_fixture(workspace)
    client_tree = _client_tree_fixture(workspace)
    resolve, acquisition = _source_fixture()

    assembler = SnapshotAssembler()
    result = assembler.build(
        readable,
        client_tree,
        index,
        resolve,
        acquisition,
        workspace,
    )

    return Snapshot.open_and_verify(workspace.root / result.snapshot_path)


def test_snapshot_assembler_seals_and_independent_verifier_accepts_snapshot(
    tmp_path: Path,
) -> None:
    opened = _build_snapshot(tmp_path)

    assert opened.descriptor.contract_version == "1.1.0"
    assert opened.descriptor.manifests.files.records == 8
    assert opened.descriptor.manifests.actionscript.records == 1
    assert opened.descriptor.manifests.stubs.records == 1
    assert opened.descriptor.manifests.packages.records == 1
    assert opened.descriptor.manifests.conflicts.records == 0
    assert (opened.path / "sources/base/res/assets/value.bin").read_bytes() == b"package-source"
    assert (opened.path / "sources/base/paths.xml").is_file()
    assert (opened.path / "sources/base/version.xml").is_file()
    assert (opened.path / "sources/base/mods/1.0/readme.txt").is_file()
    assert not (opened.path / "sources/base/WorldOfTanks.exe").exists()
    assert (opened.path / "sources/locales/EN/loc_version.xml").is_file()
    assert (opened.path / "sources/locales/EN/res/text/messages.po").is_file()
    assert (opened.path / "sources-as3/base_app/scripts/net/wg/App.as").is_file()
    assert (opened.path / "stubs/BigWorld.pyi").read_bytes() == b"def time() -> float: ...\n"


def test_snapshot_assembler_reports_phase_timings(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    readable, index = _readable_fixture(workspace)
    client_tree = _client_tree_fixture(workspace)
    resolve, acquisition = _source_fixture()

    assembler = SnapshotAssembler()
    result = assembler.build(
        readable,
        client_tree,
        index,
        resolve,
        acquisition,
        workspace,
    )

    assert result.version_name == "release-1"
    timings = result.timings.model_dump(mode="python")
    assert set(timings) == {
        "populate_seconds",
        "seal_seconds",
        "verify_descriptor_seconds",
        "verify_manifests_seconds",
        "verify_payload_seconds",
        "publish_seconds",
    }
    assert all(value >= 0 for value in timings.values())
    assert timings["populate_seconds"] > 0
    assert timings["verify_payload_seconds"] > 0

    reused = assembler.build(
        readable,
        client_tree,
        index,
        resolve,
        acquisition,
        workspace,
    )

    assert reused.timings.populate_seconds == 0
    assert reused.timings.seal_seconds == 0
    assert reused.timings.publish_seconds == 0
    assert reused.timings.verify_payload_seconds > 0


def test_snapshot_partial_publisher_links_directly_without_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    source.chmod(0o444)
    destination = tmp_path / "partial/nested/value.bin"
    replacements: list[tuple[object, object]] = []
    monkeypatch.setattr(
        "game_downloader.snapshot.os.replace",
        lambda source_path, destination_path: replacements.append((source_path, destination_path)),
    )

    method = _link_or_copy_into_partial(source, destination, known_directories=set())

    assert method == "hardlink"
    assert destination.samefile(source)
    assert replacements == []


def test_snapshot_partial_publisher_falls_back_to_readonly_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    source.chmod(0o444)
    destination = tmp_path / "partial/value.bin"

    def cross_device_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr("game_downloader.snapshot.os.link", cross_device_link)

    method = _link_or_copy_into_partial(source, destination, known_directories=set())

    assert method == "copy"
    assert destination.read_bytes() == b"payload"
    assert destination.stat().st_mode & 0o222 == 0


def test_snapshot_verifier_hashes_payload_batches_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _build_snapshot(tmp_path)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    calls = 0
    worker_threads: set[int] = set()
    from game_downloader import snapshot as snapshot_module

    original = snapshot_module._hash_file_details

    def observed_hash(path: Path) -> tuple[str, int, bytes]:
        nonlocal calls
        with lock:
            calls += 1
            should_wait = calls <= 2
            worker_threads.add(threading.get_ident())
        if should_wait:
            barrier.wait(timeout=2)
        return original(path)

    monkeypatch.setattr(snapshot_module, "_hash_file_details", observed_hash)

    SnapshotVerifier(
        policy=SnapshotVerificationPolicy(
            max_workers=2,
            hash_batch_files=1,
            hash_batch_bytes=1,
            parallel_hash_minimum_bytes=0,
        )
    ).verify(opened.path)

    assert len(worker_threads) >= 2


@pytest.mark.parametrize("target", ["READY", "payload", "stub", "manifest"])
def test_snapshot_verifier_detects_corruption(tmp_path: Path, target: str) -> None:
    opened = _build_snapshot(tmp_path)
    corrupted = tmp_path / f"corrupted-{target}"
    shutil.copytree(opened.path, corrupted)
    if target == "READY":
        path = corrupted / "READY"
        path.chmod(0o644)
        path.write_text("sha256:" + "0" * 64 + "\n")
        path.chmod(0o444)
    elif target == "payload":
        path = corrupted / "sources/base/res/assets/value.bin"
        path.chmod(0o644)
        path.write_bytes(b"corrupted")
        path.chmod(0o444)
    elif target == "stub":
        path = corrupted / "stubs/BigWorld.pyi"
        path.chmod(0o644)
        path.write_bytes(b"corrupted")
        path.chmod(0o444)
    else:
        path = corrupted / "manifests/files.jsonl"
        path.chmod(0o644)
        with path.open("ab") as stream:
            stream.write(b"{}\n")
        path.chmod(0o444)

    with pytest.raises(SnapshotVerificationError):
        Snapshot.open_and_verify(corrupted)


def test_snapshot_verifier_rejects_payload_symlink(tmp_path: Path) -> None:
    opened = _build_snapshot(tmp_path)
    corrupted = tmp_path / "corrupted-symlink"
    shutil.copytree(opened.path, corrupted)
    path = corrupted / "sources/base/res/assets/value.bin"
    path.parent.chmod(0o755)
    path.unlink()
    os.symlink(corrupted / "snapshot.json", path)

    with pytest.raises(SnapshotVerificationError):
        Snapshot.open_and_verify(corrupted)


def test_snapshot_verifier_rejects_writable_payload_directory(tmp_path: Path) -> None:
    opened = _build_snapshot(tmp_path)
    corrupted = tmp_path / "corrupted-directory-mode"
    shutil.copytree(opened.path, corrupted)
    (corrupted / "sources/base/res/assets").chmod(0o755)

    with pytest.raises(SnapshotVerificationError, match="directory is writable"):
        Snapshot.open_and_verify(corrupted)

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from game_downloader.acquisition import ArtifactCorruptError
from game_downloader.client_tree import (
    ClientTreeAssembler,
    UnsupportedInstallBundleError,
    bytes_path_to_disk,
)
from game_downloader.models import (
    AcquisitionArtifact,
    AcquisitionMode,
    ArtifactVerification,
    BytesPath,
    ContainerKind,
    DownloadedArtifact,
    DownloadMethod,
    DownloadTrace,
    PartName,
    VerificationResult,
)
from game_downloader.torrent import bytes_path_from_text
from game_downloader.workspace import Workspace


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


def _verified(
    workspace: Workspace,
    data: bytes,
    *,
    path: str,
    part: PartName,
    language: str | None,
    mode: AcquisitionMode,
    container: ContainerKind,
    transition_from: str | None = None,
) -> ArtifactVerification:
    commit = workspace.blobs.put_bytes(data)
    artifact = AcquisitionArtifact(
        artifact_id=_digest(path),
        role="client-file" if mode is AcquisitionMode.REFERENCE else "delivery-bundle",
        part=part,
        language=language,
        part_version="1",
        acquisition_mode=mode,
        path=bytes_path_from_text(path),
        size=len(data),
        torrent_descriptor_sha256="1" * 64,
        transition_from=transition_from,
        transition_to="1" if mode is AcquisitionMode.INSTALL_BUNDLE else None,
    )
    downloaded = DownloadedArtifact(
        artifact=artifact,
        blob_sha256=commit.sha256,
        blob_size=commit.size,
        blob_path=commit.relative_path,
        source_hash_verified=False,
        reused=False,
        transport=DownloadTrace(
            method=DownloadMethod.WEB_SEED,
            requested_url="https://fixture.invalid/artifact",
            final_url="https://fixture.invalid/artifact",
        ),
    )
    return ArtifactVerification(
        download=downloaded,
        container=container,
        magic_hex=data[:16].hex(),
        container_verified=container is not ContainerKind.OPAQUE,
        entries=(
            len(zipfile.ZipFile(io.BytesIO(data)).infolist())
            if container is ContainerKind.ZIP
            else None
        ),
    )


def test_client_tree_links_reference_and_extracts_zero_state_bundle(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    reference = _verified(
        workspace,
        b"root config",
        path="paths.xml",
        part=PartName.CLIENT,
        language=None,
        mode=AcquisitionMode.REFERENCE,
        container=ContainerKind.OPAQUE,
    )
    bundle = _verified(
        workspace,
        _zip_bytes(
            {
                "_service/service.xml": (
                    b"<protocol><patch_service_info><files_to_delete>"
                    b"<file>old.txt</file></files_to_delete>"
                    b"</patch_service_info></protocol>"
                ),
                "res/text/lc_messages/example.mo": b"gettext bytes",
            }
        ),
        path="locale.wgpkg",
        part=PartName.LOCALE,
        language="EN",
        mode=AcquisitionMode.INSTALL_BUNDLE,
        container=ContainerKind.ZIP,
        transition_from="0",
    )
    verified = VerificationResult(
        download_result_sha256=_digest("download"),
        artifacts=(reference, bundle),
    )

    result = ClientTreeAssembler().assemble(verified, workspace, tmp_path / "work")

    base = workspace.root / result.base_root
    locale = workspace.root / result.locale_roots["EN"]
    assert (base / "paths.xml").read_bytes() == b"root config"
    assert (locale / "res/text/lc_messages/example.mo").read_bytes() == b"gettext bytes"
    assert not (locale / "_service").exists()
    assert len(result.files) == 2
    assert all((workspace.root / item.blob_path).exists() for item in result.files)


def test_client_tree_rejects_nonzero_delta_service(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = (
        b"<protocol><patch_service_info><files_to_apply_diff>"
        b'<file result_size="3">res/file.pkg</file>'
        b"</files_to_apply_diff></patch_service_info></protocol>"
    )
    bundle = _verified(
        workspace,
        _zip_bytes({"_service/service.xml": service, "res/file.pkg.123.rdiff": b"diff"}),
        path="locale.wgpkg",
        part=PartName.LOCALE,
        language="EN",
        mode=AcquisitionMode.INSTALL_BUNDLE,
        container=ContainerKind.ZIP,
        transition_from="0",
    )
    verified = VerificationResult(
        download_result_sha256=_digest("download"),
        artifacts=(bundle,),
    )

    with pytest.raises(UnsupportedInstallBundleError, match="delta application"):
        ClientTreeAssembler().assemble(verified, workspace, tmp_path / "work")


def test_client_tree_rejects_nonzero_transition_before_extraction(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    bundle = _verified(
        workspace,
        _zip_bytes({"res/file": b"replacement"}),
        path="client.wgpkg",
        part=PartName.CLIENT,
        language=None,
        mode=AcquisitionMode.INSTALL_BUNDLE,
        container=ContainerKind.ZIP,
        transition_from="old-version",
    )
    verified = VerificationResult(
        download_result_sha256=_digest("download"),
        artifacts=(bundle,),
    )

    with pytest.raises(UnsupportedInstallBundleError, match="non-zero"):
        ClientTreeAssembler().assemble(verified, workspace, tmp_path / "work")


def test_client_tree_accepts_deletion_only_locale_bundle_as_zero_state_noop(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    reference = _verified(
        workspace,
        b"root config",
        path="paths.xml",
        part=PartName.CLIENT,
        language=None,
        mode=AcquisitionMode.REFERENCE,
        container=ContainerKind.OPAQUE,
    )
    service = (
        b'<protocol name="service" version="1.2"><patch_service_info>'
        b"<delete_at_the_beginning>true</delete_at_the_beginning>"
        b"<files_to_delete><directory>res\\text\\lc_messages</directory>"
        b"</files_to_delete></patch_service_info></protocol>"
    )
    bundle = _verified(
        workspace,
        _zip_bytes({"_service/service.xml": service}),
        path="locale.wgpkg",
        part=PartName.LOCALE,
        language="RU",
        mode=AcquisitionMode.INSTALL_BUNDLE,
        container=ContainerKind.ZIP,
    )
    verified = VerificationResult(
        download_result_sha256=_digest("download"),
        artifacts=(reference, bundle),
    )

    result = ClientTreeAssembler().assemble(verified, workspace, tmp_path / "work")

    locale = workspace.root / result.locale_roots["RU"]
    assert locale.is_dir()
    assert list(locale.iterdir()) == []
    assert [item.path for item in result.files] == ["paths.xml"]


@pytest.mark.parametrize(
    "entries",
    [
        {},
        {
            "_service/service.xml": (
                b"<protocol><patch_service_info></patch_service_info></protocol>"
            )
        },
    ],
)
def test_client_tree_rejects_empty_bundle_without_deletion_only_semantics(
    tmp_path: Path,
    entries: dict[str, bytes],
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    reference = _verified(
        workspace,
        b"root config",
        path="paths.xml",
        part=PartName.CLIENT,
        language=None,
        mode=AcquisitionMode.REFERENCE,
        container=ContainerKind.OPAQUE,
    )
    bundle = _verified(
        workspace,
        _zip_bytes(entries),
        path="locale.wgpkg",
        part=PartName.LOCALE,
        language="RU",
        mode=AcquisitionMode.INSTALL_BUNDLE,
        container=ContainerKind.ZIP,
    )
    verified = VerificationResult(
        download_result_sha256=_digest("download"),
        artifacts=(reference, bundle),
    )

    with pytest.raises(ArtifactCorruptError, match="contains no installable files"):
        ClientTreeAssembler().assemble(verified, workspace, tmp_path / "work")


def test_bytes_path_disk_encoding_is_reversible_and_collision_safe() -> None:
    invalid_utf8 = BytesPath(components_base64=("/w==",), utf8=None)
    literal_percent = bytes_path_from_text("%FF")

    assert bytes_path_to_disk(invalid_utf8) == "%FF"
    assert bytes_path_to_disk(literal_percent) == "%25FF"

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from game_downloader.models import (
    ClientTreeFile,
    ClientTreeResult,
    ClientType,
    PartName,
)
from game_downloader.vfs import VfsIndexer, VfsMaterializer, VfsOrderUnknownError, VfsPolicy
from game_downloader.workspace import Workspace


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def _tree_file(
    workspace: Workspace,
    root: Path,
    relative: str,
    data: bytes,
    *,
    part: PartName,
    language: str | None = None,
) -> ClientTreeFile:
    commit = workspace.blobs.put_bytes(data)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(commit.path, destination)
    return ClientTreeFile(
        path=relative,
        part=part,
        language=language,
        part_version="1",
        source_artifact_id=_digest(f"artifact:{language}:{relative}"),
        source_blob_sha256=commit.sha256,
        blob_sha256=commit.sha256,
        blob_size=commit.size,
        blob_path=commit.relative_path,
        link_method="hardlink",
    )


def _client_tree(workspace: Workspace) -> ClientTreeResult:
    base = workspace.root / "client-tree/base"
    locale = workspace.root / "client-tree/locales/EN"
    base.mkdir(parents=True)
    locale.mkdir(parents=True)
    paths = b"""<root><Paths><Packages>
      <Package type="sd,hd">./res/packages/z-first.pkg</Package>
      <Package type="sd,hd">./res/packages/a-second.pkg</Package>
    </Packages><Path>./res</Path></Paths></root>"""
    first = _zip_bytes(
        [("same.txt", b"first winner"), ("Case.TXT", b"first case"), ("only.bin", b"only")]
    )
    second = _zip_bytes([("same.txt", b"second loser"), ("case.txt", b"second case")])
    files = (
        _tree_file(workspace, base, "paths.xml", paths, part=PartName.CLIENT),
        _tree_file(
            workspace,
            base,
            "res/packages/z-first.pkg",
            first,
            part=PartName.CLIENT,
        ),
        _tree_file(
            workspace,
            base,
            "res/packages/a-second.pkg",
            second,
            part=PartName.SD_CONTENT,
        ),
        _tree_file(workspace, base, "res/same.txt", b"loose loser", part=PartName.CLIENT),
        _tree_file(
            workspace,
            locale,
            "res/text/example.mo",
            b"locale bytes",
            part=PartName.LOCALE,
            language="EN",
        ),
    )
    return ClientTreeResult(
        verification_result_sha256=_digest("verification"),
        base_root=base.relative_to(workspace.root).as_posix(),
        locale_roots={"EN": locale.relative_to(workspace.root).as_posix()},
        files=files,
    )


def test_vfs_uses_paths_xml_order_resolves_conflicts_and_materializes_winners(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    tree = _client_tree(workspace)
    policy = VfsPolicy(materialize_workers=2)

    index = VfsIndexer(policy).index(tree, workspace, ClientType.SD)
    same = next(item for item in index.entries if item.lookup_key == "same.txt")
    case = next(item for item in index.entries if item.lookup_key == "case.txt")
    materialized = VfsMaterializer(policy).materialize(
        index,
        workspace,
        tmp_path / "work",
        locale_languages=tuple(tree.locale_roots),
    )

    assert [item.path for item in index.packages] == [
        "res/packages/z-first.pkg",
        "res/packages/a-second.pkg",
    ]
    assert same.winner.source_path == "res/packages/z-first.pkg"
    assert len(same.candidates) == 3
    assert case.winner.canonical_path == "Case.TXT"
    assert len(case.candidates) == 2
    base = workspace.root / materialized.base_root
    locale = workspace.root / materialized.locale_roots["EN"]
    assert (base / "same.txt").read_bytes() == b"first winner"
    assert (base / "Case.TXT").read_bytes() == b"first case"
    assert (base / "only.bin").read_bytes() == b"only"
    assert (locale / "text/example.mo").read_bytes() == b"locale bytes"
    assert "Case.TXT" in {item.name for item in base.iterdir()}


def test_vfs_preserves_empty_requested_locale_root(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    tree = _client_tree(workspace)
    tree = tree.model_copy(
        update={"files": tuple(item for item in tree.files if item.language is None)}
    )

    index = VfsIndexer().index(tree, workspace, ClientType.SD)
    materialized = VfsMaterializer().materialize(
        index,
        workspace,
        tmp_path / "work",
        locale_languages=tuple(tree.locale_roots),
    )

    locale = workspace.root / materialized.locale_roots["EN"]
    assert locale.is_dir()
    assert list(locale.iterdir()) == []


def test_vfs_rejects_package_not_ordered_by_paths_xml(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    tree = _client_tree(workspace)
    extra_data = _zip_bytes([("extra", b"data")])
    base = workspace.root / tree.base_root
    extra = _tree_file(
        workspace,
        base,
        "res/packages/not-listed.pkg",
        extra_data,
        part=PartName.CLIENT,
    )
    tree = tree.model_copy(update={"files": (*tree.files, extra)})

    with pytest.raises(VfsOrderUnknownError, match="not ordered"):
        VfsIndexer().index(tree, workspace, ClientType.SD)


def test_vfs_rejects_ambiguous_duplicate_inside_one_package(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    base = workspace.root / "client-tree/base"
    locale = workspace.root / "client-tree/locales/EN"
    base.mkdir(parents=True)
    locale.mkdir(parents=True)
    paths = (
        b"<root><Paths><Packages><Package type='sd'>"
        b"./res/packages/duplicate.pkg</Package></Packages></Paths></root>"
    )
    with pytest.warns(UserWarning, match="Duplicate name"):
        package = _zip_bytes([("same", b"one"), ("same", b"two")])
    tree = ClientTreeResult(
        verification_result_sha256=_digest("verification"),
        base_root=base.relative_to(workspace.root).as_posix(),
        locale_roots={"EN": locale.relative_to(workspace.root).as_posix()},
        files=(
            _tree_file(workspace, base, "paths.xml", paths, part=PartName.CLIENT),
            _tree_file(
                workspace,
                base,
                "res/packages/duplicate.pkg",
                package,
                part=PartName.CLIENT,
            ),
        ),
    )

    with pytest.raises(VfsOrderUnknownError, match="ambiguous"):
        VfsIndexer().index(tree, workspace, ClientType.SD)

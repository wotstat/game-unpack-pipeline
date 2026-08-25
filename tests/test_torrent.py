from __future__ import annotations

import base64
from pathlib import Path

import pytest

from game_downloader.torrent import (
    TorrentFormatError,
    UnsafeTorrentPathError,
    decode_bytes_path,
    parse_torrent,
    torrent_source_components,
)

FIXTURES = Path(__file__).parent / "fixtures/torrent"


def torrent_fixture(name: str) -> bytes:
    return base64.b64decode((FIXTURES / name).read_text(encoding="ascii"))


def bencode(value: object) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        if not all(isinstance(key, bytes) for key in value):
            raise TypeError("test bencode dictionary keys must be bytes")
        keys = sorted(value)
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in keys) + b"e"
    raise TypeError(f"unsupported test bencode value: {type(value).__name__}")


def test_parse_real_multifile_torrent_preserves_byte_paths_and_hashes() -> None:
    metainfo = parse_torrent(torrent_fixture("reference-multifile.torrent.b64"))

    assert metainfo.info_hash_sha1 == "4bbda1151a8325a75b4c10d63c9ceb43cfde4681"
    assert metainfo.name.utf8 == "fixture-reference"
    assert metainfo.piece_length == 4
    assert metainfo.piece_count == 3
    assert metainfo.total_size == 10
    assert len(metainfo.files) == 3
    assert metainfo.files[0].source_sha1 == "11" * 20
    assert metainfo.files[2].padding is True
    assert metainfo.files[1].path.utf8 is None
    assert decode_bytes_path(metainfo.files[1].path) == (b"locale", b"\xff.bin")
    assert torrent_source_components(metainfo, metainfo.files[0]) == (
        b"fixture-reference",
        b"res",
        b"a.bin",
    )


@pytest.mark.parametrize(
    "path, attributes",
    [
        ([b"..", b"escape"], b""),
        ([b"safe"], b"l"),
        ([b"bad/name"], b""),
        ([b"line\nbreak"], b""),
    ],
)
def test_torrent_rejects_unsafe_paths_and_symlinks(
    path: list[bytes],
    attributes: bytes,
) -> None:
    file_entry: dict[bytes, object] = {
        b"length": 1,
        b"path": path,
        b"sha1": b"x" * 20,
    }
    if attributes:
        file_entry[b"attr"] = attributes
    raw = bencode(
        {
            b"info": {
                b"files": [file_entry],
                b"name": b"fixture",
                b"piece length": 1,
                b"pieces": b"p" * 20,
            }
        }
    )

    with pytest.raises(UnsafeTorrentPathError):
        parse_torrent(raw)


def test_torrent_rejects_piece_count_mismatch_and_noncanonical_dictionary() -> None:
    mismatched = bencode(
        {
            b"info": {
                b"length": 5,
                b"name": b"file",
                b"piece length": 4,
                b"pieces": b"p" * 20,
            }
        }
    )

    with pytest.raises(TorrentFormatError, match="piece hashes"):
        parse_torrent(mismatched)
    with pytest.raises(TorrentFormatError, match="unsorted"):
        parse_torrent(b"d1:bi1e1:ai2ee")

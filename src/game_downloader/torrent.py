from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from pydantic import ValidationError

from game_downloader.models import BytesPath, TorrentFile, TorrentMetainfo


class TorrentFormatError(ValueError):
    pass


class UnsafeTorrentPathError(TorrentFormatError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentLimits:
    max_metainfo_bytes: int = 16 * 1024 * 1024
    max_depth: int = 64
    max_values: int = 1_000_000
    max_files: int = 200_000
    max_pieces: int = 2_000_000


type BValue = int | bytes | list[BValue] | dict[bytes, BValue]


class _BencodeParser:
    def __init__(self, raw: bytes, limits: TorrentLimits) -> None:
        self.raw = raw
        self.limits = limits
        self.position = 0
        self.values = 0
        self.info_span: tuple[int, int] | None = None

    def parse(self) -> BValue:
        if not self.raw:
            raise TorrentFormatError("torrent metainfo is empty")
        if len(self.raw) > self.limits.max_metainfo_bytes:
            raise TorrentFormatError("torrent metainfo exceeds the byte limit")
        value = self._value(0, top_level=True)
        if self.position != len(self.raw):
            raise TorrentFormatError("torrent metainfo has trailing bytes")
        return value

    def _value(self, depth: int, *, top_level: bool = False) -> BValue:
        if depth > self.limits.max_depth:
            raise TorrentFormatError("bencode nesting exceeds the depth limit")
        self.values += 1
        if self.values > self.limits.max_values:
            raise TorrentFormatError("bencode value count exceeds the limit")
        if self.position >= len(self.raw):
            raise TorrentFormatError("unexpected end of bencode input")
        marker = self.raw[self.position]
        if marker == ord("i"):
            return self._integer()
        if marker == ord("l"):
            return self._list(depth)
        if marker == ord("d"):
            return self._dictionary(depth, top_level=top_level)
        if ord("0") <= marker <= ord("9"):
            return self._bytes()
        raise TorrentFormatError(f"invalid bencode marker at byte {self.position}")

    def _integer(self) -> int:
        self.position += 1
        end = self.raw.find(b"e", self.position)
        if end < 0:
            raise TorrentFormatError("unterminated bencode integer")
        encoded = self.raw[self.position : end]
        if not encoded:
            raise TorrentFormatError("empty bencode integer")
        if encoded == b"-0" or encoded.startswith(b"+"):
            raise TorrentFormatError("non-canonical bencode integer")
        unsigned = encoded[1:] if encoded.startswith(b"-") else encoded
        if not unsigned.isdigit() or (len(unsigned) > 1 and unsigned.startswith(b"0")):
            raise TorrentFormatError("non-canonical bencode integer")
        self.position = end + 1
        return int(encoded)

    def _bytes(self) -> bytes:
        colon = self.raw.find(b":", self.position)
        if colon < 0:
            raise TorrentFormatError("unterminated bencode byte-string length")
        encoded_length = self.raw[self.position : colon]
        if (
            not encoded_length
            or not encoded_length.isdigit()
            or (len(encoded_length) > 1 and encoded_length.startswith(b"0"))
        ):
            raise TorrentFormatError("non-canonical bencode byte-string length")
        length = int(encoded_length)
        start = colon + 1
        end = start + length
        if end > len(self.raw):
            raise TorrentFormatError("bencode byte string exceeds the input")
        self.position = end
        return self.raw[start:end]

    def _list(self, depth: int) -> list[BValue]:
        self.position += 1
        result: list[BValue] = []
        while True:
            if self.position >= len(self.raw):
                raise TorrentFormatError("unterminated bencode list")
            if self.raw[self.position] == ord("e"):
                self.position += 1
                return result
            result.append(self._value(depth + 1))

    def _dictionary(self, depth: int, *, top_level: bool) -> dict[bytes, BValue]:
        self.position += 1
        result: dict[bytes, BValue] = {}
        previous_key: bytes | None = None
        while True:
            if self.position >= len(self.raw):
                raise TorrentFormatError("unterminated bencode dictionary")
            if self.raw[self.position] == ord("e"):
                self.position += 1
                return result
            key = self._bytes()
            if previous_key is not None and key <= previous_key:
                raise TorrentFormatError("bencode dictionary keys are duplicate or unsorted")
            previous_key = key
            value_start = self.position
            value = self._value(depth + 1)
            if top_level and key == b"info":
                self.info_span = (value_start, self.position)
            result[key] = value


def _dictionary(value: BValue, field: str) -> dict[bytes, BValue]:
    if not isinstance(value, dict):
        raise TorrentFormatError(f"torrent field {field!r} must be a dictionary")
    return value


def _list(value: BValue, field: str) -> list[BValue]:
    if not isinstance(value, list):
        raise TorrentFormatError(f"torrent field {field!r} must be a list")
    return value


def _bytes(value: BValue, field: str) -> bytes:
    if not isinstance(value, bytes):
        raise TorrentFormatError(f"torrent field {field!r} must be bytes")
    return value


def _integer(value: BValue, field: str) -> int:
    if not isinstance(value, int):
        raise TorrentFormatError(f"torrent field {field!r} must be an integer")
    return value


def _required(mapping: dict[bytes, BValue], key: bytes, field: str) -> BValue:
    try:
        return mapping[key]
    except KeyError as exc:
        raise TorrentFormatError(f"required torrent field {field!r} is missing") from exc


def bytes_path(components: tuple[bytes, ...]) -> BytesPath:
    if not components:
        raise UnsafeTorrentPathError("torrent path has no components")
    try:
        utf8 = "/".join(component.decode("utf-8") for component in components)
    except UnicodeDecodeError:
        utf8 = None
    try:
        return BytesPath(
            components_base64=tuple(
                base64.b64encode(component).decode("ascii") for component in components
            ),
            utf8=utf8,
        )
    except ValidationError as exc:
        raise UnsafeTorrentPathError(f"unsafe torrent path: {exc}") from exc


def bytes_path_from_text(value: str) -> BytesPath:
    return bytes_path(tuple(component.encode("utf-8") for component in value.split("/")))


def decode_bytes_path(path: BytesPath) -> tuple[bytes, ...]:
    return tuple(base64.b64decode(component) for component in path.components_base64)


def torrent_source_components(
    metainfo: TorrentMetainfo,
    file: TorrentFile,
) -> tuple[bytes, ...]:
    file_components = decode_bytes_path(file.path)
    if metainfo.multi_file:
        return (*decode_bytes_path(metainfo.name), *file_components)
    return file_components


def parse_torrent(data: bytes, limits: TorrentLimits | None = None) -> TorrentMetainfo:
    selected_limits = limits or TorrentLimits()
    parser = _BencodeParser(data, selected_limits)
    root = _dictionary(parser.parse(), "root")
    info = _dictionary(_required(root, b"info", "info"), "info")
    if parser.info_span is None:
        raise TorrentFormatError("torrent info dictionary span was not captured")

    name = _bytes(_required(info, b"name", "info.name"), "info.name")
    name_path = bytes_path((name,))
    piece_length = _integer(
        _required(info, b"piece length", "info.piece length"), "info.piece length"
    )
    if piece_length <= 0:
        raise TorrentFormatError("torrent piece length must be positive")
    pieces = _bytes(_required(info, b"pieces", "info.pieces"), "info.pieces")
    if not pieces or len(pieces) % 20 != 0:
        raise TorrentFormatError("torrent pieces must contain complete SHA-1 digests")
    piece_count = len(pieces) // 20
    if piece_count > selected_limits.max_pieces:
        raise TorrentFormatError("torrent piece count exceeds the limit")

    has_files = b"files" in info
    has_length = b"length" in info
    if has_files == has_length:
        raise TorrentFormatError("torrent must use exactly one single/multi-file layout")

    files: list[TorrentFile] = []
    if has_files:
        raw_files = _list(_required(info, b"files", "info.files"), "info.files")
        if not raw_files or len(raw_files) > selected_limits.max_files:
            raise TorrentFormatError("torrent file count is empty or exceeds the limit")
        for index, raw_file in enumerate(raw_files):
            file_mapping = _dictionary(raw_file, f"info.files[{index}]")
            size = _integer(_required(file_mapping, b"length", "file.length"), "file.length")
            if size < 0:
                raise TorrentFormatError("torrent file length must be non-negative")
            raw_path = _list(_required(file_mapping, b"path", "file.path"), "file.path")
            path = bytes_path(
                tuple(_bytes(component, "file.path component") for component in raw_path)
            )
            attributes_value = file_mapping.get(b"attr", b"")
            attributes = _bytes(attributes_value, "file.attr")
            if b"l" in attributes:
                raise UnsafeTorrentPathError("torrent contains a symlink entry")
            sha1_value = file_mapping.get(b"sha1")
            source_sha1: str | None = None
            if sha1_value is not None:
                raw_sha1 = _bytes(sha1_value, "file.sha1")
                if len(raw_sha1) != 20:
                    raise TorrentFormatError("torrent file SHA-1 must contain 20 bytes")
                source_sha1 = raw_sha1.hex()
            files.append(
                TorrentFile(
                    path=path,
                    size=size,
                    source_sha1=source_sha1,
                    padding=b"p" in attributes,
                )
            )
    else:
        size = _integer(_required(info, b"length", "info.length"), "info.length")
        if size < 0:
            raise TorrentFormatError("torrent file length must be non-negative")
        sha1_value = info.get(b"sha1")
        source_sha1 = None
        if sha1_value is not None:
            raw_sha1 = _bytes(sha1_value, "info.sha1")
            if len(raw_sha1) != 20:
                raise TorrentFormatError("torrent file SHA-1 must contain 20 bytes")
            source_sha1 = raw_sha1.hex()
        files.append(TorrentFile(path=name_path, size=size, source_sha1=source_sha1))

    total_size = sum(file.size for file in files)
    if total_size <= 0:
        raise TorrentFormatError("torrent payload must not be empty")
    expected_pieces = (total_size + piece_length - 1) // piece_length
    if piece_count != expected_pieces:
        raise TorrentFormatError(
            f"torrent has {piece_count} piece hashes, expected {expected_pieces}"
        )
    info_start, info_end = parser.info_span
    try:
        return TorrentMetainfo(
            info_hash_sha1=hashlib.sha1(
                data[info_start:info_end], usedforsecurity=False
            ).hexdigest(),
            name=name_path,
            multi_file=has_files,
            piece_length=piece_length,
            piece_count=piece_count,
            pieces_sha256=hashlib.sha256(pieces).hexdigest(),
            files=tuple(files),
            total_size=total_size,
        )
    except ValidationError as exc:
        if "path" in str(exc).lower():
            raise UnsafeTorrentPathError(str(exc)) from exc
        raise TorrentFormatError(f"invalid torrent metainfo semantics: {exc}") from exc


__all__ = [
    "TorrentFormatError",
    "TorrentLimits",
    "UnsafeTorrentPathError",
    "bytes_path",
    "bytes_path_from_text",
    "decode_bytes_path",
    "parse_torrent",
    "torrent_source_components",
]

from __future__ import annotations

import hashlib
import json
from typing import cast

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class NotJsonValueError(ValueError):
    """Raised when a value cannot be represented by the canonical JSON codec."""


def canonical_json_bytes(value: object) -> bytes:
    """Encode a JSON value in the one canonical form used for local digests."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise NotJsonValueError(str(exc)) from exc
    return encoded.encode("utf-8") + b"\n"


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_sha256_digest(value: object) -> str:
    return f"sha256:{canonical_sha256(value)}"


def as_json_object(value: object) -> JsonObject:
    """Round-trip an object through JSON and require an object at the top level."""

    encoded = canonical_json_bytes(value)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise NotJsonValueError("the top-level value must be a JSON object")
    return cast(JsonObject, decoded)

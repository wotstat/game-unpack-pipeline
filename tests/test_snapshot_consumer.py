from __future__ import annotations

import hashlib
import json
from pathlib import Path

from game_downloader.contracts import ContractRegistry
from game_downloader.models import BaseLayer, FileManifestEntryV1, LocaleLayer

FIXTURE = Path(__file__).parent / "fixtures/snapshot-v1"


def _manifest_lines(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(json.loads(line) for line in path.read_bytes().splitlines())


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        + b"\n"
    )


def test_contract_only_consumer_can_verify_and_overlay_fixture() -> None:
    registry = ContractRegistry()
    descriptor_bytes = (FIXTURE / "snapshot.json").read_bytes()
    descriptor = registry.validate_game_snapshot(json.loads(descriptor_bytes))
    assert descriptor_bytes == _canonical_json(
        descriptor.model_dump(mode="json", exclude_none=True)
    )
    assert (FIXTURE / "READY").read_text() == (
        "sha256:" + hashlib.sha256(descriptor_bytes).hexdigest() + "\n"
    )

    for name in ("files", "actionscript", "stubs", "packages", "conflicts"):
        reference = getattr(descriptor.manifests, name)
        encoded = (FIXTURE / reference.path).read_bytes()
        assert hashlib.sha256(encoded).hexdigest() == reference.sha256
        assert len(encoded.splitlines()) == reference.records

    entries = tuple(
        registry.validate("file-manifest-entry", raw)
        for raw in _manifest_lines(FIXTURE / descriptor.manifests.files.path)
    )
    selected: dict[str, FileManifestEntryV1] = {}
    for entry in entries:
        assert isinstance(entry, FileManifestEntryV1)
        if isinstance(entry.layer, BaseLayer):
            selected[entry.path] = entry
    for entry in entries:
        assert isinstance(entry, FileManifestEntryV1)
        if isinstance(entry.layer, LocaleLayer) and entry.layer.language == "EN":
            selected[entry.path] = entry

    effective = selected["res/config/value.txt"]
    assert isinstance(effective.layer, LocaleLayer)
    payload = FIXTURE / descriptor.payload.locale_roots["EN"] / effective.path
    assert payload.read_text() == "english\n"
    assert hashlib.sha256(payload.read_bytes()).hexdigest() == effective.sha256

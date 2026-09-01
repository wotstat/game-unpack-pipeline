from __future__ import annotations

from copy import deepcopy

import pytest

from game_downloader.contracts import ContractRegistry, ContractValidationError
from game_downloader.models import (
    ActionScriptManifestEntryV1,
    ConflictManifestEntryV1,
    FileManifestEntryV1,
    GameSnapshotV1,
    PackageManifestEntry,
    StubManifestEntryV1,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def test_checked_in_snapshot_example_matches_schema_and_model() -> None:
    snapshot = ContractRegistry().validate_example()

    assert isinstance(snapshot, GameSnapshotV1)
    assert snapshot.contract_version == "1.1.0"
    assert snapshot.source.languages == ("EN",)


def test_contract_schema_remains_normative_for_extra_fields() -> None:
    registry = ContractRegistry()
    document = deepcopy(registry.example())
    document["unexpected"] = True

    with pytest.raises(ContractValidationError, match="Additional properties"):
        registry.validate_game_snapshot(document)


def test_contract_supports_loose_file_provenance() -> None:
    registry = ContractRegistry()
    document = {
        "path": "text/messages.po",
        "layer": {"kind": "locale", "language": "EN"},
        "size": 12,
        "sha256": SHA_A,
        "source": {
            "kind": "loose-file",
            "part": "locale",
            "language": "EN",
            "part_version": "opaque.1",
            "client_tree_path": "res/text/messages.mo",
            "client_tree_sha256": SHA_B,
            "entry_path": "text/messages.mo",
            "entry_sha256": SHA_B,
        },
        "representation": {
            "kind": "mo-to-po",
            "source_path": "text/messages.mo",
            "source_sha256": SHA_B,
            "tool": "fixture-transformer",
            "tool_version": "1",
            "diagnostics": ["messages=1"],
        },
    }

    validated = registry.validate("file-manifest-entry", document)

    assert isinstance(validated, FileManifestEntryV1)


def test_contract_supports_formatted_web_sources() -> None:
    document = {
        "path": "gui/gameface/bundle.js",
        "layer": {"kind": "base"},
        "size": 24,
        "sha256": SHA_A,
        "source": {
            "kind": "loose-file",
            "part": "client",
            "part_version": "opaque.1",
            "client_tree_path": "res/gui/gameface/bundle.js",
            "client_tree_sha256": SHA_B,
            "entry_path": "gui/gameface/bundle.js",
            "entry_sha256": SHA_B,
        },
        "representation": {
            "kind": "web-format",
            "source_path": "gui/gameface/bundle.js",
            "source_sha256": SHA_B,
            "tool": "prettier",
            "tool_version": "3.9.6",
            "diagnostics": ["parser=babel"],
        },
    }

    validated = ContractRegistry().validate("file-manifest-entry", document)

    assert isinstance(validated, FileManifestEntryV1)


def test_file_manifest_rejects_actionscript_representation() -> None:
    document = {
        "path": "base_app/scripts/App.as",
        "layer": {"kind": "base"},
        "size": 12,
        "sha256": SHA_A,
        "source": {
            "kind": "game-package",
            "part": "client",
            "part_version": "opaque.1",
            "game_package_path": "res/packages/gui.pkg",
            "game_package_sha256": SHA_B,
            "entry_path": "gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
            "entry_sha256": SHA_B,
        },
        "representation": {
            "kind": "swc-to-as",
            "source_path": "gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
            "source_sha256": SHA_B,
            "tool": "ffdec",
            "tool_version": "26.2.1",
            "diagnostics": [],
        },
    }

    with pytest.raises(ContractValidationError):
        ContractRegistry().validate("file-manifest-entry", document)


@pytest.mark.parametrize("path", ["./value", "a/./value", "a/../value", "a//value", "a/"])
def test_contract_rejects_noncanonical_relative_paths(path: str) -> None:
    document = {
        "path": path,
        "size": 1,
        "sha256": SHA_A,
        "part": "client",
        "part_version": "1",
        "container": "zip",
        "precedence": 0,
        "entries": 1,
    }

    with pytest.raises(ContractValidationError):
        ContractRegistry().validate("package-manifest-entry", document)


@pytest.mark.parametrize(
    ("name", "document", "model_type"),
    [
        (
            "actionscript-manifest-entry",
            {
                "path": "base_app/scripts/net/wg/App.as",
                "size": 12,
                "sha256": SHA_A,
                "source": {
                    "kind": "game-package",
                    "part": "client",
                    "part_version": "opaque.1",
                    "game_package_path": "res/packages/gui.pkg",
                    "game_package_sha256": SHA_B,
                    "entry_path": "gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
                    "entry_sha256": SHA_B,
                },
                "representation": {
                    "kind": "swc-to-as",
                    "source_path": "gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
                    "source_sha256": SHA_B,
                    "tool": "ffdec",
                    "tool_version": "26.2.1",
                    "diagnostics": ["language=actionscript-3"],
                },
            },
            ActionScriptManifestEntryV1,
        ),
        (
            "stub-manifest-entry",
            {
                "path": "BigWorld.pyi",
                "size": 12,
                "sha256": SHA_A,
            },
            StubManifestEntryV1,
        ),
        (
            "file-manifest-entry",
            {
                "path": "scripts/example.py",
                "layer": {"kind": "base"},
                "size": 12,
                "sha256": SHA_A,
                "source": {
                    "kind": "game-package",
                    "part": "client",
                    "part_version": "opaque.1",
                    "game_package_path": "res/packages/scripts.pkg",
                    "game_package_sha256": SHA_B,
                    "entry_path": "scripts/example.pyc",
                    "entry_sha256": SHA_B,
                },
                "representation": {
                    "kind": "pyc-to-py",
                    "source_path": "scripts/example.pyc",
                    "source_sha256": SHA_B,
                    "tool": "fixture-transformer",
                    "tool_version": "1",
                    "diagnostics": [],
                },
            },
            FileManifestEntryV1,
        ),
        (
            "package-manifest-entry",
            {
                "path": "res/packages/scripts.pkg",
                "size": 100,
                "sha256": SHA_A,
                "part": "client",
                "part_version": "opaque.1",
                "container": "zip",
                "precedence": 3,
                "entries": 5,
            },
            PackageManifestEntry,
        ),
        (
            "conflict-manifest-entry",
            {
                "canonical_path": "scripts/example.pyc",
                "layer": "base",
                "candidates": [
                    {
                        "source_kind": "game-package",
                        "source_path": "res/packages/a.pkg",
                        "source_sha256": SHA_A,
                        "entry_path": "scripts/example.pyc",
                        "precedence": 1,
                    },
                    {
                        "source_kind": "game-package",
                        "source_path": "res/packages/b.pkg",
                        "source_sha256": SHA_B,
                        "entry_path": "scripts/example.pyc",
                        "precedence": 2,
                    },
                ],
                "winner": {
                    "source_kind": "game-package",
                    "source_path": "res/packages/b.pkg",
                    "source_sha256": SHA_B,
                    "entry_path": "scripts/example.pyc",
                    "precedence": 2,
                },
                "resolution_rule": "fixture load order",
                "resolved": True,
            },
            ConflictManifestEntryV1,
        ),
    ],
)
def test_manifest_contracts_have_matching_pydantic_models(
    name: str,
    document: dict[str, object],
    model_type: type[object],
) -> None:
    validated = ContractRegistry().validate(name, document)  # type: ignore[arg-type]

    assert isinstance(validated, model_type)

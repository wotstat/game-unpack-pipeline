from __future__ import annotations

import json
import os
from collections.abc import Iterable
from importlib import resources
from pathlib import Path
from typing import Literal, cast

import jsonschema_rs
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ValidationError

from game_downloader._json import JsonObject
from game_downloader.models import (
    ActionScriptManifestEntryV1,
    ConflictManifestEntryV1,
    FileManifestEntryV1,
    GameSnapshotV1,
    PackageManifestEntry,
    StubManifestEntryV1,
)

type ContractName = Literal[
    "game-snapshot",
    "file-manifest-entry",
    "actionscript-manifest-entry",
    "stub-manifest-entry",
    "package-manifest-entry",
    "conflict-manifest-entry",
]

_SCHEMA_FILES: dict[ContractName, str] = {
    "game-snapshot": "game-snapshot.schema.json",
    "file-manifest-entry": "file-manifest-entry.schema.json",
    "actionscript-manifest-entry": "actionscript-manifest-entry.schema.json",
    "stub-manifest-entry": "stub-manifest-entry.schema.json",
    "package-manifest-entry": "package-manifest-entry.schema.json",
    "conflict-manifest-entry": "conflict-manifest-entry.schema.json",
}

_MODELS: dict[ContractName, type[BaseModel]] = {
    "game-snapshot": GameSnapshotV1,
    "file-manifest-entry": FileManifestEntryV1,
    "actionscript-manifest-entry": ActionScriptManifestEntryV1,
    "stub-manifest-entry": StubManifestEntryV1,
    "package-manifest-entry": PackageManifestEntry,
    "conflict-manifest-entry": ConflictManifestEntryV1,
}


class ContractValidationError(ValueError):
    """A document does not satisfy the checked-in external contract."""


class ContractRegistry:
    """Load and apply the normative JSON Schemas without duplicating them in code."""

    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get("GAME_DOWNLOADER_CONTRACTS_ROOT")
        self._root = root or (Path(configured) if configured else None)
        self._schemas: dict[ContractName, JsonObject] = {}
        self._validators: dict[ContractName, jsonschema_rs.Draft202012Validator] = {}
        self._diagnostic_validators: dict[ContractName, Draft202012Validator] = {}

    def schema(self, name: ContractName) -> JsonObject:
        cached = self._schemas.get(name)
        if cached is not None:
            return cached
        filename = f"v1/{_SCHEMA_FILES[name]}"
        try:
            raw = json.loads(self._read_text(filename))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractValidationError(f"cannot load contract {filename}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ContractValidationError(f"contract {filename} is not a JSON object")
        schema = cast(JsonObject, raw)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ContractValidationError(f"invalid JSON Schema {filename}: {exc.message}") from exc
        self._schemas[name] = schema
        return schema

    def validate(self, name: ContractName, document: object) -> BaseModel:
        validator = self._validators.get(name)
        if validator is None:
            try:
                validator = jsonschema_rs.Draft202012Validator(
                    self.schema(name),
                    validate_formats=True,
                    ignore_unknown_formats=False,
                )
            except jsonschema_rs.ValidationError as exc:
                raise ContractValidationError(
                    f"cannot compile contract {_SCHEMA_FILES[name]}: {exc}"
                ) from exc
            self._validators[name] = validator
        if not validator.is_valid(document):
            diagnostic = self._diagnostic_validators.get(name)
            if diagnostic is None:
                diagnostic = Draft202012Validator(
                    self.schema(name),
                    format_checker=FormatChecker(),
                )
                self._diagnostic_validators[name] = diagnostic
            errors = sorted(
                diagnostic.iter_errors(document),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
            details = "; ".join(
                f"{self._format_path(error.absolute_path)}: {error.message}" for error in errors
            )
            if not details:
                details = "compiled and diagnostic validators disagreed"
            raise ContractValidationError(f"{name} contract validation failed: {details}")
        try:
            return _MODELS[name].model_validate(document)
        except ValidationError as exc:
            raise ContractValidationError(
                f"{name} internal model rejected a schema-valid document: {exc}"
            ) from exc

    def validate_game_snapshot(self, document: object) -> GameSnapshotV1:
        validated = self.validate("game-snapshot", document)
        if not isinstance(validated, GameSnapshotV1):  # pragma: no cover - guarded by registry
            raise AssertionError("game-snapshot registry points to the wrong model")
        return validated

    def example(self) -> JsonObject:
        try:
            value = json.loads(self._read_text("v1/examples/game-snapshot.example.json"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractValidationError(f"cannot load GameSnapshot example: {exc}") from exc
        if not isinstance(value, dict):
            raise ContractValidationError("GameSnapshot example is not a JSON object")
        return cast(JsonObject, value)

    def validate_example(self) -> GameSnapshotV1:
        return self.validate_game_snapshot(self.example())

    def _read_text(self, relative_path: str) -> str:
        if self._root is not None:
            return (self._root / relative_path).read_text(encoding="utf-8")

        source_root = Path(__file__).resolve().parents[2] / "contracts"
        if source_root.is_dir():
            return (source_root / relative_path).read_text(encoding="utf-8")

        packaged = resources.files("game_downloader").joinpath(
            "_contracts", *relative_path.split("/")
        )
        return packaged.read_text(encoding="utf-8")

    @staticmethod
    def _format_path(parts: Iterable[object]) -> str:
        rendered = "$"
        for part in parts:
            rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
        return rendered

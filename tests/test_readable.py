from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import struct
import zipfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import pytest

from game_downloader._json import JsonValue
from game_downloader.engine_stubs import ModuleUsage, SourceUsageIndex
from game_downloader.models import (
    ClientTreeFile,
    ClientTreeResult,
    ClientType,
    EngineStubsResult,
    MaterializationResult,
    MaterializedFile,
    PartName,
    ReadableAssemblyResult,
    ReadableResult,
    RepresentationKind,
    RunRequest,
    Stage,
    StageResult,
    StageState,
    ToolIdentity,
    VfsCandidate,
    VfsSourceKind,
)
from game_downloader.pipeline import Pipeline, StageContext, StageImplementation
from game_downloader.readable import (
    MO_TOOL,
    PACKED_SECTION_MAGIC,
    PYTHON_27_MAGIC,
    ActionScriptOutput,
    FfdecTransformer,
    MoCatalogueConverter,
    PackedXmlDecoder,
    Python27SourceValidator,
    ReadableAssembler,
    ReadablePolicy,
    TransformFailedError,
    Uncompyle6Transformer,
    _balanced_pyc_batches,
    _ffdec_cpu_limit_seconds,
    _finish_pyc_chunk,
    _initialize_readable_process,
    _pyc_wall_timeout_seconds,
    _ReadablePlan,
    create_readable_implementations,
)
from game_downloader.workspace import Workspace


def _build_mo(
    messages: tuple[tuple[bytes, bytes], ...],
    *,
    endian: str = "little",
) -> bytes:
    prefix = "<" if endian == "little" else ">"
    magic = b"\xde\x12\x04\x95" if endian == "little" else b"\x95\x04\x12\xde"
    count = len(messages)
    original_table_offset = 28
    translation_table_offset = original_table_offset + count * 8
    data_offset = translation_table_offset + count * 8
    payload = bytearray()
    originals: list[tuple[int, int]] = []
    translations: list[tuple[int, int]] = []
    for original, _translation in messages:
        originals.append((len(original), data_offset + len(payload)))
        payload.extend(original)
        payload.append(0)
    for _original, translation in messages:
        translations.append((len(translation), data_offset + len(payload)))
        payload.extend(translation)
        payload.append(0)
    header = magic + struct.pack(
        f"{prefix}6I",
        0,
        count,
        original_table_offset,
        translation_table_offset,
        0,
        0,
    )
    original_table = b"".join(struct.pack(f"{prefix}2I", *item) for item in originals)
    translation_table = b"".join(struct.pack(f"{prefix}2I", *item) for item in translations)
    return header + original_table + translation_table + payload


def _packed_section(
    own_type: int,
    own_data: bytes,
    children: tuple[tuple[int, int, bytes], ...] = (),
) -> bytes:
    positions = [len(own_data)]
    payload = bytearray(own_data)
    for _key, child_type, child_data in children:
        payload.extend(child_data)
        positions.append(len(payload) | (child_type << 28))
    records = bytearray(struct.pack("<h", len(children)))
    for index, (key, _child_type, _child_data) in enumerate(children):
        raw_position = positions[index]
        if index == 0:
            raw_position |= own_type << 28
        records.extend(struct.pack("<Ih", raw_position, key))
    if not children:
        positions[0] |= own_type << 28
    records.extend(struct.pack("<I", positions[-1]))
    return bytes(records + payload)


def _packed_xml_fixture() -> bytes:
    names = ("title", "count", "enabled", "vector", "blob", "nested", "child")
    string_table = b"".join(name.encode() + b"\0" for name in names) + b"\0"
    nested = _packed_section(1, b"", ((6, 1, b"inside"),))
    root = _packed_section(
        1,
        b"",
        (
            (0, 1, "café".encode()),
            (1, 2, (-42).to_bytes(1, "little", signed=True)),
            (2, 4, b"\x01"),
            (3, 3, struct.pack("<3f", 1.0, 2.5, -3.0)),
            (4, 5, b"\x00\xff"),
            (5, 0, nested),
        ),
    )
    return PACKED_SECTION_MAGIC + bytes([0]) + string_table + root


def _packed_xml_namespace_fixture() -> bytes:
    names = ("xmlns:usa", "usa:A01", "wide")
    string_table = b"".join(name.encode() + b"\0" for name in names) + b"\0"
    root = _packed_section(
        1,
        b"",
        (
            (0, 1, b"usa"),
            (0, 1, b"usa"),
            (1, 1, b"qualified"),
            (2, 2, (0xFF00FF00).to_bytes(8, "little", signed=True)),
        ),
    )
    return PACKED_SECTION_MAGIC + bytes([0]) + string_table + root


def _messages() -> tuple[tuple[bytes, bytes], ...]:
    return (
        (
            b"",
            b"Project-Id-Version: fixture\\nContent-Type: text/plain; charset=UTF-8\\n",
        ),
        (b"simple", b"simple traduit"),
        (b"menu\x04apple\0apples", b"pomme\0pommes"),
    )


@pytest.mark.parametrize("endian", ["little", "big"])
def test_mo_converter_preserves_contexts_plurals_and_charset(endian: str) -> None:
    output, diagnostics = MoCatalogueConverter(ReadablePolicy()).convert(
        _build_mo(_messages(), endian=endian)
    )

    text = output.decode()
    assert 'msgctxt "menu"' in text
    assert 'msgid_plural "apples"' in text
    assert 'msgstr[0] "pomme"' in text
    assert 'msgstr[1] "pommes"' in text
    assert f"mo-endian={endian}" in diagnostics


def test_packed_xml_decoder_produces_deterministic_textual_xml() -> None:
    decoder = PackedXmlDecoder(ReadablePolicy())

    first, diagnostics = decoder.decode(_packed_xml_fixture(), "fixture.xml")
    second, _ = PackedXmlDecoder(ReadablePolicy()).decode(_packed_xml_fixture(), "fixture.xml")

    assert first == second
    root = ElementTree.fromstring(first)
    assert root.tag == "fixture.xml"
    assert root.findtext("title") == "café"
    assert root.findtext("count") == "-42"
    assert root.findtext("enabled") == "true"
    assert root.findtext("vector") == "1 2.5 -3"
    assert root.findtext("blob") == "AP8="
    assert root.findtext("./nested/child") == "inside"
    assert "nodes=8" in diagnostics


def test_packed_xml_decoder_restores_namespaces_and_wide_integers() -> None:
    output, diagnostics = PackedXmlDecoder(ReadablePolicy()).decode(
        _packed_xml_namespace_fixture(), "fixture.xml"
    )

    root = ElementTree.fromstring(output)
    assert root.findtext("{usa}A01") == "qualified"
    assert root.findtext("wide") == str(0xFF00FF00)
    assert "namespace-declarations=1" in diagnostics
    assert "duplicate-namespace-declarations=1" in diagnostics


class _FakePycTransformer:
    @property
    def identity(self) -> ToolIdentity:
        return ToolIdentity(name="fixture-pyc", version="1")

    def transform(
        self,
        source: Path,
        magic: bytes,
        scratch_directory: Path,
    ) -> tuple[bytes, tuple[str, ...]]:
        assert source.is_file()
        assert scratch_directory.is_dir()
        assert magic == PYTHON_27_MAGIC
        return b"print('readable')\n", ("fixture-decompiler",)


class _FakeActionScriptTransformer:
    @property
    def identity(self) -> ToolIdentity:
        return ToolIdentity(name="fixture-ffdec", version="1")

    def transform(
        self,
        source: Path,
        scratch_directory: Path,
    ) -> tuple[ActionScriptOutput, ...]:
        assert source.suffix == ".swc"
        with zipfile.ZipFile(source) as archive:
            assert archive.read("library.swf").startswith(b"FWS")
        assert scratch_directory.is_dir()
        return (
            ActionScriptOutput(
                path="scripts/net/wg/App.as",
                data=b"package net.wg { public class App {} }\n",
                diagnostics=("language=actionscript-3",),
            ),
        )


def _materialized_file(path: str, data: bytes, *, language: str | None = None) -> MaterializedFile:
    digest = hashlib.sha256(data).hexdigest()
    part = PartName.LOCALE if language is not None else PartName.CLIENT
    source = VfsCandidate(
        source_kind=VfsSourceKind.LOOSE_FILE,
        canonical_path=path,
        original_path=path,
        part=part,
        language=language,
        part_version="1.0",
        source_path=f"res/{path}",
        source_sha256=digest,
        precedence=1,
        uncompressed_size=len(data),
    )
    return MaterializedFile(
        path=path,
        language=language,
        size=len(data),
        sha256=digest,
        source=source,
    )


def _as3_swf() -> bytes:
    body = b"\x08\x00" + b"\x00\x00" + b"\x01\x00" + struct.pack("<H", 82 << 6) + b"\x00\x00"
    return b"FWS\x0a" + struct.pack("<I", 8 + len(body)) + body


def _swc(library: bytes | None = None, *, member_name: str = "library.swf") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("catalog.xml", "<swc/>")
        archive.writestr(member_name, library or _as3_swf())
    return output.getvalue()


def test_readable_assembler_transforms_required_formats_and_removes_compiled_sources(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    base_root = workspace.root / "materialized/base"
    locale_root = workspace.root / "materialized/locales/EN"
    base_root.mkdir(parents=True)
    locale_root.mkdir(parents=True)
    source_data = {
        "scripts/example.pyc": PYTHON_27_MAGIC + b"fixture",
        "text/catalog.mo": _build_mo(_messages()),
        "config.xml": _packed_xml_fixture(),
        "plain.xml": b'<?xml version="1.0"?><plain/>',
        "asset.bin": b"asset",
    }
    files: list[MaterializedFile] = []
    for relative, data in source_data.items():
        path = base_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o444)
        files.append(_materialized_file(relative, data))
    locale_data = _build_mo(_messages())
    (locale_root / "locale.mo").write_bytes(locale_data)
    (locale_root / "locale.mo").chmod(0o444)
    files.append(_materialized_file("locale.mo", locale_data, language="EN"))
    materialized = MaterializationResult(
        vfs_index_result_sha256="sha256:" + "1" * 64,
        base_root="materialized/base",
        locale_roots={"EN": "materialized/locales/EN"},
        files=tuple(files),
    )
    work_directory = workspace.root / "work/run/080-make-readable"
    work_directory.mkdir(parents=True)

    result = ReadableAssembler(ReadablePolicy(transform_workers=2), _FakePycTransformer()).build(
        materialized, workspace, work_directory
    )

    paths = {(item.language, item.path): item for item in result.files}
    assert (None, "scripts/example.py") in paths
    assert (None, "scripts/example.pyc") not in paths
    assert paths[(None, "text/catalog.po")].representation.kind is RepresentationKind.MO_TO_PO
    assert paths[(None, "config.xml")].representation.kind is RepresentationKind.PACKED_XML_TO_XML
    assert paths[(None, "plain.xml")].representation.kind is RepresentationKind.PASSTHROUGH
    assert paths[("EN", "locale.po")].representation.tool == MO_TOOL.name
    assert not any(item.path.lower().endswith((".pyc", ".mo")) for item in result.files)
    assert (workspace.root / result.base_root / "scripts/example.py").read_text() == (
        "print('readable')\n"
    )
    assert not os.access(workspace.root / result.base_root / "config.xml", os.W_OK)
    assert (workspace.root / result.actionscript_root).is_dir()


def test_readable_assembler_exports_only_canonical_swc_libraries(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    base_root = workspace.root / "materialized/base"
    locale_root = workspace.root / "materialized/locales/EN"
    files = {
        "gui/flash/Achievements.swf": _as3_swf(),
        "gui/flash/other.swc": _swc(),
        "gui/flash/swc/base_app-1.0-SNAPSHOT.swc": _swc(),
    }
    for relative, data in files.items():
        source = base_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        source.chmod(0o444)
    locale_root.mkdir(parents=True)
    materialized = MaterializationResult(
        vfs_index_result_sha256="sha256:" + "1" * 64,
        base_root="materialized/base",
        locale_roots={"EN": "materialized/locales/EN"},
        files=tuple(_materialized_file(path, data) for path, data in files.items()),
    )
    work_directory = workspace.root / "work/run/080-make-readable"
    work_directory.mkdir(parents=True)

    result = ReadableAssembler(actionscript_transformer=_FakeActionScriptTransformer()).build(
        materialized, workspace, work_directory
    )

    assert [item.path for item in result.files] == sorted(files)
    assert [item.path for item in result.actionscript_files] == ["base_app/scripts/net/wg/App.as"]
    exported = workspace.root / result.actionscript_root / result.actionscript_files[0].path
    assert exported.read_text() == "package net.wg { public class App {} }\n"
    assert result.actionscript_files[0].source.path == "gui/flash/swc/base_app-1.0-SNAPSHOT.swc"
    assert result.actionscript_files[0].representation.kind is RepresentationKind.SWC_TO_AS
    assert ("fixture-ffdec", "1") in {(item.name, item.version) for item in result.tools}


def test_readable_assembler_generates_engine_stubs_inside_stage_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    base_root = workspace.root / "materialized/base"
    locale_root = workspace.root / "materialized/locales/EN"
    base_root.mkdir(parents=True)
    locale_root.mkdir(parents=True)
    source = b"import BigWorld\nBigWorld.time()\n"
    (base_root / "example.py").write_bytes(source)
    (base_root / "example.py").chmod(0o444)
    materialized = MaterializationResult(
        vfs_index_result_sha256="sha256:" + "1" * 64,
        base_root="materialized/base",
        locale_roots={"EN": "materialized/locales/EN"},
        files=(_materialized_file("example.py", source),),
    )
    client_root = workspace.root / "client/base"
    binary = client_root / "win64/WorldOfTanks.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture-pe")
    client_tree = ClientTreeResult.model_construct(
        verification_result_sha256="sha256:" + "2" * 64,
        base_root="client/base",
        locale_roots={"EN": "client/locales/EN"},
        files=(),
    )

    def fake_analyze(
        *_args: object, **_kwargs: object
    ) -> tuple[
        SourceUsageIndex,
        dict[str, tuple[object, ...]],
        dict[str, tuple[object, ...]],
        dict[str, tuple[object, ...]],
    ]:
        return (
            SourceUsageIndex(
                modules={
                    "BigWorld": ModuleUsage(
                        called=frozenset({"time"}),
                        attributes=frozenset({"time"}),
                    )
                }
            ),
            {},
            {},
            {},
        )

    monkeypatch.setattr("game_downloader.readable.find_main_binaries", lambda _root: (binary,))
    monkeypatch.setattr("game_downloader.readable.analyze_engine_stubs", fake_analyze)
    work_directory = workspace.root / "work/run/080-make-readable"
    work_directory.mkdir(parents=True)

    result = ReadableAssembler().build(
        materialized,
        workspace,
        work_directory,
        client_tree=client_tree,
    )

    assert [item.path for item in result.stub_files] == [
        "BigWorld.pyi",
        "manifest.json",
        "py.typed",
    ]
    assert (workspace.root / result.stubs_root / "BigWorld.pyi").is_file()
    assert not os.access(workspace.root / result.stubs_root / "BigWorld.pyi", os.W_OK)
    manifest = json.loads((workspace.root / result.stubs_root / "manifest.json").read_text())
    assert manifest["binaries"][0]["path"] == "win64/WorldOfTanks.exe"
    assert ("game-downloader-engine-stubs", "2") in {
        (item.name, item.version) for item in result.tools
    }


def test_readable_stage_module_commits_and_reuses_every_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    base_root = workspace.root / "materialized/base"
    locale_root = workspace.root / "materialized/locales/EN"
    base_root.mkdir(parents=True)
    locale_root.mkdir(parents=True)
    source_data = {
        "scripts/example.pyc": PYTHON_27_MAGIC + b"fixture",
        "gui/flash/swc/base_app-1.0-SNAPSHOT.swc": _swc(),
        "assets/value.bin": b"asset",
    }
    materialized_files: list[MaterializedFile] = []
    for relative, data in source_data.items():
        source = base_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        source.chmod(0o444)
        materialized_files.append(_materialized_file(relative, data))
    locale_data = b'<?xml version="1.0"?><locale/>'
    (locale_root / "locale.xml").write_bytes(locale_data)
    (locale_root / "locale.xml").chmod(0o444)
    materialized_files.append(_materialized_file("locale.xml", locale_data, language="EN"))
    materialized = MaterializationResult(
        vfs_index_result_sha256="sha256:" + "1" * 64,
        base_root="materialized/base",
        locale_roots={"EN": "materialized/locales/EN"},
        files=tuple(materialized_files),
    )
    binary_digest = hashlib.sha256(b"fixture-pe").hexdigest()
    client_tree = ClientTreeResult(
        verification_result_sha256="sha256:" + "2" * 64,
        base_root="client/base",
        locale_roots={"EN": "client/locales/EN"},
        files=(
            ClientTreeFile(
                path="win64/WorldOfTanks.exe",
                part=PartName.CLIENT,
                part_version="1.0",
                source_artifact_id="sha256:" + "3" * 64,
                source_blob_sha256=binary_digest,
                blob_sha256=binary_digest,
                blob_size=10,
                blob_path=f"cache/blobs/sha256/{binary_digest[:2]}/{binary_digest}",
                link_method="hardlink",
            ),
        ),
    )

    def fake_generate_stubs(
        _self: ReadableAssembler,
        root: Path,
        _client_tree: ClientTreeResult,
        _workspace: Workspace,
        *,
        source_roots: Sequence[Path] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        assert source_roots is not None
        assert progress is not None
        assert all(source_root.is_dir() for source_root in source_roots)
        stubs = root / "stubs"
        stubs.mkdir()
        for relative, data in {
            "BigWorld.pyi": b"def time() -> float: ...\n",
            "manifest.json": b"{}\n",
            "py.typed": b"",
        }.items():
            output = stubs / relative
            output.write_bytes(data)
            output.chmod(0o444)

    monkeypatch.setattr(ReadableAssembler, "_generate_engine_stubs", fake_generate_stubs)

    def implementation(payload: Mapping[str, JsonValue]) -> StageImplementation:
        def execute(_context: StageContext) -> Mapping[str, JsonValue]:
            return payload

        return StageImplementation(implementation_version="readable-fixture-v1", execute=execute)

    implementations = {
        stage: implementation({"fixture": stage.value})
        for stage in (
            Stage.RESOLVE,
            Stage.PLAN_ACQUISITION,
            Stage.DOWNLOAD,
            Stage.VERIFY,
            Stage.INDEX_VFS,
        )
    }
    implementations[Stage.ASSEMBLE_CLIENT] = implementation(client_tree.model_dump(mode="json"))
    implementations[Stage.MATERIALIZE_VFS] = implementation(materialized.model_dump(mode="json"))
    implementations.update(
        create_readable_implementations(
            ReadablePolicy(transform_workers=2),
            _FakePycTransformer(),
            _FakeActionScriptTransformer(),
        )
    )
    pipeline = Pipeline(workspace, implementations)

    report = pipeline.start(
        RunRequest(target="fixture", client_type=ClientType.SD, languages=("EN",)),
        Stage.FINALIZE_READABLE,
    )

    readable_stages = (
        Stage.PLAN_READABLE,
        Stage.TRANSFORM_READABLE,
        Stage.DECOMPILE_ACTIONSCRIPT,
        Stage.ASSEMBLE_READABLE,
        Stage.GENERATE_ENGINE_STUBS,
        Stage.FINALIZE_READABLE,
    )
    assert report.completed_until is Stage.FINALIZE_READABLE
    assert all(
        next(item for item in report.stages if item.stage is stage).state is StageState.SUCCEEDED
        for stage in readable_stages
    )
    statistics = {item.stage: item.statistics for item in report.stages}
    assert statistics[Stage.PLAN_READABLE] == {
        "files": 4,
        "transform_files": 1,
        "actionscript_libraries": 1,
        "passthrough_files": 3,
    }
    assert statistics[Stage.TRANSFORM_READABLE]["files"] == 1
    assert statistics[Stage.DECOMPILE_ACTIONSCRIPT]["libraries"] == 1
    assert statistics[Stage.ASSEMBLE_READABLE] == {
        "files": 4,
        "actionscript_files": 1,
        "passthrough_files": 3,
    }
    assert statistics[Stage.GENERATE_ENGINE_STUBS]["typing_stubs"] == 1
    assert statistics[Stage.FINALIZE_READABLE]["files"] == 4
    final_stage = workspace.stage_path(report.run_id, Stage.FINALIZE_READABLE)
    final_result = StageResult.model_validate_json(
        workspace.read_bytes(final_stage / "result.json")
    )
    readable = ReadableResult.model_validate(final_result.payload)
    assembly_stage = workspace.stage_path(report.run_id, Stage.ASSEMBLE_READABLE)
    assembly_result = StageResult.model_validate_json(
        workspace.read_bytes(assembly_stage / "result.json")
    )
    assembly = ReadableAssemblyResult.model_validate(assembly_result.payload)
    stubs_stage = workspace.stage_path(report.run_id, Stage.GENERATE_ENGINE_STUBS)
    stubs_result = StageResult.model_validate_json(
        workspace.read_bytes(stubs_stage / "result.json")
    )
    stubs = EngineStubsResult.model_validate(stubs_result.payload)
    assert readable.base_root == assembly.base_root
    assert readable.locale_roots == assembly.locale_roots
    assert readable.actionscript_root == assembly.actionscript_root
    assert readable.stubs_root == stubs.root
    assert not list(final_stage.glob("work/*/readable"))
    assert [item.path for item in readable.files] == [
        "assets/value.bin",
        "gui/flash/swc/base_app-1.0-SNAPSHOT.swc",
        "scripts/example.py",
        "locale.xml",
    ]
    assert [item.path for item in readable.actionscript_files] == ["base_app/scripts/net/wg/App.as"]
    assert [item.path for item in readable.stub_files] == [
        "BigWorld.pyi",
        "manifest.json",
        "py.typed",
    ]

    repeated = pipeline.resume(report.run_id, Stage.FINALIZE_READABLE)

    assert all(
        next(item for item in repeated.stages if item.stage is stage).attempt == 1
        for stage in readable_stages
    )


@pytest.mark.parametrize(
    ("source", "bundle"),
    [
        ("gui/flash/swc/base_app-1.0-SNAPSHOT.swc", "base_app"),
        ("gui/flash/swc/battle.swc", "battle"),
        (
            "story_mode/gui/flash/swc/story_mode_gui_battle-1.0-SNAPSHOT.swc",
            "story_mode_gui_battle",
        ),
    ],
)
def test_actionscript_bundle_name_follows_swc_library(source: str, bundle: str) -> None:
    assert ReadableAssembler._actionscript_bundle_path(source) == bundle


def test_ffdec_adapter_exports_normalized_utf8_sources(tmp_path: Path) -> None:
    executable = tmp_path / "ffdec"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-help" ]; then\n'
        "  echo 'JPEXS Free Flash Decompiler v.26.2.1'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$FFDEC_MEMORY" != "2048m" ]; then exit 18; fi\n'
        'if [ "$JAVA_TOOL_OPTIONS" != "-Djava.awt.headless=true" ]; then exit 19; fi\n'
        'if [ "$(dd if="${10}" bs=1 count=3 2>/dev/null)" != "FWS" ]; then exit 17; fi\n'
        'mkdir -p "$9/scripts/net/wg"\n'
        "printf 'package net.wg {\\r\\n}\\r\\n' > \"$9/scripts/net/wg/App.as\"\n"
    )
    executable.chmod(0o755)
    library = b"FWS" + b"\x00" * 16
    source = tmp_path / "base_app-1.0-SNAPSHOT.swc"
    source.write_bytes(_swc(library))

    outputs = FfdecTransformer(ReadablePolicy(), executable).transform(source, tmp_path)

    assert [(item.path, item.data) for item in outputs] == [
        ("scripts/net/wg/App.as", b"package net.wg {\n}\n")
    ]
    assert outputs[0].diagnostics == (
        "language=actionscript-3",
        "backend=ffdec-26.2.1",
        "container=swc",
        "swc-member=library.swf",
        f"swc-member-sha256={hashlib.sha256(library).hexdigest()}",
    )


def test_ffdec_adapter_rejects_decompilation_error_stub(tmp_path: Path) -> None:
    executable = tmp_path / "ffdec"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-help" ]; then\n'
        "  echo 'JPEXS Free Flash Decompiler v.26.2.1'\n"
        "  exit 0\n"
        "fi\n"
        'mkdir -p "$9/scripts/net/wg"\n'
        "printf '/* Decompilation error: java.lang.OutOfMemoryError */\\n' "
        '> "$9/scripts/net/wg/Broken.as"\n'
    )
    executable.chmod(0o755)
    source = tmp_path / "gui_lobby-1.0-SNAPSHOT.swc"
    source.write_bytes(_swc())

    with pytest.raises(TransformFailedError, match="decompilation error"):
        FfdecTransformer(ReadablePolicy(), executable).transform(source, tmp_path)


@pytest.mark.parametrize("member_name", ["Library.swf", "nested/library.swf"])
def test_ffdec_adapter_rejects_swc_without_canonical_library(
    tmp_path: Path, member_name: str
) -> None:
    executable = tmp_path / "ffdec"
    executable.write_text("#!/bin/sh\necho 'JPEXS Free Flash Decompiler v.26.2.1'\n")
    executable.chmod(0o755)
    source = tmp_path / "invalid.swc"
    source.write_bytes(_swc(member_name=member_name))

    with pytest.raises(TransformFailedError, match=r"one canonical library\.swf"):
        FfdecTransformer(ReadablePolicy(), executable).transform(source, tmp_path)


def test_ffdec_budgets_cover_large_multithreaded_library() -> None:
    policy = ReadablePolicy()

    assert policy.actionscript_timeout_seconds >= 2 * 60 * 60
    assert policy.actionscript_heap_megabytes == 2048
    assert policy.actionscript_workers == 1
    assert (
        _ffdec_cpu_limit_seconds(policy.actionscript_timeout_seconds, logical_cpus=16)
        >= 2 * 60 * 60 * 16
    )


def test_pyc_wall_budget_accounts_for_parallel_cpu_contention() -> None:
    policy = ReadablePolicy()

    assert (
        _pyc_wall_timeout_seconds(
            policy.subprocess_timeout_seconds,
            workers=policy.transform_workers,
            logical_cpus=1,
        )
        == 12 * 60
    )
    assert (
        _pyc_wall_timeout_seconds(
            policy.subprocess_timeout_seconds,
            workers=policy.transform_workers,
            logical_cpus=policy.transform_workers,
        )
        == policy.subprocess_timeout_seconds
    )


def test_readable_assembler_rejects_transform_path_collision(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    workspace.initialize()
    base_root = workspace.root / "materialized/base"
    base_root.mkdir(parents=True)
    values = {
        "same.py": b"source",
        "same.pyc": PYTHON_27_MAGIC + b"compiled",
    }
    files = []
    for relative, data in values.items():
        (base_root / relative).write_bytes(data)
        (base_root / relative).chmod(0o444)
        files.append(_materialized_file(relative, data))
    materialized = MaterializationResult(
        vfs_index_result_sha256="sha256:" + "1" * 64,
        base_root="materialized/base",
        locale_roots={"EN": "materialized/locales/EN"},
        files=tuple(files),
    )
    (workspace.root / "materialized/locales/EN").mkdir(parents=True)
    work_directory = workspace.root / "work/run/080-make-readable"
    work_directory.mkdir(parents=True)

    with pytest.raises(TransformFailedError, match="collision"):
        ReadableAssembler(pyc_transformer=_FakePycTransformer()).build(
            materialized, workspace, work_directory
        )


def test_uncompyle_adapter_accepts_a_recognized_empty_module(tmp_path: Path) -> None:
    executable = tmp_path / "fixture-game-downloader-pyc"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then '
        "echo 'game-downloader-pyc 4+uncompyle6-3.9.3'; exit 0; fi\n"
        "printf '# uncompyle6 version 3.9.3\\n# Python bytecode 2.7\\n\\nreturn\\n'\n"
    )
    executable.chmod(0o755)
    source = tmp_path / "empty.pyc"
    source.write_bytes(PYTHON_27_MAGIC + b"fixture")

    output, diagnostics = Uncompyle6Transformer(ReadablePolicy(), executable).transform(
        source, PYTHON_27_MAGIC, tmp_path
    )

    assert output == b"pass\n"
    assert "empty-module" in diagnostics


def test_pinned_python27_adapter_matches_control_flow_golden(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parent / "fixtures/pyc"
    encoded = b"".join((fixture_root / "python27-control-flow.pyc.b64").read_bytes().split())
    source = tmp_path / "python27-control-flow.pyc"
    source.write_bytes(base64.b64decode(encoded, validate=True))

    transformer = Uncompyle6Transformer(ReadablePolicy())
    first, diagnostics = transformer.transform(source, PYTHON_27_MAGIC, tmp_path)
    second, _ = transformer.transform(source, PYTHON_27_MAGIC, tmp_path)

    assert first == second
    assert first.decode() == (fixture_root / "python27-control-flow.golden").read_text()
    assert first.startswith(b"clamp =")
    assert b"Decompiled by" not in first
    assert diagnostics == (
        "bytecode=python-2.7",
        "magic=03f30d0a",
        "adapter=game-downloader-pyc-4",
        "backend=uncompyle6-3.9.3",
        "removed-decompiler-module-return",
    )
    assert Python27SourceValidator(ReadablePolicy()).validate(
        first, "python27-control-flow.py"
    ) == (
        "syntax=python-2.7",
        "syntax-validator=fissix-24.4.24",
    )


def test_pinned_python27_batch_adapter_matches_single_file_output(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parent / "fixtures/pyc"
    encoded = b"".join((fixture_root / "python27-control-flow.pyc.b64").read_bytes().split())
    source = tmp_path / "python27-control-flow.pyc"
    source.write_bytes(base64.b64decode(encoded, validate=True))
    transformer = Uncompyle6Transformer(ReadablePolicy(pyc_batch_size=2))

    expected = transformer.transform(source, PYTHON_27_MAGIC, tmp_path)
    actual = transformer.transform_many(
        ((source, PYTHON_27_MAGIC), (source, PYTHON_27_MAGIC)),
        tmp_path,
    )

    assert actual == (expected, expected)
    assert not tuple(tmp_path.glob("pyc-many-*"))


def test_pyc_process_finalization_matches_serial_output(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parent / "fixtures/pyc"
    encoded = b"".join((fixture_root / "python27-control-flow.pyc.b64").read_bytes().split())
    source_bytes = base64.b64decode(encoded, validate=True)
    source = tmp_path / "python27-control-flow.pyc"
    source.write_bytes(source_bytes)
    policy = ReadablePolicy(transform_workers=2)
    transformer = Uncompyle6Transformer(policy)
    staged = transformer.stage_many(((source, PYTHON_27_MAGIC),), tmp_path)
    output, diagnostics = transformer.decode_staged(staged.outputs[0])
    plan = _ReadablePlan(
        source=_materialized_file("scripts/python27-control-flow.pyc", source_bytes),
        source_path=source,
        output_path="scripts/python27-control-flow.py",
        representation=RepresentationKind.PYC_TO_PY,
    )
    serial_root = tmp_path / "serial"
    parallel_root = tmp_path / "parallel"
    serial = ReadableAssembler(policy)._finish_pyc_transform(
        plan,
        serial_root,
        output,
        diagnostics,
    )

    try:
        with ProcessPoolExecutor(
            max_workers=2,
            initializer=_initialize_readable_process,
            initargs=(policy,),
        ) as executor:
            parallel = executor.submit(
                _finish_pyc_chunk,
                ((plan, parallel_root, staged.outputs[0]),),
            ).result()
    finally:
        shutil.rmtree(staged.root)

    assert parallel == (serial,)
    assert (parallel_root / "base" / serial.path).read_bytes() == (
        serial_root / "base" / serial.path
    ).read_bytes()


def test_pyc_batch_balancing_is_deterministic_and_bounded(tmp_path: Path) -> None:
    sizes = (100, 90, 80, 70, 60, 50, 40)
    values = []
    for index, size in enumerate(sizes):
        source = tmp_path / f"{index}.pyc"
        source.write_bytes(b"x" * size)
        values.append((index, source, PYTHON_27_MAGIC))

    first = _balanced_pyc_batches(values, batch_size=3)
    second = _balanced_pyc_batches(values, batch_size=3)

    assert first == second
    assert sorted(index for batch in first for index, _source, _magic in batch) == list(
        range(len(values))
    )
    assert all(1 <= len(batch) <= 3 for batch in first)
    loads = [sum(source.stat().st_size for _index, source, _magic in batch) for batch in first]
    assert loads == sorted(loads, reverse=True)
    assert max(loads) - min(loads) <= max(sizes)


def test_staged_pyc_output_is_rehashed_before_decoding(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parent / "fixtures/pyc"
    encoded = b"".join((fixture_root / "python27-control-flow.pyc.b64").read_bytes().split())
    source = tmp_path / "python27-control-flow.pyc"
    source.write_bytes(base64.b64decode(encoded, validate=True))
    transformer = Uncompyle6Transformer(ReadablePolicy())
    staged = transformer.stage_many(((source, PYTHON_27_MAGIC),), tmp_path)
    try:
        output = staged.outputs[0]
        output.path.write_bytes(b"x" * output.size)

        with pytest.raises(TransformFailedError, match="does not match its report"):
            transformer.decode_staged(output)
    finally:
        shutil.rmtree(staged.root)


def test_pinned_python27_adapter_handles_conditional_lambda_with_and_not(
    tmp_path: Path,
) -> None:
    fixture_root = Path(__file__).parent / "fixtures/pyc"
    encoded = b"".join((fixture_root / "python27-conditional-lambda.pyc.b64").read_bytes().split())
    source = tmp_path / "python27-conditional-lambda.pyc"
    source.write_bytes(base64.b64decode(encoded, validate=True))

    output, diagnostics = Uncompyle6Transformer(ReadablePolicy()).transform(
        source, PYTHON_27_MAGIC, tmp_path
    )

    assert output.decode() == (fixture_root / "python27-conditional-lambda.golden").read_text()
    assert "adapter=game-downloader-pyc-4" in diagnostics
    assert Python27SourceValidator(ReadablePolicy()).validate(
        output, "python27-conditional-lambda.py"
    ) == (
        "syntax=python-2.7",
        "syntax-validator=fissix-24.4.24",
    )


def test_python27_syntax_validator_rejects_invalid_source() -> None:
    validator = Python27SourceValidator(ReadablePolicy())

    with pytest.raises(TransformFailedError, match=r"Python 2\.7 source is invalid"):
        validator.validate(b"def broken(:\n", "broken.py")

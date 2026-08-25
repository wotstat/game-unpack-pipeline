from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import cast

import pytest
from capstone import CS_ARCH_X86, CS_MODE_64, Cs  # type: ignore[import-untyped]

from game_downloader.engine_stubs import (
    BindingEvidence,
    CallShape,
    ConstantEvidence,
    EngineStubError,
    EngineStubGenerator,
    EnumEvidence,
    EnumMemberEvidence,
    ModuleUsage,
    NativeBindingAnalyzer,
    NativeSignature,
    PeImage,
    SourceUsageIndex,
    _FunctionAnalysis,
    _Reference,
    _RuntimeFunction,
    _StringRecord,
    analyze_engine_stubs,
    find_main_binaries,
    parse_native_signature,
)


def test_source_only_attributes_become_native_constant_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "consumer.py").write_text("import BigWorld\nprint(BigWorld.MAGIC)\n")
    binary = tmp_path / "WorldOfTanks.exe"
    binary.write_bytes(b"fixture")

    def fake_analyze(
        _self: NativeBindingAnalyzer,
        selected_binary: Path,
        _usage: SourceUsageIndex,
        constant_candidates: object,
        **_kwargs: object,
    ) -> tuple[tuple[BindingEvidence, ...], tuple[EnumEvidence, ...], tuple[ConstantEvidence, ...]]:
        assert selected_binary == binary
        assert constant_candidates == {"MAGIC": ()}
        return (
            (),
            (),
            (
                ConstantEvidence(
                    name="MAGIC",
                    value=7,
                    binary=binary.name,
                    registration_rva=0x1234,
                ),
            ),
        )

    monkeypatch.setattr(NativeBindingAnalyzer, "analyze_with_enums", fake_analyze)

    usage, _bindings, _enums, constants = analyze_engine_stubs(
        (binary,),
        (source_root,),
    )

    assert "BigWorld" in usage.modules
    assert constants["MAGIC"][0].value == 7


def test_parse_pybind_signature_preserves_known_types_and_overloads() -> None:
    signature = parse_native_signature("({int}, {%}, {unicode}, {int}, {unicode}) -> int")

    assert signature == NativeSignature(
        descriptor="({int}, {%}, {unicode}, {int}, {unicode}) -> int",
        parameter_types=("int", "Any", "str", "int", "str"),
        return_type="int",
    )


@pytest.mark.parametrize(
    ("machine_code", "expected"),
    (
        (bytes.fromhex("45 33 c9 41 b8 02 00 00 00 48 8d 15 00 00 00 00"), 2),
        (bytes.fromhex("45 33 c9 45 8d 41 06 48 8d 15 00 00 00 00"), 6),
    ),
)
def test_native_enum_dataflow_recovers_immediate_and_lea_values(
    machine_code: bytes,
    expected: int,
) -> None:
    disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
    disassembler.detail = True
    instructions = tuple(disassembler.disasm(machine_code, 0x140000000))

    assert NativeBindingAnalyzer._constant_before(instructions, 2, "r8") == expected


def test_native_enum_discovery_uses_only_pe_registration_data() -> None:
    image_base = 0x140000000
    function_rva = 0x1000
    code = bytearray()
    references: list[_Reference] = []

    def emit_reference(opcode: bytes, value: str, string_rva: int) -> None:
        instruction_rva = function_rva + len(code)
        code.extend(opcode)
        code.extend(b"\0\0\0\0")
        references.append(_Reference(instruction_rva, _StringRecord(string_rva, value)))

    def emit_call(target_rva: int) -> None:
        instruction_address = image_base + function_rva + len(code)
        displacement = image_base + target_rva - (instruction_address + 5)
        code.extend(b"\xe8" + struct.pack("<i", displacement))

    emit_reference(b"\x4c\x8d\x05", "BlendMode", 0x5000)  # lea r8, [...]
    emit_call(0x2000)
    code.extend(b"\x45\x33\xc0")  # xor r8d, r8d
    emit_reference(b"\x48\x8d\x15", "BM_OPAQUE", 0x5010)  # lea rdx, [...]
    code.extend(b"\x48\x8b\xc8")  # mov rcx, rax
    emit_call(0x3000)
    code.extend(b"\x41\xb8\x01\x00\x00\x00")  # mov r8d, 1
    emit_reference(b"\x48\x8d\x15", "BM_STANDARD", 0x5020)
    code.extend(b"\x48\x8b\xc8")
    emit_call(0x3000)
    emit_reference(b"\x48\x8d\x05", "DebugDrawer", 0x5030)  # lea rax, [...]
    emit_reference(b"\x48\x8d\x15", "__init__", 0x5040)
    code.extend(b"\xc3")

    class FakePeImage:
        path = Path("WorldOfTanks.exe")
        image_base = 0x140000000

        @staticmethod
        def bytes_at_rva(rva: int, size: int) -> bytes:
            assert rva == function_rva
            return bytes(code[:size])

    evidence = NativeBindingAnalyzer._resolve_enums(
        cast(PeImage, FakePeImage()),
        (
            _FunctionAnalysis(
                function=_RuntimeFunction(
                    begin=function_rva,
                    end=function_rva + len(code),
                ),
                references=tuple(references),
                calls=(),
            ),
        ),
        SourceUsageIndex(),
    )

    assert evidence == (
        EnumEvidence(
            module="DebugDrawer",
            name="BlendMode",
            members=(
                EnumMemberEvidence("BM_OPAQUE", 0, references[1].instruction_rva),
                EnumMemberEvidence("BM_STANDARD", 1, references[2].instruction_rva),
            ),
            binary="WorldOfTanks.exe",
            registration_rva=references[0].instruction_rva,
        ),
    )


def test_source_usage_understands_python_2_import_aliases(tmp_path: Path) -> None:
    source = tmp_path / "scripts" / "client" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
import BigWorld as BW
from GUI import load as load_gui

BW.callback(0.1, lambda: None)
BW.player()
value = BW.time
load_gui('screen.swf')
""",
        encoding="utf-8",
    )

    index = SourceUsageIndex.scan((tmp_path,))

    assert index.modules["BigWorld"] == ModuleUsage(
        called=frozenset({"callback", "player"}),
        attributes=frozenset({"callback", "player", "time"}),
        calls={
            "callback": (CallShape(("float", "Callable")),),
            "player": (CallShape(()),),
        },
    )
    assert "load" in index.modules["GUI"].called


def test_parallel_source_usage_scan_matches_serial_result(tmp_path: Path) -> None:
    for index in range(8):
        source = tmp_path / f"package_{index}" / f"consumer_{index}.py"
        source.parent.mkdir()
        source.write_text(
            f"import BigWorld as BW\nBW.callback({index}, lambda: None)\n",
            encoding="utf-8",
        )

    serial = SourceUsageIndex.scan((tmp_path,), max_workers=1)
    parallel = SourceUsageIndex.scan((tmp_path,), max_workers=4)

    assert parallel == serial


def test_parallel_native_disassembly_matches_serial_result(tmp_path: Path) -> None:
    binary = tmp_path / "fixture.exe"
    data = bytearray(0x400)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    struct.pack_into("<H", data, coff, 0x8664)
    struct.pack_into("<H", data, coff + 2, 1)
    struct.pack_into("<H", data, coff + 16, 0xF0)
    optional = coff + 20
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<Q", data, optional + 24, 0x140000000)
    section = optional + 0xF0
    data[section : section + 8] = b".text\0\0\0"
    struct.pack_into("<I", data, section + 8, 0x200)
    struct.pack_into("<I", data, section + 12, 0x1000)
    struct.pack_into("<I", data, section + 16, 0x200)
    struct.pack_into("<I", data, section + 20, 0x200)
    struct.pack_into("<I", data, section + 36, 0x60000020)
    data[0x200:0x208] = b"\x48\x8d\x05\x79\x00\x00\x00\xc3"
    data[0x210:0x218] = b"\x48\x8d\x05\x69\x00\x00\x00\xc3"
    binary.write_bytes(data)

    image = PeImage(binary)
    functions = (
        _RuntimeFunction(begin=0x1000, end=0x1008),
        _RuntimeFunction(begin=0x1010, end=0x1018),
    )
    strings = {0x1080: _StringRecord(0x1080, "callback")}
    analyzer = NativeBindingAnalyzer()

    serial = analyzer._disassemble(image, strings, functions, max_workers=1)
    parallel = analyzer._disassemble(image, strings, functions, max_workers=2)

    assert parallel == serial
    assert [reference.target.value for item in parallel for reference in item.references] == [
        "callback",
        "callback",
    ]


def test_generator_preserves_unambiguous_native_scalar_constants(tmp_path: Path) -> None:
    output = tmp_path / "_stubs"

    report = EngineStubGenerator().write(
        output,
        usage=SourceUsageIndex(
            modules={"zlib": ModuleUsage(attributes=frozenset({"MAX_WBITS", "ZLIB_VERSION"}))}
        ),
        bindings={},
        binaries=(),
        constants={
            "MAX_WBITS": (
                ConstantEvidence(
                    name="MAX_WBITS",
                    value=15,
                    binary="python27.dll",
                    registration_rva=0x2395E70,
                ),
            ),
            "ZLIB_VERSION": (
                ConstantEvidence(
                    name="ZLIB_VERSION",
                    value="1.2.7",
                    binary="python27.dll",
                    registration_rva=0x2395F00,
                ),
            ),
        },
    )

    typing_stub = (output / "zlib.pyi").read_text(encoding="utf-8")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert "MAX_WBITS: Final[int] = 15" in typing_stub
    assert "ZLIB_VERSION: Final[str] = '1.2.7'" in typing_stub
    assert manifest["modules"]["zlib"]["constants"]["MAX_WBITS"] == {
        "binary": "python27.dll",
        "confidence": "exact-native-constant",
        "registration_rva": "0x2395e70",
        "value": 15,
    }
    assert manifest["modules"]["zlib"]["constants"]["ZLIB_VERSION"] == {
        "binary": "python27.dll",
        "confidence": "exact-native-constant",
        "registration_rva": "0x2395f00",
        "value": "1.2.7",
    }
    assert report.resolved_constants == 2


def test_main_binary_discovery_includes_python_runtime(tmp_path: Path) -> None:
    win64 = tmp_path / "win64"
    win64.mkdir()
    executable = win64 / "WorldOfTanks.exe"
    runtime = win64 / "python27.dll"
    executable.write_bytes(b"exe")
    runtime.write_bytes(b"dll")

    assert find_main_binaries(tmp_path) == (executable, runtime)


def test_generator_preserves_native_enum_value_and_symbolic_alias(tmp_path: Path) -> None:
    output = tmp_path / "_stubs"

    report = EngineStubGenerator().write(
        output,
        usage=SourceUsageIndex(),
        bindings={},
        binaries=(),
        enums={
            "BlendMode": (
                EnumEvidence(
                    module="DebugDrawer",
                    name="BlendMode",
                    members=(
                        EnumMemberEvidence(
                            name="BM_ADDITIVE",
                            value=2,
                            registration_rva=0xF9D737,
                        ),
                    ),
                    binary="WorldOfTanks.exe",
                    registration_rva=0xF9D6ED,
                ),
            )
        },
    )

    typing_stub = (output / "DebugDrawer.pyi").read_text(encoding="utf-8")
    assert "BM_ADDITIVE: ClassVar[BlendMode]  # native value: 2" in typing_stub
    assert "BM_ADDITIVE: Final[BlendMode] = BlendMode.BM_ADDITIVE" in typing_stub
    assert report.resolved_constants == 1


def test_generator_does_not_emit_enum_stub_for_existing_source_module(tmp_path: Path) -> None:
    output = tmp_path / "_stubs"

    report = EngineStubGenerator().write(
        output,
        usage=SourceUsageIndex(
            modules={"GUI": ModuleUsage(called=frozenset({"load"}))},
            source_modules=frozenset({"gui"}),
        ),
        bindings={},
        binaries=(),
        enums={
            "Subscription": (
                EnumEvidence(
                    module="gui",
                    name="Subscription",
                    members=(),
                    binary="WorldOfTanks.exe",
                    registration_rva=0xC5E175,
                ),
            )
        },
    )

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["modules"]) == ["GUI"]
    assert sorted(path.name for path in output.glob("*.pyi")) == ["GUI.pyi"]
    assert report.modules == 1


def test_generator_rejects_case_colliding_native_module_names(tmp_path: Path) -> None:
    with pytest.raises(EngineStubError, match=r"case-insensitively: GUI, gui"):
        EngineStubGenerator().write(
            tmp_path / "_stubs",
            usage=SourceUsageIndex(
                modules={
                    "GUI": ModuleUsage(called=frozenset({"load"})),
                    "gui": ModuleUsage(attributes=frozenset({"SUBSCRIPTION_ON"})),
                }
            ),
            bindings={},
            binaries=(),
        )


def test_generator_writes_typed_pyi_without_runtime_module(tmp_path: Path) -> None:
    usage = SourceUsageIndex(
        modules={
            "BigWorld": ModuleUsage(
                called=frozenset({"addSpaceGeometryMapping", "callback"}),
                attributes=frozenset({"addSpaceGeometryMapping", "callback", "time"}),
                calls={
                    "addSpaceGeometryMapping": (CallShape(("int", "None", "str")),),
                    "callback": (
                        CallShape(("float", "Callable")),
                        CallShape(("int", "Any")),
                    ),
                },
            )
        }
    )
    evidence = {
        "addSpaceGeometryMapping": (
            BindingEvidence(
                name="addSpaceGeometryMapping",
                signature=NativeSignature(
                    descriptor="({int}, {%}, {unicode}, {int}, {unicode}) -> int",
                    parameter_types=("int", "Any", "str", "int", "str"),
                    return_type="int",
                ),
                parameter_names=("spaceID", "pMapper", "path", "visMask", "environment"),
                binary="WorldOfTanks.exe",
                registration_rva=0x1111,
                confidence="exact-pybind",
            ),
        ),
        "callback": (
            BindingEvidence(
                name="callback",
                signature=NativeSignature(
                    descriptor="({float}, {%}) -> int",
                    parameter_types=("float", "Any"),
                    return_type="int",
                ),
                parameter_names=("delay", "function"),
                binary="WorldOfTanks.exe",
                registration_rva=0x1234,
                confidence="exact-pybind",
            ),
            BindingEvidence(
                name="callback",
                signature=NativeSignature(
                    descriptor="({unicode}) -> None",
                    parameter_types=("str",),
                    return_type="None",
                ),
                parameter_names=("arg0",),
                binary="WorldOfTanks.exe",
                registration_rva=0x5678,
                confidence="exact-pybind",
            ),
            BindingEvidence(
                name="callback",
                signature=NativeSignature(
                    descriptor="({int}, {unicode}) -> None",
                    parameter_types=("int", "str"),
                    return_type="None",
                ),
                parameter_names=("arg0", "arg1"),
                binary="WorldOfTanks.exe",
                registration_rva=0x6789,
                confidence="exact-pybind",
            ),
        ),
    }
    output = tmp_path / "_stubs"

    report = EngineStubGenerator().write(
        output,
        usage=usage,
        bindings=evidence,
        binaries=(),
    )

    typing_stub = (output / "BigWorld.pyi").read_text(encoding="utf-8")
    assert not (output / "BigWorld.py").exists()
    assert "def callback(delay: float, function: Any) -> int: ..." in typing_stub
    assert (
        "def addSpaceGeometryMapping(spaceID: int, pMapper: Any, path: str, "
        "visMask: int = ..., environment: str = ...) -> int: ..." in typing_stub
    )
    assert "unicode" not in typing_stub
    assert "time: Any" in typing_stub
    assert report.modules == 1
    assert report.typed_overloads == 2
    assert (output / "manifest.json").is_file()


def test_generator_refuses_to_replace_workspace_root() -> None:
    with pytest.raises(EngineStubError, match="broad engine-stubs output path"):
        EngineStubGenerator().write(
            Path.cwd(),
            usage=SourceUsageIndex(),
            bindings={},
            binaries=(),
            overwrite=True,
        )

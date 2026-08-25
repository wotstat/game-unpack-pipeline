from __future__ import annotations

import bisect
import hashlib
import json
import keyword
import math
import os
import re
import shutil
import struct
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from capstone import (  # type: ignore[import-untyped]
    CS_ARCH_X86,
    CS_GRP_CALL,
    CS_MODE_64,
    Cs,
    CsError,
    CsInsn,
)
from capstone.x86 import (  # type: ignore[import-untyped]
    X86_OP_IMM,
    X86_OP_MEM,
    X86_OP_REG,
    X86_REG_RIP,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,95}$")
_SIGNATURE = re.compile(r"^\((.*)\)\s*->\s*(.+)$")
_IMPORT = re.compile(r"^\s*import\s+(.+?)\s*(?:#.*)?$", re.MULTILINE)
_FROM_IMPORT = re.compile(
    r"^\s*from\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import\s+(.+?)\s*(?:#.*)?$",
    re.MULTILINE,
)
_PYTHON_SUFFIXES = (".py", ".pyw")
_NATIVE_SUFFIXES = (".exe", ".dll", ".pyd")
_MAX_SOURCE_BYTES = 32 * 1024 * 1024

Confidence = Literal[
    "exact-pybind",
    "exact-native-enum",
    "exact-native-constant",
    "inferred-pyarg",
    "inferred-callsite",
    "unknown",
]
ScalarValue = str | int | float | bool | None


class EngineStubError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NativeSignature:
    descriptor: str
    parameter_types: tuple[str, ...]
    return_type: str


@dataclass(frozen=True, slots=True)
class BindingEvidence:
    name: str
    signature: NativeSignature
    parameter_names: tuple[str, ...]
    binary: str
    registration_rva: int
    confidence: Confidence
    scopes: tuple[str, ...] = ()
    required_parameters: int | None = None


@dataclass(frozen=True, slots=True)
class EnumMemberEvidence:
    name: str
    value: int
    registration_rva: int


@dataclass(frozen=True, slots=True)
class EnumEvidence:
    module: str
    name: str
    members: tuple[EnumMemberEvidence, ...]
    binary: str
    registration_rva: int
    confidence: Confidence = "exact-native-enum"


@dataclass(frozen=True, slots=True)
class ConstantEvidence:
    name: str
    value: ScalarValue
    binary: str
    registration_rva: int
    confidence: Confidence = "exact-native-constant"


@dataclass(frozen=True, slots=True)
class CallShape:
    argument_types: tuple[str, ...]
    keyword_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleUsage:
    called: frozenset[str] = frozenset()
    attributes: frozenset[str] = frozenset()
    calls: Mapping[str, tuple[CallShape, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceUsageIndex:
    modules: Mapping[str, ModuleUsage] = field(default_factory=dict)
    source_modules: frozenset[str] = frozenset()

    @classmethod
    def scan(cls, roots: Sequence[Path], *, max_workers: int = 1) -> SourceUsageIndex:
        if max_workers < 1:
            raise ValueError("source usage workers must be positive")
        source_files: list[tuple[Path, Path]] = []
        for root in roots:
            if not root.is_dir():
                continue
            source_files.extend(
                (root, path)
                for path in sorted(root.rglob("*"))
                if path.is_file() and path.suffix.casefold() in _PYTHON_SUFFIXES
            )
        if not source_files:
            return cls()
        worker_count = min(max_workers, len(source_files))
        if worker_count == 1:
            return cls._scan_entries(source_files)
        chunk_count = min(len(source_files), worker_count * 4)
        chunks: list[list[tuple[Path, Path]]] = [[] for _index in range(chunk_count)]
        for index, entry in enumerate(source_files):
            chunks[index % chunk_count].append(entry)
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            partial_indexes = tuple(executor.map(_scan_source_chunk, chunks))
        return cls._merge(partial_indexes)

    @classmethod
    def _scan_entries(cls, entries: Sequence[tuple[Path, Path]]) -> SourceUsageIndex:
        called: dict[str, set[str]] = defaultdict(set)
        attributes: dict[str, set[str]] = defaultdict(set)
        calls: dict[str, dict[str, set[CallShape]]] = defaultdict(lambda: defaultdict(set))
        source_modules: set[str] = set()
        for root, path in entries:
            try:
                path_stat = path.stat()
                if path_stat.st_size > _MAX_SOURCE_BYTES:
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            source_modules.add(path.stem)
            with suppress(ValueError):
                relative = path.relative_to(root)
                source_modules.update(
                    part for part in relative.parts[:-1] if re.fullmatch(r"[A-Za-z_]\w*", part)
                )
            cls._scan_source(source, called, attributes, calls)
        return cls._from_collections(called, attributes, calls, source_modules)

    @classmethod
    def _merge(cls, values: Sequence[SourceUsageIndex]) -> SourceUsageIndex:
        called: dict[str, set[str]] = defaultdict(set)
        attributes: dict[str, set[str]] = defaultdict(set)
        calls: dict[str, dict[str, set[CallShape]]] = defaultdict(lambda: defaultdict(set))
        source_modules: set[str] = set()
        for value in values:
            source_modules.update(value.source_modules)
            for module, usage in value.modules.items():
                called[module].update(usage.called)
                attributes[module].update(usage.attributes)
                for name, shapes in usage.calls.items():
                    calls[module][name].update(shapes)
        return cls._from_collections(called, attributes, calls, source_modules)

    @classmethod
    def _from_collections(
        cls,
        called: Mapping[str, set[str]],
        attributes: Mapping[str, set[str]],
        calls: Mapping[str, Mapping[str, set[CallShape]]],
        source_modules: set[str],
    ) -> SourceUsageIndex:
        return cls(
            modules={
                module: ModuleUsage(
                    called=frozenset(sorted(called[module])),
                    attributes=frozenset(sorted(attributes[module] | called[module])),
                    calls={
                        name: tuple(
                            sorted(
                                shapes,
                                key=lambda shape: (
                                    len(shape.argument_types),
                                    shape.argument_types,
                                    shape.keyword_names,
                                ),
                            )
                        )
                        for name, shapes in sorted(calls[module].items())
                    },
                )
                for module in sorted(set(called) | set(attributes))
            },
            source_modules=frozenset(sorted(source_modules)),
        )

    @staticmethod
    def _scan_source(
        source: str,
        called: dict[str, set[str]],
        attributes: dict[str, set[str]],
        calls: dict[str, dict[str, set[CallShape]]],
    ) -> None:
        module_aliases: dict[str, str] = {}
        member_aliases: dict[str, tuple[str, str]] = {}
        for match in _IMPORT.finditer(source):
            for raw_item in match.group(1).split(","):
                item = raw_item.strip()
                parsed = re.fullmatch(
                    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?:\s+as\s+([A-Za-z_]\w*))?",
                    item,
                )
                if parsed is None:
                    continue
                module = parsed.group(1).split(".", 1)[0]
                alias = parsed.group(2) or module
                module_aliases[alias] = module
        for match in _FROM_IMPORT.finditer(source):
            module = match.group(1).split(".", 1)[0]
            for raw_item in match.group(2).strip("() ").split(","):
                item = raw_item.strip()
                parsed = re.fullmatch(
                    r"([A-Za-z_]\w*)(?:\s+as\s+([A-Za-z_]\w*))?",
                    item,
                )
                if parsed is None or parsed.group(1) == "*":
                    continue
                member = parsed.group(1)
                alias = parsed.group(2) or member
                member_aliases[alias] = (module, member)
                attributes[module].add(member)

        for alias, module in module_aliases.items():
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(alias)}\.([A-Za-z_]\w*)")
            for match in pattern.finditer(source):
                member = match.group(1)
                attributes[module].add(member)
                tail = source[match.end() : match.end() + 32]
                if re.match(r"\s*\(", tail):
                    called[module].add(member)
                    shape = SourceUsageIndex._call_shape(source, match.end())
                    if shape is not None:
                        calls[module][member].add(shape)
        for alias, (module, member) in member_aliases.items():
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(alias)}\s*(?=\()")
            for match in pattern.finditer(source):
                called[module].add(member)
                shape = SourceUsageIndex._call_shape(source, match.end())
                if shape is not None:
                    calls[module][member].add(shape)

    @staticmethod
    def _call_shape(source: str, position: int) -> CallShape | None:
        opening = source.find("(", position, position + 32)
        if opening < 0:
            return None
        depth = 0
        quote: str | None = None
        escaped = False
        closing = -1
        for index in range(opening, min(len(source), opening + 8192)):
            character = source[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if character in {'"', "'"}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing < 0:
            return None
        arguments = _split_descriptor_arguments(source[opening + 1 : closing])
        positional_types: list[str] = []
        keywords: list[str] = []
        for argument in arguments:
            keyword_match = re.match(r"([A-Za-z_]\w*)\s*=(?!=)", argument)
            value = argument
            if keyword_match is not None:
                keywords.append(keyword_match.group(1))
                value = argument[keyword_match.end() :]
            positional_types.append(SourceUsageIndex._literal_type(value.strip()))
        return CallShape(tuple(positional_types), tuple(keywords))

    @staticmethod
    def _literal_type(value: str) -> str:
        if re.fullmatch(r"[-+]?\d+", value):
            return "int"
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
            return "float"
        if value.startswith(("'", '"', "u'", 'u"', "r'", 'r"')):
            return "str"
        if value in {"True", "False"}:
            return "bool"
        if value == "None":
            return "None"
        if value.startswith(("lambda ", "lambda:")):
            return "Callable"
        return "Any"


def _scan_source_chunk(entries: Sequence[tuple[Path, Path]]) -> SourceUsageIndex:
    return SourceUsageIndex._scan_entries(entries)


def _split_descriptor_arguments(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    result: list[str] = []
    start = 0
    depth = 0
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    openings = set(pairs)
    closings = set(pairs.values())
    for index, character in enumerate(value):
        if character in openings:
            depth += 1
        elif character in closings:
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            result.append(value[start:index].strip())
            start = index + 1
    result.append(value[start:].strip())
    return tuple(item for item in result if item)


def _annotation(value: str) -> str:
    normalized = value.strip()
    while normalized.startswith("{") and normalized.endswith("}"):
        normalized = normalized[1:-1].strip()
    normalized = normalized.replace("const ", "").replace(" &", "").strip()
    lowered = normalized.casefold()
    generic = re.fullmatch(r"([A-Za-z_:]+)\[(.*)\]", normalized)
    if generic is not None:
        container = generic.group(1).casefold()
        members = tuple(_annotation(item) for item in _split_descriptor_arguments(generic.group(2)))
        if container in {"list", "sequence", "vector"}:
            return f"list[{members[0] if members else 'Any'}]"
        if container in {"optional"}:
            return f"{members[0] if members else 'Any'} | None"
        if container in {"dict", "mapping"}:
            key = members[0] if members else "Any"
            item = members[1] if len(members) > 1 else "Any"
            return f"dict[{key}, {item}]"
        if container in {"tuple"}:
            return f"tuple[{', '.join(members) if members else 'Any, ...'}]"
    aliases = {
        "%": "Any",
        "object": "Any",
        "pyobject": "Any",
        "py::object": "Any",
        "handle": "Any",
        "callable": "Any",
        "function": "Any",
        "*args": "Any",
        "**kwargs": "Any",
        "none": "None",
        "void": "None",
        "bool": "bool",
        "float": "float",
        "double": "float",
        "int": "int",
        "int32": "int",
        "int64": "int",
        "uint": "int",
        "uint32": "int",
        "uint64": "int",
        "long": "int",
        "size_t": "int",
        "str": "str",
        "string": "str",
        "unicode": "str",
        "bytes": "bytes",
        "dict": "dict[Any, Any]",
        "list": "list[Any]",
        "tuple": "tuple[Any, ...]",
    }
    if lowered in aliases:
        return aliases[lowered]
    math_types = {"Vector2", "Vector3", "Vector4", "Matrix", "Quaternion"}
    leaf = normalized.rsplit("::", 1)[-1]
    if leaf in math_types:
        return f"Math.{leaf}"
    if re.fullmatch(r"[A-Za-z_]\w*", leaf):
        return leaf
    return "Any"


def parse_native_signature(descriptor: str) -> NativeSignature:
    match = _SIGNATURE.fullmatch(descriptor.strip())
    if match is None:
        raise ValueError(f"not a native signature descriptor: {descriptor!r}")
    parameters = tuple(_annotation(item) for item in _split_descriptor_arguments(match.group(1)))
    return NativeSignature(
        descriptor=descriptor.strip(),
        parameter_types=parameters,
        return_type=_annotation(match.group(2)),
    )


def _same_signature_family(left: NativeSignature, right: NativeSignature) -> bool:
    if left.return_type != right.return_type:
        return False
    shorter, longer = sorted((left.parameter_types, right.parameter_types), key=len)
    return len(shorter) != len(longer) and longer[: len(shorter)] == shorter


@dataclass(frozen=True, slots=True)
class _PeSection:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    characteristics: int

    @property
    def executable(self) -> bool:
        return bool(self.characteristics & 0x20000000 or self.characteristics & 0x20)


@dataclass(frozen=True, slots=True)
class _StringRecord:
    rva: int
    value: str


@dataclass(frozen=True, slots=True)
class _RuntimeFunction:
    begin: int
    end: int


@dataclass(frozen=True, slots=True)
class _Reference:
    instruction_rva: int
    target: _StringRecord


@dataclass(frozen=True, slots=True)
class _Call:
    instruction_rva: int
    target_rva: int


@dataclass(frozen=True, slots=True)
class _FunctionAnalysis:
    function: _RuntimeFunction
    references: tuple[_Reference, ...]
    calls: tuple[_Call, ...]


class PeImage:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = path.read_bytes()
        except OSError as exc:
            raise EngineStubError(f"cannot read native binary {path}: {exc}") from exc
        if len(self.data) < 0x100 or self.data[:2] != b"MZ":
            raise EngineStubError(f"not a PE image: {path}")
        pe_offset = self._u32(0x3C)
        if pe_offset + 24 > len(self.data) or self.data[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise EngineStubError(f"invalid PE header: {path}")
        coff = pe_offset + 4
        section_count = self._u16(coff + 2)
        optional_size = self._u16(coff + 16)
        optional = coff + 20
        magic = self._u16(optional)
        if magic != 0x20B:
            raise EngineStubError(f"only PE32+ images are supported: {path}")
        self.image_base = self._u64(optional + 24)
        directories = optional + 112
        self.exception_rva = self._u32(directories + 3 * 8)
        self.exception_size = self._u32(directories + 3 * 8 + 4)
        section_table = optional + optional_size
        sections: list[_PeSection] = []
        for index in range(section_count):
            offset = section_table + index * 40
            if offset + 40 > len(self.data):
                raise EngineStubError(f"truncated PE section table: {path}")
            name = self.data[offset : offset + 8].split(b"\0", 1)[0].decode("ascii", "replace")
            sections.append(
                _PeSection(
                    name=name,
                    virtual_size=self._u32(offset + 8),
                    virtual_address=self._u32(offset + 12),
                    raw_size=self._u32(offset + 16),
                    raw_offset=self._u32(offset + 20),
                    characteristics=self._u32(offset + 36),
                )
            )
        self.sections = tuple(sections)

    def _u16(self, offset: int) -> int:
        return cast(int, struct.unpack_from("<H", self.data, offset)[0])

    def _u32(self, offset: int) -> int:
        return cast(int, struct.unpack_from("<I", self.data, offset)[0])

    def _u64(self, offset: int) -> int:
        return cast(int, struct.unpack_from("<Q", self.data, offset)[0])

    def rva_to_offset(self, rva: int) -> int | None:
        for section in self.sections:
            extent = max(section.virtual_size, section.raw_size)
            if section.virtual_address <= rva < section.virtual_address + extent:
                relative = rva - section.virtual_address
                if relative >= section.raw_size:
                    return None
                offset = section.raw_offset + relative
                return offset if offset < len(self.data) else None
        return rva if 0 <= rva < len(self.data) else None

    def bytes_at_rva(self, rva: int, size: int) -> bytes:
        offset = self.rva_to_offset(rva)
        if offset is None:
            return b""
        return self.data[offset : min(len(self.data), offset + size)]

    def strings(self) -> tuple[_StringRecord, ...]:
        records: list[_StringRecord] = []
        for section in self.sections:
            if section.raw_size == 0:
                continue
            start = section.raw_offset
            end = min(len(self.data), start + section.raw_size)
            cursor = start
            while cursor < end:
                if 0x20 <= self.data[cursor] <= 0x7E:
                    value_start = cursor
                    while cursor < end and 0x20 <= self.data[cursor] <= 0x7E:
                        cursor += 1
                    if cursor - value_start >= 2 and (cursor == end or self.data[cursor] == 0):
                        value = self.data[value_start:cursor].decode("ascii")
                        rva = section.virtual_address + value_start - section.raw_offset
                        records.append(_StringRecord(rva=rva, value=value))
                cursor += 1
        return tuple(records)

    def runtime_functions(self) -> tuple[_RuntimeFunction, ...]:
        functions: list[_RuntimeFunction] = []
        offset = self.rva_to_offset(self.exception_rva)
        if offset is not None and self.exception_size >= 12:
            end = min(len(self.data), offset + self.exception_size)
            for cursor in range(offset, end - 11, 12):
                begin, finish, _unwind = struct.unpack_from("<III", self.data, cursor)
                if begin and finish > begin and self.rva_to_offset(begin) is not None:
                    functions.append(_RuntimeFunction(begin=begin, end=finish))
        if not functions:
            functions.extend(
                _RuntimeFunction(
                    begin=section.virtual_address,
                    end=section.virtual_address + section.raw_size,
                )
                for section in self.sections
                if section.executable and section.raw_size
            )
        return tuple(sorted(set(functions), key=lambda item: (item.begin, item.end)))


class NativeBindingAnalyzer:
    def analyze(
        self,
        binary: Path,
        usage: SourceUsageIndex,
    ) -> tuple[BindingEvidence, ...]:
        bindings, _enums, _constants = self.analyze_with_enums(binary, usage, {})
        return bindings

    def analyze_with_enums(
        self,
        binary: Path,
        usage: SourceUsageIndex,
        constant_candidates: Mapping[str, tuple[ScalarValue, ...]],
        *,
        max_workers: int = 1,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[
        tuple[BindingEvidence, ...],
        tuple[EnumEvidence, ...],
        tuple[ConstantEvidence, ...],
    ]:
        if max_workers < 1:
            raise ValueError("native analysis workers must be positive")
        report_progress = progress or (lambda _message: None)
        started = time.monotonic()
        image = PeImage(binary)
        all_strings = image.strings()
        runtime_functions = image.runtime_functions()
        report_progress(
            f"loaded {len(image.data)} bytes, {len(all_strings)} strings and "
            f"{len(runtime_functions)} runtime functions in {time.monotonic() - started:.1f}s"
        )
        signatures: dict[int, NativeSignature] = {}
        identifiers: dict[int, _StringRecord] = {}
        wanted_names = {
            name for module in usage.modules.values() for name in module.attributes
        } | set(usage.modules)
        constant_string_values = {
            value
            for values in constant_candidates.values()
            for value in values
            if isinstance(value, str)
        }
        constant_strings: dict[int, _StringRecord] = {}
        for record in all_strings:
            if _SIGNATURE.fullmatch(record.value):
                with suppress(ValueError):
                    signatures[record.rva] = parse_native_signature(record.value)
            elif record.value in constant_string_values:
                constant_strings[record.rva] = record
            elif _IDENTIFIER.fullmatch(record.value):
                identifiers[record.rva] = record
        relevant_strings = {
            **{
                rva: _StringRecord(rva=rva, value=signature.descriptor)
                for rva, signature in signatures.items()
            },
            **identifiers,
            **constant_strings,
        }
        started = time.monotonic()
        analyses = self._disassemble(
            image,
            relevant_strings,
            runtime_functions,
            max_workers=max_workers,
        )
        report_progress(
            f"disassembled {len(runtime_functions)} runtime functions into "
            f"{len(analyses)} relevant analyses with {max_workers} workers in "
            f"{time.monotonic() - started:.1f}s"
        )
        started = time.monotonic()
        bindings = self._resolve(binary.name, analyses, signatures, wanted_names)
        report_progress(f"resolved bindings in {time.monotonic() - started:.1f}s")
        started = time.monotonic()
        enums = self._resolve_enums(image, analyses, usage)
        report_progress(f"resolved enums in {time.monotonic() - started:.1f}s")
        started = time.monotonic()
        constants = self._resolve_constants(image, analyses, constant_candidates)
        report_progress(f"resolved constants in {time.monotonic() - started:.1f}s")
        return bindings, enums, constants

    @staticmethod
    def _register_family(register: str) -> str:
        aliases = {
            "r8b": "r8",
            "r8w": "r8",
            "r8d": "r8",
            "r9b": "r9",
            "r9w": "r9",
            "r9d": "r9",
        }
        return aliases.get(register, register)

    @classmethod
    def _constant_before(
        cls,
        instructions: Sequence[CsInsn],
        index: int,
        register: str,
    ) -> int | None:
        register = cls._register_family(register)
        for position in range(index - 1, max(-1, index - 9), -1):
            instruction = instructions[position]
            with suppress(CsError):
                if instruction.group(CS_GRP_CALL):
                    break
            operands = instruction.operands
            if len(operands) < 2 or operands[0].type != X86_OP_REG:
                continue
            destination = cls._register_family(instruction.reg_name(operands[0].reg))
            if destination != register:
                continue
            if (
                instruction.mnemonic == "xor"
                and operands[1].type == X86_OP_REG
                and cls._register_family(instruction.reg_name(operands[1].reg)) == register
            ):
                return 0
            if instruction.mnemonic == "mov" and operands[1].type == X86_OP_IMM:
                value = int(operands[1].imm)
                if instruction.reg_name(operands[0].reg).endswith("d"):
                    value &= 0xFFFF_FFFF
                    if value >= 0x8000_0000:
                        value -= 0x1_0000_0000
                return value
            if instruction.mnemonic == "lea" and operands[1].type == X86_OP_MEM:
                base = cls._register_family(instruction.reg_name(operands[1].mem.base))
                if base and base != register:
                    base_value = cls._constant_before(
                        instructions,
                        position,
                        base,
                    )
                    if base_value is not None:
                        return base_value + int(operands[1].mem.disp)
        return None

    @classmethod
    def _resolve_enums(
        cls,
        image: PeImage,
        analyses: Sequence[_FunctionAnalysis],
        usage: SourceUsageIndex,
    ) -> tuple[EnumEvidence, ...]:
        disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
        disassembler.detail = True
        resolved: dict[tuple[str, str], EnumEvidence] = {}
        for analysis in analyses:
            code = image.bytes_at_rva(
                analysis.function.begin,
                analysis.function.end - analysis.function.begin,
            )
            try:
                instructions = tuple(
                    disassembler.disasm(
                        code,
                        image.image_base + analysis.function.begin,
                    )
                )
            except (CsError, IndexError, ValueError):
                continue
            by_rva = {
                instruction.address - image.image_base: index
                for index, instruction in enumerate(instructions)
            }

            references = tuple(sorted(analysis.references, key=lambda item: item.instruction_rva))
            reference_registers: dict[int, str] = {}
            for reference in references:
                instruction_index = by_rva.get(reference.instruction_rva)
                if instruction_index is None:
                    continue
                operands = instructions[instruction_index].operands
                if operands and operands[0].type == X86_OP_REG:
                    reference_registers[reference.instruction_rva] = cls._register_family(
                        instructions[instruction_index].reg_name(operands[0].reg)
                    )
            member_calls: list[tuple[int, int, EnumMemberEvidence]] = []
            for reference in references:
                if reference_registers.get(reference.instruction_rva) != "rdx":
                    continue
                instruction_index = by_rva.get(reference.instruction_rva)
                if instruction_index is None:
                    continue
                value = cls._constant_before(instructions, instruction_index, "r8")
                if value is None:
                    continue
                moved_enum_object = False
                call_target: int | None = None
                for instruction in instructions[instruction_index + 1 : instruction_index + 7]:
                    operands = instruction.operands
                    if (
                        instruction.mnemonic == "mov"
                        and len(operands) >= 2
                        and operands[0].type == X86_OP_REG
                        and operands[1].type == X86_OP_REG
                        and cls._register_family(instruction.reg_name(operands[0].reg)) == "rcx"
                        and cls._register_family(instruction.reg_name(operands[1].reg)) == "rax"
                    ):
                        moved_enum_object = True
                    with suppress(CsError):
                        if (
                            instruction.group(CS_GRP_CALL)
                            and instruction.operands
                            and instruction.operands[0].type == X86_OP_IMM
                        ):
                            call_target = int(instruction.operands[0].imm) - image.image_base
                            break
                if not moved_enum_object or call_target is None:
                    continue
                member_calls.append(
                    (
                        reference.instruction_rva,
                        call_target,
                        EnumMemberEvidence(
                            name=reference.target.value,
                            value=value,
                            registration_rva=reference.instruction_rva,
                        ),
                    )
                )

            for sequence in cls._enum_member_sequences(member_calls):
                first_rva = sequence[0][0]
                constructor = next(
                    (
                        reference
                        for reference in reversed(references)
                        if first_rva - 256 <= reference.instruction_rva < first_rva
                        and reference_registers.get(reference.instruction_rva) == "r8"
                        and reference.target.value[0].isupper()
                    ),
                    None,
                )
                if constructor is None:
                    continue
                enum_name = constructor.target.value
                members = tuple(
                    sorted(
                        {item.name: item for _rva, _target, item in sequence}.values(),
                        key=lambda item: (item.value, item.name),
                    )
                )
                if len(members) < 2 or enum_name in {member.name for member in members}:
                    continue
                module = cls._enum_owner(
                    references,
                    reference_registers,
                    usage,
                    enum_name,
                    members,
                    sequence[-1][0],
                )
                if module is None:
                    continue
                candidate = EnumEvidence(
                    module=module,
                    name=enum_name,
                    members=members,
                    binary=image.path.name,
                    registration_rva=constructor.instruction_rva,
                )
                key = (module, enum_name)
                previous = resolved.get(key)
                if previous is None or len(candidate.members) > len(previous.members):
                    resolved[key] = candidate
        return tuple(sorted(resolved.values(), key=lambda item: (item.module, item.name)))

    @staticmethod
    def _enum_member_sequences(
        values: Sequence[tuple[int, int, EnumMemberEvidence]],
    ) -> tuple[tuple[tuple[int, int, EnumMemberEvidence], ...], ...]:
        sequences: list[tuple[tuple[int, int, EnumMemberEvidence], ...]] = []
        current: list[tuple[int, int, EnumMemberEvidence]] = []
        for value in sorted(values):
            if current and (value[1] != current[-1][1] or value[0] - current[-1][0] > 128):
                if len(current) >= 2:
                    sequences.append(tuple(current))
                current = []
            current.append(value)
        if len(current) >= 2:
            sequences.append(tuple(current))
        return tuple(sequences)

    @staticmethod
    def _enum_owner(
        references: Sequence[_Reference],
        reference_registers: Mapping[int, str],
        usage: SourceUsageIndex,
        enum_name: str,
        members: Sequence[EnumMemberEvidence],
        last_member_rva: int,
    ) -> str | None:
        member_names = {member.name for member in members}
        source_owners = {
            module
            for module, module_usage in usage.modules.items()
            if enum_name in module_usage.attributes
            or member_names.intersection(module_usage.attributes)
        }
        if len(source_owners) == 1:
            return next(iter(source_owners))

        for initializer in references:
            if (
                initializer.instruction_rva <= last_member_rva
                or initializer.target.value != "__init__"
                or reference_registers.get(initializer.instruction_rva) != "rdx"
            ):
                continue
            owner = next(
                (
                    reference.target.value
                    for reference in reversed(references)
                    if initializer.instruction_rva - 512
                    <= reference.instruction_rva
                    < initializer.instruction_rva
                    and reference.instruction_rva > last_member_rva
                    and reference.target.value not in member_names
                    and reference.target.value != enum_name
                    and reference.target.value[0].isupper()
                    and reference_registers.get(reference.instruction_rva) != "rdx"
                ),
                None,
            )
            if owner is not None:
                return owner
        return None

    @classmethod
    def _resolve_constants(
        cls,
        image: PeImage,
        analyses: Sequence[_FunctionAnalysis],
        constant_candidates: Mapping[str, tuple[ScalarValue, ...]],
    ) -> tuple[ConstantEvidence, ...]:
        if not constant_candidates:
            return ()
        disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
        disassembler.detail = True
        resolved: dict[tuple[str, ScalarValue], ConstantEvidence] = {}
        for analysis in analyses:
            if not any(
                reference.target.value in constant_candidates for reference in analysis.references
            ):
                continue
            code = image.bytes_at_rva(
                analysis.function.begin,
                analysis.function.end - analysis.function.begin,
            )
            try:
                instructions = tuple(
                    disassembler.disasm(
                        code,
                        image.image_base + analysis.function.begin,
                    )
                )
            except (CsError, IndexError, ValueError):
                continue
            by_rva = {
                instruction.address - image.image_base: index
                for index, instruction in enumerate(instructions)
            }
            references = tuple(sorted(analysis.references, key=lambda item: item.instruction_rva))
            for reference in references:
                name = reference.target.value
                expected_values = constant_candidates.get(name)
                if expected_values is None:
                    continue
                instruction_index = by_rva.get(reference.instruction_rva)
                if instruction_index is None:
                    continue
                name_instruction = instructions[instruction_index]
                name_operands = name_instruction.operands
                if (
                    not name_operands
                    or name_operands[0].type != X86_OP_REG
                    or cls._register_family(name_instruction.reg_name(name_operands[0].reg))
                    != "rdx"
                ):
                    continue
                has_registration_call = False
                for instruction in instructions[instruction_index + 1 : instruction_index + 7]:
                    with suppress(CsError):
                        if instruction.group(CS_GRP_CALL):
                            has_registration_call = True
                            break
                if not has_registration_call:
                    continue

                native_integer = cls._constant_before(
                    instructions,
                    instruction_index,
                    "r8",
                )
                value: ScalarValue | object = object()
                if not expected_values and native_integer is not None:
                    value = native_integer
                else:
                    for expected in expected_values:
                        if isinstance(expected, bool) and native_integer in {0, 1}:
                            if expected is bool(native_integer):
                                value = expected
                                break
                        elif type(expected) is int and native_integer == expected:
                            value = expected
                            break
                if not isinstance(value, (str, int, float, bool)) and value is not None:
                    for value_reference in reversed(references):
                        if not (
                            value_reference.target.value in expected_values
                            and isinstance(value_reference.target.value, str)
                            and 0
                            < reference.instruction_rva - value_reference.instruction_rva
                            <= 128
                        ):
                            continue
                        value_index = by_rva.get(value_reference.instruction_rva)
                        if value_index is None or value_index >= instruction_index:
                            continue
                        moved_result = False
                        called_constructor = False
                        for instruction in instructions[value_index + 1 : instruction_index]:
                            with suppress(CsError):
                                if instruction.group(CS_GRP_CALL):
                                    called_constructor = True
                            operands = instruction.operands
                            if (
                                len(operands) >= 2
                                and instruction.mnemonic == "mov"
                                and operands[0].type == X86_OP_REG
                                and operands[1].type == X86_OP_REG
                                and cls._register_family(instruction.reg_name(operands[0].reg))
                                == "r8"
                                and instruction.reg_name(operands[1].reg) == "rax"
                            ):
                                moved_result = True
                        if called_constructor and moved_result:
                            value = value_reference.target.value
                            break
                if not isinstance(value, (str, int, float, bool)) and value is not None:
                    continue
                candidate = ConstantEvidence(
                    name=name,
                    value=value,
                    binary=image.path.name,
                    registration_rva=reference.instruction_rva,
                )
                resolved.setdefault((name, candidate.value), candidate)
        return tuple(
            sorted(
                resolved.values(),
                key=lambda item: (item.name, repr(item.value), item.registration_rva),
            )
        )

    @staticmethod
    def _instruction_targets(instruction: CsInsn, image: PeImage) -> Iterable[int]:
        for operand in instruction.operands:
            if operand.type == X86_OP_IMM:
                value = int(operand.imm)
                if image.image_base <= value < image.image_base + 0x1_0000_0000:
                    yield value - image.image_base
            elif operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP:
                value = instruction.address + instruction.size + int(operand.mem.disp)
                if image.image_base <= value < image.image_base + 0x1_0000_0000:
                    yield value - image.image_base

    def _disassemble(
        self,
        image: PeImage,
        strings: Mapping[int, _StringRecord],
        functions: Sequence[_RuntimeFunction],
        *,
        max_workers: int,
    ) -> tuple[_FunctionAnalysis, ...]:
        if max_workers <= 1 or len(functions) <= 1:
            return self._disassemble_functions(image, strings, functions)
        worker_count = min(max_workers, len(functions))
        target_bytes = max(
            1,
            math.ceil(
                sum(function.end - function.begin for function in functions) / (worker_count * 4)
            ),
        )
        chunks: list[list[_RuntimeFunction]] = []
        current: list[_RuntimeFunction] = []
        current_bytes = 0
        for function in functions:
            size = function.end - function.begin
            if current and current_bytes + size > target_bytes:
                chunks.append(current)
                current = []
                current_bytes = 0
            current.append(function)
            current_bytes += size
        if current:
            chunks.append(current)
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_initialize_native_process,
            initargs=(image.path, dict(strings)),
        ) as executor:
            partial_analyses = executor.map(
                _disassemble_native_chunk,
                (tuple(chunk) for chunk in chunks),
            )
            return tuple(
                sorted(
                    (analysis for values in partial_analyses for analysis in values),
                    key=lambda analysis: (
                        analysis.function.begin,
                        analysis.function.end,
                    ),
                )
            )

    @classmethod
    def _disassemble_functions(
        cls,
        image: PeImage,
        strings: Mapping[int, _StringRecord],
        functions: Sequence[_RuntimeFunction],
    ) -> tuple[_FunctionAnalysis, ...]:
        disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
        disassembler.detail = True
        disassembler.skipdata = True
        analyses: list[_FunctionAnalysis] = []
        for function in functions:
            code = image.bytes_at_rva(function.begin, function.end - function.begin)
            if not code:
                continue
            references: list[_Reference] = []
            calls: list[_Call] = []
            try:
                instructions = disassembler.disasm(code, image.image_base + function.begin)
                for instruction in instructions:
                    instruction_rva = instruction.address - image.image_base
                    try:
                        for target in cls._instruction_targets(instruction, image):
                            record = strings.get(target)
                            if record is not None:
                                references.append(_Reference(instruction_rva, record))
                        if instruction.group(CS_GRP_CALL) and instruction.operands:
                            operand = instruction.operands[0]
                            if operand.type == X86_OP_IMM:
                                target = int(operand.imm) - image.image_base
                                if 0 <= target < 0x1_0000_0000:
                                    calls.append(_Call(instruction_rva, target))
                    except CsError:
                        continue
            except (CsError, IndexError, ValueError):
                continue
            if references:
                analyses.append(
                    _FunctionAnalysis(
                        function=function,
                        references=tuple(references),
                        calls=tuple(calls),
                    )
                )
        return tuple(analyses)

    @staticmethod
    def _resolve(
        binary_name: str,
        analyses: Sequence[_FunctionAnalysis],
        signatures: Mapping[int, NativeSignature],
        wanted_names: set[str],
    ) -> tuple[BindingEvidence, ...]:
        by_start = {analysis.function.begin: analysis for analysis in analyses}
        starts = sorted(by_start)

        def containing(target: int) -> _FunctionAnalysis | None:
            index = bisect.bisect_right(starts, target) - 1
            if index < 0:
                return None
            candidate = by_start[starts[index]]
            return candidate if target < candidate.function.end else None

        signatures_by_function: dict[int, tuple[NativeSignature, ...]] = {}
        for analysis in analyses:
            values = tuple(
                dict.fromkeys(
                    signatures[reference.target.rva]
                    for reference in analysis.references
                    if reference.target.rva in signatures
                )
            )
            if values:
                signatures_by_function[analysis.function.begin] = values

        evidence: list[BindingEvidence] = []
        for analysis in analyses:
            name_references = [
                reference
                for reference in analysis.references
                if reference.target.value in wanted_names
            ]
            if not name_references:
                continue
            # A runtime function can register several unrelated scopes. Scope assignment
            # requires object-level dataflow, not merely another module-name xref in the same
            # unwind range, so keep it unresolved for now.
            scopes: tuple[str, ...] = ()
            ordered_names = sorted(name_references, key=lambda item: item.instruction_rva)
            for name_index, name_reference in enumerate(ordered_names):
                segment_end = (
                    ordered_names[name_index + 1].instruction_rva
                    if name_index + 1 < len(ordered_names)
                    else min(analysis.function.end, name_reference.instruction_rva + 1024)
                )
                segment_start = max(analysis.function.begin, name_reference.instruction_rva - 160)
                signature_candidates: list[tuple[int, NativeSignature]] = []
                for call in analysis.calls:
                    if not segment_start <= call.instruction_rva < segment_end:
                        continue
                    target_function = containing(call.target_rva)
                    if target_function is None:
                        continue
                    for signature in signatures_by_function.get(target_function.function.begin, ()):
                        signature_candidates.append(
                            (abs(call.instruction_rva - name_reference.instruction_rva), signature)
                        )
                if not signature_candidates:
                    for signature in signatures_by_function.get(analysis.function.begin, ()):
                        signature_candidates.append((0, signature))
                if not signature_candidates:
                    continue
                closest = min(distance for distance, _signature in signature_candidates)
                closest_signatures = tuple(
                    dict.fromkeys(
                        signature
                        for distance, signature in signature_candidates
                        if distance == closest
                    )
                )
                selected = tuple(
                    dict.fromkeys(
                        (
                            *closest_signatures,
                            *(
                                signature
                                for _distance, signature in signature_candidates
                                if any(
                                    _same_signature_family(signature, closest_signature)
                                    for closest_signature in closest_signatures
                                )
                            ),
                        )
                    )
                )
                for signature in selected:
                    # A nearby identifier is not enough to prove py::arg ownership: large
                    # registration functions interleave argument names from adjacent bindings.
                    # Keep stable generated names until dataflow proves argument_record writes.
                    parameter_names = tuple(
                        f"arg{index}" for index in range(len(signature.parameter_types))
                    )
                    evidence.append(
                        BindingEvidence(
                            name=name_reference.target.value,
                            signature=signature,
                            parameter_names=parameter_names,
                            binary=binary_name,
                            registration_rva=name_reference.instruction_rva,
                            confidence="exact-pybind",
                            scopes=scopes,
                        )
                    )
        unique: dict[tuple[str, str, tuple[str, ...]], BindingEvidence] = {}
        for item in evidence:
            key = item.name, item.signature.descriptor, item.parameter_names
            previous = unique.get(key)
            if previous is None or item.registration_rva < previous.registration_rva:
                unique[key] = item
        return tuple(
            sorted(unique.values(), key=lambda item: (item.name, item.signature.descriptor))
        )


_NATIVE_PROCESS_IMAGE: PeImage | None = None
_NATIVE_PROCESS_STRINGS: Mapping[int, _StringRecord] | None = None


def _initialize_native_process(
    binary: Path,
    strings: Mapping[int, _StringRecord],
) -> None:
    global _NATIVE_PROCESS_IMAGE, _NATIVE_PROCESS_STRINGS
    _NATIVE_PROCESS_IMAGE = PeImage(binary)
    _NATIVE_PROCESS_STRINGS = strings


def _disassemble_native_chunk(
    functions: Sequence[_RuntimeFunction],
) -> tuple[_FunctionAnalysis, ...]:
    if _NATIVE_PROCESS_IMAGE is None or _NATIVE_PROCESS_STRINGS is None:
        raise RuntimeError("native analysis process was not initialized")
    return NativeBindingAnalyzer._disassemble_functions(
        _NATIVE_PROCESS_IMAGE,
        _NATIVE_PROCESS_STRINGS,
        functions,
    )


@dataclass(frozen=True, slots=True)
class BinaryReport:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class StubGenerationReport:
    modules: int
    typing_files: int
    typed_overloads: int
    resolved_constants: int
    unknown_functions: int
    manifest_path: str


class EngineStubGenerator:
    def write(
        self,
        output: Path,
        *,
        usage: SourceUsageIndex,
        bindings: Mapping[str, Sequence[BindingEvidence]],
        binaries: Sequence[Path],
        binary_root: Path | None = None,
        enums: Mapping[str, Sequence[EnumEvidence]] | None = None,
        constants: Mapping[str, Sequence[ConstantEvidence]] | None = None,
        overwrite: bool = False,
    ) -> StubGenerationReport:
        output = output.absolute()
        protected = {Path(output.anchor), Path.home().absolute(), Path.cwd().absolute()}
        if output in protected:
            raise EngineStubError(f"refusing broad engine-stubs output path: {output}")
        if output.exists() and not overwrite:
            raise EngineStubError(f"output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        typed_overloads = 0
        resolved_constants = 0
        unknown_functions = 0
        enums = enums or {}
        constant_evidence = constants or {}
        enums_by_module: dict[str, dict[str, list[EnumEvidence]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for enum_values in enums.values():
            for enum_value in enum_values:
                enums_by_module[enum_value.module][enum_value.name].append(enum_value)
        try:
            module_names = sorted(
                set(usage.modules) | (set(enums_by_module) - set(usage.source_modules))
            )
            module_by_casefold: dict[str, str] = {}
            for module in module_names:
                previous = module_by_casefold.get(module.casefold())
                if previous is not None:
                    raise EngineStubError(
                        f"engine stub module names collide case-insensitively: {previous}, {module}"
                    )
                module_by_casefold[module.casefold()] = module
            manifest_modules: dict[str, Any] = {}
            for module in module_names:
                module_usage = usage.modules.get(module, ModuleUsage())
                module_enum_members: dict[str, frozenset[str]] = {}
                module_aliases: dict[str, tuple[str, str]] = {}
                for enum_name, enum_values in enums_by_module.get(module, {}).items():
                    discovered_members = frozenset(
                        member.name for value in enum_values for member in value.members
                    )
                    module_enum_members[enum_name] = discovered_members
                    for member in discovered_members:
                        module_aliases.setdefault(member, (enum_name, member))
                functions = {name for name in module_usage.called if self._valid_member(name)}
                attributes = set(module_usage.attributes) - functions - set(module_enum_members)
                attributes = {name for name in attributes if self._valid_member(name)}
                typing_text, module_manifest, typed, constant_count, unknown = self._render_module(
                    module,
                    module_usage,
                    functions,
                    attributes,
                    bindings,
                    module_aliases,
                    module_enum_members,
                    enums_by_module.get(module, {}),
                    constant_evidence,
                )
                (temporary / f"{module}.pyi").write_text(typing_text, encoding="utf-8")
                manifest_modules[module] = module_manifest
                typed_overloads += typed
                resolved_constants += constant_count
                unknown_functions += unknown
            binary_reports = tuple(
                self._binary_report(path, binary_root=binary_root) for path in binaries
            )
            manifest = {
                "schema_version": 1,
                "generator": "game-downloader-engine-stubs",
                "binaries": [asdict(report) for report in binary_reports],
                "modules": manifest_modules,
                "summary": {
                    "modules": len(module_names),
                    "typed_overloads": typed_overloads,
                    "resolved_constants": resolved_constants,
                    "unknown_functions": unknown_functions,
                },
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (temporary / "py.typed").write_bytes(b"")
            for generated in temporary.rglob("*"):
                if generated.is_file():
                    generated.chmod(0o444)
            if output.exists():
                if not overwrite:
                    raise EngineStubError(f"output already exists: {output}")
                shutil.rmtree(output)
            os.replace(temporary, output)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return StubGenerationReport(
            modules=len(module_names),
            typing_files=len(module_names),
            typed_overloads=typed_overloads,
            resolved_constants=resolved_constants,
            unknown_functions=unknown_functions,
            manifest_path=(output / "manifest.json").as_posix(),
        )

    @staticmethod
    def _valid_member(name: str) -> bool:
        return name.isidentifier() and not keyword.iskeyword(name) and name != "__debug__"

    @staticmethod
    def _binary_report(path: Path, *, binary_root: Path | None = None) -> BinaryReport:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        display_path = path
        if binary_root is not None:
            try:
                display_path = path.relative_to(binary_root)
            except ValueError as exc:
                raise EngineStubError(f"native binary is outside Client Tree: {path}") from exc
        return BinaryReport(path=display_path.as_posix(), sha256=digest.hexdigest(), size=size)

    def _render_module(
        self,
        module: str,
        module_usage: ModuleUsage,
        functions: set[str],
        attributes: set[str],
        bindings: Mapping[str, Sequence[BindingEvidence]],
        aliases: Mapping[str, tuple[str, str]],
        enum_members: Mapping[str, frozenset[str]],
        enums: Mapping[str, Sequence[EnumEvidence]],
        constant_evidence: Mapping[str, Sequence[ConstantEvidence]],
    ) -> tuple[str, dict[str, Any], int, int, int]:
        pyi_lines = [
            "# Generated by game-downloader from static native metadata; do not edit.",
            "from typing import Any, ClassVar, Final, overload",
            "",
        ]
        if self._module_uses_math(functions, bindings):
            pyi_lines.insert(2, "import Math")
        module_manifest: dict[str, Any] = {"functions": {}, "attributes": sorted(attributes)}
        typed = 0
        resolved_constant_count = 0
        unknown = 0
        for name in sorted(functions):
            evidence = self._evidence_for_module(
                module,
                bindings.get(name, ()),
                module_usage.calls.get(name, ()),
            )
            pyi_lines.extend(self._render_typing_function(name, evidence, indentation=""))
            if evidence:
                typed += len(evidence)
                module_manifest["functions"][name] = [
                    self._evidence_document(item) for item in evidence
                ]
            else:
                unknown += 1
                module_manifest["functions"][name] = [{"confidence": "unknown"}]
        constant_manifest: dict[str, Any] = {}
        plain_attributes = attributes - set(aliases)
        for name in sorted(plain_attributes):
            constant = self._unambiguous_constant(constant_evidence.get(name, ()))
            if constant is None:
                pyi_lines.append(f"{name}: Any")
            else:
                pyi_lines.append(
                    f"{name}: Final[{self._scalar_annotation(constant.value)}] = {constant.value!r}"
                )
                constant_manifest[name] = self._constant_document(constant)
                resolved_constant_count += 1
        if plain_attributes:
            pyi_lines.append("")
        if constant_manifest:
            module_manifest["constants"] = constant_manifest

        class_manifest: dict[str, Any] = {}
        for class_name, expected_members in sorted(enum_members.items()):
            pyi_lines.append(f"class {class_name}:")
            enum = self._enum_for_class(
                class_name,
                expected_members,
                enums.get(class_name, ()),
            )
            enum_values = (
                {member.name: member for member in enum.members} if enum is not None else {}
            )
            class_attributes = sorted(name for name in expected_members if self._valid_member(name))
            if not class_attributes:
                pyi_lines.append("    ...")
            for attribute in class_attributes:
                member = enum_values.get(attribute)
                if member is None:
                    pyi_lines.append(f"    {attribute}: Any")
                else:
                    pyi_lines.append(
                        f"    {attribute}: ClassVar[{class_name}]  # native value: {member.value}"
                    )
                    resolved_constant_count += 1
            pyi_lines.append("")
            class_manifest[class_name] = {
                "methods": {},
                "attributes": class_attributes,
            }
            if enum is not None:
                class_manifest[class_name]["enum"] = self._enum_document(enum)
        if class_manifest:
            module_manifest["classes"] = class_manifest
        alias_manifest: dict[str, Any] = {}
        for name, (owner, member_name) in sorted(aliases.items()):
            if not self._valid_member(name) or not self._valid_member(owner):
                continue
            enum = self._enum_for_class(
                owner,
                enum_members.get(owner, frozenset()),
                enums.get(owner, ()),
            )
            member = (
                next((item for item in enum.members if item.name == member_name), None)
                if enum is not None
                else None
            )
            if enum is None or member is None:
                continue
            pyi_lines.append(f"{name}: Final[{owner}] = {owner}.{member_name}")
            alias_manifest[name] = {
                "owner": owner,
                "member": member_name,
                "value": member.value,
                "registration_rva": f"0x{member.registration_rva:x}",
                "confidence": enum.confidence,
            }
        if aliases:
            pyi_lines.append("")
        if alias_manifest:
            module_manifest["aliases"] = alias_manifest
        return (
            "\n".join(pyi_lines).rstrip() + "\n",
            module_manifest,
            typed,
            resolved_constant_count,
            unknown,
        )

    @staticmethod
    def _unambiguous_constant(
        values: Sequence[ConstantEvidence],
    ) -> ConstantEvidence | None:
        distinct = {(type(value.value), value.value) for value in values}
        if len(distinct) != 1:
            return None
        return min(values, key=lambda value: (value.binary, value.registration_rva))

    @staticmethod
    def _scalar_annotation(value: ScalarValue) -> str:
        if value is None:
            return "None"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        return "str"

    @staticmethod
    def _constant_document(value: ConstantEvidence) -> dict[str, Any]:
        return {
            "binary": value.binary,
            "confidence": value.confidence,
            "registration_rva": f"0x{value.registration_rva:x}",
            "value": value.value,
        }

    @staticmethod
    def _enum_for_class(
        name: str,
        expected_members: frozenset[str],
        values: Sequence[EnumEvidence],
    ) -> EnumEvidence | None:
        candidates = tuple(value for value in values if value.name == name)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda value: (
                len(expected_members.intersection(member.name for member in value.members)),
                len(value.members),
            ),
        )

    @staticmethod
    def _enum_document(value: EnumEvidence) -> dict[str, Any]:
        return {
            "binary": value.binary,
            "confidence": value.confidence,
            "module": value.module,
            "registration_rva": f"0x{value.registration_rva:x}",
            "members": {
                member.name: {
                    "value": member.value,
                    "registration_rva": f"0x{member.registration_rva:x}",
                }
                for member in value.members
            },
        }

    @staticmethod
    def _module_uses_math(
        functions: set[str],
        bindings: Mapping[str, Sequence[BindingEvidence]],
    ) -> bool:
        return any(
            annotation.startswith("Math.")
            for name in functions
            for evidence in bindings.get(name, ())
            for annotation in (
                *evidence.signature.parameter_types,
                evidence.signature.return_type,
            )
        )

    @staticmethod
    def _deduplicate_evidence(
        values: Sequence[BindingEvidence],
    ) -> tuple[BindingEvidence, ...]:
        unique: dict[tuple[tuple[str, ...], str, tuple[str, ...]], BindingEvidence] = {}
        for value in values:
            key = (
                value.signature.parameter_types,
                value.signature.return_type,
                value.parameter_names,
            )
            unique.setdefault(key, value)
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    len(item.signature.parameter_types),
                    item.signature.parameter_types,
                    item.signature.return_type,
                ),
            )
        )

    def _evidence_for_module(
        self,
        module: str,
        values: Sequence[BindingEvidence],
        calls: Sequence[CallShape],
    ) -> tuple[BindingEvidence, ...]:
        scoped = tuple(value for value in values if not value.scopes or module in value.scopes)
        exact_scope = tuple(value for value in scoped if module in value.scopes)
        candidates = exact_scope or scoped
        candidates = self._deduplicate_evidence(candidates)
        if exact_scope or not calls or len(candidates) <= 1:
            return self._with_callsite_arity(candidates, calls)
        informative_calls = tuple(
            call for call in calls if any(value != "Any" for value in call.argument_types)
        )
        calls = informative_calls or calls
        selected: dict[tuple[tuple[str, ...], str, tuple[str, ...]], BindingEvidence] = {}
        for call in calls:
            scored = [
                (self._compatibility_score(item.signature, call), item)
                for item in candidates
                if len(item.signature.parameter_types) >= len(call.argument_types)
            ]
            if not scored:
                continue
            best_score = max(score for score, _item in scored)
            for score, item in scored:
                if score != best_score:
                    continue
                key = (
                    item.signature.parameter_types,
                    item.signature.return_type,
                    item.parameter_names,
                )
                selected[key] = item
        selected_values = tuple(selected.values())
        for item in candidates:
            if any(
                _same_signature_family(item.signature, chosen.signature)
                for chosen in selected_values
            ):
                key = (
                    item.signature.parameter_types,
                    item.signature.return_type,
                    item.parameter_names,
                )
                selected[key] = item
        resolved = self._deduplicate_evidence(tuple(selected.values())) if selected else candidates
        return self._with_callsite_arity(resolved, calls)

    @staticmethod
    def _with_callsite_arity(
        evidence: Sequence[BindingEvidence],
        calls: Sequence[CallShape],
    ) -> tuple[BindingEvidence, ...]:
        if not calls:
            return tuple(evidence)
        minimum = min(len(call.argument_types) for call in calls)
        return tuple(
            replace(item, required_parameters=minimum)
            if minimum < len(item.signature.parameter_types)
            else item
            for item in evidence
        )

    @staticmethod
    def _compatibility_score(signature: NativeSignature, call: CallShape) -> int:
        score = 10 if len(signature.parameter_types) == len(call.argument_types) else 0
        score -= len(signature.parameter_types) - len(call.argument_types)
        for expected, observed in zip(signature.parameter_types, call.argument_types, strict=False):
            if observed == "Any":
                if expected != "Any":
                    score -= 1
                continue
            if expected == "Any":
                continue
            if expected == observed or (observed == "int" and expected == "float"):
                score += 3
            else:
                score -= 4
        return score

    @staticmethod
    def _safe_names(evidence: BindingEvidence, *, method: bool) -> tuple[str, ...]:
        names: list[str] = []
        occupied: set[str] = {"self"} if method else set()
        for index, raw in enumerate(evidence.parameter_names):
            candidate = raw if raw.isidentifier() and not keyword.iskeyword(raw) else f"arg{index}"
            if candidate in occupied:
                candidate = f"arg{index}"
            while candidate in occupied:
                candidate += "_"
            occupied.add(candidate)
            names.append(candidate)
        return tuple(names)

    def _render_typing_function(
        self,
        name: str,
        evidence: Sequence[BindingEvidence],
        *,
        indentation: str,
        method: bool = False,
    ) -> list[str]:
        if not evidence:
            prefix = "self, " if method else ""
            return [
                f"{indentation}def {name}({prefix}*args: Any, **kwargs: Any) -> Any: ...",
                "",
            ]
        lines: list[str] = []
        overloaded = len(evidence) > 1
        for item in evidence:
            if overloaded:
                lines.append(f"{indentation}@overload")
            names = self._safe_names(item, method=method)
            parameters = [
                (
                    f"{parameter_name}: {annotation} = ..."
                    if item.required_parameters is not None and index >= item.required_parameters
                    else f"{parameter_name}: {annotation}"
                )
                for index, (parameter_name, annotation) in enumerate(
                    zip(names, item.signature.parameter_types, strict=True)
                )
            ]
            if method:
                parameters.insert(0, "self")
            lines.append(
                f"{indentation}def {name}({', '.join(parameters)}) "
                f"-> {item.signature.return_type}: ..."
            )
        lines.append("")
        return lines

    @staticmethod
    def _evidence_document(item: BindingEvidence) -> dict[str, Any]:
        return {
            "binary": item.binary,
            "confidence": item.confidence,
            "descriptor": item.signature.descriptor,
            "parameter_names": list(item.parameter_names),
            "parameter_types": list(item.signature.parameter_types),
            "registration_rva": f"0x{item.registration_rva:x}",
            "required_parameters": item.required_parameters,
            "return_type": item.signature.return_type,
            "scopes": list(item.scopes),
        }


def analyze_engine_stubs(
    binaries: Sequence[Path],
    source_roots: Sequence[Path],
    *,
    max_workers: int = 1,
    progress: Callable[[str], None] | None = None,
) -> tuple[
    SourceUsageIndex,
    dict[str, tuple[BindingEvidence, ...]],
    dict[str, tuple[EnumEvidence, ...]],
    dict[str, tuple[ConstantEvidence, ...]],
]:
    if max_workers < 1:
        raise ValueError("engine stub workers must be positive")
    report_progress = progress or (lambda _message: None)
    started = time.monotonic()
    usage = SourceUsageIndex.scan(source_roots, max_workers=max_workers)
    report_progress(
        f"indexed Python source usage for {len(usage.source_modules)} modules "
        f"in {time.monotonic() - started:.1f}s"
    )
    module_names = set(usage.modules)
    constant_candidate_sets: dict[str, set[ScalarValue]] = defaultdict(set)
    constant_owners: dict[str, set[str]] = defaultdict(set)
    for module, module_usage in usage.modules.items():
        for name in module_usage.attributes - module_usage.called:
            constant_candidate_sets[name]
            constant_owners[name].add(module)
    constant_candidates = {
        name: tuple(sorted(values, key=repr))
        for name, values in sorted(constant_candidate_sets.items())
    }
    collected: dict[str, list[BindingEvidence]] = defaultdict(list)
    collected_enums: dict[str, list[EnumEvidence]] = defaultdict(list)
    collected_constants: dict[str, list[ConstantEvidence]] = defaultdict(list)
    started = time.monotonic()
    analyses: tuple[
        tuple[
            tuple[BindingEvidence, ...],
            tuple[EnumEvidence, ...],
            tuple[ConstantEvidence, ...],
        ],
        ...,
    ]
    if len(binaries) == 1:
        analyses = (
            NativeBindingAnalyzer().analyze_with_enums(
                binaries[0],
                usage,
                constant_candidates,
                max_workers=max_workers,
                progress=lambda message: report_progress(f"{binaries[0].name}: {message}"),
            ),
        )
    elif len(binaries) > 1 and max_workers > 1:
        with ProcessPoolExecutor(max_workers=min(max_workers, len(binaries))) as executor:
            analyses = tuple(
                executor.map(
                    _analyze_native_binary,
                    ((binary, usage, constant_candidates, 1) for binary in binaries),
                )
            )
    else:
        analyses = tuple(
            _analyze_native_binary((binary, usage, constant_candidates, 1)) for binary in binaries
        )
    report_progress(
        f"analyzed {len(binaries)} native binaries in {time.monotonic() - started:.1f}s"
    )
    for bindings, enums, constants in analyses:
        for item in bindings:
            collected[item.name].append(item)
        for enum_item in enums:
            collected_enums[enum_item.name].append(enum_item)
        for constant in constants:
            collected_constants[constant.name].append(constant)
    collected_constants = defaultdict(
        list,
        {
            name: values
            for name, values in collected_constants.items()
            if len(constant_owners[name]) == 1
        },
    )
    visible_modules = {
        name
        for name in module_names
        if name not in usage.source_modules
        and any(
            attribute in collected or attribute in collected_constants
            for attribute in usage.modules.get(name, ModuleUsage()).attributes
        )
    }
    filtered_usage = SourceUsageIndex(
        modules={
            name: usage.modules[name] for name in sorted(visible_modules) if name in usage.modules
        },
        source_modules=usage.source_modules,
    )
    return (
        filtered_usage,
        {name: tuple(values) for name, values in sorted(collected.items())},
        {name: tuple(values) for name, values in sorted(collected_enums.items())},
        {name: tuple(values) for name, values in sorted(collected_constants.items())},
    )


def _analyze_native_binary(
    arguments: tuple[
        Path,
        SourceUsageIndex,
        Mapping[str, tuple[ScalarValue, ...]],
        int,
    ],
) -> tuple[
    tuple[BindingEvidence, ...],
    tuple[EnumEvidence, ...],
    tuple[ConstantEvidence, ...],
]:
    binary, usage, constant_candidates, max_workers = arguments
    return NativeBindingAnalyzer().analyze_with_enums(
        binary,
        usage,
        constant_candidates,
        max_workers=max_workers,
    )


def find_main_binaries(client_root: Path) -> tuple[Path, ...]:
    preferred = (
        client_root / "win64" / "WorldOfTanks.exe",
        client_root / "win64" / "Tanki.exe",
    )
    found = tuple(path for path in preferred if path.is_file())
    if found:
        python_runtimes = tuple(
            sorted(path for path in (client_root / "win64").glob("python*.dll") if path.is_file())
        )
        return (*found, *python_runtimes)
    candidates = tuple(
        sorted(
            (
                path
                for path in client_root.rglob("*")
                if path.is_file() and path.suffix.casefold() in _NATIVE_SUFFIXES
            ),
            key=lambda path: (-path.stat().st_size, path.as_posix()),
        )
    )
    if not candidates:
        raise EngineStubError(f"no native binaries found under {client_root}")
    return candidates[:1]


__all__ = [
    "BindingEvidence",
    "ConstantEvidence",
    "EngineStubError",
    "EngineStubGenerator",
    "EnumEvidence",
    "EnumMemberEvidence",
    "ModuleUsage",
    "NativeBindingAnalyzer",
    "NativeSignature",
    "PeImage",
    "SourceUsageIndex",
    "StubGenerationReport",
    "analyze_engine_stubs",
    "find_main_binaries",
    "parse_native_signature",
]

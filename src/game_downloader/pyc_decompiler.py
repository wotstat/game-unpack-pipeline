"""Pinned Python 2.7 decompiler adapter used by the readable stage.

This module is intentionally a subprocess boundary.  It parses bytecode as data and
never imports or executes the input module.  The small grammar additions cover control
flow emitted by the current WOT/MT Python 2.7 compiler that uncompyle6 3.9.3 does not
recognize on its own.
"""

# Grammar productions must remain one physical line for spark_parser.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import io
import json
import signal
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final, TextIO

from uncompyle6.main import decompile_file  # type: ignore[import-untyped]
from uncompyle6.parser import nop_func  # type: ignore[import-untyped]
from uncompyle6.parsers.parse27 import Python27Parser  # type: ignore[import-untyped]
from uncompyle6.semantics.consts import TABLE_DIRECT  # type: ignore[import-untyped]
from uncompyle6.semantics.pysource import SourceWalker  # type: ignore[import-untyped]
from uncompyle6.util import better_repr  # type: ignore[import-untyped]
from xdis.cross_types import UnicodeForPython3  # type: ignore[import-untyped]

ADAPTER_NAME: Final = "game-downloader-pyc"
ADAPTER_VERSION: Final = "4"
BACKEND_NAME: Final = "uncompyle6"
BACKEND_VERSION: Final = "3.9.3"
TOOL_VERSION: Final = f"{ADAPTER_VERSION}+{BACKEND_NAME}-{BACKEND_VERSION}"

_PATCHED = False

_GRAMMAR_RULES = """
    game_downloader_and ::= expr JUMP_IF_FALSE_OR_POP expr
    game_downloader_or ::= expr JUMP_IF_TRUE_OR_POP expr
    expr ::= game_downloader_and
    expr ::= game_downloader_or

    game_downloader_false_expr ::= expr POP_JUMP_IF_FALSE game_downloader_and RETURN_END_IF come_froms expr RETURN_VALUE
    game_downloader_conditional_expr ::= expr POP_JUMP_IF_FALSE game_downloader_or RETURN_END_IF come_froms expr RETURN_VALUE
    game_downloader_negated_false_expr ::= expr POP_JUMP_IF_TRUE game_downloader_and RETURN_END_IF come_froms expr RETURN_VALUE
    return ::= game_downloader_false_expr
    return ::= game_downloader_conditional_expr
    return ::= game_downloader_negated_false_expr
    return_if_stmt ::= game_downloader_false_expr
    return_if_stmt ::= game_downloader_conditional_expr
    return_if_stmt ::= game_downloader_negated_false_expr

    game_downloader_nested_lambda ::= expr jmp_false expr return_if_lambda return_stmt_lambda
    if_exp_lambda ::= expr jmp_false expr return_if_lambda game_downloader_nested_lambda LAMBDA_MARKER
    game_downloader_if_return_lambda ::= expr jmp_false expr RETURN_VALUE_LAMBDA COME_FROM expr RETURN_VALUE_LAMBDA LAMBDA_MARKER
    stmt ::= game_downloader_if_return_lambda
    game_downloader_conditional_and_lambda ::= expr POP_JUMP_IF_FALSE expr JUMP_IF_FALSE_OR_POP unary_not RETURN_END_IF_LAMBDA come_froms return_stmt_lambda LAMBDA_MARKER
    stmt ::= game_downloader_conditional_and_lambda

    game_downloader_chained_compare ::= expr expr DUP_TOP ROT_THREE COMPARE_OP JUMP_IF_FALSE_OR_POP expr COMPARE_OP RETURN_VALUE COME_FROM ROT_TWO POP_TOP
    return_if_stmt ::= game_downloader_chained_compare RETURN_END_IF
    game_downloader_optional_chained_expr ::= expr POP_JUMP_IF_FALSE expr expr DUP_TOP ROT_THREE COMPARE_OP JUMP_IF_FALSE_OR_POP expr COMPARE_OP JUMP_ABSOLUTE COME_FROM ROT_TWO POP_TOP RETURN_END_IF COME_FROM expr RETURN_VALUE
    return ::= game_downloader_optional_chained_expr

    for_block ::= l_stmts_opt JUMP_BACK JUMP_BACK
    for_block ::= l_stmts_opt lastl_stmt JUMP_BACK JUMP_BACK

    game_downloader_ifelse_continue ::= testexpr c_stmts_opt JUMP_FORWARD CONTINUE come_froms
    stmt ::= game_downloader_ifelse_continue

    game_downloader_editor_fallback ::= expr POP_JUMP_IF_TRUE expr POP_JUMP_IF_FALSE expr COME_FROM POP_JUMP_IF_FALSE assign expr POP_JUMP_IF_FALSE expr COME_FROM POP_JUMP_IF_FALSE assign JUMP_ABSOLUTE CONTINUE JUMP_ABSOLUTE CONTINUE JUMP_ABSOLUTE CONTINUE JUMP_FORWARD come_froms
    stmt ::= game_downloader_editor_fallback
"""


def _python27_unicode_literal(value: UnicodeForPython3) -> str:
    raw = value.value
    text = raw.decode("utf-8", "surrogatepass") if isinstance(raw, bytes) else str(raw)
    return "u" + ascii(text)


def _python27_literal(value: Any) -> str:
    """Render a constant as deterministic, ASCII-only Python 2.7 source."""
    if isinstance(value, UnicodeForPython3):
        return _python27_unicode_literal(value)
    if isinstance(value, bytes):
        return repr(value)
    if isinstance(value, str):
        # xdis decodes marshal TYPE_STRING values that contain valid UTF-8. Re-encoding
        # recovers the original Python 2 byte string; the explicit b prefix also keeps
        # its type under ``from __future__ import unicode_literals``.
        return repr(value.encode("utf-8"))
    if isinstance(value, tuple):
        rendered = ", ".join(_python27_literal(item) for item in value)
        if len(value) == 1:
            rendered += ","
        return f"({rendered})"
    if isinstance(value, list):
        return "[" + ", ".join(_python27_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        dictionary_items = sorted(
            (_python27_literal(key), _python27_literal(item)) for key, item in value.items()
        )
        return "{" + ", ".join(f"{key}: {item}" for key, item in dictionary_items) + "}"
    if isinstance(value, (set, frozenset)):
        set_items = sorted(_python27_literal(item) for item in value)
        constructor = "frozenset" if isinstance(value, frozenset) else "set"
        return f"{constructor}([" + ", ".join(set_items) + "])"
    return str(better_repr(value, (2, 7)))


def _install_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    observed = version(BACKEND_NAME)
    if observed != BACKEND_VERSION:
        raise RuntimeError(
            f"{ADAPTER_NAME} requires {BACKEND_NAME} {BACKEND_VERSION}, found {observed}"
        )

    original_grammar = Python27Parser.customize_grammar_rules

    def patched_grammar(self: Any, tokens: Any, customize: Any) -> None:
        original_grammar(self, tokens, customize)
        self.addRule(_GRAMMAR_RULES, nop_func)

    Python27Parser.customize_grammar_rules = patched_grammar

    # xdis 6.1.8's compatibility wrapper emits malformed Python for quotes,
    # backslashes, control characters and non-ASCII code points.  Keep the
    # wrapper (it preserves the Python 2 unicode-vs-str distinction) but give
    # it a round-trippable representation.
    UnicodeForPython3.__repr__ = _python27_unicode_literal

    original_load_const = SourceWalker.n_LOAD_CONST

    def patched_load_const(self: Any, node: Any) -> Any:
        data = node.pattr
        if isinstance(
            data,
            (UnicodeForPython3, bytes, str, tuple, list, dict, set, frozenset),
        ):
            self.write(_python27_literal(data))
            self.prune()
            return None
        return original_load_const(self, node)

    SourceWalker.n_LOAD_CONST = patched_load_const

    original_subscript = SourceWalker.n_subscript

    def patched_subscript(self: Any, node: Any) -> Any:
        # The Python 2.7 parser represents ``value[a, b]`` as a tuple under
        # the subscript expression.  Upstream only removes tuple parentheses
        # for its older ``build_list`` AST shape.  The current ``tuple`` shape
        # otherwise renders Ellipsis as invalid ``value[(a, ...)]`` source.
        if len(node) >= 2 and len(node[-2]):
            candidate = node[-2][0]
            if (
                getattr(candidate, "kind", None) == "tuple"
                and len(candidate)
                and getattr(candidate[-1], "kind", "").startswith("BUILD_TUPLE")
            ):
                candidate.kind = "build_tuple2"
        return original_subscript(self, node)

    SourceWalker.n_subscript = patched_subscript

    TABLE_DIRECT.update(
        {
            "game_downloader_and": ("%c and %c", 0, 2),
            "game_downloader_or": ("%c or %c", 0, 2),
            "game_downloader_false_expr": ("(%c and %c) or %c", 0, 2, 5),
            "game_downloader_conditional_expr": ("%c if %c else %c", 2, 0, 5),
            "game_downloader_negated_false_expr": (
                "((not %c) and %c) or %c",
                0,
                2,
                5,
            ),
            "game_downloader_nested_lambda": (
                "%p if %c else %c",
                (2, "expr", 27),
                (0, "expr"),
                4,
            ),
            "game_downloader_if_return_lambda": (
                "%p if %c else %c",
                (2, "expr", 27),
                (0, "expr"),
                5,
            ),
            "game_downloader_conditional_and_lambda": (
                "%c and %c if %c else %c",
                2,
                4,
                0,
                7,
            ),
            "game_downloader_chained_compare": (
                '%p %[4]{pattr.replace("-", " ")} %p %[7]{pattr.replace("-", " ")} %p',
                (0, 19),
                (1, 19),
                (6, 19),
            ),
            "game_downloader_optional_chained_expr": (
                '(%p %[6]{pattr.replace("-", " ")} %p '
                '%[9]{pattr.replace("-", " ")} %p) if %c else %c',
                (2, 19),
                (3, 19),
                (8, 19),
                0,
                16,
            ),
            "game_downloader_ifelse_continue": (
                "%|if %c:\n%+%c%-%|else:\n%+%|continue\n%-",
                0,
                1,
            ),
            "game_downloader_editor_fallback": (
                "%|if not %c:\n%+"
                "%|if not (%c and %c):\n%+%|continue\n%-"
                "%c"
                "%|if not (%c and %c):\n%+%|continue\n%-"
                "%c%-",
                0,
                2,
                4,
                7,
                8,
                10,
                13,
            ),
        }
    )

    original_preorder = SourceWalker.preorder

    def patched_preorder(self: Any, node: Any = None) -> Any:
        # uncompyle6 3.9.3 looks for the class name inside a nested closure tree.
        # Python 2.7 stores the authoritative name in classdefdeco2[0].  Supplying
        # that value to the location expected by the upstream action avoids an
        # AttributeError without changing the parsed class body.
        if getattr(node, "kind", None) == "classdefdeco2" and len(node) >= 3:
            mkfunc = node[-3]
            if getattr(mkfunc, "kind", None) == "mkfunc" and len(mkfunc):
                closure = mkfunc[0]
                if getattr(closure, "kind", None) == "load_closure" and len(closure):
                    target = closure[0]
                    if not hasattr(target, "pattr"):
                        target.pattr = node[0].pattr
        return original_preorder(self, node)

    SourceWalker.preorder = patched_preorder
    _PATCHED = True


class _ItemTimeoutError(TimeoutError):
    pass


def _raise_item_timeout(_signum: int, _frame: object) -> None:
    raise _ItemTimeoutError("PYC decompilation timed out")


def decompile(source: Path, *, outstream: TextIO = sys.stdout) -> None:
    _install_patches()
    decompile_file(str(source), outstream=outstream)


def _batch_decompile(manifest_path: Path, output_root: Path) -> dict[str, object]:
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw_manifest, dict):
        raise ValueError("batch manifest must be an object")
    raw_items = raw_manifest.get("items")
    timeout_seconds = raw_manifest.get("timeout_seconds")
    max_output_bytes = raw_manifest.get("max_output_bytes")
    if (
        not isinstance(raw_items, list)
        or not 1 <= len(raw_items) <= 256
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 3600
        or not isinstance(max_output_bytes, int)
        or not 1024 <= max_output_bytes <= 1024 * 1024 * 1024
    ):
        raise ValueError("batch manifest limits are invalid")
    if not output_root.is_dir() or any(output_root.iterdir()):
        raise ValueError("batch output directory must exist and be empty")

    use_alarm = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
    previous_handler: Any = None
    if use_alarm:
        previous_handler = signal.signal(signal.SIGALRM, _raise_item_timeout)
    outputs: list[dict[str, object]] = []
    seen: set[str] = set()
    try:
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("batch item must be an object")
            item_id = raw_item.get("id")
            source_value = raw_item.get("source")
            if (
                not isinstance(item_id, str)
                or len(item_id) != 8
                or not item_id.isascii()
                or not item_id.isdigit()
                or item_id in seen
                or not isinstance(source_value, str)
            ):
                raise ValueError("batch item identity is invalid")
            seen.add(item_id)
            source = Path(source_value)
            if not source.is_absolute() or not source.is_file() or source.is_symlink():
                raise ValueError(f"batch input is not a regular absolute file: {source}")
            destination = output_root / f"{item_id}.py"
            rendered = io.StringIO()
            try:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
                decompile(source, outstream=rendered)
            except _ItemTimeoutError as exc:
                raise RuntimeError(f"PYC decompilation timed out for {source}") from exc
            except Exception as exc:
                raise RuntimeError(
                    f"PYC decompilation failed for {source}: {type(exc).__name__}: {exc}"
                ) from exc
            finally:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, 0)
            encoded = rendered.getvalue().encode("utf-8")
            if len(encoded) > max_output_bytes:
                raise RuntimeError(f"PYC output exceeds policy for {source}")
            with destination.open("xb") as stream:
                stream.write(encoded)
            outputs.append(
                {
                    "id": item_id,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                }
            )
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    return {"outputs": outputs}


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        try:
            _install_patches()
        except Exception as exc:
            print(f"{ADAPTER_NAME}: {exc}", file=sys.stderr)
            return 1
        print(f"{ADAPTER_NAME} {TOOL_VERSION}")
        return 0
    if len(arguments) == 3 and arguments[0] == "--batch":
        try:
            report = _batch_decompile(Path(arguments[1]), Path(arguments[2]))
        except Exception as exc:
            print(f"{ADAPTER_NAME}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
        return 0
    if len(arguments) != 1:
        print(
            f"usage: {ADAPTER_NAME} PYC_FILE | --batch MANIFEST OUTPUT_DIR",
            file=sys.stderr,
        )
        return 2

    source = Path(arguments[0])
    if not source.is_file():
        print(f"{ADAPTER_NAME}: input is not a file: {source}", file=sys.stderr)
        return 2
    try:
        decompile(source)
    except Exception as exc:
        print(f"{ADAPTER_NAME}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

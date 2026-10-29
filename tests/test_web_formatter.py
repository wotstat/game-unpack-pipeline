from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from game_downloader.readable import PrettierFormatter, ReadablePolicy, TransformFailedError


def _formatter(policy: ReadablePolicy | None = None) -> PrettierFormatter:
    executable = Path(__file__).parents[1] / "node_modules/.bin/prettier"
    assert executable.is_file(), "Install pinned web tools with npm ci or scripts/check.sh"
    return PrettierFormatter(policy or ReadablePolicy(), executable)


def _fake_formatter(tmp_path: Path, module: str) -> PrettierFormatter:
    package = tmp_path / "prettier"
    executable = package / "bin/prettier"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho 3.9.6\n")
    executable.chmod(0o755)
    (package / "index.mjs").write_text(module)
    return PrettierFormatter(ReadablePolicy(subprocess_timeout_seconds=1), executable)


def test_batch_formatter_matches_cli_bytes_and_ignores_source_configuration(tmp_path: Path) -> None:
    formatter = _formatter(ReadablePolicy(web_formatter_tab_width=4))
    (tmp_path / ".prettierrc").write_text('{"semi":false,"singleQuote":true}')
    fixtures = (
        ("a.js", "babel", b'const value={name:"\xd0\xbc\xd0\xb8\xd1\x80"}\r\n'),
        ("bom.js", "babel", b"\xef\xbb\xbfconst value=1\r\n"),
        ("a.css", "css", b".test{color:red;margin:0  1px}"),
        ("a.html", "html", b"<div><p>Hello</p><p>World</p></div>"),
    )
    sources = []
    for name, parser, content in fixtures:
        source = tmp_path / name
        source.write_bytes(content)
        sources.append((source, parser))
    expected = [formatter.format(source, parser)[0] for source, parser in sources]

    batch = formatter.stage_many(sources, tmp_path)
    try:
        assert [formatter.read_staged(batch, output) for output in batch.outputs] == expected
    finally:
        shutil.rmtree(batch.root)


@pytest.mark.parametrize("content", [b"const value=", b"const value='\xff'"])
def test_batch_formatter_failure_removes_all_staged_outputs(tmp_path: Path, content: bytes) -> None:
    valid = tmp_path / "valid.js"
    valid.write_text("const value=1")
    invalid = tmp_path / "invalid.js"
    invalid.write_bytes(content)

    with pytest.raises(TransformFailedError, match="web formatter batch failed"):
        _formatter().stage_many(((valid, "babel"), (invalid, "babel")), tmp_path)

    assert not list(tmp_path.glob("web-batch-*"))


def test_batch_formatter_rejects_oversized_output(tmp_path: Path) -> None:
    source = tmp_path / "large.js"
    source.write_text(f"const value = {'a' * 2000!r}")
    formatter = _formatter(ReadablePolicy(max_transform_output_bytes=1024))

    with pytest.raises(TransformFailedError, match="output exceeds policy"):
        formatter.stage_many(((source, "babel"),), tmp_path)

    assert not list(tmp_path.glob("web-batch-*"))


def test_batch_formatter_terminates_a_synchronous_parser_at_per_file_deadline(
    tmp_path: Path,
) -> None:
    formatter = _fake_formatter(
        tmp_path,
        "export const version='3.9.6'; export function format() { while (true) {} }",
    )
    source = tmp_path / "input.js"
    source.write_text("fixture")
    started = time.monotonic()

    with pytest.raises(TransformFailedError, match="timed out"):
        formatter.stage_many(((source, "babel"),), tmp_path)

    assert time.monotonic() - started < 10
    assert not list(tmp_path.glob("web-batch-*"))


def test_batch_formatter_checks_imported_module_version(tmp_path: Path) -> None:
    formatter = _fake_formatter(tmp_path, "export const version='unexpected';")
    source = tmp_path / "input.js"
    source.write_text("fixture")

    with pytest.raises(TransformFailedError, match="pinned Prettier version"):
        formatter.stage_many(((source, "babel"),), tmp_path)


def test_batch_formatter_rechecks_digest_before_consuming_output(tmp_path: Path) -> None:
    source = tmp_path / "input.js"
    source.write_text("const value=1")
    formatter = _formatter()
    batch = formatter.stage_many(((source, "babel"),), tmp_path)
    try:
        output = batch.outputs[0]
        (batch.root / "outputs" / output.id).write_bytes(b" " * output.size)

        with pytest.raises(TransformFailedError, match="differs from its report"):
            formatter.read_staged(batch, output)
    finally:
        shutil.rmtree(batch.root)

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
COLLECT_RESULT_SCRIPT = REPOSITORY_ROOT / ".github/scripts/collect-result.py"


def _readable_version() -> Callable[[Path], str]:
    namespace = runpy.run_path(COLLECT_RESULT_SCRIPT.as_posix())
    return cast(Callable[[Path], str], namespace["_readable_version"])


def _parallel_range_fallback_report() -> Callable[[dict[str, object] | None], dict[str, object]]:
    namespace = runpy.run_path(COLLECT_RESULT_SCRIPT.as_posix())
    return cast(
        Callable[[dict[str, object] | None], dict[str, object]],
        namespace["_parallel_range_fallback_report"],
    )


def test_readable_version_matches_wot_src_commit_subject_format(tmp_path: Path) -> None:
    version_path = tmp_path / "sources/base/version.xml"
    version_path.parent.mkdir(parents=True)
    version_path.write_text(
        "<root><version>\n  v.2.3.1.5400   #1827\n</version></root>",
        encoding="utf-8",
    )

    assert _readable_version()(tmp_path) == "2.3.1.5400 #1827"


@pytest.mark.parametrize(
    "document",
    (
        "<root><version>2.3.1.5400 #1827</version></root>",
        "<root><version>v.2.3.1 #1827</version></root>",
        "<root><version>v.2.3.1.5400</version></root>",
        "<root />",
    ),
)
def test_readable_version_rejects_values_wot_src_cannot_use_as_commit_subject(
    tmp_path: Path,
    document: str,
) -> None:
    version_path = tmp_path / "sources/base/version.xml"
    version_path.parent.mkdir(parents=True)
    version_path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid readable version"):
        _readable_version()(tmp_path)


def test_parallel_range_fallback_report_identifies_artifact_host_and_reason() -> None:
    report = _parallel_range_fallback_report()(
        {
            "artifacts": [
                {
                    "artifact": {
                        "artifact_id": "sha256:" + "1" * 64,
                        "path": {
                            "components_base64": ["Y2xpZW50LmJpbg=="],
                            "utf8": "client.bin",
                        },
                    },
                    "reused": False,
                    "transport": {
                        "parallel_range_fallbacks": [
                            {
                                "reason": "validator-changed",
                                "source_host": "cdn.example",
                                "response_status": 206,
                                "range_index": 3,
                                "attempts": 1,
                                "discarded_bytes": 131072,
                            }
                        ]
                    },
                },
                {
                    "artifact": {
                        "artifact_id": "sha256:" + "2" * 64,
                        "path": {
                            "components_base64": ["Y2FjaGVkLmJpbg=="],
                            "utf8": "cached.bin",
                        },
                    },
                    "reused": True,
                    "transport": {
                        "parallel_range_fallbacks": [
                            {
                                "reason": "probe-failed",
                                "source_host": "old.invalid",
                                "response_status": None,
                                "range_index": None,
                                "attempts": 1,
                                "discarded_bytes": 0,
                            }
                        ]
                    },
                },
            ]
        }
    )

    assert report == {
        "schema_version": 1,
        "download_result_available": True,
        "fallback_artifacts": 1,
        "fallback_count": 1,
        "discarded_bytes": 131072,
        "fallbacks": [
            {
                "artifact_id": "sha256:" + "1" * 64,
                "artifact_path": {
                    "components_base64": ["Y2xpZW50LmJpbg=="],
                    "utf8": "client.bin",
                },
                "reason": "validator-changed",
                "source_host": "cdn.example",
                "response_status": 206,
                "range_index": 3,
                "attempts": 1,
                "discarded_bytes": 131072,
            }
        ],
    }

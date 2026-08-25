from __future__ import annotations

import json
from pathlib import Path

from scripts.render_release_check_report import render_report


def _write_result(
    results_dir: Path,
    *,
    target: str,
    stored_release_name: str | None,
    current_release_name: str | None,
    state: str,
    action: str,
) -> None:
    (results_dir / f"{target}.json").write_text(
        json.dumps(
            {
                "target": target,
                "stored_release_name": stored_release_name,
                "current_release_name": current_release_name,
                "state": state,
                "action": action,
            }
        ),
        encoding="utf-8",
    )


def test_report_renders_all_release_check_states(tmp_path: Path) -> None:
    _write_result(
        tmp_path,
        target="wot-eu",
        stored_release_name="2.0.0",
        current_release_name="2.0.0",
        state="up_to_date",
        action="none",
    )
    _write_result(
        tmp_path,
        target="mt-ru",
        stored_release_name="1.9.0",
        current_release_name="2.0.0",
        state="update_available",
        action="dispatched",
    )

    report = render_report(
        results_dir=tmp_path,
        targets=["wot-eu", "mt-ru"],
        dispatch_pipelines=True,
    )

    assert "**🚀 Dispatch enabled** · ✅ 1 current · 🆕 1 update · ❌ 0 errors" in report
    assert "| WoT EU | <code>2.0.0</code> | <code>2.0.0</code> | ✅ Up to date | — |" in report
    assert (
        "| Мир танков RU | <code>1.9.0</code> | <code>2.0.0</code> | "
        "🆕 Update available | 🚀 Dispatched |"
    ) in report


def test_report_marks_missing_or_invalid_results_as_errors(tmp_path: Path) -> None:
    (tmp_path / "wot-na.json").write_text("not json", encoding="utf-8")

    report = render_report(
        results_dir=tmp_path,
        targets=["wot-na", "wot-asia"],
        dispatch_pipelines=False,
    )

    assert "**🧪 Dry run** · ✅ 0 current · 🆕 0 updates · ❌ 2 errors" in report
    assert report.count("❌ Result unavailable") == 2


def test_report_escapes_release_names_for_job_summary(tmp_path: Path) -> None:
    _write_result(
        tmp_path,
        target="wot-cn",
        stored_release_name=None,
        current_release_name="<release>|next\nline",
        state="update_available",
        action="dry_run",
    )

    report = render_report(
        results_dir=tmp_path,
        targets=["wot-cn"],
        dispatch_pipelines=False,
    )

    assert "| WoT CN | — | <code>&lt;release&gt;&#124;next&#10;line</code> |" in report
    assert "🧪 Dry run" in report


def test_report_handles_failed_plan_and_empty_selection(tmp_path: Path) -> None:
    assert "Target selection failed" in render_report(
        results_dir=tmp_path,
        targets=[],
        dispatch_pipelines=False,
        plan_result="failure",
    )
    assert "No targets selected" in render_report(
        results_dir=tmp_path,
        targets=[],
        dispatch_pipelines=False,
    )

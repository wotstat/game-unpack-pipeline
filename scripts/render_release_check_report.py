from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from pathlib import Path

TARGET_LABELS = {
    "wot-eu": "WoT EU",
    "wot-na": "WoT NA",
    "wot-asia": "WoT Asia",
    "wot-common-test": "WoT Common Test",
    "wot-cn": "WoT CN",
    "mt-ru": "Мир танков RU",
    "mt-public-test": "Мир танков Public Test",
}
STATE_LABELS = {
    "up_to_date": "✅ Up to date",
    "update_available": "🆕 Update available",
    "error": "❌ Check failed",
}
ACTION_LABELS = {
    "none": "—",
    "dry_run": "🧪 Dry run",
    "already_running": "⏳ Already running",
    "manual_retry_required": "🛑 Manual retry required",
    "dispatched": "🚀 Dispatched",
    "check_failed": "—",
    "active_check_failed": "❌ Active-run check failed",
    "dispatch_failed": "❌ Dispatch failed",
    "result_unavailable": "❌ Result unavailable",
}


@dataclass(frozen=True)
class TargetResult:
    target: str
    stored_release_name: str | None
    current_release_name: str | None
    state: str
    action: str


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("release name must be a string or null")
    return value


def _unavailable_result(target: str) -> TargetResult:
    return TargetResult(
        target=target,
        stored_release_name=None,
        current_release_name=None,
        state="error",
        action="result_unavailable",
    )


def _read_result(results_dir: Path, target: str) -> TargetResult:
    try:
        raw = json.loads((results_dir / f"{target}.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("target") != target:
            raise ValueError("result target does not match its filename")
        state = raw.get("state")
        action = raw.get("action")
        if state not in STATE_LABELS or action not in ACTION_LABELS:
            raise ValueError("result has an unknown state or action")
        return TargetResult(
            target=target,
            stored_release_name=_optional_string(raw.get("stored_release_name")),
            current_release_name=_optional_string(raw.get("current_release_name")),
            state=state,
            action=action,
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return _unavailable_result(target)


def _release_name(value: str | None) -> str:
    if value is None:
        return "—"
    escaped = html.escape(value, quote=True).replace("|", "&#124;")
    escaped = escaped.replace("\r", "&#13;").replace("\n", "&#10;")
    return f"<code>{escaped}</code>"


def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def render_report(
    *,
    results_dir: Path,
    targets: list[str],
    dispatch_pipelines: bool,
    plan_result: str = "success",
) -> str:
    lines = ["## Game release check", ""]
    if plan_result != "success":
        lines.append("❌ Target selection failed; no release checks were started.")
        return "\n".join(lines) + "\n"
    if not targets:
        lines.append("⚪ No targets selected.")
        return "\n".join(lines) + "\n"

    results = [_read_result(results_dir, target) for target in targets]
    current_count = sum(result.state == "up_to_date" for result in results)
    update_count = sum(result.state == "update_available" for result in results)
    error_count = sum(result.state == "error" for result in results)
    mode = "🚀 Dispatch enabled" if dispatch_pipelines else "🧪 Dry run"
    lines.extend(
        (
            (
                f"**{mode}** · ✅ {current_count} current · "
                f"🆕 {_count(update_count, 'update', 'updates')} · "
                f"❌ {_count(error_count, 'error', 'errors')}"
            ),
            "",
            "| Target | Stored release | Current release | Result | Pipeline |",
            "| --- | --- | --- | --- | --- |",
        )
    )
    for result in results:
        target_label = html.escape(TARGET_LABELS.get(result.target, result.target), quote=True)
        lines.append(
            "| "
            + " | ".join(
                (
                    target_label,
                    _release_name(result.stored_release_name),
                    _release_name(result.current_release_name),
                    STATE_LABELS[result.state],
                    ACTION_LABELS[result.action],
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _targets(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(target, str) for target in parsed):
        raise ValueError("TARGETS must be a JSON array of strings")
    return parsed


def main() -> None:
    report = render_report(
        results_dir=Path(os.environ["RESULTS_DIR"]),
        targets=_targets(os.environ["TARGETS"]),
        dispatch_pipelines=os.environ.get("DISPATCH_PIPELINES", "false").lower() == "true",
        plan_result=os.environ.get("PLAN_RESULT", "failure"),
    )
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(report)
    print(report, end="")


if __name__ == "__main__":
    main()

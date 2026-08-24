from __future__ import annotations

import html
import os
import re
import uuid
from collections.abc import Mapping
from urllib.parse import quote


TARGET_LABELS = {
    "wot-eu": "WoT EU",
    "wot-na": "WoT NA",
    "wot-asia": "WoT Asia",
    "wot-common-test": "WoT Common Test",
    "wot-cn": "WoT CN",
    "mt-ru": "Мир танков RU",
    "mt-public-test": "Мир танков Public Test",
}
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _escaped(value: str) -> str:
    return html.escape(value, quote=True)


def _enabled(value: str) -> bool:
    return value.strip().lower() == "true"


def _languages(value: str) -> str:
    value = value.strip()
    if value.upper() == "ALL":
        return "ALL"
    return ", ".join(part.strip().upper() for part in value.split(",") if part.strip())


def _commit_url(owner: str, repository: str, commit_sha: str) -> str | None:
    if not COMMIT_SHA.fullmatch(commit_sha):
        return None
    return (
        "https://github.com/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/commit/{commit_sha}"
    )


def _publication_line(
    *,
    icon: str,
    repository: str,
    enabled: bool,
    job_result: str,
    publication_state: str,
    commit_sha: str,
    owner: str,
) -> str:
    label = f"{icon} <code>{_escaped(repository)}</code> — "
    if not enabled:
        return label + "disabled"
    if job_result == "failure":
        return label + "failed"
    if job_result == "cancelled":
        return label + "cancelled"
    if job_result != "success":
        return label + "not started"
    if publication_state == "unchanged":
        return label + "unchanged"
    if publication_state == "published":
        commit_url = _commit_url(owner, repository, commit_sha)
        return label + (f'<a href="{commit_url}">updated</a>' if commit_url else "updated")
    return label + "completed"


def render_message(environment: Mapping[str, str]) -> str:
    pipeline_result = environment.get("PIPELINE_RESULT", "failure")
    version_name = environment.get("VERSION_NAME", "").strip()
    if pipeline_result == "success" and not version_name:
        pipeline_result = "warning"

    icon = {
        "success": "✅",
        "cancelled": "⚠️",
        "warning": "⚠️",
    }.get(pipeline_result, "❌")
    target = environment.get("TARGET", "unknown")
    target_label = TARGET_LABELS.get(target, target)
    version_label = version_name or "version unavailable"
    client_type = environment.get("CLIENT_TYPE", "unknown").strip().upper()
    languages = _languages(environment.get("LANGUAGES", "")) or "unknown"

    header = (
        f"<b>{icon} {_escaped(target_label)} · {_escaped(version_label)}</b> · "
        f"<code>{_escaped(client_type)}</code> · <code>{_escaped(languages)}</code>"
    )
    owner = environment.get("GITHUB_REPOSITORY_OWNER", "wotstat")
    lines = [header]

    if pipeline_result not in {"success", "warning"}:
        for label, variable in (
            ("Provision", "PROVISION_RESULT"),
            ("Builder", "WORKLOAD_RESULT"),
            ("Queue watchdog", "QUEUE_WATCHDOG_RESULT"),
        ):
            lines.append(
                f"{label} — <code>{_escaped(environment.get(variable, 'unknown'))}</code>"
            )
        cleanup_result = _escaped(environment.get("CLEANUP_RESULT", "unknown"))
        deleted_count = _escaped(environment.get("CLEANUP_DELETED_COUNT", "unknown"))
        lines.append(
            f"Cleanup — <code>{cleanup_result}</code> (deleted: <code>{deleted_count}</code>)"
        )

    lines.extend(
        (
            _publication_line(
                icon="📦",
                repository="wot-src",
                enabled=_enabled(environment.get("WOT_SRC_ENABLED", "false")),
                job_result=environment.get("WOT_SRC_RESULT", "skipped"),
                publication_state=environment.get("WOT_SRC_PUBLICATION_STATE", ""),
                commit_sha=environment.get("WOT_SRC_COMMIT_SHA", ""),
                owner=owner,
            ),
            _publication_line(
                icon="🖼",
                repository="wot-gui-assets",
                enabled=_enabled(environment.get("WOT_GUI_ASSETS_ENABLED", "false")),
                job_result=environment.get("WOT_GUI_ASSETS_RESULT", "skipped"),
                publication_state=environment.get("WOT_GUI_ASSETS_PUBLICATION_STATE", ""),
                commit_sha=environment.get("WOT_GUI_ASSETS_COMMIT_SHA", ""),
                owner=owner,
            ),
        )
    )

    run_url = _escaped(environment["PIPELINE_RUN_URL"])
    lines.extend(("", f'<a href="{run_url}">Open pipeline run →</a>'))
    return "\n".join(lines)


def main() -> None:
    message = render_message(os.environ)
    output_path = os.environ["GITHUB_OUTPUT"]
    delimiter = f"telegram_message_{uuid.uuid4().hex}"
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"message<<{delimiter}\n{message}\n{delimiter}\n")
    print(message)


if __name__ == "__main__":
    main()

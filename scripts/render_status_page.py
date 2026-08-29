#!/usr/bin/env python3
# ruff: noqa: RUF001
"""Render the public pipeline status page from current files and their Git history."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.release_status import (
    TARGETS,
    PipelineRun,
    ReleaseStatus,
    ReleaseStatusError,
    load_status,
    parse_status_document,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
DEFAULT_REPOSITORY_URL = "https://github.com/wotstat/game-unpack-pipeline"
MOSCOW = ZoneInfo("Europe/Moscow")
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
SHORT_MONTHS = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)

CurrentState = Literal["success", "failure", "cancelled", "pending"]
BADGE_COLORS: dict[CurrentState, str] = {
    "success": "brightgreen",
    "failure": "red",
    "cancelled": "yellow",
    "pending": "lightgrey",
}
BADGE_CACHE_SECONDS = 300


@dataclass(frozen=True)
class TargetInfo:
    id: str
    name: str
    family: str


@dataclass(frozen=True)
class GitCommit:
    sha: str
    committed_at: datetime
    paths: tuple[str, ...]


@dataclass(frozen=True)
class HistoryEntry:
    target: str
    result: Literal["success", "failure", "cancelled"]
    release_name: str | None
    readable_version: str | None
    started_at: datetime
    completed_at: datetime
    duration_seconds: int | None
    run_id: int | None
    run_attempt: int | None
    run_url: str | None
    legacy: bool = False


TARGET_INFO = (
    TargetInfo("wot-eu", "Европа", "World of Tanks"),
    TargetInfo("wot-na", "Северная Америка", "World of Tanks"),
    TargetInfo("wot-asia", "Азия", "World of Tanks"),
    TargetInfo("wot-cn", "Китай", "World of Tanks"),
    TargetInfo("wot-common-test", "Общий тест WoT", "World of Tanks"),
    TargetInfo("mt-ru", "Россия", "Мир танков"),
    TargetInfo("mt-public-test", "Общий тест МТ", "Мир танков"),
)
TARGET_BY_ID = {target.id: target for target in TARGET_INFO}


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def _history_from_run(target: str, run: PipelineRun) -> HistoryEntry:
    return HistoryEntry(
        target=target,
        result=run.result,
        release_name=run.release_name,
        readable_version=run.readable_version,
        started_at=_parse_timestamp(run.started_at),
        completed_at=_parse_timestamp(run.completed_at),
        duration_seconds=run.duration_seconds,
        run_id=run.run_id,
        run_attempt=run.run_attempt,
        run_url=run.run_url,
    )


def _git(repository_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _status_commits(repository_root: Path) -> tuple[GitCommit, ...]:
    output = _git(
        repository_root,
        "log",
        "--format=@@COMMIT@@%H%x09%cI",
        "--name-only",
        "--",
        "status",
    )
    commits: list[GitCommit] = []
    sha: str | None = None
    committed_at: datetime | None = None
    paths: list[str] = []

    def append_current() -> None:
        if sha is not None and committed_at is not None:
            commits.append(GitCommit(sha, committed_at, tuple(paths)))

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("@@COMMIT@@"):
            append_current()
            marker = line.removeprefix("@@COMMIT@@")
            sha, timestamp = marker.split("\t", 1)
            committed_at = _parse_timestamp(timestamp)
            paths = []
        elif line.startswith("status/") and line.endswith(".json"):
            paths.append(line)
    append_current()
    return tuple(commits)


def _legacy_history_entry(
    target: str,
    payload: object,
    committed_at: datetime,
) -> HistoryEntry | None:
    if not isinstance(payload, dict) or set(payload) != {"release_name"}:
        return None
    release_name = payload["release_name"]
    if not isinstance(release_name, str) or not release_name.strip():
        return None
    return HistoryEntry(
        target=target,
        result="success",
        release_name=release_name,
        readable_version=None,
        started_at=committed_at,
        completed_at=committed_at,
        duration_seconds=None,
        run_id=None,
        run_attempt=None,
        run_url=None,
        legacy=True,
    )


def collect_history(
    repository_root: Path,
    statuses: dict[str, ReleaseStatus],
    *,
    limit: int,
) -> tuple[HistoryEntry, ...]:
    entries = [
        _history_from_run(target, status.last_run)
        for target, status in statuses.items()
        if status.last_run is not None
    ]
    for commit in _status_commits(repository_root):
        for path in commit.paths:
            target = Path(path).stem
            if target not in TARGETS:
                continue
            try:
                payload = json.loads(_git(repository_root, "show", f"{commit.sha}:{path}"))
            except (json.JSONDecodeError, subprocess.CalledProcessError):
                continue
            legacy = _legacy_history_entry(target, payload, commit.committed_at)
            if legacy is not None:
                entries.append(legacy)
                continue
            try:
                historical = parse_status_document(payload, f"{commit.sha}:{path}")
            except ReleaseStatusError:
                continue
            if historical.last_run is not None:
                entries.append(_history_from_run(target, historical.last_run))

    rich_keys: set[tuple[str, int, int]] = set()
    rich_version_times: list[tuple[str, str | None, datetime]] = []
    deduplicated: list[HistoryEntry] = []
    for entry in sorted(entries, key=lambda item: item.completed_at, reverse=True):
        if entry.run_id is not None and entry.run_attempt is not None:
            key = (entry.target, entry.run_id, entry.run_attempt)
            if key in rich_keys:
                continue
            rich_keys.add(key)
            rich_version_times.append((entry.target, entry.release_name, entry.completed_at))
            deduplicated.append(entry)
            continue
        duplicates_rich_entry = any(
            target == entry.target
            and release_name == entry.release_name
            and abs((completed_at - entry.completed_at).total_seconds()) <= 6 * 60 * 60
            for target, release_name, completed_at in rich_version_times
        )
        if not duplicates_rich_entry:
            deduplicated.append(entry)
    return tuple(deduplicated[:limit])


def _current_state(status: ReleaseStatus) -> CurrentState:
    if status.last_run is not None:
        return status.last_run.result
    return "success" if status.release_name is not None else "pending"


def render_badge(target: str, status: ReleaseStatus) -> str:
    state = _current_state(status)
    return (
        json.dumps(
            {
                "schemaVersion": 1,
                "label": target,
                "message": status.readable_version or "no data",
                "color": BADGE_COLORS[state],
                "cacheSeconds": BADGE_CACHE_SECONDS,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _format_date(value: datetime) -> str:
    local = value.astimezone(MOSCOW)
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}, {local:%H:%M}"


def _format_short_date(value: datetime) -> str:
    local = value.astimezone(MOSCOW)
    return f"{local.day} {SHORT_MONTHS[local.month - 1]}, {local:%H:%M}"


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "длительность недоступна"
    minutes, remaining = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {remaining:02d} с"
    return f"{remaining} с"


def _target_last_updated(
    target: str,
    status: ReleaseStatus,
    history: tuple[HistoryEntry, ...],
) -> datetime | None:
    if status.last_run is not None:
        return _parse_timestamp(status.last_run.completed_at)
    return next((entry.completed_at for entry in history if entry.target == target), None)


def _state_label(state: CurrentState) -> str:
    return {
        "success": "Успешно",
        "failure": "Ошибка",
        "cancelled": "Отменено",
        "pending": "Нет данных",
    }[state]


def _overall_copy(states: tuple[CurrentState, ...]) -> tuple[str, str, str]:
    problems = sum(state in {"failure", "cancelled"} for state in states)
    pending = states.count("pending")
    if problems:
        return (
            "Есть проблемы.",
            f"Последний запуск завершился неуспешно для {problems} из {len(states)} регионов.",
            "failure",
        )
    if pending:
        return (
            "Статус неполный.",
            f"Для {pending} из {len(states)} регионов ещё нет успешной публикации.",
            "pending",
        )
    return (
        "Всё актуально.",
        f"Все {len(states)} регионов синхронизированы с последними обработанными версиями игр.",
        "success",
    )


def _github_icon() -> str:
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M12 .7a11.3 11.3 0 0 0-3.6 22c.6.1.8-.2.8-.5v-2c-3.3.7-4-1.4-4-1.4-.'
        "5-1.4-1.3-1.7-1.3-1.7-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3."
        "5 1 .1-.8.4-1.3.8-1.6-2.6-.3-5.4-1.3-5.4-5.6 0-1.2.4-2.2 1.2-3-.1-.3-.5-1.5.1-3.1 "
        "0 0 1-.3 3.1 1.2a10.8 10.8 0 0 1 5.6 0c2.1-1.5 3.1-1.2 3.1-1.2.6 1.6.2 2.8.1 3.1.8."
        "8 1.2 1.8 1.2 3 0 4.3-2.8 5.3-5.4 5.6.4.4.8 1.1.8 2.2v3.3c0 .3.2.6.8.5A11.3 11.3 0 0 "
        '0 12 .7Z"/></svg>'
    )


def _render_product(
    family: str,
    statuses: dict[str, ReleaseStatus],
    history: tuple[HistoryEntry, ...],
) -> str:
    targets = [target for target in TARGET_INFO if target.family == family]
    rows: list[str] = []
    successful = 0
    for target in targets:
        status = statuses[target.id]
        state = _current_state(status)
        if state == "success":
            successful += 1
        updated = _target_last_updated(target.id, status, history)
        version = status.readable_version or "—"
        target_name = html.escape(target.name)
        updated_label = _format_short_date(updated) if updated else "ещё не запускался"
        state_label = _state_label(state)
        rows.append(
            f"""<li id="{target.id}" class="target-row target-row--{state}">
              <div class="target-name">
                <strong>{target_name}</strong><code>{target.id}</code>
              </div>
              <span class="target-version">{html.escape(version)}</span>
              <span class="target-date">{updated_label}</span>
              <span class="target-state" title="{state_label}"
                aria-label="{state_label}"></span>
            </li>"""
        )
    state_class = "success" if successful == len(targets) else "pending"
    return f"""<section class="product-group">
      <div class="product-heading">
        <div><p>Продукт</p><h2>{html.escape(family)}</h2></div>
        <span class="product-count product-count--{state_class}">
          <i></i>{successful} из {len(targets)} регионов
        </span>
      </div>
      <ul>{"".join(rows)}</ul>
    </section>"""


def _history_description(entry: HistoryEntry) -> str:
    version = html.escape(entry.readable_version or entry.release_name or "неизвестной версии")
    if entry.legacy:
        return f"Зафиксирована версия {version}"
    if entry.result == "success":
        return f"Опубликована версия {version}"
    if entry.result == "cancelled":
        return f"Запуск для версии {version} отменён"
    return f"Публикация версии {version} не завершена"


def _render_history(entries: tuple[HistoryEntry, ...]) -> str:
    if not entries:
        return '<p class="empty-history">Запусков в Git-истории пока нет.</p>'
    rows: list[str] = []
    for entry in entries:
        target = TARGET_BY_ID[entry.target]
        if entry.run_url is not None and entry.run_id is not None:
            run = f'<a href="{html.escape(entry.run_url, quote=True)}">#{entry.run_id}</a>' + (
                f" · попытка {entry.run_attempt}"
                if entry.run_attempt and entry.run_attempt > 1
                else ""
            )
        else:
            run = "старый формат статуса"
        rows.append(
            f"""<li class="timeline-item timeline-item--{entry.result}">
              <span class="timeline-node" aria-hidden="true"></span>
              <div class="timeline-copy">
                <div><strong>{html.escape(target.name)}</strong>
                  <time>{_format_date(entry.started_at)} МСК</time>
                </div>
                <p>{_history_description(entry)}</p>
                <small>{_format_duration(entry.duration_seconds)} · {run}</small>
              </div>
            </li>"""
        )
    return f"<ol>{''.join(rows)}</ol>"


def render_page(
    statuses: dict[str, ReleaseStatus],
    history: tuple[HistoryEntry, ...],
    *,
    repository_url: str,
    site_url: str,
) -> str:
    states = tuple(_current_state(statuses[target.id]) for target in TARGET_INFO)
    title, description, overall_state = _overall_copy(states)
    updates = [
        updated
        for target in TARGET_INFO
        if (updated := _target_last_updated(target.id, statuses[target.id], history)) is not None
    ]
    last_updated = max(updates) if updates else None
    canonical = (
        f'<link rel="canonical" href="{html.escape(site_url.rstrip("/") + "/", quote=True)}" />'
        if site_url
        else ""
    )
    history_url = f"{repository_url.rstrip('/')}/commits/main/status"
    updated_label = _format_date(last_updated) + " МСК" if last_updated else "ещё не было"
    escaped_repository_url = html.escape(repository_url, quote=True)
    escaped_history_url = html.escape(history_url, quote=True)
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="description"
      content="Публичный статус скачивания и публикации игровых клиентов wotstat." />
    <meta name="theme-color" content="#ffffff" />
    <meta property="og:title" content="game-unpack-pipeline — статус" />
    <meta property="og:description"
      content="Текущие версии по регионам и история запусков pipeline." />
    <meta property="og:type" content="website" />
    {canonical}
    <title>WOTSTAT – Unpack status</title>
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body>
    <header class="repo-header">
      <a class="repo-name" href="{escaped_repository_url}"
        aria-label="Репозиторий wotstat/game-unpack-pipeline">
        {_github_icon()}<span>wotstat/<strong>game-unpack-pipeline</strong></span>
      </a>
      <span class="public-label">публичный статус</span>
    </header>
    <main class="status-layout">
      <div class="current-status">
        <section class="overall overall--{overall_state}">
          <p class="eyebrow">Состояние публикаций</p>
          <h1>{html.escape(title[:-1])}<span>.</span></h1>
          <p>{html.escape(description)}</p>
          <div><span>Последнее обновление</span><strong>{updated_label}</strong></div>
        </section>
        {_render_product("World of Tanks", statuses, history)}
        {_render_product("Мир танков", statuses, history)}
      </div>
      <aside class="timeline" aria-labelledby="history-title">
        <div class="timeline-heading">
          <p class="eyebrow">История Git</p><h2 id="history-title">Что происходило</h2>
        </div>
        {_render_history(history)}
        <a class="history-link" href="{escaped_history_url}">Вся история →</a>
      </aside>
    </main>
    <footer>Состояние формируется из файлов репозитория · история — из их Git-коммитов</footer>
  </body>
</html>
"""


def build_site(
    repository_root: Path,
    output_dir: Path,
    *,
    history_limit: int,
    repository_url: str,
    site_url: str,
) -> None:
    status_dir = repository_root / "status"
    statuses = {target: load_status(status_dir, target) for target in TARGETS}
    history = collect_history(repository_root, statuses, limit=history_limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    badges_dir = output_dir / "badges"
    badges_dir.mkdir(exist_ok=True)
    (output_dir / "index.html").write_text(
        render_page(
            statuses,
            history,
            repository_url=repository_url,
            site_url=site_url,
        ),
        encoding="utf-8",
    )
    for target, status in statuses.items():
        (badges_dir / f"{target}.json").write_text(
            render_badge(target, status),
            encoding="utf-8",
        )
    shutil.copyfile(repository_root / "status-page/styles.css", output_dir / "styles.css")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output-dir", type=Path, default=Path("_site"))
    parser.add_argument("--history-limit", type=int, default=12)
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--site-url", default="")
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.history_limit < 1 or arguments.history_limit > 100:
        raise SystemExit("--history-limit must be between 1 and 100")
    build_site(
        arguments.repository_root.resolve(),
        arguments.output_dir.resolve(),
        history_limit=arguments.history_limit,
        repository_url=arguments.repository_url,
        site_url=arguments.site_url,
    )


if __name__ == "__main__":
    main()

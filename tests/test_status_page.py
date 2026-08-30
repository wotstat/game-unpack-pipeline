from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from scripts.release_status import PipelineRun, ReleaseStatus
from scripts.render_status_page import HistoryEntry, build_site, render_badge, render_page

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_build_site_uses_real_status_files_and_git_history(tmp_path: Path) -> None:
    build_site(
        REPOSITORY_ROOT,
        tmp_path,
        history_limit=12,
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="https://wotstat.github.io/game-unpack-pipeline",
    )

    page = (tmp_path / "index.html").read_text()
    assert "1.44.0.0 #2262" in page
    assert "2.3.1.3 #926" in page
    assert "Общий тест WoT" in page
    assert "Общий тест МТ" in page  # noqa: RUF001
    assert "#33258764585" in page
    assert "Статус неполный" in page
    assert 'rel="canonical" href="https://wotstat.github.io/game-unpack-pipeline/"' in page
    assert (tmp_path / "styles.css").is_file()
    assert (tmp_path / ".nojekyll").is_file()
    assert (tmp_path / "badges").is_dir()
    assert {path.name for path in (tmp_path / "badges").iterdir()} == {
        "mt-public-test.json",
        "mt-ru.json",
        "wot-asia.json",
        "wot-cn.json",
        "wot-common-test.json",
        "wot-eu.json",
        "wot-na.json",
    }
    assert json.loads((tmp_path / "badges/wot-eu.json").read_text()) == {
        "schemaVersion": 1,
        "label": "wot-eu",
        "message": "2.3.1.3 #926",
        "color": "brightgreen",
        "cacheSeconds": 300,
    }
    assert json.loads((tmp_path / "badges/wot-na.json").read_text()) == {
        "schemaVersion": 1,
        "label": "wot-na",
        "message": "no data",
        "color": "lightgrey",
        "cacheSeconds": 300,
    }


def test_page_keeps_successful_version_visible_after_failed_run() -> None:
    failure = PipelineRun(
        result="failure",
        release_name="1.45.0.9000",
        readable_version="1.45.0.0 #2300",
        started_at="2026-08-30T08:00:00Z",
        completed_at="2026-08-30T08:12:00Z",
        duration_seconds=720,
        run_id=200,
        run_attempt=1,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/200",
    )
    statuses = {
        target: ReleaseStatus(
            release_name="1.44.0.8017",
            readable_version="1.44.0.0 #2262",
            last_run=failure if target == "mt-ru" else None,
        )
        for target in (
            "wot-eu",
            "wot-na",
            "wot-asia",
            "wot-common-test",
            "wot-cn",
            "mt-ru",
            "mt-public-test",
        )
    }
    history = (
        HistoryEntry(
            target="mt-ru",
            result="failure",
            release_name=failure.release_name,
            readable_version=failure.readable_version,
            started_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
            completed_at=datetime(2026, 8, 30, 8, 12, tzinfo=UTC),
            duration_seconds=720,
            run_id=200,
            run_attempt=1,
            run_url=failure.run_url,
        ),
    )

    page = render_page(
        statuses,
        history,
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="",
    )

    assert "Есть проблемы" in page
    assert "1.44.0.0 #2262" in page
    assert "Публикация версии 1.45.0.0 #2300 не завершена" in page
    assert "12 мин 00 с" in page  # noqa: RUF001
    assert "actions/runs/200" in page
    assert 'id="mt-ru"' in page
    assert json.loads(render_badge("mt-ru", statuses["mt-ru"])) == {
        "schemaVersion": 1,
        "label": "mt-ru",
        "message": "1.44.0.0 #2262",
        "color": "red",
        "cacheSeconds": 300,
    }
    cancelled_status = replace(statuses["mt-ru"], last_run=replace(failure, result="cancelled"))
    assert json.loads(render_badge("mt-ru", cancelled_status))["color"] == "yellow"


def test_page_describes_failed_run_without_known_version() -> None:
    failure = PipelineRun(
        result="failure",
        release_name=None,
        readable_version=None,
        started_at="2026-08-30T08:00:00Z",
        completed_at="2026-08-30T08:12:00Z",
        duration_seconds=720,
        run_id=200,
        run_attempt=1,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/200",
    )
    statuses = {
        target: ReleaseStatus(
            release_name=None,
            readable_version=None,
            last_run=failure if target == "mt-ru" else None,
        )
        for target in (
            "wot-eu",
            "wot-na",
            "wot-asia",
            "wot-common-test",
            "wot-cn",
            "mt-ru",
            "mt-public-test",
        )
    }

    page = render_page(
        statuses,
        (
            HistoryEntry(
                target="mt-ru",
                result="failure",
                release_name=None,
                readable_version=None,
                started_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
                completed_at=datetime(2026, 8, 30, 8, 12, tzinfo=UTC),
                duration_seconds=720,
                run_id=200,
                run_attempt=1,
                run_url=failure.run_url,
            ),
        ),
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="",
    )

    assert "Публикация не завершена. Версия неизвестна" in page
    assert "Публикация версии неизвестной версии не завершена" not in page


def test_history_distinguishes_wot_and_mt_test_targets() -> None:
    statuses = {
        target: ReleaseStatus(release_name=None, readable_version=None, last_run=None)
        for target in (
            "wot-eu",
            "wot-na",
            "wot-asia",
            "wot-common-test",
            "wot-cn",
            "mt-ru",
            "mt-public-test",
        )
    }
    history = tuple(
        HistoryEntry(
            target=target,
            result="success",
            release_name="1.0.0.0",
            readable_version="1.0.0.0 #1",
            started_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
            completed_at=datetime(2026, 8, 30, 8, 1, tzinfo=UTC),
            duration_seconds=60,
            run_id=run_id,
            run_attempt=1,
            run_url=f"https://github.com/wotstat/game-unpack-pipeline/actions/runs/{run_id}",
        )
        for target, run_id in (("wot-common-test", 201), ("mt-public-test", 202))
    )

    page = render_page(
        statuses,
        history,
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="",
    )

    assert page.count("Общий тест WoT") == 2
    assert page.count("Общий тест МТ") == 2  # noqa: RUF001

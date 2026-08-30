from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from scripts.release_status import TARGETS, PipelineRun, ReleaseStatus, load_status
from scripts.render_status_page import (
    HistoryEntry,
    ReleaseCheck,
    build_site,
    render_badge,
    render_page,
    render_release_check_badge,
)

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
    assert 'class="overall overall--' in page
    assert 'class="theme-toggle"' in page
    assert 'class="theme-transition-scope"' in page
    assert 'src="theme.js"' in page
    assert 'rel="canonical" href="https://wotstat.github.io/game-unpack-pipeline/"' in page
    assert (tmp_path / "styles.css").is_file()
    assert (tmp_path / "theme.js").is_file()
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
        "release-check.json",
    }
    for target in TARGETS:
        expected = json.loads(render_badge(target, load_status(REPOSITORY_ROOT / "status", target)))
        assert json.loads((tmp_path / f"badges/{target}.json").read_text()) == expected
    assert json.loads((tmp_path / "badges/release-check.json").read_text()) == json.loads(
        render_release_check_badge(None)
    )


def test_page_uses_selected_overall_and_empty_state_copy() -> None:
    success = PipelineRun(
        result="success",
        release_name="1.45.0.9000",
        readable_version="1.45.0.0 #2300",
        started_at="2026-08-30T08:00:00Z",
        completed_at="2026-08-30T08:12:00Z",
        duration_seconds=720,
        run_id=200,
        run_attempt=1,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/200",
    )
    successful_statuses = {
        target: ReleaseStatus(
            release_name=success.release_name,
            readable_version=success.readable_version,
            last_run=success,
        )
        for target in TARGETS
    }

    successful_page = render_page(
        successful_statuses,
        (),
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="",
    )

    assert "Всё в порядке" in successful_page
    assert "Для всех 7 регионов успешно обработаны актуальные версии." in successful_page

    pending_statuses = {
        target: ReleaseStatus(release_name=None, readable_version=None, last_run=None)
        for target in TARGETS
    }
    pending_page = render_page(
        pending_statuses,
        (),
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="",
    )

    assert "Данных недостаточно" in pending_page
    assert "Для 7 из 7 регионов ещё нет успешной публикации." in pending_page
    assert "Обновлений ещё не было" in pending_page
    assert "Проверок ещё не было" in pending_page
    assert 'content="WOTSTAT — unpack status"' in pending_page
    assert 'content="Статус распаковки WoT и MT по регионам и история запусков."' in pending_page


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
    assert "Обработка версии 1.45.0.0 #2300 завершилась с ошибкой" in page  # noqa: RUF001
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

    assert "Не удалось завершить обработку, версия неизвестна" in page  # noqa: RUF001
    assert (
        "Обработка версии неизвестной версии завершилась с ошибкой" not in page  # noqa: RUF001
    )


def test_page_links_latest_pipeline_and_release_check_and_renders_global_badge() -> None:
    pipeline = PipelineRun(
        result="success",
        release_name="2.3.1.5412",
        readable_version="2.3.1.3 #926",
        started_at="2026-08-30T10:00:00Z",
        completed_at="2026-08-30T10:30:00Z",
        duration_seconds=1800,
        run_id=300,
        run_attempt=1,
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/300",
    )
    statuses = {
        target: ReleaseStatus(
            release_name=pipeline.release_name,
            readable_version=pipeline.readable_version,
            last_run=pipeline if target == "wot-eu" else None,
        )
        for target in TARGETS
    }
    release_check = ReleaseCheck(
        completed_at=datetime(2026, 8, 30, 13, 17, tzinfo=UTC),
        conclusion="success",
        run_url="https://github.com/wotstat/game-unpack-pipeline/actions/runs/301",
    )

    page = render_page(
        statuses,
        (),
        repository_url="https://github.com/wotstat/game-unpack-pipeline",
        site_url="",
        release_check=release_check,
    )

    assert (
        '<a href="https://github.com/wotstat/game-unpack-pipeline/actions/runs/300">'
        "30 августа 2026, 13:30 МСК</a>"  # noqa: RUF001
    ) in page
    assert (
        '<a href="https://github.com/wotstat/game-unpack-pipeline/actions/runs/301">'
        "30 августа 2026, 16:17 МСК</a>"  # noqa: RUF001
    ) in page
    assert json.loads(render_release_check_badge(release_check)) == {
        "schemaVersion": 1,
        "label": "release check",
        "message": "30 Aug 16:17 MSK",
        "color": "brightgreen",
        "cacheSeconds": 300,
    }


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

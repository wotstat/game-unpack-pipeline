from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scripts.release_status import PipelineRun, ReleaseStatus
from scripts.render_status_page import HistoryEntry, build_site, render_page

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
    assert "#33258764585" in page
    assert "Статус неполный" in page
    assert 'rel="canonical" href="https://wotstat.github.io/game-unpack-pipeline/"' in page
    assert (tmp_path / "styles.css").is_file()
    assert (tmp_path / ".nojekyll").is_file()


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

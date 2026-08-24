from __future__ import annotations

import unittest

from scripts.render_telegram_report import render_message


class TelegramReportTests(unittest.TestCase):
    def test_success_report_is_compact_and_preserves_all_languages(self) -> None:
        message = render_message(
            {
                "PIPELINE_RESULT": "success",
                "TARGET": "wot-eu",
                "VERSION_NAME": "2.3.1.5400",
                "CLIENT_TYPE": "sd",
                "LANGUAGES": "all",
                "GITHUB_REPOSITORY_OWNER": "wotstat",
                "WOT_SRC_ENABLED": "true",
                "WOT_SRC_RESULT": "success",
                "WOT_SRC_PUBLICATION_STATE": "published",
                "WOT_SRC_COMMIT_SHA": "a" * 40,
                "WOT_GUI_ASSETS_ENABLED": "true",
                "WOT_GUI_ASSETS_RESULT": "success",
                "WOT_GUI_ASSETS_PUBLICATION_STATE": "unchanged",
                "WOT_GUI_ASSETS_COMMIT_SHA": "b" * 40,
                "PIPELINE_RUN_URL": "https://github.com/wotstat/game-unpack-pipeline/actions/runs/1",
            }
        )

        self.assertEqual(
            message,
            "\n".join(
                (
                    "<b>✅ WoT EU · 2.3.1.5400</b> · <code>SD</code> · "
                    "<code>ALL</code>",
                    "📦 <code>wot-src</code> — "
                    '<a href="https://github.com/wotstat/wot-src/commit/'
                    f"{'a' * 40}\">updated</a>",
                    "🖼 <code>wot-gui-assets</code> — unchanged",
                    "",
                    '<a href="https://github.com/wotstat/game-unpack-pipeline/actions/runs/1">'
                    "Open pipeline run →</a>",
                )
            ),
        )

    def test_report_normalizes_explicit_languages_and_disabled_publishers(self) -> None:
        message = render_message(
            {
                "PIPELINE_RESULT": "success",
                "TARGET": "mt-ru",
                "VERSION_NAME": "1.44.0.7899",
                "CLIENT_TYPE": "hd",
                "LANGUAGES": "ru, be",
                "WOT_SRC_ENABLED": "false",
                "WOT_GUI_ASSETS_ENABLED": "false",
                "PIPELINE_RUN_URL": "https://github.com/example/run",
            }
        )

        self.assertIn(
            "<b>✅ Мир танков RU · 1.44.0.7899</b> · <code>HD</code> · "
            "<code>RU, BE</code>",
            message,
        )
        self.assertIn("📦 <code>wot-src</code> — disabled", message)
        self.assertIn("🖼 <code>wot-gui-assets</code> — disabled", message)

    def test_failure_report_keeps_operational_job_states(self) -> None:
        message = render_message(
            {
                "PIPELINE_RESULT": "failure",
                "TARGET": "wot-common-test",
                "VERSION_NAME": "",
                "CLIENT_TYPE": "sd",
                "LANGUAGES": "EN",
                "PROVISION_RESULT": "success",
                "WORKLOAD_RESULT": "failure",
                "QUEUE_WATCHDOG_RESULT": "success",
                "CLEANUP_RESULT": "success",
                "CLEANUP_DELETED_COUNT": "6",
                "WOT_SRC_ENABLED": "true",
                "WOT_SRC_RESULT": "skipped",
                "WOT_GUI_ASSETS_ENABLED": "true",
                "WOT_GUI_ASSETS_RESULT": "skipped",
                "PIPELINE_RUN_URL": "https://github.com/example/run?a=1&b=2",
            }
        )

        self.assertIn("<b>❌ WoT Common Test · version unavailable</b>", message)
        self.assertIn("Builder — <code>failure</code>", message)
        self.assertIn("Cleanup — <code>success</code> (deleted: <code>6</code>)", message)
        self.assertIn("📦 <code>wot-src</code> — not started", message)
        self.assertIn("run?a=1&amp;b=2", message)


if __name__ == "__main__":
    unittest.main()

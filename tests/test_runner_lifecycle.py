from __future__ import annotations

import base64
import http.client
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import runner_lifecycle as lifecycle

ROOT = Path(__file__).resolve().parents[1]


class ResourceIdentityTests(unittest.TestCase):
    def test_builds_deterministic_names_and_ownership_descriptor(self) -> None:
        identity = lifecycle.ResourceIdentity(
            repository="wotstat/game-unpack-pipeline",
            run_id="123456",
            run_attempt="2",
            instance_key="manual-123456-2",
        )

        self.assertEqual(identity.server_name, "gup-manual-123456-2")
        self.assertEqual(identity.security_group_name, "gup-manual-123456-2-sg")
        self.assertEqual(identity.scope_label, "gup-run-123456-2")
        self.assertEqual(
            identity.descriptor,
            "game-unpack-pipeline;repository=wotstat/game-unpack-pipeline;"
            "run_id=123456;run_attempt=2;instance_key=manual-123456-2",
        )

        downloader = identity.runner("downloader")
        assets = identity.runner("wot-gui-assets")
        source = identity.runner("wot-src")
        uploader = identity.runner("wotstat-assets")
        self.assertEqual(downloader.name, "gup-manual-123456-2-downloader")
        self.assertEqual(downloader.label, "gup-manual-123456-2-downloader")
        self.assertEqual(downloader.repository, "wotstat/game-unpack-pipeline")
        self.assertEqual(assets.name, "gup-manual-123456-2-wot-gui-assets")
        self.assertEqual(assets.repository, "wotstat/game-unpack-pipeline")
        self.assertEqual(source.name, "gup-manual-123456-2-wot-src")
        self.assertEqual(source.repository, "wotstat/game-unpack-pipeline")
        self.assertEqual(uploader.name, "gup-manual-123456-2-wotstat-assets")
        self.assertEqual(uploader.repository, "wotstat/game-unpack-pipeline")
        self.assertEqual(downloader.scope_label, assets.scope_label)
        self.assertEqual(downloader.scope_label, source.scope_label)
        self.assertEqual(downloader.scope_label, uploader.scope_label)

    def test_rejects_unsafe_instance_keys(self) -> None:
        rejected = ["", "UPPER", "-leading", "trailing-", "has space", "a" * 49]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(lifecycle.LifecycleError):
                lifecycle.validate_instance_key(value)

    def test_accepts_single_character_and_full_length_keys(self) -> None:
        self.assertEqual(lifecycle.validate_instance_key("a"), "a")
        full_length = "a" + "b" * 46 + "9"
        self.assertEqual(len(full_length), 48)
        self.assertEqual(lifecycle.validate_instance_key(full_length), full_length)


class IdentifierTests(unittest.TestCase):
    def test_accepts_openstack_and_selectel_uuid_forms(self) -> None:
        dashed = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
        compact = dashed.replace("-", "")
        self.assertIsNotNone(lifecycle.UUID_RE.fullmatch(dashed))
        self.assertIsNotNone(lifecycle.UUID_RE.fullmatch(compact))
        self.assertEqual(lifecycle.normalized_uuid(dashed), compact)


class JobRoutingTests(unittest.TestCase):
    def test_matches_the_unique_runner_label_independently_of_job_name(self) -> None:
        job: dict[str, object] = {
            "name": "Download and unpack (wot-eu, sd)",
            "labels": ["self-hosted", "gup-manual-123456-2"],
        }

        self.assertTrue(lifecycle.job_targets_runner(job, "gup-manual-123456-2"))
        self.assertFalse(lifecycle.job_targets_runner(job, "gup-manual-another-1"))

    def test_rejects_missing_or_malformed_job_labels(self) -> None:
        self.assertFalse(lifecycle.job_targets_runner({}, "gup-manual-123456-2"))
        self.assertFalse(
            lifecycle.job_targets_runner(
                {"labels": "self-hosted,gup-manual-123456-2"},
                "gup-manual-123456-2",
            )
        )


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = lifecycle.ResourceIdentity(
            repository="wotstat/game-unpack-pipeline",
            run_id="123456",
            run_attempt="2",
            instance_key="manual-123456-2",
        )

    def test_accepts_mapping_and_serialized_server_properties(self) -> None:
        properties = {
            "gup_repository": self.identity.repository,
            "gup_run_id": self.identity.run_id,
            "gup_run_attempt": self.identity.run_attempt,
            "gup_instance_key": self.identity.instance_key,
        }
        mapping_server = {"name": self.identity.server_name, "properties": properties}
        serialized_server = {
            "name": self.identity.server_name,
            "properties": ", ".join(f"{key}='{value}'" for key, value in properties.items()),
        }

        self.assertTrue(lifecycle.server_has_ownership_markers(mapping_server, self.identity))
        self.assertTrue(lifecycle.server_has_ownership_markers(serialized_server, self.identity))

    def test_rejects_server_with_only_matching_name(self) -> None:
        server = {"name": self.identity.server_name, "properties": {}}
        self.assertFalse(lifecycle.server_has_ownership_markers(server, self.identity))


class CloudConfigTests(unittest.TestCase):
    def test_jit_configs_are_encoded_and_runners_use_isolated_users(self) -> None:
        template = (ROOT / "scripts" / "bootstrap-actions-runner.sh").read_text()
        assets_jit_config = "sensitive-assets-jit-configuration"
        downloader_jit_config = "sensitive-downloader-jit-configuration"
        source_jit_config = "sensitive-source-jit-configuration"
        uploader_jit_config = "sensitive-uploader-jit-configuration"
        cloud_config = lifecycle.render_cloud_config(
            template,
            runner_download_url=(
                "https://github.com/actions/runner/releases/download/v2.999.0/"
                "actions-runner-linux-x64-2.999.0.tar.gz"
            ),
            runner_sha256="a" * 64,
            runner_version="2.999.0",
            runner_jit_configs={
                "downloader": downloader_jit_config,
                "wot-gui-assets": assets_jit_config,
                "wot-src": source_jit_config,
                "wotstat-assets": uploader_jit_config,
            },
        )

        self.assertTrue(cloud_config.startswith("#cloud-config\n"))
        self.assertNotIn(downloader_jit_config, cloud_config)
        self.assertNotIn(assets_jit_config, cloud_config)
        self.assertNotIn(source_jit_config, cloud_config)
        self.assertNotIn(uploader_jit_config, cloud_config)
        encoded = re.search(r"^    content: (\S+)$", cloud_config, re.MULTILINE)
        self.assertIsNotNone(encoded)
        assert encoded is not None
        bootstrap = base64.b64decode(encoded.group(1)).decode()
        self.assertIn(
            "DOWNLOADER_RUNNER_JIT_CONFIG=sensitive-downloader-jit-configuration",
            bootstrap,
        )
        self.assertIn(
            "WOT_GUI_ASSETS_RUNNER_JIT_CONFIG=sensitive-assets-jit-configuration",
            bootstrap,
        )
        self.assertIn(
            "WOT_SRC_RUNNER_JIT_CONFIG=sensitive-source-jit-configuration",
            bootstrap,
        )
        self.assertIn(
            "WOTSTAT_ASSETS_RUNNER_JIT_CONFIG=sensitive-uploader-jit-configuration",
            bootstrap,
        )
        self.assertIn("WOT_GUI_ASSETS_RUNNER_JIT_CONFIG", bootstrap)
        self.assertIn("unset \\\n", bootstrap)
        self.assertIn("User=game-downloader", bootstrap)
        self.assertIn("User=wot-gui-assets-publisher", bootstrap)
        self.assertIn("User=wot-src-publisher", bootstrap)
        self.assertIn("User=wotstat-assets-uploader", bootstrap)
        self.assertIn(
            "apt-get install --yes --no-install-recommends git",
            bootstrap,
        )
        self.assertIn(
            "install -d -o game-downloader -g game-downloader -m 0700 \\\n"
            "  /run/actions-runner/downloader",
            bootstrap,
        )
        self.assertIn(
            "install -d -o wot-gui-assets-publisher -g wot-gui-assets-publisher -m 0700 \\\n"
            "  /run/actions-runner/wot-gui-assets",
            bootstrap,
        )
        self.assertIn(
            "install -d -o wot-src-publisher -g wot-src-publisher -m 0700 \\\n"
            "  /run/actions-runner/wot-src",
            bootstrap,
        )
        self.assertIn(
            "install -d -o wotstat-assets-uploader -g wotstat-assets-uploader -m 0700 \\\n"
            "  /run/actions-runner/wotstat-assets",
            bootstrap,
        )
        self.assertIn(
            'readonly jit_config_file="/run/actions-runner/${role}/jit-config"',
            bootstrap,
        )
        self.assertNotIn("/run/actions-runner-${role}-jit-config", bootstrap)
        self.assertIn("game-downloader ALL=(ALL) NOPASSWD: ALL", bootstrap)
        self.assertNotIn("wot-gui-assets-publisher ALL=", bootstrap)
        self.assertNotIn("wot-src-publisher ALL=", bootstrap)
        self.assertNotIn("wotstat-assets-uploader ALL=", bootstrap)
        self.assertNotIn("RUNNER_ALLOW_RUNASROOT", bootstrap)
        self.assertNotIn("set -x", bootstrap)
        self.assertIn("permissions: '0700'", cloud_config)


class SanitizerTests(unittest.TestCase):
    def test_redacts_known_token_shapes_and_jit_lines(self) -> None:
        registration_token = "ghs" + "_" + "a" * 40
        personal_access_token = "github" + "_pat_" + "test_value"
        source = "\n".join(
            [
                f"token={registration_token}",
                personal_access_token,
                "./run.sh --jitconfig very-secret",
                'encoded_jit_config="also-secret"',
                "password=known-value",
            ]
        )
        result = lifecycle.sanitized(source, ["known-value"])
        self.assertNotIn(registration_token, result)
        self.assertNotIn(personal_access_token, result)
        self.assertNotIn("very-secret", result)
        self.assertNotIn("also-secret", result)
        self.assertNotIn("known-value", result)


class SelectelConfigTests(unittest.TestCase):
    def test_reads_compact_project_id(self) -> None:
        environment = {
            "SELECTEL_OS_AUTH_URL": "https://cloud.api.selcloud.ru/identity/v3",
            "SELECTEL_OS_USERNAME": "service-user",
            "SELECTEL_OS_PASSWORD": "not-a-real-secret",
            "SELECTEL_OS_USER_DOMAIN_NAME": "123456",
            "SELECTEL_OS_PROJECT_ID": "497f6eca62764993bfeb53cbbbba6f08",
            "SELECTEL_OS_REGION_NAME": "ru-9",
            "SELECTEL_AVAILABILITY_ZONE": "ru-9a",
            "SELECTEL_IMAGE_ID": "image-id",
            "SELECTEL_FLAVOR_ID": "1312",
            "SELECTEL_PUBLIC_NETWORK_API_URL": (
                "https://ru-9.cloud.api.selcloud.ru/public-network"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            config = lifecycle.SelectelConfig.from_environment()

        self.assertEqual(config.project_id, environment["SELECTEL_OS_PROJECT_ID"])
        self.assertEqual(config.region_name, "ru-9")


class HttpJsonTests(unittest.TestCase):
    def test_retries_idempotent_request_after_remote_disconnect(self) -> None:
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"ports":[]}'
        response.headers.items.return_value = []
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with (
            patch(
                "scripts.runner_lifecycle.urllib.request.urlopen",
                side_effect=[http.client.RemoteDisconnected("test"), response],
            ) as urlopen,
            patch("scripts.runner_lifecycle.time.sleep") as sleep,
        ):
            status, payload, _ = lifecycle.http_json(
                "GET", "https://api.example.test/v1/public_ports"
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ports": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()


class CleanupReportingTests(unittest.TestCase):
    def test_reports_deletions_even_when_later_cleanup_fails(self) -> None:
        arguments = Mock(
            instance_key="manual-123456-2",
            run_id="123456",
            run_attempt="2",
            diagnostics=False,
            downloader_runner_id="1",
            wot_gui_assets_runner_id="2",
            wot_src_runner_id="3",
            wotstat_assets_runner_id="4",
            server_id="",
            port_id="",
            security_group_id="",
        )
        config = Mock(password="not-a-real-secret")

        def record_runner(
            *_args: object,
            deleted_resources: list[str] | None = None,
            **_kwargs: object,
        ) -> None:
            assert deleted_resources is not None
            deleted_resources.append("github-runner")

        def record_server(
            *_args: object,
            deleted_resources: list[str] | None = None,
            **_kwargs: object,
        ) -> None:
            assert deleted_resources is not None
            deleted_resources.append("selectel-server")

        def record_port(
            *_args: object,
            deleted_resources: list[str] | None = None,
            **_kwargs: object,
        ) -> None:
            assert deleted_resources is not None
            deleted_resources.append("selectel-public-port")

        def delete_security_group(
            *_args: object,
            deleted_resources: list[str] | None = None,
            **_kwargs: object,
        ) -> None:
            assert deleted_resources is not None
            deleted_resources.append("selectel-security-group")
            raise lifecycle.LifecycleError("simulated post-delete failure")

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output"
            environment = {
                "GITHUB_APP_TOKEN": "test-token",
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_REPOSITORY": "wotstat/game-unpack-pipeline",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(
                    lifecycle.SelectelConfig,
                    "from_environment",
                    return_value=config,
                ),
                patch.object(lifecycle, "delete_github_runners", side_effect=record_runner),
                patch.object(lifecycle, "delete_servers", side_effect=record_server),
                patch.object(lifecycle, "selectel_token", return_value="selectel-token"),
                patch.object(lifecycle, "delete_public_ports", side_effect=record_port),
                patch.object(
                    lifecycle,
                    "delete_security_groups",
                    side_effect=delete_security_group,
                ),
                self.assertRaises(lifecycle.LifecycleError),
            ):
                lifecycle.cleanup(arguments)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "deleted_count=7\n")


class WorkflowContractTests(unittest.TestCase):
    def test_dispatch_exposes_only_production_inputs(self) -> None:
        workflow = (ROOT / ".github/workflows/process-game-release.yml").read_text(encoding="utf-8")

        for removed_input in (
            "benchmark_percent:",
            "light:",
            "runner_profile:",
            "until:",
            "workers:",
        ):
            self.assertNotIn(removed_input, workflow)
        self.assertIn(
            "      publish_wot_src:\n"
            "        description: Publish the snapshot to wot-src\n"
            "        required: true\n"
            "        default: true\n"
            "        type: boolean",
            workflow,
        )
        self.assertIn(
            "      publish_wot_gui_assets:\n"
            "        description: Publish the snapshot to wot-gui-assets\n"
            "        required: true\n"
            "        default: true\n"
            "        type: boolean",
            workflow,
        )
        self.assertIn(
            "      publish_wotstat_assets:\n"
            "        description: Upload the snapshot with wotstat-assets-uploader\n"
            "        required: true\n"
            "        default: true\n"
            "        type: boolean",
            workflow,
        )
        self.assertIn("if: inputs.publish_wot_src &&", workflow)
        self.assertIn("if: inputs.publish_wot_gui_assets &&", workflow)
        self.assertIn("if: inputs.publish_wotstat_assets &&", workflow)
        self.assertIn("WOT_SRC_PUBLISH_EXPECTED_RESULT", workflow)
        self.assertIn("WOT_GUI_ASSETS_PUBLISH_EXPECTED_RESULT", workflow)
        self.assertIn("WOTSTAT_ASSETS_UPLOAD_EXPECTED_RESULT", workflow)
        self.assertNotIn("selectel_location", workflow)
        self.assertNotIn("ru-7a", workflow)
        self.assertNotIn("ru-9a", workflow)
        self.assertIn('SELECTEL_AVAILABILITY_ZONE: "ru-7b"', workflow)
        self.assertIn('SELECTEL_OS_REGION_NAME: "ru-7"', workflow)
        self.assertIn('SELECTEL_FLAVOR_ID: "HFL2.16-32768-256-AMD"', workflow)
        self.assertNotIn("vars.SELECTEL_FLAVOR_ID", workflow)
        self.assertIn(
            "  download:\n"
            "    name: Download and unpack (manual-${{ github.run_id }}-${{ github.run_attempt }})",
            workflow,
        )
        self.assertNotIn("wotstat/game-snapshot-builder", workflow)
        self.assertIn("run: bash .github/scripts/run-stage.sh snapshot", workflow)

    def test_provisions_and_calls_all_snapshot_consumers(self) -> None:
        workflow = (ROOT / ".github/workflows/process-game-release.yml").read_text(encoding="utf-8")

        self.assertIn('PYTHONUNBUFFERED: "1"', workflow)
        self.assertIn("downloader_runner_label", workflow)
        self.assertIn("wot_gui_assets_runner_label", workflow)
        self.assertIn("wot_src_runner_label", workflow)
        self.assertIn("wotstat_assets_runner_label", workflow)
        self.assertNotIn("WOT_GUI_ASSETS_REPOSITORY", workflow)
        self.assertNotIn("WOT_SRC_REPOSITORY", workflow)
        self.assertNotIn("permission-actions: write", workflow)
        self.assertNotIn("game-unpack-pipeline,wot-src,wot-gui-assets", workflow)
        self.assertIn("--downloader-runner-id", workflow)
        self.assertIn("--wot-gui-assets-runner-id", workflow)
        self.assertIn("--wot-src-runner-id", workflow)
        self.assertIn("--wotstat-assets-runner-id", workflow)
        self.assertNotIn("dispatch-publication", workflow)
        self.assertIn(
            "uses: wotstat/wot-src/.github/workflows/publish-snapshot.yml@main",
            workflow,
        )
        self.assertIn(
            "uses: wotstat/wot-gui-assets/.github/workflows/publish-snapshot.yml@main",
            workflow,
        )
        self.assertIn(
            "uses: wotstat/wotstat-assets-uploader/.github/workflows/upload-snapshot.yml@main",
            workflow,
        )
        self.assertEqual(
            workflow.count("secrets:\n      GH_APP_PRIVATE_KEY: ${{ secrets.GH_APP_PRIVATE_KEY }}"),
            2,
        )
        self.assertNotIn("secrets: inherit", workflow)
        self.assertFalse((ROOT / ".github/workflows/publish-snapshot.yml").exists())
        self.assertIn("publish-wot-gui-assets:", workflow)
        self.assertIn("publish-wot-src:", workflow)
        self.assertIn("publish-wotstat-assets:", workflow)
        self.assertIn("configuration_environment: wotstat-assets-uploader", workflow)
        self.assertNotIn("wotstat-assets-tmp", workflow)
        self.assertIn("notify:", workflow)
        self.assertIn("needs.cleanup.outputs.deleted_count", workflow)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", workflow)
        self.assertIn("secrets.TELEGRAM_CHAT_ID", workflow)
        self.assertIn("environment: telegram", workflow)
        self.assertIn("READABLE_VERSION: ${{ needs.download.outputs.readable_version }}", workflow)
        self.assertNotIn("VERSION_NAME: ${{ needs.download.outputs.version_name }}", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("PIPELINE_STARTED_AT: ${{ steps.timing.outputs.started_at }}", workflow)
        self.assertIn("format: html", workflow)
        self.assertIn("message: ${{ steps.report.outputs.message }}", workflow)

        reconciler = (ROOT / ".github/workflows/reconcile-release-resources.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("game-unpack-pipeline,wot-src,wot-gui-assets", reconciler)
        self.assertNotIn("WOT_GUI_ASSETS_REPOSITORY", reconciler)
        self.assertNotIn("WOT_SRC_REPOSITORY", reconciler)
        self.assertIn("for selectel_region in ru-7 ru-9", reconciler)
        self.assertIn("fromJSON(needs.reconcile.outputs.deleted_count || '0') > 0", reconciler)
        self.assertIn("environment: telegram", reconciler)


if __name__ == "__main__":
    unittest.main()

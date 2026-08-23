from __future__ import annotations

import base64
import http.client
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runner_lifecycle as lifecycle  # noqa: E402


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
        self.assertEqual(identity.runner_label, "gup-manual-123456-2")
        self.assertEqual(identity.scope_label, "gup-run-123456-2")
        self.assertEqual(
            identity.descriptor,
            "game-unpack-pipeline;repository=wotstat/game-unpack-pipeline;"
            "run_id=123456;run_attempt=2;instance_key=manual-123456-2",
        )

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
        job = {
            "name": "Workload / Build snapshot (wot-eu, sd)",
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
            "properties": ", ".join(
                f"{key}='{value}'" for key, value in properties.items()
            ),
        }

        self.assertTrue(
            lifecycle.server_has_ownership_markers(mapping_server, self.identity)
        )
        self.assertTrue(
            lifecycle.server_has_ownership_markers(serialized_server, self.identity)
        )

    def test_rejects_server_with_only_matching_name(self) -> None:
        server = {"name": self.identity.server_name, "properties": {}}
        self.assertFalse(lifecycle.server_has_ownership_markers(server, self.identity))


class CloudConfigTests(unittest.TestCase):
    def test_jit_config_is_encoded_and_bootstrap_runs_as_root(self) -> None:
        template = (ROOT / "scripts" / "bootstrap-actions-runner.sh").read_text()
        jit_config = "sensitive-jit-configuration"
        cloud_config = lifecycle.render_cloud_config(
            template,
            runner_download_url=(
                "https://github.com/actions/runner/releases/download/v2.999.0/"
                "actions-runner-linux-x64-2.999.0.tar.gz"
            ),
            runner_sha256="a" * 64,
            runner_version="2.999.0",
            runner_jit_config=jit_config,
        )

        self.assertTrue(cloud_config.startswith("#cloud-config\n"))
        self.assertNotIn(jit_config, cloud_config)
        encoded = re.search(r"^    content: (\S+)$", cloud_config, re.MULTILINE)
        self.assertIsNotNone(encoded)
        bootstrap = base64.b64decode(encoded.group(1)).decode()
        self.assertIn("RUNNER_JIT_CONFIG=sensitive-jit-configuration", bootstrap)
        self.assertNotIn("readonly RUNNER_JIT_CONFIG=", bootstrap)
        self.assertIn("unset RUNNER_JIT_CONFIG", bootstrap)
        self.assertIn("RUNNER_ALLOW_RUNASROOT=1", bootstrap)
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
            patch.object(
                lifecycle.urllib.request,
                "urlopen",
                side_effect=[http.client.RemoteDisconnected(), response],
            ) as urlopen,
            patch.object(lifecycle.time, "sleep") as sleep,
        ):
            status, payload, _ = lifecycle.http_json(
                "GET", "https://api.example.test/v1/public_ports"
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ports": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()


class WorkflowContractTests(unittest.TestCase):
    def test_coerces_cli_worker_input_before_reusable_workflow_call(self) -> None:
        workflow = (ROOT / ".github/workflows/ephemeral-light-snapshot.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workers: ${{ fromJSON(inputs.workers) }}", workflow)
        self.assertIn(
            "benchmark_percent: ${{ fromJSON(inputs.benchmark_percent) }}",
            workflow,
        )
        self.assertIn("until: ${{ inputs.until }}", workflow)
        self.assertIn("profile_stages: true", workflow)
        self.assertIn(
            "wotstat/game-snapshot-builder/.github/workflows/build-snapshot.yml@v0.3.13",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()

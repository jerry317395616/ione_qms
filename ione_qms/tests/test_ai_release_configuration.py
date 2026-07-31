from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from ione_qms.ai import release


class _Document(dict):
	def __init__(self, name: str, **values) -> None:
		super().__init__(values)
		self.name = name


class TestAIAgentReleaseConfiguration(TestCase):
	def _configuration(self, extra_params: str) -> dict:
		policy = _Document(
			"POLICY-1",
			policy_code="POL-1",
			agent_category="Quality Analysis",
			flow_agent="AGENT-1",
			service_user="agent@example.test",
			allowed_tools=[],
			allowed_hospitals=[],
			allowed_departments=[],
			contains_patient_data=0,
			requires_human_review=1,
			maximum_records_per_run=10,
			allow_auto_approve=0,
			allow_scheduled_run=0,
		)
		agent = _Document(
			"AGENT-1",
			title="Reviewed Agent",
			model="MODEL-1",
			enabled=1,
			max_iterations=8,
			instructions="Use governed evidence.",
			tools=[],
		)
		model = _Document(
			"MODEL-1",
			model_id="openai/qwen3.6-35b-a3b-fp8",
			provider="openai",
			base_url=None,
			enabled=1,
			context_window=32_768,
			params='{"temperature": 0.1, "max_tokens": 2048}',
		)
		provider = _Document(
			"openai",
			enabled=1,
			base_url="http://models.internal:1234/v1",
			extra_params=extra_params,
			api_key="must-never-enter-the-release",
		)
		with (
			patch.object(release, "assert_ione_tool_integrity"),
			patch.object(release, "tool_registry_fingerprint", return_value="fingerprint"),
			patch.object(release, "live_tool_schema_manifest", return_value=[]),
			patch.object(
				release,
				"runtime_commit_manifest",
				return_value={
					"flow": "2" * 40,
					"frappe": "1" * 40,
					"ione_qms": "3" * 40,
				},
			),
			patch.object(release.frappe.db, "exists", return_value=True),
			patch.object(release.frappe, "get_doc", return_value=provider),
		):
			return release.build_release_configuration(policy, agent, model)

	def test_provider_extra_params_are_merged_and_secret_free(self) -> None:
		configuration = self._configuration('{"temperature": 0.8, "top_p": 0.9, "timeout": 45}')
		self.assertEqual(configuration["contract_version"], 3)
		self.assertEqual(configuration["provider"]["name"], "openai")
		self.assertEqual(configuration["provider"]["extra_params"]["top_p"], 0.9)
		self.assertEqual(configuration["model"]["effective_params"]["temperature"], 0.1)
		self.assertEqual(configuration["model"]["effective_params"]["top_p"], 0.9)
		self.assertNotIn("must-never-enter-the-release", release.canonical_json(configuration))

	def test_provider_parameter_drift_changes_release_configuration(self) -> None:
		first = self._configuration('{"top_p": 0.8}')
		second = self._configuration('{"top_p": 0.9}')
		self.assertNotEqual(release.canonical_json(first), release.canonical_json(second))

	def test_configured_qms_sha_must_match_actual_git_head(self) -> None:
		configured = "a" * 40
		actual = "b" * 40
		with (
			patch.object(release.frappe, "conf", {"ione_qms_commit_sha": configured}),
			patch.object(release, "_actual_app_commit_sha", return_value=actual),
			patch.object(
				release.frappe,
				"throw",
				side_effect=RuntimeError("configured SHA mismatch"),
			),
			self.assertRaisesRegex(RuntimeError, "configured SHA mismatch"),
		):
			release.current_app_commit_sha()

	def test_runtime_manifest_pins_required_and_installed_optional_apps(self) -> None:
		commits = {
			"ione_qms": "0" * 40,
			"frappe": "1" * 40,
			"flow": "2" * 40,
			"drive": "3" * 40,
		}
		with (
			patch.object(release.frappe, "get_installed_apps", return_value=["flow", "drive"]),
			patch.object(release, "current_app_commit_sha", return_value=commits["ione_qms"]),
			patch.object(
				release,
				"_actual_app_commit_sha",
				side_effect=lambda app_name: commits[app_name],
			),
		):
			self.assertEqual(release.runtime_commit_manifest(), commits)

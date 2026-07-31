from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(*parts: str) -> str:
	return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def metadata(doctype: str) -> dict:
	scrubbed = doctype.lower().replace(" ", "_")
	return json.loads(
		ROOT.joinpath(
			"ione_qms",
			"ione_improvement",
			"doctype",
			scrubbed,
			f"{scrubbed}.json",
		).read_text(encoding="utf-8")
	)


class TestPDCARecurrenceStaticContract(unittest.TestCase):
	def test_governed_metadata_is_complete_and_materialized_records_are_read_only(self) -> None:
		expected = {
			"IONE Finding Recurrence Policy",
			"IONE Finding Recurrence Run",
			"IONE Finding Recurrence Evaluation",
			"IONE Pareto Cause Item",
			"IONE PDCA Effect Measurement",
		}
		for name in expected:
			self.assertEqual(metadata(name)["name"], name)
		self.assertEqual(metadata("IONE Finding Recurrence Policy")["autoname"], "field:policy_code")
		self.assertEqual(metadata("IONE Finding Recurrence Run")["autoname"], "field:run_key")
		self.assertEqual(
			metadata("IONE Finding Recurrence Evaluation")["autoname"],
			"field:evaluation_key",
		)
		for name in ("IONE Finding Recurrence Run", "IONE Finding Recurrence Evaluation"):
			self.assertTrue(
				all(not row.get("create") and not row.get("write") for row in metadata(name)["permissions"])
			)

	def test_policy_has_no_enabled_default_or_hospital_threshold_guess(self) -> None:
		policy = metadata("IONE Finding Recurrence Policy")
		fields = {row["fieldname"]: row for row in policy["fields"]}
		self.assertEqual(fields["enabled"]["default"], "0")
		self.assertNotIn("default", fields["threshold"])
		self.assertNotIn("default", fields["window_days"])
		self.assertEqual(
			fields["grouping_mode"]["options"],
			"Rule and Approved Organizational Scope",
		)
		service = source("ione_qms", "services", "finding_recurrence.py")
		self.assertIn("requester and reviewer must be different named users", service)
		self.assertIn("_assert_no_overlapping_policy(doc)", service)
		self.assertIn("Requested or reviewed recurrence policy definitions are immutable", service)

	def test_scan_is_bounded_human_confirmed_and_does_not_select_patient_or_narrative(self) -> None:
		service = source("ione_qms", "services", "finding_recurrence.py")
		self.assertIn("MAX_FINDINGS_PER_POLICY_RUN = 5_000", service)
		self.assertIn("MAX_GROUPS_PER_POLICY_RUN = 500", service)
		self.assertIn('["detected_at", "<"', service)
		self.assertIn('"to_status":"Confirmed"', service)
		self.assertIn('payload.get("reviewed_by") == "Administrator"', service)
		query_fields = service.split("fields=[", 1)[1].split("],", 1)[0]
		for forbidden in ("patient", "encounter", "description", "title", "evidence_text"):
			self.assertNotIn(f'"{forbidden}"', query_fields)

	def test_evaluations_and_special_projects_are_immutable_and_duplicate_safe(self) -> None:
		service = source("ione_qms", "services", "finding_recurrence.py")
		self.assertIn("recurrence_case_key", service)
		self.assertIn("active_recurrence_key", service)
		self.assertIn("frappe.DuplicateEntryError", service)
		self.assertIn("_existing_run_response", service)
		self.assertIn("finding_links_json", service)
		self.assertIn("recurrence_snapshot_hash", service)
		self.assertNotIn('"patient":', service)
		replay = service.split("def _existing_run_response", 1)[1].split(
			"\ndef ",
			1,
		)[0]
		self.assertIn("_validate_recurrence_run_snapshot", replay)
		self.assertIn('str(item["project"])', replay)
		self.assertNotIn('"projects": []', replay)

	def test_five_why_and_fishbone_have_structured_validation(self) -> None:
		service = source("ione_qms", "services", "pdca_structure.py")
		self.assertIn('ROOT_CAUSE_METHODS = frozenset({"Five Why", "Fishbone", "Other"})', service)
		self.assertIn("parent_cause_code", service)
		self.assertIn("immediately preceding level", service)
		self.assertIn("FISHBONE_CATEGORIES", service)
		self.assertIn("Fishbone causes require a supported fishbone category", service)

	def test_pareto_rank_count_and_cumulative_percentage_are_deterministic(self) -> None:
		service = source("ione_qms", "services", "pdca_structure.py")
		self.assertIn("key=lambda item: (-int(item[1].occurrence_count), item[0])", service)
		self.assertIn("cumulative += int(row.occurrence_count)", service)
		self.assertIn('Decimal("0.01")', service)
		self.assertIn("rank, cumulative count, or cumulative percent is not deterministic", service)

	def test_effect_measurement_and_standardization_gate_measuring_and_closed(self) -> None:
		service = source("ione_qms", "services", "pdca_structure.py")
		self.assertIn("baseline_value", service)
		self.assertIn("target_value", service)
		self.assertIn("actual_value", service)
		self.assertIn("source scope does not match the PDCA project", service)
		self.assertIn("project owner cannot independently verify", service.lower())
		self.assertIn("PDCA closure requires a complete immutable standardization", service)
		self.assertIn('transition == ("Active", "Measuring")', service)
		self.assertIn('transition == ("Measuring", "Closed")', service)
		self.assertIn("_validated_frozen_effect_hashes", service)
		frozen = service.split("def _validated_frozen_effect_hashes", 1)[1].split("\ndef ", 1)[0]
		self.assertNotIn("_indicator_result_snapshot(", frozen)
		self.assertIn("_HEX64.fullmatch(source_hash)", frozen)
		self.assertIn("scope is immutable once effect evidence is verified", service)

	def test_hooks_permissions_scope_export_and_workspace_are_wired(self) -> None:
		hooks = source("ione_qms", "hooks.py")
		permissions = source("ione_qms", "permissions.py")
		scope = source("ione_qms", "services", "scope_hierarchy.py")
		export = source("ione_qms", "services", "data_export.py")
		workspace = source("tools", "generate_workspaces.py")
		for name in (
			"IONE Finding Recurrence Policy",
			"IONE Finding Recurrence Run",
			"IONE Finding Recurrence Evaluation",
		):
			self.assertIn(name, hooks)
			self.assertIn(name, scope)
			self.assertIn(name, export)
			self.assertIn(name, workspace)
		self.assertIn("finding_recurrence_policy_query", permissions)
		self.assertIn("dispatch_finding_recurrence_scans", hooks)

	def test_mutating_api_is_post_only_and_desk_javascript_is_valid(self) -> None:
		api = source("ione_qms", "api", "pdca.py")
		self.assertEqual(api.count('@frappe.whitelist(methods=["POST"])'), 6)
		hooks = source("ione_qms", "hooks.py")
		for script in ("finding_recurrence_policy.js", "pdca_project.js"):
			self.assertIn(script, hooks)
			javascript = ROOT / "ione_qms" / "public" / "js" / script
			self.assertIn('type: "POST"', javascript.read_text(encoding="utf-8"))
			node = shutil.which("node")
			self.assertIsNotNone(node, "Node.js is required for Desk JavaScript syntax validation")
			result = subprocess.run(  # noqa: S603 - node path is resolved from the trusted PATH
				[node, "--check", str(javascript)],
				capture_output=True,
				text=True,
				check=False,
			)
			self.assertEqual(result.returncode, 0, result.stderr)

	def test_generator_and_validator_include_all_recurrence_and_pdca_artifacts(self) -> None:
		generator = source("tools", "generate_doctypes.py")
		validator = source("tools", "validate_app.py")
		for name in (
			"IONE Finding Recurrence Policy",
			"IONE Finding Recurrence Run",
			"IONE Finding Recurrence Evaluation",
			"IONE Pareto Cause Item",
			"IONE PDCA Effect Measurement",
		):
			self.assertIn(name, generator)
			self.assertIn(name, validator)


if __name__ == "__main__":
	unittest.main()

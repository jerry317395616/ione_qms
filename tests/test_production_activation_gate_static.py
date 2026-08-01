from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ione_qms"


class TestProductionActivationGateStatic(TestCase):
	def _doctype(self, relative: str) -> dict:
		return json.loads((PACKAGE / relative).read_text(encoding="utf-8"))

	def test_all_word_spec_production_gates_are_mandatory_and_exactly_mirrored(self) -> None:
		service = (PACKAGE / "services" / "production_readiness.py").read_text(encoding="utf-8")
		contracts = (PACKAGE / "production_contracts.py").read_text(encoding="utf-8")
		child = self._doctype(
			"ione_administration/doctype/ione_production_gate_evidence/ione_production_gate_evidence.json"
		)
		gate_field = next(field for field in child["fields"] if field["fieldname"] == "gate_code")
		gate_codes = set(gate_field["options"].splitlines())
		expected = {
			"release_integrity",
			"hospital_scope",
			"source_contracts",
			"clinical_content",
			"data_reconciliation",
			"ai_governance",
			"identity_access",
			"security_privacy",
			"performance_capacity",
			"high_availability",
			"backup_restore",
			"clinical_uat",
			"change_approval",
			"oncall_observability",
		}
		self.assertEqual(gate_codes, expected)
		for gate_code in expected:
			self.assertIn(f'\t"{gate_code}",', contracts)
		self.assertIn("from ione_qms.production_contracts import", service)
		self.assertIn("REQUIRED_GATE_CODES", service)
		self.assertIn("exactly {len(REQUIRED_GATE_CODES)} gate rows", service)
		self.assertIn("seen != set(REQUIRED_GATE_CODES)", service)

	def test_activation_and_assessment_cannot_be_created_by_administrator_permissions(self) -> None:
		assessment = self._doctype(
			"ione_administration/doctype/ione_production_readiness_assessment/"
			"ione_production_readiness_assessment.json"
		)
		event = self._doctype(
			"ione_administration/doctype/ione_production_activation_event/"
			"ione_production_activation_event.json"
		)
		assessment_system = next(row for row in assessment["permissions"] if row["role"] == "System Manager")
		self.assertEqual(assessment_system["create"], 0)
		self.assertEqual(assessment_system["write"], 0)
		for row in event["permissions"]:
			self.assertEqual(row["create"], 0)
			self.assertEqual(row["write"], 0)
			self.assertEqual(row["delete"], 0)

	def test_system_settings_activation_event_is_read_only_and_hooks_are_fail_closed(self) -> None:
		settings = self._doctype("ione_administration/doctype/ione_system_settings/ione_system_settings.json")
		field = next(
			field for field in settings["fields"] if field["fieldname"] == "production_activation_event"
		)
		self.assertEqual(field["fieldtype"], "Link")
		self.assertEqual(field["options"], "IONE Production Activation Event")
		self.assertEqual(field["read_only"], 1)
		hooks = (PACKAGE / "hooks.py").read_text(encoding="utf-8")
		self.assertIn('"IONE Production Readiness Assessment": {', hooks)
		self.assertIn("validate_production_readiness_assessment", hooks)
		self.assertIn('"IONE Production Activation Event": {', hooks)
		self.assertIn("validate_production_activation_event", hooks)
		self.assertIn("validate_append_only", hooks)
		self.assertIn("prevent_delete", hooks)

	def test_all_state_changes_are_post_only_and_use_named_separated_actors(self) -> None:
		api = (PACKAGE / "api" / "production.py").read_text(encoding="utf-8")
		service = (PACKAGE / "services" / "production_readiness.py").read_text(encoding="utf-8")
		self.assertEqual(api.count('@frappe.whitelist(methods=["POST"])'), 6)
		for required in (
			"Production readiness governance requires a named accountable user.",
			"The assessor cannot independently approve the same assessment.",
			"independent of the assessor and reviewer",
			"may only be created by the governed API",
		):
			self.assertIn(required, service)

	def test_runtime_switches_reverify_current_activation_and_kill_switch_dependents(self) -> None:
		runtime = (PACKAGE / "services" / "runtime_settings.py").read_text(encoding="utf-8")
		service = (PACKAGE / "services" / "production_readiness.py").read_text(encoding="utf-8")
		self.assertIn("production_activation_is_current()", runtime)
		self.assertIn("doc.enable_ai = 0", runtime)
		self.assertIn("doc.enable_realtime_rules = 0", runtime)
		self.assertIn("assert_activation_event_authorizes", runtime)
		self.assertIn("The production activation event record hash is invalid.", service)
		self.assertIn('active_hmac_key("ione_production_hmac")', service)
		self.assertIn('verification_hmac_key("ione_production_hmac"', service)
		self.assertIn("Production readiness governance HMAC signature is invalid.", service)
		self.assertIn("does not match the running app commits", service)
		self.assertIn("The production evidence manifest hash has changed.", service)

	def test_submitted_manifest_files_are_private_immutable_and_bounded(self) -> None:
		file_override = (PACKAGE / "overrides" / "file.py").read_text(encoding="utf-8")
		service = (PACKAGE / "services" / "production_readiness.py").read_text(encoding="utf-8")
		self.assertIn("validate_production_evidence_manifest_file(self)", file_override)
		self.assertIn("prevent_production_evidence_manifest_deletion(self)", file_override)
		self.assertIn("MAX_MANIFEST_BYTES = 1024 * 1024", service)
		self.assertIn('"/private/files/"', service)
		self.assertIn("Submitted production evidence manifest files are immutable.", service)

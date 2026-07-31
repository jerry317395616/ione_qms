from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ione_qms"


def _source(*parts: str) -> str:
	return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
	tree = ast.parse(source)
	node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
	return ast.get_source_segment(source, node) or ""


def _literal_assignment(source: str, name: str):
	tree = ast.parse(source)
	for node in tree.body:
		target = None
		if isinstance(node, ast.Assign):
			target = next(
				(item for item in node.targets if isinstance(item, ast.Name) and item.id == name),
				None,
			)
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			target = node.target if node.target.id == name else None
		if target is not None:
			return ast.literal_eval(node.value)
	raise AssertionError(f"{name} assignment not found")


def _generator_module():
	path = ROOT / "tools" / "generate_doctypes.py"
	spec = importlib.util.spec_from_file_location("ione_generator_contract", path)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	module._extend_indicator_schemas()
	return module


class TestRuleTestShadowAuthorityStatic(TestCase):
	def test_generator_defines_minimal_immutable_receipt_contracts(self) -> None:
		generator = _generator_module()
		schemas = generator.SCHEMAS
		for doctype in (
			"IONE QC Rule Test Execution",
			"IONE QC Rule Shadow Execution",
			"IONE QC Rule Shadow Feedback",
			"IONE Definition Lineage Receipt",
		):
			self.assertIn(doctype, schemas)
		test_fields = {field["fieldname"]: field for field in schemas["IONE QC Rule Test Case"]["fields"]}
		self.assertEqual(
			set(test_fields["sample_type"]["options"].splitlines()),
			{
				"Positive",
				"Negative",
				"Exclusion",
				"Boundary",
				"Missing Data",
				"Real World",
				"Regression",
			},
		)
		self.assertTrue(test_fields["definition_hash"]["read_only"])
		execution_fields = {field["fieldname"] for field in schemas["IONE QC Rule Test Execution"]["fields"]}
		self.assertTrue(
			{
				"rule_checksum",
				"definition_hash",
				"input_hash",
				"expected_hash",
				"output_hash",
				"executor_version",
				"record_hash",
			}.issubset(execution_fields)
		)
		validation_fields = {
			field["fieldname"]: field for field in schemas["IONE QC Rule Validation Run"]["fields"]
		}
		self.assertEqual(validation_fields["run_type"]["options"], "Historical Replay")
		self.assertIn("outcome_manifest_json", validation_fields)
		self.assertIn("executor_version", validation_fields)
		feedback_fields = {
			field["fieldname"]: field for field in schemas["IONE QC Rule Shadow Feedback"]["fields"]
		}
		self.assertTrue({"hospital", "campus", "department", "ward", "gold_label"}.issubset(feedback_fields))
		self.assertTrue(feedback_fields["assessment"]["read_only"])
		signed_chain_fields = {
			"receipt_chain_scope",
			"receipt_chain_sequence",
			"receipt_chain_key",
			"previous_receipt",
			"previous_receipt_hmac",
			"signature_version",
			"hmac_key_id",
			"receipt_hmac",
		}
		for doctype in (
			"IONE QC Rule Validation Run",
			"IONE QC Rule Shadow Execution",
			"IONE QC Rule Shadow Feedback",
		):
			fields = {field["fieldname"]: field for field in schemas[doctype]["fields"]}
			self.assertTrue(signed_chain_fields.issubset(fields))
			for fieldname in signed_chain_fields:
				self.assertTrue(fields[fieldname]["read_only"])
			self.assertTrue(fields["receipt_chain_key"]["unique"])
			self.assertTrue(fields["receipt_hmac"]["reqd"])

	def test_case_mutation_resets_projection_and_gate_binds_exact_receipt(self) -> None:
		source = _source("ione_qms", "rule_engine", "testing.py")
		validator = _function(source, "validate_rule_test_case")
		reset = _function(source, "_reset_test_projection")
		gate = _function(source, "require_current_rule_test_receipts")
		self.assertIn("_lock_rule_versions", validator)
		for fieldname in (
			"actual_result",
			"test_status",
			"result_json",
			"last_run_at",
			"latest_execution_receipt",
		):
			self.assertIn(fieldname, reset)
		for binding in (
			"rule_checksum",
			"definition_hash",
			"input_hash",
			"expected_hash",
			"output_hash",
			"executor_version",
			"record_hash",
		):
			self.assertIn(binding, gate)
		self.assertIn("RULE_TEST_SAMPLE_TYPES.difference", gate)
		self.assertIn("cannot treat an execution Error as passing", gate)
		self.assertIn("current_executor_version", gate)
		self.assertIn("!= current_executor_version", gate)
		self.assertIn("_receipt_record_hash_is_valid", gate)

	def test_shadow_is_online_rollback_only_and_has_no_action_path(self) -> None:
		source = _source("ione_qms", "rule_engine", "executor.py")
		select_shadow = _function(source, "_get_shadow_rules")
		save_shadow = _function(source, "_save_shadow_execution")
		shadow_evaluator = _function(source, "_execute_shadow_rule_version")
		event_evaluator = _function(source, "_evaluate_event")
		self.assertIn("rv.status = 'Shadow Run'", select_shadow)
		self.assertIn("shadow_observation_start", select_shadow)
		self.assertIn("shadow_observation_end", select_shadow)
		self.assertIn("for update", save_shadow.lower())
		self.assertNotIn("_create_candidate_finding", save_shadow)
		self.assertNotIn("_notify_rule_alert", save_shadow)
		self.assertIn('!= "Deterministic"', shadow_evaluator)
		self.assertNotIn("get_rule(", shadow_evaluator)
		self.assertNotIn("side_effect_free", shadow_evaluator)
		self.assertIn("rollback(save_point=evaluation_savepoint)", event_evaluator)
		self.assertIn('"ManualEncounterEvaluation"', event_evaluator)
		self.assertIn('"mode": "Shadow"', event_evaluator)
		self.assertIn('"action": "Measure Only"', event_evaluator)

	def test_shadow_gate_requires_window_feedback_and_zero_safety_defects(self) -> None:
		source = _source("ione_qms", "rule_engine", "validation.py")
		gate = _function(source, "require_online_shadow_observation")
		feedback = _function(source, "validate_shadow_feedback")
		for token in (
			"shadow_min_events",
			"shadow_min_feedback",
			"shadow_max_false_positive_rate",
			"shadow_max_false_negative_rate",
			"shadow_min_feedback_coverage_rate",
			"shadow_min_passed_feedback",
			"shadow_min_excluded_feedback",
			"False Negative",
			"Missing Data",
			"Rule Error",
			"Needs Review",
		):
			self.assertIn(token, source)
		self.assertIn("observation window has not completed", gate)
		self.assertIn("_verify_shadow_execution_receipt", gate)
		self.assertIn("_verify_shadow_feedback_receipt", gate)
		self.assertNotIn("_deterministic_feedback_sample", source)
		self.assertIn("verify_receipt_chain", gate)
		self.assertIn("required_names", gate)
		self.assertIn("RuleResult.PASSED.value", gate)
		self.assertIn("RuleResult.EXCLUDED.value", gate)
		self.assertIn("full-population independent gold feedback", gate)
		self.assertIn("reviewed_count != event_count", gate)
		self.assertIn("false_negative_rate", gate)
		self.assertIn("gold_positive_reviews", gate)
		contract = _function(source, "validate_shadow_observation_contract")
		self.assertIn("minimum_feedback_coverage_rate != 100", contract)
		self.assertIn("for update", feedback.lower())
		self.assertIn("stale evaluator commit", feedback)
		self.assertIn('!= "Shadow Run"', feedback)
		self.assertIn("gold_label", feedback)
		self.assertIn("prepare_signed_receipt", feedback)

	def test_release_gates_recompute_receipts_instead_of_accepting_hash_shape(self) -> None:
		source = _source("ione_qms", "rule_engine", "validation.py")
		canonical = _function(source, "_verify_canonical_receipt")
		replay = _function(source, "_verify_validation_run_receipt")
		execution = _function(source, "_verify_shadow_execution_receipt")
		feedback = _function(source, "_verify_shadow_feedback_receipt")
		self.assertIn("canonical_record_hash_values", canonical)
		self.assertIn("hmac.compare_digest", canonical)
		for token in (
			"outcome_manifest_json",
			"sample_hash",
			"result_hash",
			"run_key",
			"row_counts",
			"source_semantic_hash",
			"_validation_events",
			"_build_context",
			"_execute_shadow_rule_version",
			"verify_signed_receipt",
			"verify_receipt_chain",
		):
			self.assertIn(token, replay)
		self.assertIn("execution_key", execution)
		self.assertIn("event_hash", execution)
		self.assertIn("context_hash", execution)
		self.assertIn("_build_context", execution)
		self.assertIn("_execute_shadow_rule_version", execution)
		self.assertIn("verify_signed_receipt", execution)
		self.assertIn("feedback_key", feedback)
		self.assertIn("gold_label", feedback)
		self.assertIn("verify_signed_receipt", feedback)

	def test_shadow_receipt_failure_logging_stays_outside_the_exception_handler(self) -> None:
		source = _source("ione_qms", "rule_engine", "executor.py")
		evaluator = _function(source, "_evaluate_event")
		self.assertLess(
			evaluator.index("shadow_persistence_error_code = type(exc).__name__"),
			evaluator.index("if shadow_persistence_error_code:"),
		)
		self.assertLess(
			evaluator.index("if shadow_persistence_error_code:"),
			evaluator.index("frappe.log_error(", evaluator.index("if shadow_persistence_error_code:")),
		)
		tree = ast.parse(evaluator)
		matching_handlers = [
			handler
			for handler in ast.walk(tree)
			if isinstance(handler, ast.ExceptHandler)
			and "shadow_persistence_error_code" in (ast.get_source_segment(evaluator, handler) or "")
		]
		self.assertEqual(len(matching_handlers), 1)
		self.assertFalse(
			any(
				isinstance(node, ast.Call)
				and isinstance(node.func, ast.Attribute)
				and isinstance(node.func.value, ast.Name)
				and node.func.value.id == "frappe"
				and node.func.attr == "log_error"
				for node in ast.walk(matching_handlers[0])
			)
		)

	def test_parent_freeze_and_exact_authority_are_server_hooks(self) -> None:
		hooks = _literal_assignment(_source("ione_qms", "hooks.py"), "doc_events")
		self.assertEqual(
			hooks["IONE QC Standard"]["validate"],
			"ione_qms.services.versions.validate_standard",
		)
		self.assertEqual(
			hooks["IONE QC Indicator"]["validate"],
			"ione_qms.services.versions.validate_indicator",
		)
		self.assertEqual(
			hooks["IONE QC Standard Clause"]["on_trash"],
			"ione_qms.services.versions.prevent_standard_clause_deletion",
		)
		for doctype in (
			"IONE QC Standard Version",
			"IONE QC Rule Version",
			"IONE QC Indicator Version",
		):
			self.assertEqual(
				hooks[doctype]["on_trash"],
				"ione_qms.services.versions.prevent_definition_version_deletion",
			)
		for doctype in (
			"IONE QC Rule Test Execution",
			"IONE QC Rule Shadow Execution",
			"IONE Definition Lineage Receipt",
		):
			self.assertEqual(
				hooks[doctype]["validate"],
				"ione_qms.services.immutability.validate_append_only",
			)
		self.assertEqual(
			hooks["IONE QC Rule Test Case"]["on_trash"],
			"ione_qms.rule_engine.testing.prevent_rule_test_case_delete",
		)
		queries = _literal_assignment(
			_source("ione_qms", "hooks.py"),
			"permission_query_conditions",
		)
		self.assertEqual(
			queries["IONE QC Rule Shadow Feedback"],
			"ione_qms.permissions.rule_shadow_feedback_query",
		)
		scope_source = _source("ione_qms", "services", "scope_hierarchy.py")
		self.assertIn('"IONE QC Rule Shadow Feedback"', scope_source)

	def test_authority_checksum_and_quarantine_migration_are_fail_closed(self) -> None:
		source = _source("ione_qms", "services", "versions.py")
		authority = _function(source, "_build_authority_snapshot")
		loader = _function(source, "_load_standard_authority")
		backfill = _function(source, "backfill_definition_lineage")
		for token in (
			"source_file_hash",
			"standard_version",
			"checksum",
			"content_hash",
		):
			self.assertIn(token, authority)
		self.assertIn('approval_status") or "") != "Approved"', loader)
		self.assertIn('clause.get("status") or "") != "Published"', loader)
		self.assertIn("_standard_version_content", loader)
		self.assertIn('state == "Draft"', backfill)
		self.assertIn('"Quarantined"', backfill)
		self.assertIn("_insert_definition_lineage_receipt", backfill)
		self.assertIn("URL_ONLY_STANDARD_AUTHORITY", backfill)
		self.assertNotIn("except Exception", backfill)
		self.assertIn("def _retire_quarantined_version", source)
		lineage_states = source[
			source.index("DEFINITION_LINEAGE_ACTIVE_STATES") : source.index("MAX_SOURCE_STANDARD_FILE_BYTES")
		]
		for state in ("Expert Review", "Test", "Historical Replay"):
			self.assertIn(f'"{state}"', lineage_states)
		migration_state = _source("ione_qms", "services", "migration_state.py")
		self.assertIn('"id": "definition-authority-lineage"', migration_state)

	def test_file_mixin_guards_reviewed_standard_source_content(self) -> None:
		source = _source("ione_qms", "overrides", "file.py")
		for function_name in (
			"validate_standard_source_file",
			"prevent_standard_source_file_deletion",
		):
			self.assertIn(function_name, source)
		versions = _source("ione_qms", "services", "versions.py")
		archive = _function(versions, "archive_standard_source_url")
		downloader = _function(versions, "_download_standard_authority")
		hasher = _function(versions, "_hash_standard_source_file")
		self.assertIn("is_private=1", archive)
		self.assertIn("source_file_hash", archive)
		self.assertIn("ione_standard_archive_allowed_hosts", downloader)
		network_guard = _source("ione_qms", "services", "standard_archive.py")
		for token in (
			"address.is_global",
			"_PinnedHTTPSConnection",
			"server_hostname=self.host",
			"SAFE_STANDARD_MEDIA_TYPES",
			"_validate_docx_archive",
		):
			self.assertIn(token, network_guard)
		self.assertNotIn('"text/html"', network_guard)
		self.assertNotIn('"image/svg+xml"', network_guard)
		self.assertIn('"/private/files/"', hasher)

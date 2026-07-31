from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ione_qms.rule_engine import executor, testing, validation
from ione_qms.rule_engine.models import RuleEvaluation, RuleResult


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class _Doc:
	def __init__(self, values: dict, previous: dict | None = None):
		object.__setattr__(self, "_values", values)
		object.__setattr__(self, "_previous", previous)
		object.__setattr__(self, "name", values.get("name", "TEST-1"))
		object.__setattr__(self, "doctype", values.get("doctype", "IONE QC Rule Test Case"))

	def get(self, fieldname: str):
		return self._values.get(fieldname)

	def set(self, fieldname: str, value) -> None:
		self._values[fieldname] = value

	def get_doc_before_save(self):
		return self._previous

	def __getattr__(self, fieldname: str):
		try:
			return self._values[fieldname]
		except KeyError as exc:
			raise AttributeError(fieldname) from exc

	def __setattr__(self, fieldname: str, value) -> None:
		if fieldname.startswith("_") or fieldname in {"name", "doctype"}:
			object.__setattr__(self, fieldname, value)
		else:
			self._values[fieldname] = value


class TestRuleTestShadowGovernance(TestCase):
	def test_definition_change_resets_all_mutable_projection_fields(self) -> None:
		previous = {
			"rule_version": "RULE-v1",
			"case_name": "Positive",
			"sample_type": "Positive",
			"input_json": '{"risk":1}',
			"expected_result": "Failed",
			"definition_hash": "a" * 64,
			"actual_result": "Failed",
			"test_status": "Passed",
			"result_json": "{}",
			"last_run_at": "2026-07-01",
			"latest_execution_receipt": "RECEIPT-1",
		}
		doc = _Doc(
			{
				**previous,
				"input_json": '{"risk":2}',
			},
			previous,
		)
		with (
			patch.object(testing.frappe.db, "sql"),
			patch.object(testing.frappe.db, "get_value", return_value="Test"),
		):
			testing.validate_rule_test_case(doc)
		self.assertEqual(doc.get("test_status"), "Not Run")
		for fieldname in (
			"actual_result",
			"result_json",
			"last_run_at",
			"latest_execution_receipt",
		):
			self.assertIsNone(doc.get(fieldname))
		self.assertNotEqual(doc.get("definition_hash"), previous["definition_hash"])

	def test_case_is_frozen_after_version_leaves_test(self) -> None:
		doc = _Doc(
			{
				"rule_version": "RULE-v1",
				"case_name": "Regression",
				"sample_type": "Regression",
				"input_json": "{}",
				"expected_result": "Passed",
			}
		)
		with (
			patch.object(testing.frappe.db, "sql"),
			patch.object(testing.frappe.db, "get_value", return_value="Historical Replay"),
			patch.object(testing.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "frozen after"),
		):
			testing.validate_rule_test_case(doc)

	def test_current_receipt_gate_requires_all_categories_and_exact_checksum(self) -> None:
		version = frappe._dict(name="RULE-v1", checksum="c" * 64)
		rows = []
		receipts: dict[str, frappe._dict] = {}
		expected_by_type = {
			"Positive": "Failed",
			"Negative": "Passed",
			"Exclusion": "Excluded",
			"Boundary": "Passed",
			"Missing Data": "Insufficient Data",
			"Real World": "Passed",
			"Regression": "Passed",
		}
		for index, (sample_type, expected) in enumerate(expected_by_type.items(), start=1):
			name = f"CASE-{index}"
			input_json = f'{{"case":{index}}}'
			definition_hash = testing.rule_test_definition_hash(
				rule_version=version.name,
				case_name=name,
				sample_type=sample_type,
				input_json={"case": index},
				expected_result=expected,
			)
			receipt_name = f"RECEIPT-{index}"
			rows.append(
				frappe._dict(
					name=name,
					case_name=name,
					sample_type=sample_type,
					input_json=input_json,
					expected_result=expected,
					definition_hash=definition_hash,
					test_status="Passed",
					latest_execution_receipt=receipt_name,
				)
			)
			receipts[receipt_name] = frappe._dict(
				test_case=name,
				rule_version=version.name,
				rule_checksum=version.checksum,
				sample_type=sample_type,
				definition_hash=definition_hash,
				input_hash=testing._hash_payload({"case": index}),
				expected_result=expected,
				expected_hash=testing._hash_payload({"expected_result": expected}),
				actual_result=expected,
				output_hash="a" * 64,
				executor_version="IONE_RULE_EVALUATOR_V2:" + "f" * 40,
				lifecycle_state="Test",
				passed=1,
				record_hash="b" * 64,
			)

		def get_value(_doctype, name, _fields, **_kwargs):
			return receipts[name]

		executor_version = "IONE_RULE_EVALUATOR_V2:" + "f" * 40
		with (
			patch.object(testing.frappe.db, "exists", return_value=True),
			patch.object(testing.frappe, "get_all", return_value=rows),
			patch.object(testing.frappe.db, "get_value", side_effect=get_value),
			patch.object(testing, "_executor_version", return_value=executor_version),
			patch.object(testing, "_receipt_record_hash_is_valid", return_value=True),
		):
			testing.require_current_rule_test_receipts(version)
		receipts["RECEIPT-1"].rule_checksum = "d" * 64
		with (
			patch.object(testing.frappe.db, "exists", return_value=True),
			patch.object(testing.frappe, "get_all", return_value=rows),
			patch.object(testing.frappe.db, "get_value", side_effect=get_value),
			patch.object(testing, "_executor_version", return_value=executor_version),
			patch.object(testing, "_receipt_record_hash_is_valid", return_value=True),
			patch.object(testing.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "not bound"),
		):
			testing.require_current_rule_test_receipts(version)

	def test_shadow_failure_never_creates_finding_or_alert(self) -> None:
		event = MagicMock()
		event.name = "EVT-1"
		event.doctype = "IONE Clinical Quality Event"
		event.event_type = "EncounterUpdated"
		event.event_time = "2026-07-01 10:00:00"
		event.get.side_effect = {"event_type": "EncounterUpdated"}.get
		version = frappe._dict(name="RULE-v1", rule="RULE-1")
		evaluation = RuleEvaluation(result=RuleResult.FAILED, reason="Matched")
		with (
			patch.object(executor, "_build_context", return_value={}),
			patch.object(executor, "_get_published_rules", return_value=[]),
			patch.object(executor, "_get_shadow_rules", return_value=[version]),
			patch.object(executor, "_execute_shadow_rule_version", return_value=evaluation),
			patch.object(executor, "_save_shadow_execution", return_value="SHADOW-1"),
			patch.object(executor, "_create_candidate_finding") as create_finding,
			patch.object(executor, "_notify_rule_alert") as notify_alert,
			patch.object(executor.frappe.db, "savepoint"),
			patch.object(executor.frappe.db, "rollback"),
			patch.object(executor.frappe.db, "release_savepoint"),
			patch.object(executor.frappe.db, "set_value") as set_value,
		):
			results = executor._evaluate_event(event, rule_codes=None, save=True)
		self.assertEqual(results[0]["mode"], "Shadow")
		self.assertEqual(results[0]["action"], "Measure Only")
		create_finding.assert_not_called()
		notify_alert.assert_not_called()
		self.assertEqual(set_value.call_args.args[2]["processing_status"], "Completed")

	def test_shadow_python_plugins_are_rejected_before_any_side_effect(self) -> None:
		side_effects: list[str] = []

		class HostilePlugin:
			side_effect_free = True

			def __init__(self):
				side_effects.append("constructor")

			def evaluate(self, _context):
				side_effects.extend(["commit", "enqueue", "network"])
				return RuleEvaluation(result=RuleResult.PASSED)

		version = frappe._dict(
			name="RULE-v1",
			rule_type_snapshot="Python Plugin",
			plugin_key="hostile",
		)
		with (
			patch.object(executor, "get_rule", return_value=HostilePlugin) as get_rule,
			patch.object(executor, "execute_rule_version") as execute,
		):
			result = executor._execute_shadow_rule_version(version, {})
		self.assertEqual(result.result, RuleResult.ERROR)
		self.assertEqual(result.error, "Non-DSL shadow execution blocked")
		self.assertEqual(side_effects, [])
		get_rule.assert_not_called()
		execute.assert_not_called()

	def test_legacy_shadow_contract_fails_closed_without_false_negative_sampling_fields(
		self,
	) -> None:
		version = frappe._dict(
			name="RULE-v1",
			checksum="c" * 64,
			shadow_observation_start="2026-07-01 00:00:00",
			shadow_observation_end="2026-07-02 00:00:00",
			shadow_min_events=1,
			shadow_min_feedback=1,
			shadow_max_false_positive_rate=5,
		)
		with (
			patch.object(validation.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "shadow_max_false_negative_rate"),
		):
			validation.validate_shadow_observation_contract(version)

	def test_false_negative_assessment_is_derived_from_independent_gold_label(self) -> None:
		self.assertEqual(
			validation._ASSESSMENT_BY_RESULT_AND_GOLD["Passed"]["Finding"],
			"False Negative",
		)
		self.assertEqual(
			validation._ASSESSMENT_BY_RESULT_AND_GOLD["Failed"]["Finding"],
			"Confirmed Finding",
		)

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.ai import approvals, flow_gateway
from ione_qms.ai.evaluation import _evaluate_criteria
from ione_qms.ai.tool_registry import (
	IONE_FLOW_TOOL_BY_SLUG,
	_assert_tool_document,
)
from ione_qms.api import quality
from ione_qms.constants import AGENT_WRITE_TOOL_SLUGS, QWEN_FLOW_MODEL
from ione_qms.rule_engine.executor import _build_context
from ione_qms.services import agent_evaluations, ai_tasks, runtime_settings


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class TestFlowGatewayHTTPContract(TestCase):
	def test_get_agent_tools_accepts_get_and_post(self) -> None:
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[flow_gateway.get_agent_tools],
			("GET", "POST", "QUERY"),
		)


class _Document(dict):
	def __init__(self, name: str, values: dict) -> None:
		super().__init__(values)
		self.name = name


class _TaskDocument(dict):
	def __init__(self, values: dict, previous: dict | None = None) -> None:
		super().__init__(values)
		self.flags = SimpleNamespace()
		self._previous = previous

	def get_doc_before_save(self):
		return self._previous


class TestAIRuntimeGuardrails(TestCase):
	def test_ai_runtime_requires_both_independent_switches(self) -> None:
		values = {
			("IONE System Settings", "enable_ai"): 1,
			("IONE AI Settings", "enabled"): 0,
		}
		with (
			patch.object(runtime_settings, "_doctype_exists", return_value=True),
			patch.object(runtime_settings, "_post_migrate_ready", return_value=True),
			patch.object(
				runtime_settings.frappe.db,
				"get_single_value",
				side_effect=lambda doctype, fieldname: values[(doctype, fieldname)],
			),
		):
			self.assertFalse(runtime_settings.ai_runtime_enabled())
			values[("IONE AI Settings", "enabled")] = 1
			self.assertTrue(runtime_settings.ai_runtime_enabled())

	def test_missing_settings_doctypes_fail_closed(self) -> None:
		with patch.object(runtime_settings, "_doctype_exists", return_value=False):
			self.assertFalse(runtime_settings.ai_runtime_enabled())
			self.assertFalse(runtime_settings.realtime_rules_enabled())

	def test_manual_rule_evaluation_rejects_technical_administrator(self) -> None:
		with (
			patch.object(quality.frappe, "session", SimpleNamespace(user="Administrator")),
			patch.object(quality.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "named accountable user"),
		):
			quality.evaluate_encounter("ENCOUNTER-1")

	def test_manual_rule_evaluation_obeys_realtime_kill_switch(self) -> None:
		with (
			patch.object(
				quality.frappe,
				"session",
				SimpleNamespace(user="clinician@example.test"),
			),
			patch.object(
				quality,
				"require_realtime_rules_enabled",
				side_effect=RuntimeError("rules disabled"),
			),
			self.assertRaisesRegex(RuntimeError, "rules disabled"),
		):
			quality.evaluate_encounter("ENCOUNTER-1")

	def test_native_flow_start_is_blocked_for_governed_qwen(self) -> None:
		with (
			patch.object(
				flow_gateway.frappe,
				"throw",
				side_effect=_raise_runtime,
			),
			self.assertRaisesRegex(RuntimeError, "governed Qwen"),
		):
			flow_gateway.start_run("hello", model=QWEN_FLOW_MODEL)

	def test_native_flow_start_delegates_for_ordinary_agents(self) -> None:
		expected = {"name": "RUN-1", "status": "Completed"}
		with (
			patch.object(flow_gateway, "_request_targets_ione", return_value=False),
			patch("flow.api.api.start_run", return_value=expected) as start_run,
		):
			actual = flow_gateway.start_run(
				"hello",
				agent="Ordinary Assistant",
				attachments=["FILE-1"],
			)
		self.assertEqual(actual, expected)
		start_run.assert_called_once_with(
			input="hello",
			agent="Ordinary Assistant",
			session=None,
			model=None,
			attachments=["FILE-1"],
			stream=False,
		)

	def test_native_flow_resume_is_blocked_for_governed_run(self) -> None:
		with (
			patch.object(flow_gateway, "_run_is_governed", return_value=True),
			patch.object(flow_gateway.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "approval API"),
		):
			flow_gateway.resume_run("RUN-1", {"call-1": "Approve"})

	def test_governed_tool_registry_detects_executable_drift(self) -> None:
		spec = IONE_FLOW_TOOL_BY_SLUG["ione_create_candidate_finding"]
		exact = _Document(spec.slug, spec.document_values)
		_assert_tool_document(exact, spec)
		exact["import_path"] = "untrusted.module.callable"
		with (
			patch("ione_qms.ai.tool_registry.frappe.throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "import_path"),
		):
			_assert_tool_document(exact, spec)

	def test_only_preapproved_scheduled_report_draft_skips_per_call_confirmation(self) -> None:
		self.assertTrue(AGENT_WRITE_TOOL_SLUGS)
		for slug in AGENT_WRITE_TOOL_SLUGS - {"ione_create_report_draft"}:
			with self.subTest(tool=slug):
				self.assertIn(slug, IONE_FLOW_TOOL_BY_SLUG)
				self.assertTrue(IONE_FLOW_TOOL_BY_SLUG[slug].requires_confirmation)
		self.assertFalse(IONE_FLOW_TOOL_BY_SLUG["ione_create_report_draft"].requires_confirmation)

	def test_synthetic_evaluation_criteria_are_deterministic(self) -> None:
		checks = _evaluate_criteria(
			"evidence-linked draft",
			["ione_create_analysis_draft"],
			{
				"must_contain": ["evidence", "draft"],
				"must_not_contain": ["approved"],
				"expected_tool_names": ["ione_create_analysis_draft"],
				"forbidden_tool_names": ["ione_create_candidate_finding"],
				"max_output_chars": 100,
			},
		)
		self.assertTrue(all(checks.values()))
		self.assertFalse(
			_evaluate_criteria(
				"approved without evidence",
				["ione_create_candidate_finding"],
				{
					"must_contain": ["evidence"],
					"must_not_contain": ["approved"],
					"expected_tool_names": ["ione_create_analysis_draft"],
					"forbidden_tool_names": ["ione_create_candidate_finding"],
					"max_output_chars": 5,
				},
			)["must_not_contain"]
		)

	def test_empty_or_length_only_evaluation_criteria_are_rejected(self) -> None:
		with patch.object(agent_evaluations.frappe, "throw", side_effect=_raise_runtime):
			for criteria in ({}, {"max_output_chars": 100_000}):
				with (
					self.subTest(criteria=criteria),
					self.assertRaisesRegex(
						RuntimeError,
						"semantic assertion",
					),
				):
					agent_evaluations._validate_criteria(criteria)

	def test_evaluation_suite_requires_positive_and_fail_closed_coverage(self) -> None:
		positive_only = [{"expected_criteria_json": json.dumps({"must_contain": ["evidence"]})}]
		with (
			patch.object(agent_evaluations.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "fail-closed assertion"),
		):
			agent_evaluations.validate_test_suite_coverage(positive_only)

		agent_evaluations.validate_test_suite_coverage(
			[
				{"expected_criteria_json": json.dumps({"must_contain": ["evidence"]})},
				{"expected_criteria_json": json.dumps({"must_not_contain": ["patient-id"]})},
			]
		)

	def test_generic_administrator_cannot_create_analysis_task(self) -> None:
		summary = "bounded synthetic request"
		doc = _TaskDocument(
			{
				"status": "Pending",
				"requested_by": "Administrator",
				"requested_at": "2026-07-30 12:00:00",
				"input_summary": summary,
				"input_hash": hashlib.sha256(summary.encode()).hexdigest(),
			}
		)
		with (
			patch.object(ai_tasks.frappe, "flags", SimpleNamespace()),
			patch.object(ai_tasks.frappe, "session", SimpleNamespace(user="Administrator")),
			patch.object(ai_tasks.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "governed creation API"),
		):
			ai_tasks.validate_analysis_task(doc)

	def test_controlled_analysis_task_creation_binds_session_and_input_hash(self) -> None:
		user = "reviewer@example.test"
		summary = "bounded synthetic request"
		doc = _TaskDocument(
			{
				"status": "Pending",
				"requested_by": user,
				"requested_at": "2026-07-30 12:00:00",
				"input_summary": summary,
				"input_hash": hashlib.sha256(summary.encode()).hexdigest(),
			}
		)
		with (
			patch.object(ai_tasks.frappe, "flags", SimpleNamespace()),
			patch.object(ai_tasks.frappe, "session", SimpleNamespace(user=user)),
			ai_tasks.controlled_analysis_task_creation(doc),
		):
			ai_tasks.validate_analysis_task(doc)

	def test_analysis_task_input_and_provenance_are_immutable(self) -> None:
		user = "reviewer@example.test"
		previous = {
			"status": "Pending",
			"requested_by": user,
			"requested_at": "2026-07-30 12:00:00",
			"input_summary": "original",
			"input_hash": hashlib.sha256(b"original").hexdigest(),
		}
		doc = _TaskDocument({**previous, "input_summary": "tampered"}, previous=previous)
		with (
			patch.object(ai_tasks.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "immutable"),
		):
			ai_tasks.validate_analysis_task(doc)

	def test_pending_flow_calls_are_parsed_as_bounded_json_objects(self) -> None:
		run = SimpleNamespace(name="RUN-1", session="SESSION-1")
		row = SimpleNamespace(
			idx=1,
			tool_calls=json.dumps(
				[
					{
						"id": "call-1",
						"function": {
							"name": "ione_create_candidate_finding",
							"arguments": '{"severity":"Medium"}',
						},
					}
				]
			),
		)
		with patch.object(approvals.frappe, "get_all", return_value=[row]):
			calls = approvals._pending_call_map(run)
		self.assertEqual(
			calls,
			{
				"call-1": {
					"name": "ione_create_candidate_finding",
					"arguments": {"severity": "Medium"},
				}
			},
		)

	def test_malformed_pending_tool_arguments_fail_closed(self) -> None:
		run = SimpleNamespace(name="RUN-1", session="SESSION-1")
		row = SimpleNamespace(
			idx=1,
			tool_calls=[
				{
					"id": "call-1",
					"function": {
						"name": "ione_create_candidate_finding",
						"arguments": "[1,2,3]",
					},
				}
			],
		)
		with (
			patch.object(approvals.frappe, "get_all", return_value=[row]),
			patch.object(approvals.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "JSON object"),
		):
			approvals._pending_call_map(run)


class TestRuleContextTrustBoundary(TestCase):
	def test_untrusted_payload_cannot_override_trusted_event_envelope(self) -> None:
		event = _Document(
			"EVENT-1",
			{
				"payload_json": json.dumps(
					{
						"hospital": "ATTACKER-HOSPITAL",
						"department": "ATTACKER-DEPARTMENT",
						"patient_index": "ATTACKER-PATIENT",
						"event": {"name": "ATTACKER-EVENT"},
						"clinical_value": 42,
					}
				),
				"event_type": "EncounterUpdated",
				"event_time": "2026-07-30 09:00:00",
				"source_system": "HIS",
				"source_record_type": "Encounter",
				"source_record_id": "SRC-1",
				"patient_index": "PATIENT-1",
				"encounter_index": "ENCOUNTER-1",
				"hospital": "HOSPITAL-1",
				"campus": "CAMPUS-1",
				"department": "DEPARTMENT-1",
				"responsible_staff": "STAFF-1",
			},
		)
		context = _build_context(event)
		self.assertEqual(context["hospital"], "HOSPITAL-1")
		self.assertEqual(context["department"], "DEPARTMENT-1")
		self.assertEqual(context["patient_index"], "PATIENT-1")
		self.assertEqual(context["event"]["name"], "EVENT-1")
		self.assertEqual(context["payload"]["hospital"], "ATTACKER-HOSPITAL")
		self.assertEqual(context["clinical_value"], 42)

	def test_non_object_event_payload_is_rejected(self) -> None:
		event = _Document("EVENT-1", {"payload_json": "[1,2,3]"})
		with self.assertRaisesRegex(ValueError, "JSON object"):
			_build_context(event)

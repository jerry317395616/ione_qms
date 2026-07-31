from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import ANY, MagicMock, patch

from ione_qms.api.ai import (
	_accepted_candidate_finding,
	_apply_candidate_review_workflow,
	review_candidate_finding,
)
from ione_qms.hooks import (
	doc_events,
	has_permission,
	override_whitelisted_methods,
	permission_query_conditions,
	scheduler_events,
)
from ione_qms.indicator_engine import validate_formula_schema
from ione_qms.services.findings import _same_optional_value
from ione_qms.services.indicators import result_status
from ione_qms.tasks.analytics import _duration_ms, _usage_metrics
from ione_qms.tasks.quality import _enqueue_overdue_notification


class TestRuntimeSchemaContracts(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		app_root = Path(__file__).resolve().parents[1]
		cls.schemas = {}
		for path in app_root.glob("ione_*/doctype/*/*.json"):
			schema = json.loads(path.read_text(encoding="utf-8"))
			if schema.get("doctype") == "DocType":
				cls.schemas[schema["name"]] = schema

	def field(self, doctype: str, fieldname: str) -> dict:
		schema = self.schemas[doctype]
		fields = {field["fieldname"]: field for field in schema["fields"]}
		self.assertIn(fieldname, fields, f"{doctype}.{fieldname} is required by runtime code")
		return fields[fieldname]

	def assert_link(self, doctype: str, fieldname: str, target: str, *, required: bool = False) -> None:
		field = self.field(doctype, fieldname)
		self.assertEqual(field.get("fieldtype"), "Link")
		self.assertEqual(field.get("options"), target)
		if required:
			self.assertEqual(field.get("reqd"), 1)

	def assert_unique(self, doctype: str, fieldname: str) -> None:
		self.assertEqual(self.field(doctype, fieldname).get("unique"), 1)

	def assert_validate_handler(self, doctype: str, handler: str) -> None:
		configured = doc_events[doctype]["validate"]
		handlers = configured if isinstance(configured, list) else [configured]
		self.assertIn(handler, handlers)

	def test_finding_candidate_and_evidence_contracts(self) -> None:
		finding = self.schemas["IONE QC Finding"]
		agent_reviewer = next(
			permission
			for permission in finding["permissions"]
			if permission.get("role") == "IONE Agent Reviewer"
		)
		self.assertEqual(agent_reviewer.get("read"), 1)
		self.assertFalse(agent_reviewer.get("create"))
		self.assertFalse(agent_reviewer.get("write"))
		evidence_type = self.field("IONE QC Finding Evidence", "evidence_type")
		self.assertIn("Workflow Transition", str(evidence_type.get("options") or "").splitlines())
		self.assert_unique("IONE QC Finding Evidence", "evidence_hash")

	def test_ai_access_log_is_an_immutable_evidence_receipt(self) -> None:
		for fieldname in (
			"source_doctype",
			"source_name",
			"source_version",
			"source_record_hash",
			"content_hash",
			"snapshot_json",
			"record_hash",
		):
			self.field("IONE AI Data Access Log", fieldname)
		self.assertEqual(
			self.field("IONE AI Data Access Log", "snapshot_json").get("permlevel"),
			9,
		)
		self.assertFalse(
			any(
				permission.get("create")
				for permission in self.schemas["IONE AI Data Access Log"]["permissions"]
			)
		)
		self.assert_validate_handler(
			"IONE AI Data Access Log",
			"ione_qms.services.immutability.validate_append_only",
		)

	def test_ai_execution_claim_and_flow_lineage_contracts(self) -> None:
		for fieldname in (
			"execution_attempt",
			"execution_phase",
			"claimed_at",
			"heartbeat_at",
			"lease_expires_at",
			"execution_token",
			"last_execution_token_hash",
		):
			self.field("IONE AI Analysis Task", fieldname)
		for fieldname in ("execution_token", "last_execution_token_hash"):
			field = self.field("IONE AI Analysis Task", fieldname)
			self.assertEqual(field.get("permlevel"), 2)
			self.assertEqual(field.get("hidden"), 1)
			self.assertEqual(field.get("read_only"), 1)

		for doctype in ("IONE Flow Run Link", "IONE AI Execution Event"):
			for fieldname in (
				"hospital",
				"campus",
				"department",
				"ward",
				"patient",
				"encounter",
				"responsible_staff",
				"execution_attempt",
				"execution_token_hash",
				"record_hash",
			):
				self.field(doctype, fieldname)
			self.assert_validate_handler(
				doctype,
				"ione_qms.services.immutability.validate_append_only",
			)

		for fieldname in (
			"revision_key",
			"run_iteration",
			"input_messages_hash",
			"output_messages_hash",
			"tool_trace_hash",
			"attachment_text_hash",
		):
			self.field("IONE Flow Run Link", fieldname)
		self.assert_unique("IONE Flow Run Link", "revision_key")
		self.assertFalse(self.field("IONE Flow Run Link", "flow_run").get("unique"))
		self.assert_unique("IONE AI Execution Event", "execution_event_key")
		self.assertFalse(
			any(
				permission.get(action)
				for permission in self.schemas["IONE AI Execution Event"]["permissions"]
				for action in ("create", "write", "delete", "export", "print", "share")
			)
		)
		self.assertIn(
			"ione_qms.ai.orchestrator.recover_stale_analysis_tasks",
			scheduler_events["cron"]["*/5 * * * *"],
		)

	def test_indicator_runtime_fields_are_persisted(self) -> None:
		self.assertEqual(
			self.field("IONE QC Indicator Version", "calculation_frequency").get("reqd"),
			1,
		)
		self.field("IONE QC Indicator Version", "rolling_window_days")
		self.assert_link(
			"IONE Indicator Calculation",
			"indicator_version",
			"IONE QC Indicator Version",
			required=True,
		)
		self.field("IONE Indicator Calculation", "dimension_json")
		self.assert_link("IONE Indicator Calculation", "result", "IONE Indicator Result")
		self.assert_link("IONE Daily Quality Fact", "medical_staff", "IONE Medical Staff")
		self.assert_link("IONE Indicator Alert", "medical_staff", "IONE Medical Staff")
		for doctype in (
			"IONE QC Indicator",
			"IONE QC Indicator Version",
			"IONE Indicator Result",
			"IONE Monthly Quality Fact",
		):
			self.field(doctype, "target_min")
			self.field(doctype, "target_max")

	def test_quality_issue_preserves_source_lineage(self) -> None:
		self.assert_link("IONE Data Quality Issue", "event", "IONE Clinical Quality Event")
		self.assert_link("IONE Data Quality Issue", "source_system", "IONE Source System")
		for fieldname in ("source_record_type", "source_record_id", "description", "detected_at"):
			self.field("IONE Data Quality Issue", fieldname)

	def test_fact_grains_have_mandatory_source_keys(self) -> None:
		self.assert_link(
			"IONE Monthly Quality Fact",
			"indicator_version",
			"IONE QC Indicator Version",
			required=True,
		)
		self.field("IONE Monthly Quality Fact", "dimension_hash")
		self.assert_link(
			"IONE Finding Analysis Fact",
			"finding",
			"IONE QC Finding",
			required=True,
		)
		self.assert_unique("IONE Finding Analysis Fact", "finding")
		self.assert_link(
			"IONE Surgery Quality Fact",
			"surgery_qc",
			"IONE Surgery QC",
			required=True,
		)
		self.assert_unique("IONE Surgery Quality Fact", "surgery_qc")
		self.assert_link(
			"IONE Agent Analysis Fact",
			"analysis_task",
			"IONE AI Analysis Task",
			required=True,
		)
		self.assert_unique("IONE Agent Analysis Fact", "analysis_task")
		self.assert_link(
			"IONE Agent Analysis Fact",
			"flow_run_link",
			"IONE Flow Run Link",
		)

	def test_surgery_case_fields_support_required_analytics(self) -> None:
		for fieldname in ("surgery_level", "compliant", "unplanned_return", "complication"):
			self.field("IONE Surgery QC", fieldname)

	def test_projection_tables_are_materializer_only(self) -> None:
		for doctype, key_field in (
			("IONE Medical Record QC", "record_key"),
			("IONE Surgery QC", "surgery_key"),
			("IONE Medical Safety Event", "safety_event_key"),
			("IONE Data Reconciliation", "reconciliation_key"),
		):
			with self.subTest(doctype=doctype):
				key = self.field(doctype, key_field)
				self.assertEqual(key.get("reqd"), 1)
				self.assertEqual(key.get("read_only"), 1)
				self.assertEqual(key.get("unique"), 1)
				self.assertFalse(
					any(
						permission.get("create")
						for permission in self.schemas[doctype].get("permissions", [])
					)
				)
				self.assertEqual(
					(
						doc_events[doctype]["validate"][0]
						if isinstance(doc_events[doctype]["validate"], list)
						else doc_events[doctype]["validate"]
					),
					"ione_qms.services.projections.validate_projection_identity",
				)

	def test_medical_safety_event_privacy_and_closure_contract(self) -> None:
		schema = self.schemas["IONE Medical Safety Event"]
		for fieldname in (
			"anonymous_report",
			"near_miss",
			"investigation_summary",
			"root_cause_analysis",
			"improvement_action",
			"verification_outcome",
			"lessons_learned",
			"closed_by",
			"closed_at",
		):
			self.field("IONE Medical Safety Event", fieldname)
		for fieldname in ("reported_by", "reported_at", "closed_by", "closed_at"):
			field = self.field("IONE Medical Safety Event", fieldname)
			self.assertEqual(field.get("permlevel"), 1)
			self.assertEqual(field.get("read_only"), 1)
		level_one_readers = {
			permission["role"]
			for permission in schema["permissions"]
			if int(permission.get("permlevel") or 0) == 1 and permission.get("read")
		}
		self.assertEqual(level_one_readers, {"IONE QC Reviewer", "IONE Medical Affairs"})
		self.assert_validate_handler(
			"IONE Medical Safety Event",
			"ione_qms.services.projections.validate_projection_identity",
		)
		self.assert_validate_handler(
			"IONE Medical Safety Event",
			"ione_qms.services.safety_events.validate_medical_safety_event",
		)

	def test_finding_contract_is_enforced_before_database_update(self) -> None:
		self.assert_validate_handler(
			"IONE QC Finding",
			"ione_qms.services.findings.validate_finding",
		)

	def test_agent_service_has_no_generic_ai_artifact_mutation_permission(self) -> None:
		for doctype in (
			"IONE AI Analysis Task",
			"IONE AI Candidate Finding",
			"IONE AI Report Draft",
		):
			with self.subTest(doctype=doctype):
				service_permission = next(
					(
						permission
						for permission in self.schemas[doctype]["permissions"]
						if permission.get("role") == "IONE Agent Service"
					),
					{},
				)
				for permission_type in ("read", "write", "create", "delete", "export", "print", "share"):
					self.assertFalse(service_permission.get(permission_type))

	def test_agent_release_and_evaluation_are_pinned_immutable_artifacts(self) -> None:
		active_key = self.field("IONE Agent Release", "active_parent_key")
		self.assertEqual(active_key.get("unique"), 1)
		self.assertEqual(active_key.get("read_only"), 1)
		self.assertEqual(active_key.get("hidden"), 1)
		evaluation = self.schemas["IONE Agent Evaluation"]
		for fieldname in (
			"release_checksum",
			"test_suite_hash",
			"result_hash",
			"requested_by",
			"review_status",
			"reviewed_by",
			"reviewed_at",
			"review_comment",
		):
			self.assertEqual(self.field("IONE Agent Evaluation", fieldname).get("read_only"), 1)
		self.assertFalse(
			any(
				permission.get(permission_type)
				for permission in evaluation["permissions"]
				for permission_type in ("create", "write", "delete", "share")
			)
		)

	def test_tool_approval_raw_content_is_reviewer_only_and_api_mutated(self) -> None:
		schema = self.schemas["IONE AI Tool Approval"]
		prompt_hash = self.field("IONE AI Tool Approval", "question_prompt_hash")
		self.assertEqual(prompt_hash.get("read_only"), 1)
		self.assertFalse(prompt_hash.get("reqd"))
		self.assertEqual(prompt_hash.get("length"), 64)
		for fieldname in ("arguments_json", "question_prompt"):
			field = self.field("IONE AI Tool Approval", fieldname)
			self.assertEqual(field.get("permlevel"), 1)
			self.assertEqual(field.get("read_only"), 1)
		level_one_readers = {
			permission["role"]
			for permission in schema["permissions"]
			if int(permission.get("permlevel") or 0) == 1 and permission.get("read")
		}
		self.assertEqual(
			level_one_readers,
			{"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"},
		)
		self.assertFalse(
			any(
				permission.get(permission_type)
				for permission in schema["permissions"]
				for permission_type in ("create", "write", "delete", "export", "print", "share")
			)
		)
		self.assertTrue(
			{"System Manager", "IONE Agent Administrator", "IONE Agent Service"}.isdisjoint(
				{permission["role"] for permission in schema["permissions"]}
			)
		)

	def test_flow_native_entrypoints_and_documents_are_guarded(self) -> None:
		for doctype in ("Flow Model", "Flow Run", "Flow Session"):
			self.assertIn(doctype, permission_query_conditions)
			self.assertIn(doctype, has_permission)
		for command in (
			"start_run",
			"resume_run",
			"stop_run",
			"recover_session",
			"submit_feedback",
			"get_agent_tools",
		):
			self.assertEqual(
				override_whitelisted_methods[f"flow.api.{command}"],
				f"ione_qms.ai.flow_gateway.{command}",
			)
			self.assertEqual(
				override_whitelisted_methods[f"flow.api.api.{command}"],
				f"ione_qms.ai.flow_gateway.{command}",
			)

	def test_overdue_escalation_runs_daily_at_eight(self) -> None:
		self.assertIn(
			"ione_qms.tasks.quality.escalate_overdue_items",
			scheduler_events["cron"]["0 8 * * *"],
		)

	def test_integration_retry_and_reconciliation_are_scheduled(self) -> None:
		self.assertIn(
			"ione_qms.tasks.integration.retry_due_messages",
			scheduler_events["cron"]["*/2 * * * *"],
		)
		self.assertIn(
			"ione_qms.tasks.integration.reconcile_integrations",
			scheduler_events["cron"]["30 1 * * *"],
		)

	def test_integration_payload_is_field_level_restricted(self) -> None:
		schema = self.schemas["IONE Integration Message"]
		self.assertEqual(self.field("IONE Integration Message", "payload_json").get("permlevel"), 9)
		self.assertNotIn(
			"System Manager",
			{permission["role"] for permission in schema["permissions"]},
		)
		operator_levels = {
			int(permission.get("permlevel") or 0)
			for permission in schema["permissions"]
			if permission.get("role") == "IONE Integration Operator"
		}
		self.assertEqual(operator_levels, {0})
		self.assertFalse(
			any(
				int(permission.get("permlevel") or 0) == 9 and permission.get("read")
				for permission in schema["permissions"]
			)
		)
		self.assertFalse(
			any(
				permission.get("create")
				for permission in schema["permissions"]
				if int(permission.get("permlevel") or 0) == 0
			)
		)

	def test_clinical_event_and_data_quality_raw_values_have_no_generic_reader(self) -> None:
		for doctype, fields in (
			("IONE Clinical Quality Event", ("payload_json",)),
			("IONE Data Quality Issue", ("source_record_id", "actual_value")),
		):
			with self.subTest(doctype=doctype):
				for fieldname in fields:
					self.assertEqual(self.field(doctype, fieldname).get("permlevel"), 9)
				self.assertFalse(
					any(
						int(permission.get("permlevel") or 0) == 9 and permission.get("read")
						for permission in self.schemas[doctype]["permissions"]
					)
				)

	def test_ai_task_raw_request_excludes_technical_agent_administrator(self) -> None:
		schema = self.schemas["IONE AI Analysis Task"]
		self.assertEqual(self.field("IONE AI Analysis Task", "input_summary").get("permlevel"), 9)
		self.assertNotIn(
			"IONE Agent Administrator",
			{permission["role"] for permission in schema["permissions"]},
		)


class TestProjectionHelpers(TestCase):
	def test_usage_metrics_supports_openai_and_qwen_shapes(self) -> None:
		self.assertEqual(
			_usage_metrics('{"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}'),
			{"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
		)
		self.assertEqual(
			_usage_metrics({"input_tokens": "3", "output_tokens": 4}),
			{"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
		)

	def test_duration_is_bounded_and_deterministic(self) -> None:
		self.assertEqual(
			_duration_ms(datetime(2026, 7, 30, 8), datetime(2026, 7, 30, 8, 0, 1, 250000)),
			1250,
		)
		self.assertEqual(
			_duration_ms(datetime(2026, 7, 30, 8), datetime(2026, 7, 30, 7)),
			0,
		)

	def test_target_range_status_is_inclusive_and_validated(self) -> None:
		self.assertEqual(
			result_status(
				10,
				direction="Target Range",
				target_min=10,
				target_max=20,
			),
			"Met",
		)
		self.assertEqual(
			result_status(
				21,
				direction="Target Range",
				target_min=10,
				target_max=20,
			),
			"Below Target",
		)
		with self.assertRaisesRegex(ValueError, "target_min"):
			result_status(
				15,
				direction="Target Range",
				target_min=20,
				target_max=10,
			)

	def test_optional_candidate_values_normalize_empty_strings(self) -> None:
		self.assertTrue(_same_optional_value(None, ""))
		self.assertTrue(_same_optional_value("MEDIUM", "MEDIUM"))
		self.assertFalse(_same_optional_value("MEDIUM", "HIGH"))

	def test_responsible_staff_filter_is_valid_for_reviewed_datasets(self) -> None:
		formula = validate_formula_schema(
			{
				"measure": "count",
				"numerator": {
					"dataset": "medical_record_qc",
					"filters": {"responsible_staff": "STAFF-001"},
				},
				"dimensions": ["medical_staff"],
			}
		)
		self.assertEqual(formula["measure"], "count")

	@patch("ione_qms.tasks.quality.enqueue_create_notification")
	def test_overdue_notification_is_minimized_and_deduplicated(self, enqueue) -> None:
		_enqueue_overdue_notification(
			doctype="IONE QC Finding",
			document_name="QF-0001",
			label="质量问题",
			department="DEPT-ICU",
			due_date="2026-07-28",
			overdue_days=2,
			recipients={"reviewer@example.test", "director@example.test"},
		)
		users, notification = enqueue.call_args.args
		self.assertEqual(users, ["director@example.test", "reviewer@example.test"])
		self.assertEqual(
			enqueue.call_args.kwargs["dedupe_on"],
			["document_type", "document_name", "subject"],
		)
		self.assertEqual(notification["document_type"], "IONE QC Finding")
		self.assertEqual(notification["document_name"], "QF-0001")
		self.assertIn("逾期 2 天", notification["subject"])
		self.assertNotIn("patient", json.dumps(notification, ensure_ascii=False).lower())
		self.assertNotIn("encounter", json.dumps(notification, ensure_ascii=False).lower())

	@patch("ione_qms.api.ai.remove_docshare")
	@patch("ione_qms.api.ai.apply_workflow", side_effect=RuntimeError("workflow failed"))
	@patch("ione_qms.api.ai.add_docshare", return_value=object())
	@patch(
		"ione_qms.api.ai.frappe.db.get_value",
		side_effect=[None, "TEMP-SHARE"],
	)
	def test_candidate_workflow_capability_is_always_revoked(
		self,
		_get_value,
		_add_share,
		_apply,
		remove_share,
	) -> None:
		finding = SimpleNamespace(doctype="IONE QC Finding", name="QF-0001")
		with self.assertRaisesRegex(RuntimeError, "workflow failed"):
			_apply_candidate_review_workflow(finding)
		remove_share.assert_called_once_with(
			"IONE QC Finding",
			"QF-0001",
			ANY,
			flags={"ignore_permissions": True},
		)

	@patch("ione_qms.api.ai.remove_docshare")
	@patch(
		"ione_qms.api.ai.add_docshare",
		side_effect=RuntimeError("post-save follow failed"),
	)
	@patch(
		"ione_qms.api.ai.frappe.db.get_value",
		side_effect=[None, "TEMP-SHARE"],
	)
	def test_candidate_workflow_revokes_share_when_add_fails_after_persistence(
		self,
		_get_value,
		_add_share,
		remove_share,
	) -> None:
		finding = SimpleNamespace(doctype="IONE QC Finding", name="QF-0001")
		with self.assertRaisesRegex(RuntimeError, "post-save follow failed"):
			_apply_candidate_review_workflow(finding)
		remove_share.assert_called_once_with(
			"IONE QC Finding",
			"QF-0001",
			ANY,
			flags={"ignore_permissions": True},
		)

	@patch("ione_qms.api.ai.remove_docshare")
	@patch("ione_qms.api.ai.apply_workflow")
	@patch("ione_qms.api.ai.add_docshare", return_value=object())
	def test_candidate_workflow_preserves_read_but_revokes_existing_write_share(
		self,
		_add_share,
		_apply,
		remove_share,
	) -> None:
		finding = SimpleNamespace(doctype="IONE QC Finding", name="QF-0001")
		existing = SimpleNamespace(
			name="SHARE-1",
			read=1,
			share=0,
			modified=datetime(2026, 7, 30, 8),
			modified_by="reviewer@example.test",
		)
		with (
			patch("ione_qms.api.ai.frappe.db.get_value", return_value=existing),
			patch("ione_qms.api.ai.frappe.db.set_value") as set_value,
		):
			_apply_candidate_review_workflow(finding)
		remove_share.assert_not_called()
		set_value.assert_called_once_with(
			"DocShare",
			"SHARE-1",
			{
				"read": 1,
				"write": 0,
				"submit": 0,
				"share": 0,
				"modified": existing.modified,
				"modified_by": existing.modified_by,
			},
			update_modified=False,
		)

	def test_candidate_review_rolls_back_when_workflow_fails(self) -> None:
		candidate = MagicMock()
		candidate.name = "AIC-0001"
		candidate.task = "TASK-1"
		candidate.status = "Pending Review"
		with (
			patch("ione_qms.api.ai.require_role"),
			patch(
				"ione_qms.api.ai.frappe.session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch("ione_qms.api.ai.frappe.get_doc", return_value=candidate),
			patch(
				"ione_qms.api.ai.frappe.db.get_value",
				return_value="requester@example.test",
			),
			patch(
				"ione_qms.api.ai.frappe.db.advisory_lock",
				return_value=nullcontext(),
			),
			patch("ione_qms.api.ai.frappe.db.savepoint") as savepoint,
			patch("ione_qms.api.ai.frappe.db.rollback") as rollback,
			patch("ione_qms.api.ai.frappe.db.release_savepoint") as release,
			patch(
				"ione_qms.api.ai._accepted_candidate_finding",
				side_effect=RuntimeError("workflow failed"),
			),
		):
			with self.assertRaisesRegex(RuntimeError, "workflow failed"):
				review_candidate_finding("AIC-0001", "Accept", "reviewed candidate")
		savepoint.assert_called_once_with("ione_ai_candidate_review")
		rollback.assert_called_once_with(save_point="ione_ai_candidate_review")
		release.assert_not_called()
		candidate.save.assert_not_called()

	def test_existing_candidate_key_requires_exact_pending_provenance(self) -> None:
		candidate = SimpleNamespace(name="AIC-0001")
		existing = MagicMock()
		existing.get.return_value = "Candidate"
		with (
			patch(
				"ione_qms.api.ai.frappe.db.get_value",
				return_value="QF-PRESEEDED",
			),
			patch("ione_qms.api.ai.frappe.get_doc", return_value=existing),
			patch(
				"ione_qms.api.ai._is_human_accepted_ai_candidate",
				return_value=False,
			),
			patch(
				"ione_qms.api.ai.frappe.throw",
				side_effect=RuntimeError("invalid provenance"),
			),
			self.assertRaisesRegex(RuntimeError, "invalid provenance"),
		):
			_accepted_candidate_finding(candidate)

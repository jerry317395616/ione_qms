from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "ione_qms" / "services" / "ai_report_schedules.py"
TOOLS_PATH = ROOT / "ione_qms" / "ai" / "tools.py"
EVIDENCE_PATH = ROOT / "ione_qms" / "ai" / "evidence.py"
TOOL_REGISTRY_PATH = ROOT / "ione_qms" / "ai" / "tool_registry.py"
API_PATH = ROOT / "ione_qms" / "api" / "ai.py"
ORCHESTRATOR_PATH = ROOT / "ione_qms" / "ai" / "orchestrator.py"
HOOKS_PATH = ROOT / "ione_qms" / "hooks.py"
GENERATOR_PATH = ROOT / "tools" / "generate_doctypes.py"
EXPORT_PATH = ROOT / "ione_qms" / "services" / "data_export.py"
SCOPE_PATH = ROOT / "ione_qms" / "services" / "scope_hierarchy.py"
PERMISSIONS_PATH = ROOT / "ione_qms" / "permissions.py"
CLIENT_PATH = ROOT / "ione_qms" / "public" / "js" / "ai_report_governance.js"

DOCTYPE_ROOT = ROOT / "ione_qms" / "ione_flow_ai" / "doctype"
SCHEDULE_JSON = DOCTYPE_ROOT / "ione_ai_report_schedule" / "ione_ai_report_schedule.json"
SNAPSHOT_JSON = DOCTYPE_ROOT / "ione_quality_report_snapshot" / "ione_quality_report_snapshot.json"
RECOVERY_JSON = (
	DOCTYPE_ROOT / "ione_ai_report_recovery_authorization" / "ione_ai_report_recovery_authorization.json"
)
TASK_JSON = DOCTYPE_ROOT / "ione_ai_analysis_task" / "ione_ai_analysis_task.json"
POLICY_JSON = DOCTYPE_ROOT / "ione_agent_policy" / "ione_agent_policy.json"
REPORT_DRAFT_JSON = DOCTYPE_ROOT / "ione_ai_report_draft" / "ione_ai_report_draft.json"


def _function_source(source: str, function_name: str) -> str:
	tree = ast.parse(source)
	function = next(
		node
		for node in tree.body
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
	)
	return ast.get_source_segment(source, function) or ""


def _fields_by_name(payload: dict) -> dict[str, dict]:
	return {str(field["fieldname"]): field for field in payload["fields"]}


def _permissions_by_role(payload: dict) -> dict[str, dict]:
	return {str(permission["role"]): permission for permission in payload["permissions"]}


class TestAIReportScheduleStaticContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.service_source = SERVICE_PATH.read_text(encoding="utf-8")
		cls.tools_source = TOOLS_PATH.read_text(encoding="utf-8")
		cls.evidence_source = EVIDENCE_PATH.read_text(encoding="utf-8")
		cls.registry_source = TOOL_REGISTRY_PATH.read_text(encoding="utf-8")
		cls.api_source = API_PATH.read_text(encoding="utf-8")
		cls.orchestrator_source = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
		cls.hooks_source = HOOKS_PATH.read_text(encoding="utf-8")
		cls.generator_source = GENERATOR_PATH.read_text(encoding="utf-8")
		cls.export_source = EXPORT_PATH.read_text(encoding="utf-8")
		cls.scope_source = SCOPE_PATH.read_text(encoding="utf-8")
		cls.permissions_source = PERMISSIONS_PATH.read_text(encoding="utf-8")
		cls.client_source = CLIENT_PATH.read_text(encoding="utf-8")
		cls.schedule_schema = json.loads(SCHEDULE_JSON.read_text(encoding="utf-8"))
		cls.snapshot_schema = json.loads(SNAPSHOT_JSON.read_text(encoding="utf-8"))
		cls.recovery_schema = json.loads(RECOVERY_JSON.read_text(encoding="utf-8"))
		cls.task_schema = json.loads(TASK_JSON.read_text(encoding="utf-8"))
		cls.policy_schema = json.loads(POLICY_JSON.read_text(encoding="utf-8"))
		cls.report_draft_schema = json.loads(REPORT_DRAFT_JSON.read_text(encoding="utf-8"))

	def test_schedule_has_approved_coverage_and_required_indicator_contract(self) -> None:
		fields = _fields_by_name(self.schedule_schema)
		coverage = fields["coverage_start_period"]
		required = fields["required_indicator_codes_json"]
		self.assertEqual(coverage["fieldtype"], "Date")
		self.assertEqual(coverage.get("read_only"), 1)
		self.assertEqual(required["fieldtype"], "Code")
		self.assertEqual(required.get("options"), "JSON")
		self.assertEqual(required.get("reqd"), 1)
		self.assertIn('"coverage_start_period"', self.service_source)
		self.assertIn('"required_indicator_codes_json"', self.service_source)
		checksum = _function_source(self.service_source, "_schedule_checksum")
		self.assertIn("_SCHEDULE_SEMANTIC_FIELDS", checksum)
		semantic_assignment = next(
			node
			for node in ast.parse(self.service_source).body
			if isinstance(node, ast.Assign)
			and any(
				isinstance(target, ast.Name) and target.id == "_SCHEDULE_SEMANTIC_FIELDS"
				for target in node.targets
			)
		)
		semantic_fields = ast.literal_eval(semantic_assignment.value)
		self.assertIn("coverage_start_period", semantic_fields)
		self.assertIn("required_indicator_codes_json", semantic_fields)

	def test_first_period_and_bounded_catchup_are_not_inferred_from_current_month(self) -> None:
		next_due = _function_source(self.service_source, "_next_due_period")
		self.assertIn("coverage_start_period", next_due)
		self.assertIn("last_period_start", next_due)
		self.assertIn("last_period_end", next_due)
		self.assertIn("last_result", next_due)
		self.assertIn("12", self.service_source)
		self.assertNotIn(
			"if not last_result and not last_start_value and not last_end_value:\n"
			"\t\treturn latest_period_start, latest_period_end",
			next_due,
		)

	def test_snapshot_contract_records_complete_required_indicator_coverage(self) -> None:
		builder = _function_source(self.service_source, "_quality_summary_data")
		validator = _function_source(
			self.service_source,
			"_validated_snapshot_payload",
		)
		for source in (builder, validator):
			self.assertIn('"indicator_coverage"', source)
			self.assertIn('"required_codes"', source)
			self.assertIn('"observed_codes"', source)
			self.assertIn('"missing_codes"', source)
			self.assertIn('"complete"', source)
		self.assertIn("_required_indicator_codes(schedule)", builder)
		self.assertIn(
			'"required_indicator_codes_json"',
			_function_source(self.service_source, "_required_indicator_codes"),
		)
		self.assertIn("missing", builder.lower())
		self.assertIn("frappe.throw", builder)
		self.assertIn('"indicator": ["in", indicator_names]', builder)
		self.assertIn("observed_codes != required_codes", builder)
		self.assertIn(
			"observed_codes != required_codes",
			validator,
		)

	def test_revoked_schedule_is_revalidated_before_any_model_call(self) -> None:
		runner = _function_source(self.orchestrator_source, "run_analysis_task")
		live_validator = _function_source(
			self.orchestrator_source,
			"_validate_live_task_provenance_before_model_call",
		)
		self.assertIn(
			"_validate_live_task_provenance_before_model_call(claim.task)",
			runner,
		)
		self.assertLess(
			runner.index("_validate_live_task_provenance_before_model_call(claim.task)"),
			runner.index('frappe.get_doc("Flow Agent", policy.flow_agent)'),
		)
		self.assertLess(
			runner.index("_validate_live_task_provenance_before_model_call(claim.task)"),
			runner.index("agent.run("),
		)
		self.assertIn('"Quality Report"', live_validator)
		self.assertIn('"Scheduled"', live_validator)
		self.assertIn("validated_quality_report_snapshot_for_task(task)", live_validator)

	def test_snapshot_queries_are_permission_filtered_bounded_and_aggregate_only(self) -> None:
		builder = _function_source(self.service_source, "_quality_summary_data")
		permissioned = _function_source(self.service_source, "_permissioned_rows")
		self.assertNotIn("frappe.get_all", builder)
		self.assertIn("_permissioned_rows(", builder)
		self.assertIn('frappe.has_permission(doctype, "read")', permissioned)
		self.assertIn("frappe.get_list(doctype, **kwargs)", permissioned)
		self.assertIn('"limit_page_length": limit + 1', permissioned)

		builder_tree = ast.parse(builder)
		selected_fields: set[str] = set()
		for call in (
			node
			for node in ast.walk(builder_tree)
			if isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id == "_permissioned_rows"
		):
			for keyword in call.keywords:
				if keyword.arg != "fields" or not isinstance(
					keyword.value,
					(ast.Tuple, ast.List),
				):
					continue
				for item in keyword.value.elts:
					if isinstance(item, ast.Constant) and isinstance(item.value, str):
						selected_fields.add(item.value)
		self.assertTrue(selected_fields)
		self.assertTrue(
			{
				"indicator",
				"indicator_version",
				"numerator",
				"denominator",
				"severity",
				"status",
			}.issubset(selected_fields)
		)
		self.assertFalse(
			selected_fields.intersection(
				{
					"patient",
					"patient_id",
					"patient_name",
					"encounter",
					"medical_record",
					"title",
					"description",
					"reason",
					"review_comment",
					"content",
					"payload_json",
				}
			)
		)

	def test_snapshot_creation_and_execution_revalidate_live_governance(self) -> None:
		create_validator = _function_source(
			self.service_source,
			"validate_quality_report_snapshot",
		)
		task_validator = _function_source(
			self.service_source,
			"validated_quality_report_snapshot_for_task",
		)
		for source in (create_validator, task_validator):
			self.assertIn('"Approved"', source)
			self.assertIn('"enabled"', source)
			self.assertIn("_assert_approval_checksum", source)
			self.assertIn("_validate_existing_snapshot", source)
		self.assertIn(
			"_validate_schedule_definition(schedule, require_active_policy=True)",
			task_validator,
		)
		self.assertIn("_eligible_user", task_validator)
		self.assertIn("require_scope_read", task_validator)

	def test_dispatch_is_idempotent_and_one_failure_does_not_abort_other_schedules(self) -> None:
		dispatcher = _function_source(
			self.service_source,
			"dispatch_monthly_quality_reports",
		)
		one_schedule = _function_source(self.service_source, "_dispatch_one_schedule")
		self.assertIn("for schedule_name in rows", dispatcher)
		self.assertIn("except Exception as exc", dispatcher)
		self.assertIn("frappe.db.rollback()", dispatcher)
		self.assertIn("frappe.log_error(", dispatcher)
		self.assertIn('result = "failed"', dispatcher)
		self.assertIn('"dispatch_key"', one_schedule)
		self.assertIn("frappe.db.get_value(", one_schedule)
		self.assertIn("frappe.db.advisory_lock(", one_schedule)
		self.assertIn("frappe.db.savepoint(", one_schedule)
		self.assertIn("frappe.db.rollback()", one_schedule)
		self.assertIn("frappe.db.commit()", one_schedule)
		self.assertIn("_enqueue_scheduled_task_after_commit(task)", one_schedule)
		self.assertLess(
			one_schedule.index("_update_schedule_runtime(", one_schedule.index("savepoint =")),
			one_schedule.index("_enqueue_scheduled_task_after_commit(task)"),
		)
		self.assertLess(
			one_schedule.index("_enqueue_scheduled_task_after_commit(task)"),
			one_schedule.index("frappe.db.release_savepoint(savepoint)"),
		)
		create_task = _function_source(self.service_source, "_create_scheduled_task")
		self.assertIn("doc.flags.skip_ai_enqueue = True", create_task)
		enqueue_task = _function_source(
			self.service_source,
			"_enqueue_scheduled_task_after_commit",
		)
		self.assertIn("frappe.db.after_commit.add(enqueue_committed_task)", enqueue_task)
		self.assertIn("_enqueue_task_name(", enqueue_task)
		self.assertIn("enqueue_after_commit=False", enqueue_task)
		self.assertIn("except Exception as exc", enqueue_task)
		self.assertIn("except Exception:", enqueue_task)
		self.assertNotIn("get_ai_queue", enqueue_task)
		existing_state = _function_source(
			self.service_source,
			"_existing_dispatch_task_state",
		)
		for token in (
			"report_period_start",
			"report_period_end",
			"_validate_task_dispatch_provenance(schedule, task, period_start, period_end)",
			'"Failed", "Rejected", "Cancelled"',
			'"Pending", "Queued", "Running", "Pending Confirmation", "Retry"',
			'"Completed", "Reviewed"',
			'"IONE AI Report Draft"',
		):
			self.assertIn(token, existing_state)
		self.assertLess(
			existing_state.index(
				"_validate_task_dispatch_provenance(schedule, task, period_start, period_end)"
			),
			existing_state.index('if status in {"Failed", "Rejected", "Cancelled"}'),
		)

	def test_review_and_operation_endpoints_are_post_only_and_separated(self) -> None:
		tree = ast.parse(self.api_source)
		for function_name in (
			"review_quality_report_schedule",
			"operate_quality_report_schedule",
			"authorize_quality_report_recovery",
		):
			function = next(
				node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name
			)
			decorators = [
				ast.get_source_segment(self.api_source, decorator) or ""
				for decorator in function.decorator_list
			]
			self.assertIn('frappe.whitelist(methods=["POST"])', decorators)

		review = _function_source(self.service_source, "review_report_schedule")
		self.assertIn("_SCHEDULE_REVIEW_ROLES", review)
		self.assertIn("requested_by", review)
		self.assertIn("cannot approve their own schedule", review)
		draft_review = _function_source(self.api_source, "review_report_draft")
		self.assertIn("report_reviewer", draft_review)
		self.assertIn("requested_by", draft_review)
		self.assertIn("may only be reviewed", draft_review)

	def test_terminal_recovery_is_human_authorized_bounded_and_idempotent(self) -> None:
		authorize = _function_source(self.service_source, "authorize_report_recovery")
		for token in (
			"_named_user()",
			"report_reviewer",
			"Only the exact configured report reviewer",
			"expected_prior_task",
			"_idempotent_recovery_replay",
			"_period_recovery_receipts",
			"REPORT_MAX_RECOVERY_AUTHORIZATIONS",
			"_ensure_recovery_exhaustion_incident",
			"_recovery_dispatch_key",
			"prior_task=current_task",
			"recovery_authorization=receipt",
			"frappe.db.advisory_lock",
			"frappe.db.commit()",
			"_enqueue_scheduled_task_after_commit(recovery_task)",
		):
			self.assertIn(token, authorize)
		self.assertLess(
			authorize.index("_update_schedule_runtime(", authorize.index("savepoint =")),
			authorize.index("_enqueue_scheduled_task_after_commit(recovery_task)"),
		)
		self.assertLess(
			authorize.index("_enqueue_scheduled_task_after_commit(recovery_task)"),
			authorize.index("frappe.db.release_savepoint(savepoint)"),
		)
		self.assertIn("_RECOVERY_TERMINAL_STATUSES", self.service_source)
		self.assertNotIn('frappe.set_value("IONE AI Analysis Task"', authorize)
		self.assertIn(
			"frm.doc.report_reviewer === frappe.session.user",
			self.client_source,
		)
		self.assertIn('frm.doc.last_result === "Failed"', self.client_source)
		self.assertIn(
			"ione_qms.api.ai.authorize_quality_report_recovery",
			self.client_source,
		)
		self.assertIn("prior_task: frm.doc.last_task", self.client_source)

		current_state = _function_source(self.service_source, "_current_runtime_task_state")
		dispatch_one = _function_source(self.service_source, "_dispatch_one_schedule")
		self.assertIn("_existing_dispatch_task_state", current_state)
		self.assertIn('current_state == "failed"', dispatch_one)
		self.assertIn('"RECOVERY_LIMIT_EXHAUSTED"', dispatch_one)
		self.assertIn("_ensure_recovery_exhaustion_incident", dispatch_one)

	def test_recovery_receipt_and_task_provenance_are_immutable_and_checksum_bound(self) -> None:
		fields = _fields_by_name(self.recovery_schema)
		expected_receipt_fields = {
			"authorization_key",
			"schedule",
			"schedule_approval_checksum",
			"report_snapshot",
			"period_start",
			"period_end",
			"hospital",
			"campus",
			"department",
			"ward",
			"prior_task",
			"prior_terminal_status",
			"prior_dispatch_key",
			"recovery_sequence",
			"authorized_by",
			"authorized_at",
			"reason_code",
			"reason",
			"recovery_dispatch_key",
			"authorization_checksum",
		}
		self.assertTrue(expected_receipt_fields.issubset(fields))
		self.assertEqual(fields["authorization_key"].get("unique"), 1)
		self.assertEqual(fields["recovery_dispatch_key"].get("unique"), 1)
		for fieldname in expected_receipt_fields:
			self.assertEqual(fields[fieldname].get("read_only"), 1)
		for permission in self.recovery_schema["permissions"]:
			for action in ("create", "write", "delete", "export", "print", "share"):
				self.assertNotEqual(permission.get(action), 1)
		self.assertEqual(self.recovery_schema.get("allow_import"), 0)

		task_fields = _fields_by_name(self.task_schema)
		for fieldname in (
			"prior_report_task",
			"report_recovery_authorization",
			"recovery_sequence",
			"recovery_authorization_checksum",
		):
			self.assertEqual(task_fields[fieldname].get("read_only"), 1)
		integrity = _function_source(
			self.service_source,
			"_assert_recovery_authorization_integrity",
		)
		for token in (
			"_recovery_authorization_key",
			"_recovery_dispatch_key",
			"_recovery_checksum",
			"validated_quality_report_snapshot_for_task(prior_task)",
			"prior_terminal_status",
			"prior_dispatch_key",
			"recovery_sequence",
			"authorized_by",
		):
			self.assertIn(token, integrity)

		query = _function_source(
			self.permissions_source,
			"quality_report_recovery_authorization_query",
		)
		permission = _function_source(
			self.permissions_source,
			"quality_report_recovery_authorization_permission",
		)
		for source in (query, permission):
			self.assertIn("authorized_by", source)
			self.assertIn("IONE Agent Service", source)
			self.assertIn("is_administrator", source)
		self.assertIn("read", source)
		self.assertIn("select", source)
		self.assertIn("IONE Auditor", source)
		self.assertIn("scope", source.lower())

	def test_tools_are_exact_snapshot_bound_and_draft_is_human_reviewed(self) -> None:
		summary_tool = _function_source(self.tools_source, "ione_get_quality_summary")
		draft_tool = _function_source(self.tools_source, "ione_create_report_draft")
		self.assertIn("validated_quality_report_snapshot_for_task", summary_tool)
		self.assertIn("_require_requester_read", summary_tool)
		self.assertIn('source_doctype="IONE Quality Report Snapshot"', summary_tool)
		self.assertIn('source_record_hash=snapshot.get("data_hash")', summary_tool)
		self.assertIn("snapshot=summary", summary_tool)

		self.assertIn('"Quality Report"', draft_tool)
		self.assertIn('"Scheduled"', draft_tool)
		self.assertIn("requires_human_review", draft_tool)
		self.assertIn("validated_quality_report_snapshot_for_task", draft_tool)
		self.assertIn("aggregate_report_output_violation", draft_tool)
		self.assertIn("output_violation", draft_tool)
		self.assertLess(
			draft_tool.index("aggregate_report_output_violation"),
			draft_tool.index("frappe.get_doc("),
		)
		self.assertIn('reference.source_doctype != "IONE Quality Report Snapshot"', draft_tool)
		self.assertIn("reference.source_name != snapshot.name", draft_tool)
		self.assertIn("reference.source_record_hash", draft_tool)
		self.assertIn("reference.content_hash", draft_tool)
		self.assertIn("frappe.db.advisory_lock", draft_tool)

		summary_start = self.registry_source.index('"ione_get_quality_summary"')
		summary_fragment = self.registry_source[
			summary_start : self.registry_source.index("\n\t),", summary_start)
		]
		self.assertIn("\n\t\tFalse,", summary_fragment)
		draft_fragment = self.registry_source[
			self.registry_source.index('"ione_create_report_draft"') : self.registry_source.index(
				"\n\t),", self.registry_source.index('"ione_create_report_draft"')
			)
		]
		self.assertIn("\n\t\tFalse,", draft_fragment)
		self.assertIn('"IONE Quality Report Snapshot"', self.evidence_source)

	def test_metadata_is_immutable_unique_and_least_privilege(self) -> None:
		schedule_permissions = _permissions_by_role(self.schedule_schema)
		for role in (
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		):
			self.assertEqual(schedule_permissions[role].get("create"), 1)
			self.assertEqual(schedule_permissions[role].get("write"), 1)
		for role in ("IONE Agent Reviewer", "IONE Agent Administrator", "IONE Auditor"):
			self.assertNotEqual(schedule_permissions[role].get("create"), 1)
			self.assertNotEqual(schedule_permissions[role].get("write"), 1)

		for permission in self.snapshot_schema["permissions"]:
			for action in ("create", "write", "delete", "export", "print", "share"):
				self.assertNotEqual(permission.get(action), 1)
		self.assertEqual(self.snapshot_schema.get("allow_import"), 0)

		task_fields = _fields_by_name(self.task_schema)
		self.assertEqual(task_fields["dispatch_key"].get("unique"), 1)
		for fieldname in (
			"origin",
			"report_schedule",
			"report_snapshot",
			"report_period_start",
			"report_period_end",
			"report_reviewer",
			"schedule_approval_checksum",
		):
			self.assertEqual(task_fields[fieldname].get("read_only"), 1)
		self.assertEqual(
			_fields_by_name(self.report_draft_schema)["task"].get("unique"),
			1,
		)

		policy_permissions = _permissions_by_role(self.policy_schema)
		for role in ("IONE Department Director", "IONE Department QC Officer"):
			self.assertEqual(policy_permissions[role].get("read"), 1)
			for action in ("create", "write", "delete", "export", "print", "share"):
				self.assertNotEqual(policy_permissions[role].get(action), 1)

		schedule_definition = _function_source(
			self.service_source,
			"_validate_schedule_definition",
		)
		self.assertIn(
			'safe_tools = {"ione_get_quality_summary", "ione_create_report_draft"}',
			schedule_definition,
		)
		self.assertIn("allowed_tools != safe_tools", schedule_definition)
		self.assertIn('frappe.get_doc("Flow Agent", policy.flow_agent)', schedule_definition)
		self.assertIn("agent_tools != safe_tools", schedule_definition)

	def test_hooks_scope_and_native_export_governance_cover_new_records(self) -> None:
		self.assertIn(
			'"IONE AI Report Schedule": "public/js/ai_report_governance.js"',
			self.hooks_source,
		)
		self.assertIn(
			'"IONE AI Report Draft": "public/js/ai_report_governance.js"',
			self.hooks_source,
		)
		for doctype, query, permission in (
			(
				"IONE AI Report Schedule",
				"quality_report_schedule_query",
				"quality_report_schedule_permission",
			),
			(
				"IONE Quality Report Snapshot",
				"quality_report_snapshot_query",
				"quality_report_snapshot_permission",
			),
			(
				"IONE AI Report Recovery Authorization",
				"quality_report_recovery_authorization_query",
				"quality_report_recovery_authorization_permission",
			),
		):
			self.assertIn(f'"{doctype}"', self.hooks_source)
			self.assertIn(f"ione_qms.permissions.{query}", self.hooks_source)
			self.assertIn(f"ione_qms.permissions.{permission}", self.hooks_source)
			self.assertIn(f'"{doctype}"', self.scope_source)
			self.assertIn(f'"{doctype}"', self.generator_source)
			self.assertIn(f'"{doctype}"', self.export_source)
		self.assertIn(
			'"0 6 * * *": ["ione_qms.services.ai_report_schedules.dispatch_monthly_quality_reports"]',
			self.hooks_source,
		)
		self.assertIn('"IONE AI Report Schedule": {', self.hooks_source)
		self.assertIn('"IONE Quality Report Snapshot": {', self.hooks_source)
		self.assertIn('"IONE AI Report Recovery Authorization": {', self.hooks_source)
		self.assertIn("prevent_report_schedule_deletion", self.hooks_source)
		self.assertIn("prevent_quality_report_snapshot_deletion", self.hooks_source)
		self.assertIn("prevent_report_recovery_authorization_deletion", self.hooks_source)

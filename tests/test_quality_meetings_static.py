from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "services" / "quality_meetings.py"
API = ROOT / "ione_qms" / "api" / "quality_meetings.py"
DESK = ROOT / "ione_qms" / "public" / "js" / "quality_meetings.js"
DOCTYPE_ROOT = ROOT / "ione_qms" / "ione_improvement" / "doctype"
HOOKS = ROOT / "ione_qms" / "hooks.py"
PERMISSIONS = ROOT / "ione_qms" / "permissions.py"
DATA_EXPORT = ROOT / "ione_qms" / "services" / "data_export.py"
SCOPE = ROOT / "ione_qms" / "services" / "scope_hierarchy.py"
QUALITY_TASKS = ROOT / "ione_qms" / "tasks" / "quality.py"
WORKBENCH_ROOT = ROOT / "ione_qms" / "ione_analytics" / "page" / "ione_quality_action_workbench"
WORKSPACE = (
	ROOT / "ione_qms" / "ione_improvement" / "workspace" / "ione_improvement" / "ione_improvement.json"
)

DOCTYPE_SLUGS = {
	"IONE Meeting Agenda Item": "ione_meeting_agenda_item",
	"IONE Meeting Attendee": "ione_meeting_attendee",
	"IONE Quality Meeting": "ione_quality_meeting",
	"IONE Meeting Minute": "ione_meeting_minute",
	"IONE Meeting Decision": "ione_meeting_decision",
	"IONE Quality Action Item": "ione_quality_action_item",
	"IONE Quality Action Verification Round": "ione_quality_action_verification_round",
	"IONE Quality Experience Share": "ione_quality_experience_share",
}


def _source(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _function_source(path: Path, function_name: str) -> str:
	source = _source(path)
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"Missing function {function_name}")


def _doctype(name: str) -> dict:
	slug = DOCTYPE_SLUGS[name]
	return json.loads(_source(DOCTYPE_ROOT / slug / f"{slug}.json"))


def _field(payload: dict, fieldname: str) -> dict:
	for item in payload["fields"]:
		if item.get("fieldname") == fieldname:
			return item
	raise AssertionError(f"Missing field {payload['name']}.{fieldname}")


def _select_options(payload: dict, fieldname: str) -> list[str]:
	return [item for item in str(_field(payload, fieldname).get("options") or "").splitlines() if item]


class TestQualityMeetingsStatic(TestCase):
	def setUp(self) -> None:
		self.service = _source(SERVICE)
		self.api = _source(API)
		self.desk = _source(DESK)

	def test_meeting_and_minute_approval_are_separate_and_human_controlled(self) -> None:
		review_meeting = _function_source(SERVICE, "review_quality_meeting")
		review_minute = _function_source(SERVICE, "review_meeting_minute")
		self.assertIn("meeting_approver", review_meeting)
		self.assertIn("independent named approver", review_meeting)
		self.assertIn("minute_approver", review_minute)
		self.assertIn("independent named approver", review_minute)
		self.assertIn("_derive_meeting_decisions", review_minute)
		self.assertNotIn("IONE Agent Service", review_meeting)
		self.assertNotIn("IONE Agent Service", review_minute)

	def test_approved_agenda_freezes_ai_report_material_without_copying_content(self) -> None:
		material = _function_source(SERVICE, "_validate_agenda_material")
		snapshot = _function_source(SERVICE, "_meeting_agenda_snapshot")
		self.assertIn('"status") or "") != "Approved"', material)
		self.assertIn('"reviewed_by"', material)
		self.assertIn("_assert_compatible_scope", material)
		self.assertIn("material_checksum", material)
		self.assertIn("ai_report_draft", snapshot)
		self.assertNotIn("doc.content =", self.service)
		self.assertNotIn("meeting.content =", self.service)
		self.assertNotIn("apply_workflow", self.service)

	def test_minutes_decisions_and_experience_publication_are_checksummed_and_retained(self) -> None:
		decision_validator = _function_source(SERVICE, "validate_meeting_decision")
		experience = _function_source(SERVICE, "validate_quality_experience_share")
		deletion = _function_source(SERVICE, "prevent_quality_meeting_deletion")
		self.assertIn("immutable append-only", decision_validator)
		self.assertIn("checksum", decision_validator)
		self.assertIn("_validate_deidentification", experience)
		self.assertIn("_validate_experience_version", experience)
		self.assertIn("cannot be deleted", deletion)

	def test_minute_approval_serializes_on_meeting_and_has_a_unique_durable_key(self) -> None:
		review = _function_source(SERVICE, "review_meeting_minute")
		validator = _function_source(SERVICE, "validate_meeting_minute")
		self.assertIn('"IONE Meeting Minute", minute, "meeting"', review)
		self.assertIn("meeting-minute-review", review)
		self.assertIn("meeting_lock", review)
		self.assertGreaterEqual(review.count("for_update=True"), 2)
		self.assertIn("approved_meeting_key", review)
		self.assertIn("approved_meeting_key", validator)

	def test_experience_publication_serializes_on_code_and_has_a_unique_active_key(self) -> None:
		review = _function_source(SERVICE, "review_quality_experience_share")
		validator = _function_source(SERVICE, "validate_quality_experience_share")
		self.assertIn('"experience_code"', review)
		self.assertIn("experience-publication", review)
		self.assertIn("code_lock", review)
		self.assertIn("for_update=True", review)
		self.assertIn("active_publication_key", review)
		self.assertIn("active_publication_key", validator)

	def test_action_items_bind_sources_owner_due_date_and_independent_verifier(self) -> None:
		create = _function_source(SERVICE, "create_quality_action_item")
		submit = _function_source(SERVICE, "submit_quality_action_verification")
		review = _function_source(SERVICE, "review_quality_action_item")
		cancel = _function_source(SERVICE, "cancel_quality_action_item")
		round_validator = _function_source(SERVICE, "validate_quality_action_verification_round")
		chain = _function_source(SERVICE, "_validated_action_verification_chain")
		workbench = _function_source(SERVICE, "quality_action_workbench")
		self.assertIn("_resolved_action_links", create)
		self.assertIn("due_date cannot be in the past", create)
		self.assertIn("Action creator and independent verifier", create)
		self.assertIn("assigned_to", review)
		self.assertIn("submitted_for_verification_by", review)
		self.assertIn("independent named verifier", review)
		self.assertIn("request_key", submit)
		for operation in (submit, review, cancel):
			self.assertIn("advisory_lock", operation)
			self.assertIn("_idempotency_key", operation)
		self.assertIn("_append_action_verification_round", review)
		self.assertIn("latest_verification_round", review)
		self.assertNotIn("doc.verification_comment =", review)
		self.assertNotIn("doc.verified_by =", review)
		self.assertIn("immutable append-only receipts", round_validator)
		self.assertIn("previous_receipt_checksum", round_validator)
		self.assertIn("owner_submission_checksum", round_validator)
		self.assertIn("receipt_checksum", round_validator)
		self.assertIn("MAX_ACTION_VERIFICATION_ROUNDS", chain)
		self.assertIn("_action_verification_round_snapshot", chain)
		self.assertIn("terminal quality action verification receipt", chain)
		self.assertIn("chronologically ordered", chain)
		self.assertIn("overdue", workbench)
		self.assertIn("Pending Verification", workbench)
		self.assertIn("MAX_WORKBENCH_ROWS", workbench)
		self.assertIn("requestKey()", self.desk)
		self.assertGreaterEqual(self.api.count("request_key: str"), 3)

	def test_experience_content_is_structured_deidentified_and_never_source_narrative(self) -> None:
		content = _function_source(SERVICE, "_experience_content")
		deidentified = _function_source(SERVICE, "_validate_deidentification")
		source = _function_source(SERVICE, "_validate_experience_source")
		self.assertIn("contain exactly", content)
		self.assertIn("_DIRECT_IDENTIFIER_PATTERNS", deidentified)
		self.assertIn("_PROHIBITED_EXPERIENCE_KEYS", deidentified)
		self.assertIn("No source narrative/content is loaded here", self.service)
		self.assertIn('{"Confirmed", "Closed"}', source)
		self.assertIn('"Approved"', source)
		self.assertNotIn(".narrative", self.service)
		self.assertNotIn('get("narrative")', self.service)
		self.assertNotIn('get("patient")', self.service)
		self.assertNotIn('get("encounter")', self.service)

	def test_every_custom_endpoint_is_post_only(self) -> None:
		tree = ast.parse(self.api)
		functions = [
			node for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
		]
		self.assertGreaterEqual(len(functions), 16)
		for function in functions:
			with self.subTest(endpoint=function.name):
				decorators = [
					ast.get_source_segment(self.api, decorator) or "" for decorator in function.decorator_list
				]
				self.assertTrue(
					any('frappe.whitelist(methods=["POST"])' in item for item in decorators),
					f"{function.name} is not POST-only",
				)

	def test_desk_actions_call_governed_api_using_post_and_do_not_save_status_directly(self) -> None:
		self.assertIn('type: "POST"', self.desk)
		for method in (
			"submit_quality_meeting",
			"record_quality_meeting_held",
			"review_meeting_minute",
			"submit_quality_action_verification",
			"review_quality_experience_share",
		):
			self.assertIn(method, self.desk)
		self.assertNotIn('frm.set_value("status"', self.desk)
		self.assertNotIn("frm.save(", self.desk)

	def test_all_eight_schemas_and_exact_state_contracts_are_generated(self) -> None:
		for name in DOCTYPE_SLUGS:
			with self.subTest(doctype=name):
				payload = _doctype(name)
				self.assertEqual(payload["name"], name)

		self.assertEqual(_doctype("IONE Meeting Agenda Item").get("istable"), 1)
		self.assertEqual(_doctype("IONE Meeting Attendee").get("istable"), 1)
		self.assertEqual(
			_select_options(_doctype("IONE Quality Meeting"), "status"),
			["Draft", "Submitted", "Approved", "Held", "Minutes Pending", "Closed", "Cancelled"],
		)
		self.assertEqual(
			_select_options(_doctype("IONE Meeting Minute"), "status"),
			["Draft", "Submitted", "Approved", "Rejected"],
		)
		self.assertEqual(
			_select_options(_doctype("IONE Quality Action Item"), "status"),
			["Open", "In Progress", "Pending Verification", "Closed", "Cancelled"],
		)
		self.assertEqual(
			_select_options(_doctype("IONE Quality Action Verification Round"), "decision"),
			["Close", "Rework", "Cancel"],
		)
		self.assertEqual(
			_select_options(_doctype("IONE Quality Experience Share"), "status"),
			["Draft", "Submitted", "Published", "Rejected", "Retired"],
		)

	def test_meeting_metadata_requires_named_approvers_without_invented_governance_defaults(
		self,
	) -> None:
		meeting = _doctype("IONE Quality Meeting")
		fields = {field["fieldname"] for field in meeting["fields"]}
		self.assertTrue(_field(meeting, "meeting_approver").get("reqd"))
		self.assertTrue(_field(meeting, "minute_approver").get("reqd"))
		self.assertTrue(_field(meeting, "meeting_type").get("reqd"))
		self.assertFalse(_field(meeting, "meeting_type").get("default"))
		self.assertFalse(
			fields
			& {
				"quorum",
				"quorum_count",
				"committee",
				"committee_members",
				"notice_period",
				"required_meeting_type",
			}
		)

	def test_materialized_records_are_readonly_and_native_export_is_denied(self) -> None:
		for name in (
			"IONE Meeting Decision",
			"IONE Quality Action Item",
			"IONE Quality Action Verification Round",
		):
			with self.subTest(materialized=name):
				for permission in _doctype(name)["permissions"]:
					for action in ("create", "write", "delete", "export", "print"):
						self.assertFalse(permission.get(action), f"{name} grants {action}")

		for name in (
			"IONE Quality Meeting",
			"IONE Meeting Minute",
			"IONE Meeting Decision",
			"IONE Quality Action Item",
			"IONE Quality Action Verification Round",
			"IONE Quality Experience Share",
		):
			with self.subTest(governed=name):
				for permission in _doctype(name)["permissions"]:
					self.assertFalse(permission.get("delete"), f"{name} grants delete")
					self.assertFalse(permission.get("export"), f"{name} grants export")
					self.assertFalse(permission.get("print"), f"{name} grants print")

	def test_minute_and_publication_concurrency_slots_are_unique(self) -> None:
		agenda = _doctype("IONE Meeting Agenda Item")
		meeting = _doctype("IONE Quality Meeting")
		minute = _doctype("IONE Meeting Minute")
		decision = _doctype("IONE Meeting Decision")
		experience = _doctype("IONE Quality Experience Share")
		action = _doctype("IONE Quality Action Item")
		action_round = _doctype("IONE Quality Action Verification Round")
		for payload, fieldname in (
			(agenda, "agenda_key"),
			(meeting, "meeting_code"),
			(minute, "approved_meeting_key"),
			(decision, "decision_key"),
			(action, "current_submission_key"),
			(action_round, "round_key"),
			(action_round, "owner_submission_key"),
			(action_round, "verifier_decision_key"),
			(experience, "version_key"),
			(experience, "active_publication_key"),
		):
			with self.subTest(doctype=payload["name"], field=fieldname):
				self.assertTrue(_field(payload, fieldname).get("unique"))

	def test_experience_schema_cannot_store_patient_encounter_or_raw_source_ids(self) -> None:
		experience = _doctype("IONE Quality Experience Share")
		fields = {field["fieldname"] for field in experience["fields"]}
		for forbidden in (
			"patient",
			"patient_key",
			"patient_reference",
			"encounter",
			"encounter_key",
			"encounter_reference",
			"source_record_id",
			"source_patient_id",
			"source_encounter_id",
			"raw_source_id",
		):
			with self.subTest(field=forbidden):
				self.assertNotIn(forbidden, fields)

	def test_hooks_scope_export_workbench_and_overdue_notification_are_wired(self) -> None:
		hooks = _source(HOOKS)
		permissions = _source(PERMISSIONS)
		data_export = _source(DATA_EXPORT)
		scope = _source(SCOPE)
		quality_tasks = _source(QUALITY_TASKS)
		workspace = _source(WORKSPACE)
		workbench_js = _source(WORKBENCH_ROOT / "ione_quality_action_workbench.js")
		workbench_json = json.loads(_source(WORKBENCH_ROOT / "ione_quality_action_workbench.json"))

		for name in (
			"IONE Quality Meeting",
			"IONE Meeting Minute",
			"IONE Meeting Decision",
			"IONE Quality Action Item",
			"IONE Quality Action Verification Round",
			"IONE Quality Experience Share",
		):
			with self.subTest(doctype=name):
				minimum_hook_references = 3 if name == "IONE Quality Action Verification Round" else 4
				self.assertGreaterEqual(hooks.count(f'"{name}"'), minimum_hook_references)
				self.assertIn(f'"{name}"', permissions)
				self.assertIn(f'"{name}"', data_export)
				self.assertIn(f'"{name}"', scope)
				self.assertIn(name, workspace)

		self.assertIn('"IONE Quality Action Item"', quality_tasks)
		self.assertIn('"verifier"', quality_tasks)
		self.assertIn('type: "POST"', workbench_js)
		self.assertIn("quality_action_workbench", workbench_js)
		self.assertEqual(workbench_json["page_name"], "ione-quality-action-workbench")


if __name__ == "__main__":
	import unittest

	unittest.main()

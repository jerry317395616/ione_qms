from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.api.quality import _safety_report_idempotency_key
from ione_qms.integration.projections import materialize_event_projection
from ione_qms.services.projections import system_audit_identity
from ione_qms.services.safety_events import validate_medical_safety_event


class _SafetyDoc:
	def __init__(self, values: dict | None = None, *, before: dict | None = None) -> None:
		object.__setattr__(
			self,
			"_values",
			{
				"event_type": "Medication Error",
				"severity": "Medium",
				"narrative": "A clinically meaningful safety event narrative.",
				"status": "Reported",
				"anonymous_report": 0,
				"near_miss": 0,
				"report_channel": "Clinical Integration",
				"owner": "integration@example.test",
				**(values or {}),
			},
		)
		object.__setattr__(self, "_before", _SafetyDoc(before) if before is not None else None)
		object.__setattr__(self, "flags", SimpleNamespace(ione_projection_materializer=True))

	def __getattr__(self, fieldname: str):
		try:
			return self._values[fieldname]
		except KeyError as exc:
			raise AttributeError(fieldname) from exc

	def __setattr__(self, fieldname: str, value) -> None:
		if fieldname.startswith("_") or fieldname == "flags":
			object.__setattr__(self, fieldname, value)
		else:
			self._values[fieldname] = value

	def get(self, fieldname: str, default=None):
		return self._values.get(fieldname, default)

	def is_new(self) -> bool:
		return self._before is None

	def get_doc_before_save(self):
		return self._before


class TestSafetyEventGovernance(TestCase):
	def assert_validation_error(self, doc: _SafetyDoc, message: str) -> None:
		def raise_validation_error(actual_message, *args, **kwargs):
			del args, kwargs
			raise frappe.ValidationError(str(actual_message))

		with (
			patch(
				"ione_qms.services.safety_events.frappe.throw",
				side_effect=raise_validation_error,
			),
			self.assertRaisesRegex(frappe.ValidationError, message),
		):
			validate_medical_safety_event(doc)

	def test_direct_creation_is_rejected(self) -> None:
		doc = _SafetyDoc()
		doc.flags.ione_projection_materializer = False
		self.assert_validation_error(doc, "governed report or integration path")

	def test_new_projection_cannot_skip_to_closed(self) -> None:
		doc = _SafetyDoc(
			{
				"status": "Closed",
				"investigation_summary": "Investigation is complete.",
				"root_cause_analysis": "Root cause was established.",
				"improvement_action": "Improvement action was completed.",
				"verification_outcome": "Verification confirmed effectiveness.",
				"lessons_learned": "Lessons were documented.",
			}
		)
		self.assert_validation_error(doc, "workflow in Reported status")

	def test_anonymous_projection_cannot_persist_identity_or_timestamp(self) -> None:
		for leaked_field, leaked_value in (
			("reported_by", "reporter@example.test"),
			("reported_at", "2026-07-30 08:00:00"),
		):
			with self.subTest(leaked_field=leaked_field):
				doc = _SafetyDoc(
					{
						"anonymous_report": 1,
						"report_channel": "Anonymous Portal",
						"owner": "Administrator",
						leaked_field: leaked_value,
					}
				)
				self.assert_validation_error(doc, "cannot store")

	def test_anonymous_projection_requires_technical_owner(self) -> None:
		doc = _SafetyDoc(
			{
				"anonymous_report": 1,
				"report_channel": "Anonymous Portal",
				"owner": "reporter@example.test",
			}
		)
		self.assert_validation_error(doc, "technical audit identity")

	def test_improving_state_cannot_clear_root_cause_or_action(self) -> None:
		doc = _SafetyDoc(
			{
				"status": "Improving",
				"investigation_summary": "Investigation is complete.",
				"root_cause_analysis": "Root cause was established.",
				"improvement_action": "",
			},
			before={
				"status": "Improving",
				"investigation_summary": "Investigation is complete.",
				"root_cause_analysis": "Root cause was established.",
				"improvement_action": "Improvement action was underway.",
			},
		)
		self.assert_validation_error(doc, "documented improvement action")

	def test_close_requires_verification_and_lessons(self) -> None:
		doc = _SafetyDoc(
			{
				"status": "Closed",
				"investigation_summary": "Investigation is complete.",
				"root_cause_analysis": "Root cause was established.",
				"improvement_action": "Improvement action was completed.",
				"verification_outcome": "",
				"lessons_learned": "Lessons were documented.",
			},
			before={"status": "Improving"},
		)
		with (
			patch(
				"ione_qms.services.safety_events.frappe.session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch(
				"ione_qms.services.safety_events.frappe.get_roles",
				return_value=["IONE QC Reviewer"],
			),
		):
			self.assert_validation_error(doc, "verification outcome")

	def test_administrator_cannot_perform_business_workflow_transition(self) -> None:
		doc = _SafetyDoc(
			{
				"status": "Under Review",
			},
			before={"status": "Reported"},
		)
		with patch(
			"ione_qms.services.safety_events.frappe.session",
			SimpleNamespace(user="Administrator"),
		):
			self.assert_validation_error(doc, "named accountable reviewer")

	def test_close_stamps_accountable_reviewer_after_all_gates(self) -> None:
		closed_at = datetime(2026, 7, 30, 9, 0, 0)
		doc = _SafetyDoc(
			{
				"status": "Closed",
				"investigation_summary": "Investigation is complete.",
				"root_cause_analysis": "Root cause was established.",
				"improvement_action": "Improvement action was completed.",
				"verification_outcome": "Verification confirmed effectiveness.",
				"lessons_learned": "Lessons were documented.",
			},
			before={"status": "Improving"},
		)
		with (
			patch(
				"ione_qms.services.safety_events.frappe.session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch(
				"ione_qms.services.safety_events.frappe.get_roles",
				return_value=["IONE QC Reviewer"],
			),
			patch("ione_qms.services.safety_events.now_datetime", return_value=closed_at),
		):
			validate_medical_safety_event(doc)
		self.assertEqual(doc.get("closed_by"), "reviewer@example.test")
		self.assertEqual(doc.get("closed_at"), closed_at)


class TestSafetyProjectionContracts(TestCase):
	@patch(
		"ione_qms.integration.projections.materialize_medical_safety_event",
		return_value="SAFETY-1",
	)
	def test_false_string_does_not_trigger_anonymous_system_owner(self, materialize) -> None:
		result = materialize_event_projection(
			"EVENT-1",
			"SafetyEvent.Reported",
			{
				"safety_event_type": "Medication Error",
				"safety_event_time": "2026-07-30 08:00:00",
				"severity": "Medium",
				"narrative": "A clinically meaningful safety event narrative.",
				"anonymous_report": "false",
				"report_channel": "Clinical Integration",
			},
		)
		self.assertEqual(result, "SAFETY-1")
		self.assertIs(materialize.call_args.args[0]["_system_owner"], False)

	def test_external_projection_cannot_enter_local_workflow_as_closed(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			materialize_event_projection(
				"EVENT-2",
				"SafetyEvent.Reported",
				{
					"safety_event_type": "Medication Error",
					"safety_event_time": "2026-07-30 08:00:00",
					"severity": "Medium",
					"narrative": "A clinically meaningful safety event narrative.",
					"safety_status": "Closed",
				},
			)

	def test_external_projection_cannot_assert_internal_reporter(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			materialize_event_projection(
				"EVENT-3",
				"SafetyEvent.Reported",
				{
					"safety_event_type": "Medication Error",
					"safety_event_time": "2026-07-30 08:00:00",
					"severity": "Medium",
					"narrative": "A clinically meaningful safety event narrative.",
					"reported_by": "staff@example.test",
				},
			)

	def test_anonymous_receipt_key_does_not_encode_reporter_principal(self) -> None:
		first = _safety_report_idempotency_key(
			request_id="088e63db-bc25-4b04-a046-97f63822b8aa",
			reporter="first@example.test",
			anonymous=True,
		)
		second = _safety_report_idempotency_key(
			request_id="088e63db-bc25-4b04-a046-97f63822b8aa",
			reporter="second@example.test",
			anonymous=True,
		)
		self.assertEqual(first, second)
		self.assertNotIn("first@example.test", first)

	def test_system_audit_identity_restores_request_principal_and_sid(self) -> None:
		session = SimpleNamespace(user="reporter@example.test", sid="opaque-session-id")
		with patch(
			"ione_qms.services.projections.frappe.local",
			SimpleNamespace(session=session),
		):
			with self.assertRaisesRegex(RuntimeError, "persistence failed"):
				with system_audit_identity():
					self.assertEqual(session.user, "Administrator")
					self.assertEqual(session.sid, "opaque-session-id")
					raise RuntimeError("persistence failed")
		self.assertEqual(session.user, "reporter@example.test")
		self.assertEqual(session.sid, "opaque-session-id")

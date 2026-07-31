from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.services import quality_meetings as meetings


class _Doc(dict):
	def __init__(self, doctype: str = "", name: str = "", **values) -> None:
		super().__init__(values)
		self.doctype = doctype
		self.name = name

	def __getattr__(self, fieldname: str):
		try:
			return self[fieldname]
		except KeyError as exc:
			raise AttributeError(fieldname) from exc

	def __setattr__(self, fieldname: str, value) -> None:
		if fieldname in {"doctype", "name"}:
			object.__setattr__(self, fieldname, value)
		else:
			self[fieldname] = value

	def set(self, fieldname: str, value) -> None:
		self[fieldname] = value


def _scope(**overrides) -> dict:
	values = {
		"hospital": "HOSP-1",
		"campus": "CAMPUS-1",
		"department": "DEPT-1",
		"ward": "",
	}
	values.update(overrides)
	return values


class TestQualityMeetingContracts(TestCase):
	def test_workflow_transitions_do_not_allow_ai_or_approval_shortcuts(self) -> None:
		self.assertEqual(meetings.MEETING_TRANSITIONS["Draft"], {"Submitted", "Cancelled"})
		self.assertEqual(meetings.MEETING_TRANSITIONS["Submitted"], {"Approved", "Cancelled"})
		self.assertEqual(meetings.MEETING_TRANSITIONS["Approved"], {"Held", "Cancelled"})
		self.assertEqual(meetings.MEETING_TRANSITIONS["Held"], {"Minutes Pending"})
		self.assertEqual(meetings.MEETING_TRANSITIONS["Minutes Pending"], {"Closed"})
		self.assertEqual(meetings.MINUTE_TRANSITIONS["Draft"], {"Submitted"})
		self.assertEqual(meetings.MINUTE_TRANSITIONS["Submitted"], {"Approved", "Rejected"})

	def test_agenda_checksum_freezes_plan_but_not_recorded_attendance(self) -> None:
		agenda = _Doc(
			agenda_key="a" * 64,
			subject="Monthly quality review",
			objective="Review approved aggregate quality report.",
			presenter="reviewer@example.test",
			duration_minutes=30,
			material_type="AI Report Draft",
			ai_report_draft="REPORT-1",
			material_reference="",
			material_checksum="b" * 64,
		)
		attendee = _Doc(
			user="chair@example.test",
			participation_role="Chair",
			required_attendee=1,
			attendance_status="Pending",
		)
		doc = _Doc(
			"IONE Quality Meeting",
			"MEETING-1",
			meeting_code="QM-2026-07",
			title="Monthly quality meeting",
			meeting_type="Configured review",
			**_scope(),
			scheduled_start="2026-07-30 09:00:00",
			scheduled_end="2026-07-30 10:00:00",
			chair="chair@example.test",
			secretary="",
			meeting_approver="approver@example.test",
			minute_approver="minute@example.test",
			agenda_items=[agenda],
			attendees=[attendee],
		)
		before = meetings._meeting_agenda_checksum(doc)
		attendee.attendance_status = "Attended"
		attendee.joined_at = "2026-07-30 09:00:00"
		attendee.left_at = "2026-07-30 10:00:00"
		attendee.attendance_evidence = "ATTENDANCE-LOG-1"
		self.assertEqual(before, meetings._meeting_agenda_checksum(doc))
		agenda.objective = "Changed after approval"
		self.assertNotEqual(before, meetings._meeting_agenda_checksum(doc))

	def test_attendance_requires_exact_manifest_and_chair_participation(self) -> None:
		doc = _Doc(
			"IONE Quality Meeting",
			"MEETING-1",
			chair="chair@example.test",
			attendees=[
				_Doc(user="chair@example.test"),
				_Doc(user="member@example.test"),
			],
		)
		rows = meetings._attendance_rows(
			[
				{
					"user": "chair@example.test",
					"attendance_status": "Attended",
					"joined_at": "2026-07-30 09:00:00",
					"left_at": "2026-07-30 10:00:00",
					"attendance_evidence": "SIGNED-CHAIR-LOG",
					"absence_reason": "",
				},
				{
					"user": "member@example.test",
					"attendance_status": "Absent",
					"joined_at": None,
					"left_at": None,
					"attendance_evidence": "SIGNED-ABSENCE-LOG",
					"absence_reason": "Approved clinical duty conflict",
				},
			],
			frappe.utils.get_datetime("2026-07-30 09:00:00"),
			frappe.utils.get_datetime("2026-07-30 10:00:00"),
		)
		meetings._apply_attendance(
			doc,
			rows,
			frappe.utils.get_datetime("2026-07-30 09:00:00"),
			frappe.utils.get_datetime("2026-07-30 10:00:00"),
		)
		self.assertEqual(doc.attendees[0].attendance_status, "Attended")
		self.assertEqual(doc.attendees[1].attendance_status, "Absent")

		rows[0]["attendance_status"] = "Absent"
		rows[0]["joined_at"] = None
		rows[0]["left_at"] = None
		with self.assertRaises(frappe.ValidationError):
			meetings._apply_attendance(
				doc,
				rows,
				frappe.utils.get_datetime("2026-07-30 09:00:00"),
				frappe.utils.get_datetime("2026-07-30 10:00:00"),
			)

	def test_decisions_json_is_exact_bounded_and_canonical(self) -> None:
		rows = meetings._decision_rows(
			[
				{
					"decision_code": "D-1",
					"decision_text": "Adopt the reviewed corrective action.",
					"rationale": "Evidence demonstrates a recurring governed issue.",
				}
			]
		)
		self.assertEqual(rows[0]["decision_code"], "D-1")
		with self.assertRaises(frappe.ValidationError):
			meetings._decision_rows(
				[
					{
						"decision_code": "D-1",
						"decision_text": "Adopt the reviewed corrective action.",
						"rationale": "Evidence demonstrates a recurring governed issue.",
						"patient": "PATIENT-SECRET",
					}
				]
			)

	def test_action_links_inherit_the_most_specific_compatible_scope(self) -> None:
		meeting = _Doc(
			"IONE Quality Meeting",
			"MEETING-1",
			status="Closed",
			**_scope(campus="", department="", ward=""),
		)
		finding = _Doc(
			"IONE QC Finding",
			"FINDING-1",
			status="Confirmed",
			**_scope(),
		)

		def get_doc(doctype: str, name: str):
			return {
				("IONE Quality Meeting", "MEETING-1"): meeting,
				("IONE QC Finding", "FINDING-1"): finding,
			}[(doctype, name)]

		with patch.object(meetings.frappe, "get_doc", side_effect=get_doc):
			links, selected_scope = meetings._resolved_action_links(
				meeting="MEETING-1",
				meeting_decision=None,
				finding="FINDING-1",
				pdca_project=None,
				safety_event=None,
			)
		self.assertEqual(links["meeting"], "MEETING-1")
		self.assertEqual(selected_scope.department, "DEPT-1")

	def test_action_verification_rounds_form_an_append_only_checksum_chain(self) -> None:
		action = _Doc(
			"IONE Quality Action Item",
			"ACTION-1",
			assigned_to="owner@example.test",
			verifier="verifier@example.test",
			**_scope(),
		)

		def round_row(round_no: int, decision: str, previous: str) -> _Doc:
			row = _Doc(
				"IONE Quality Action Verification Round",
				f"ROUND-{round_no}",
				round_key=meetings._checksum({"action_item": action.name, "round_no": round_no}),
				action_item=action.name,
				round_no=round_no,
				**_scope(),
				assigned_to_snapshot=action.assigned_to,
				verifier_snapshot=action.verifier,
				completion_evidence_snapshot=f"Completed governed action evidence round {round_no}.",
				effect_result_snapshot=f"Measured governed effect result round {round_no}.",
				owner_submission_key=f"owner-request-000{round_no}",
				submitted_by=action.assigned_to,
				submitted_at=f"2026-07-{28 + round_no:02d} 09:00:00",
				decision=decision,
				verification_comment=f"Independent decision for governed round {round_no}.",
				verifier_decision_key=f"verifier-request-000{round_no}",
				verified_by=action.verifier,
				verified_at=f"2026-07-{28 + round_no:02d} 10:00:00",
				action_status_after="In Progress" if decision == "Rework" else "Closed",
				previous_receipt_checksum=previous,
			)
			row.completion_evidence_hash = meetings._action_evidence_hash(row)
			row.owner_submission_checksum = meetings._action_owner_submission_checksum(row)
			row.receipt_checksum = meetings._checksum(meetings._action_verification_round_snapshot(row))
			return row

		first = round_row(1, "Rework", "")
		second = round_row(2, "Close", first.receipt_checksum)
		with patch.object(meetings.frappe, "get_all", return_value=[first, second]):
			rows = meetings._validated_action_verification_chain(action)
		self.assertEqual([row.round_no for row in rows], [1, 2])

		second.previous_receipt_checksum = "f" * 64
		with (
			patch.object(meetings.frappe, "get_all", return_value=[first, second]),
			self.assertRaises(frappe.ValidationError),
		):
			meetings._validated_action_verification_chain(action)

		second.previous_receipt_checksum = first.receipt_checksum
		first.decision = "Close"
		first.action_status_after = "Closed"
		first.receipt_checksum = meetings._checksum(meetings._action_verification_round_snapshot(first))
		second.previous_receipt_checksum = first.receipt_checksum
		second.receipt_checksum = meetings._checksum(meetings._action_verification_round_snapshot(second))
		with (
			patch.object(meetings.frappe, "get_all", return_value=[first, second]),
			self.assertRaises(frappe.ValidationError),
		):
			meetings._validated_action_verification_chain(action)

	def test_action_idempotency_keys_are_opaque_bounded_identifiers(self) -> None:
		self.assertEqual(
			meetings._idempotency_key("owner-request-0001"),
			"owner-request-0001",
		)
		with self.assertRaises(frappe.ValidationError):
			meetings._idempotency_key("contains patient narrative")

	def test_experience_content_is_structured_and_rejects_direct_identifiers(self) -> None:
		content = {
			"context_summary": "A deidentified aggregate service context.",
			"issue_pattern": "A recurring process control gap was confirmed.",
			"improvement_actions": ["Apply the independently approved control."],
			"verified_outcome": "The configured quality measure improved after verification.",
			"lessons": "Independent verification is required before closure.",
			"applicability": "Applicable only after local governance review.",
		}
		normalized = meetings._experience_content(content)
		doc = _Doc(
			deidentification_attested=1,
			deidentification_method="Manual removal and independent structured review.",
			safety_event="EVENT-1",
			meeting_minute="",
		)
		meetings._validate_deidentification(doc, normalized)
		normalized["lessons"] = "Contact reviewer@example.test for the original patient narrative."
		with self.assertRaises(frappe.ValidationError):
			meetings._validate_deidentification(doc, normalized)

	def test_experience_checksum_is_canonical_and_version_bound(self) -> None:
		first = {
			"experience_code": "EXP-1",
			"version_number": 1,
			"content": {"lessons": "Independently verified"},
		}
		second = {
			"content": {"lessons": "Independently verified"},
			"version_number": 1,
			"experience_code": "EXP-1",
		}
		self.assertEqual(meetings._checksum(first), meetings._checksum(second))
		self.assertNotEqual(
			meetings._checksum(first),
			meetings._checksum({**first, "version_number": 2}),
		)


if __name__ == "__main__":
	import unittest

	unittest.main()

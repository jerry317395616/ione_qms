from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services.findings import (
	_is_controlled_rectification_sync,
	_reject_self_approval,
	_require_verified_closed_rectification,
	_validate_department_transition_only,
)


class _Finding:
	def __init__(self, **values) -> None:
		self.doctype = "IONE QC Finding"
		self.name = values.get("name", "QF-0001")
		self.values = values
		self.meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname=fieldname)
				for fieldname in ("status", "title", "description", "severity", "due_date")
			]
		)

	def get(self, fieldname: str):
		return self.values.get(fieldname)


class TestFindingGuardrails(TestCase):
	def test_department_actor_can_transition_but_cannot_edit_content(self) -> None:
		previous = {
			"status": "Confirmed",
			"title": "Original",
			"description": "Reviewed",
			"severity": "High",
			"due_date": "2026-08-01",
		}
		doc = _Finding(**{**previous, "status": "Appealed"})
		with patch(
			"ione_qms.services.findings.frappe.get_roles",
			return_value=["IONE Physician"],
		):
			_validate_department_transition_only(doc, previous)
		doc.values["title"] = "Changed"
		with (
			patch(
				"ione_qms.services.findings.frappe.get_roles",
				return_value=["IONE Physician"],
			),
			patch(
				"ione_qms.services.findings.frappe.throw",
				side_effect=RuntimeError("status only"),
			),
			self.assertRaisesRegex(RuntimeError, "status only"),
		):
			_validate_department_transition_only(doc, previous)

	def test_maker_cannot_approve_own_finding_outside_explicit_edges(self) -> None:
		doc = _Finding(owner="maker@example.test")
		with (
			patch(
				"ione_qms.services.findings.frappe.session",
				SimpleNamespace(user="maker@example.test"),
			),
			patch(
				"ione_qms.services.findings.frappe.throw",
				side_effect=RuntimeError("self approval"),
			),
			self.assertRaisesRegex(RuntimeError, "self approval"),
		):
			_reject_self_approval(doc, "Pending QC Review", "Confirmed")

	def test_explicit_execution_edge_allows_self_action(self) -> None:
		doc = _Finding(owner="maker@example.test")
		with patch(
			"ione_qms.services.findings.frappe.session",
			SimpleNamespace(user="maker@example.test"),
		):
			_reject_self_approval(doc, "Confirmed", "Appealed")

	def test_rectification_sync_capability_is_exact_and_database_backed(self) -> None:
		doc = _Finding(name="QF-0001")
		context = {
			"finding": "QF-0001",
			"rectification": "RECT-0001",
			"target_status": "Pending Functional Review",
		}
		with (
			patch(
				"ione_qms.services.findings.frappe.flags",
				SimpleNamespace(ione_rectification_finding_sync=context),
			),
			patch(
				"ione_qms.services.findings.frappe.db.get_value",
				return_value={
					"finding": "QF-0001",
					"status": "Pending Functional Review",
				},
			),
		):
			self.assertTrue(_is_controlled_rectification_sync(doc, "Pending Functional Review"))
			self.assertFalse(_is_controlled_rectification_sync(doc, "Closed"))

	def test_finding_closure_requires_verified_closed_rectification(self) -> None:
		with (
			patch(
				"ione_qms.services.findings.frappe.get_all",
				return_value=["RECT-0001"],
			),
			patch(
				"ione_qms.services.findings.frappe.db.exists",
				return_value="VER-0001",
			),
		):
			_require_verified_closed_rectification("QF-0001")
		with (
			patch("ione_qms.services.findings.frappe.get_all", return_value=[]),
			patch(
				"ione_qms.services.findings.frappe.throw",
				side_effect=RuntimeError("verification required"),
			),
			self.assertRaisesRegex(RuntimeError, "verification required"),
		):
			_require_verified_closed_rectification("QF-0001")

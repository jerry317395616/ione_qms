from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.permissions import finding_permission, qc_finding_query


class TestFindingReviewerPermissions(TestCase):
	def test_agent_reviewer_query_adds_only_the_accepted_formal_relationship(self) -> None:
		with (
			patch("ione_qms.permissions._clinical_condition", return_value="1=0"),
			patch(
				"ione_qms.permissions.frappe.get_roles",
				return_value=["IONE Agent Reviewer"],
			),
			patch(
				"ione_qms.permissions.frappe.db.escape",
				return_value="'reviewer@example.test'",
			),
		):
			condition = qc_finding_query("reviewer@example.test")
		self.assertIn("candidate.formal_finding = `tabIONE QC Finding`.name", condition)
		self.assertIn("candidate.reviewed_by = 'reviewer@example.test'", condition)
		self.assertIn("candidate.status = 'Accepted'", condition)

	def test_accepted_candidate_relationship_grants_read_but_not_write(self) -> None:
		doc = SimpleNamespace(doctype="IONE QC Finding", name="QF-0001")
		with (
			patch("ione_qms.permissions.frappe.flags", SimpleNamespace()),
			patch(
				"ione_qms.permissions.frappe.get_roles",
				return_value=["IONE Agent Reviewer"],
			),
			patch("ione_qms.permissions.frappe.db.exists", return_value=True),
			patch("ione_qms.permissions._clinical_permission", return_value=False),
		):
			self.assertTrue(finding_permission(doc, "read", user="reviewer@example.test"))
			self.assertFalse(finding_permission(doc, "write", user="reviewer@example.test"))

	def test_temporary_capability_is_bound_to_exact_user_and_finding(self) -> None:
		doc = SimpleNamespace(doctype="IONE QC Finding", name="QF-0001")
		flags = SimpleNamespace(
			ione_candidate_reviewer="reviewer@example.test",
			ione_candidate_review_finding="QF-0001",
		)
		with (
			patch("ione_qms.permissions.frappe.flags", flags),
			patch("ione_qms.permissions.frappe.get_roles", return_value=[]),
			patch("ione_qms.permissions._clinical_permission", return_value=False),
		):
			self.assertTrue(finding_permission(doc, "write", user="reviewer@example.test"))
			self.assertFalse(finding_permission(doc, "write", user="other@example.test"))
			self.assertFalse(
				finding_permission(
					SimpleNamespace(doctype="IONE QC Finding", name="QF-0002"),
					"write",
					user="reviewer@example.test",
				)
			)

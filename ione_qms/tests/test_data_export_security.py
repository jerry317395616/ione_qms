from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ione_qms import permissions
from ione_qms.services import data_export


def _export_request(
	*,
	scope_mode: str = "Department",
	hospital: str | None = "HOSP-1",
	campus: str | None = "CAMPUS-1",
	department: str | None = "DEPT-1",
	reference_doctype: str = "IONE QC Finding",
	requested_by: str = "requester@example.test",
	status: str = "Pending",
) -> SimpleNamespace:
	doc = SimpleNamespace(
		doctype="IONE Data Export Request",
		name="IONE-EXP-1",
		request_id="IONE-EXP-1",
		scope_mode=scope_mode,
		hospital=hospital,
		campus=campus,
		department=department,
		reference_doctype=reference_doctype,
		requested_by=requested_by,
		status=status,
		expires_at=None,
		output_file=None,
		record_count=0,
		flags=SimpleNamespace(),
	)
	doc.check_permission = MagicMock()
	doc.save = MagicMock()
	doc.get = lambda fieldname: getattr(doc, fieldname, None)
	return doc


def _context(*roles: str, user: str = "auditor@example.test") -> permissions.AccessContext:
	return permissions.AccessContext(
		user=user,
		roles=frozenset(roles),
		hospitals=frozenset(),
		campuses=frozenset(),
		departments=frozenset(),
		wards=frozenset(),
		staff_records=frozenset(),
	)


class TestDataExportScopeSecurity(TestCase):
	def test_department_scope_is_frozen_with_verified_parents(self) -> None:
		meta = SimpleNamespace(
			get_field=lambda fieldname: (
				SimpleNamespace(
					fieldtype="Link",
					options=data_export._SCOPE_LINKS[fieldname],
				)
				if fieldname in data_export._SCOPE_LINKS
				else None
			)
		)
		with (
			patch.object(data_export.frappe, "get_meta", return_value=meta),
			patch.object(
				data_export.frappe.db,
				"get_value",
				return_value={"hospital": "HOSP-1", "campus": "CAMPUS-1"},
			),
			patch.object(data_export, "_require_requester_scope") as require_scope,
		):
			scope = data_export._derive_export_scope(
				{"department": "DEPT-1"},
				"IONE QC Finding",
				"requester@example.test",
			)
		self.assertEqual(
			scope,
			{
				"scope_mode": "Department",
				"hospital": "HOSP-1",
				"campus": "CAMPUS-1",
				"department": "DEPT-1",
			},
		)
		require_scope.assert_called_once_with(
			doctype="IONE QC Finding",
			user="requester@example.test",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
		)

	def test_non_exact_and_absent_scope_filters_fail_to_privileged_modes(self) -> None:
		meta = SimpleNamespace(
			get_field=lambda fieldname: (
				SimpleNamespace(
					fieldtype="Link",
					options=data_export._SCOPE_LINKS[fieldname],
				)
				if fieldname in data_export._SCOPE_LINKS
				else None
			)
		)
		with patch.object(data_export.frappe, "get_meta", return_value=meta):
			multiple = data_export._derive_export_scope(
				{"department": ["in", ["DEPT-1", "DEPT-2"]]},
				"IONE QC Finding",
				"requester@example.test",
			)
			unscoped = data_export._derive_export_scope(
				{"status": "Confirmed"},
				"IONE QC Finding",
				"requester@example.test",
			)
		self.assertEqual(multiple["scope_mode"], "Multiple")
		self.assertEqual(unscoped["scope_mode"], "Unscoped")
		for scope in (multiple, unscoped):
			self.assertIsNone(scope["hospital"])
			self.assertIsNone(scope["campus"])
			self.assertIsNone(scope["department"])

	def test_auditor_requires_an_explicit_applicable_user_permission(self) -> None:
		doc = _export_request()
		cases = (
			((("IONE Medical Department", "DEPT-1", ""),), True),
			((("IONE Hospital Campus", "CAMPUS-1", ""),), True),
			((("IONE Hospital", "HOSP-1", ""),), True),
			((("IONE Medical Department", "DEPT-2", ""),), False),
			((("IONE Medical Department", "DEPT-1", "IONE Surgery QC"),), False),
			((("IONE Medical Department", "DEPT-1", "IONE QC Finding"),), True),
		)
		for grants, expected in cases:
			with (
				self.subTest(grants=grants),
				patch.object(data_export, "_explicit_scope_grants", return_value=grants),
			):
				self.assertIs(
					data_export.auditor_can_access_export_request(
						doc,
						"auditor@example.test",
					),
					expected,
				)

	def test_hospital_multiple_and_unscoped_exports_are_never_auditor_visible(self) -> None:
		with patch.object(
			data_export,
			"_explicit_scope_grants",
			return_value=(("IONE Hospital", "HOSP-1", ""),),
		):
			for mode in ("Hospital", "Multiple", "Unscoped"):
				with self.subTest(mode=mode):
					doc = _export_request(
						scope_mode=mode,
						campus=None,
						department=None,
					)
					self.assertFalse(
						data_export.auditor_can_access_export_request(
							doc,
							"auditor@example.test",
						)
					)

	def test_auditor_list_query_excludes_hospital_wide_scope(self) -> None:
		grants = (
			("IONE Medical Department", "DEPT-1", ""),
			("IONE Hospital Campus", "CAMPUS-1", ""),
			("IONE Hospital", "HOSP-1", ""),
		)
		with (
			patch.object(data_export, "_explicit_scope_grants", return_value=grants),
			patch.object(data_export.frappe.db, "escape", side_effect=lambda value: f"'{value}'"),
		):
			query = data_export.auditor_export_request_query("auditor@example.test")
		self.assertIn("scope_mode = 'Department'", query)
		self.assertIn("scope_mode in ('Department', 'Campus')", query)
		self.assertNotIn("scope_mode = 'Hospital'", query)
		self.assertNotIn("scope_mode in ('Department', 'Campus', 'Hospital')", query)

	def test_request_list_and_document_permissions_fail_closed_for_unscoped_auditor(self) -> None:
		context = _context("IONE Auditor")
		doc = _export_request(scope_mode="Unscoped", campus=None, department=None)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "auditor_export_request_query", return_value="1=0"),
			patch.object(permissions, "auditor_can_access_export_request", return_value=False),
		):
			self.assertEqual(
				permissions.data_export_request_query(context.user),
				"1=0",
			)
			self.assertFalse(
				permissions.data_export_request_permission(
					doc,
					"read",
					context.user,
				)
			)

	def test_medical_affairs_remains_global_but_administrator_is_denied(self) -> None:
		doc = _export_request(scope_mode="Unscoped", campus=None, department=None)
		medical_affairs = _context("IONE Medical Affairs", user="ma@example.test")
		with patch.object(permissions, "get_access_context", return_value=medical_affairs):
			self.assertEqual(permissions.data_export_request_query(medical_affairs.user), "")
			self.assertTrue(permissions.data_export_request_permission(doc, "read", medical_affairs.user))
		administrator = _context(user="Administrator")
		with patch.object(permissions, "get_access_context", return_value=administrator):
			self.assertEqual(permissions.data_export_request_query("Administrator"), "1=0")
			self.assertFalse(permissions.data_export_request_permission(doc, "read", "Administrator"))

	def test_private_export_file_rechecks_exact_completed_parent_permission(self) -> None:
		request = _export_request(status="Completed")
		request.output_file = "FILE-1"
		file_doc = SimpleNamespace(
			name="FILE-1",
			attached_to_doctype="IONE Data Export Request",
			attached_to_name=request.name,
		)
		with (
			patch.object(permissions, "_doctype_exists", return_value=True),
			patch.object(permissions.frappe, "get_doc", return_value=request),
			patch.object(permissions, "data_export_request_permission", return_value=True),
		):
			self.assertTrue(
				permissions._data_export_file_permission(
					file_doc,
					"read",
					"auditor@example.test",
				)
			)
			request.output_file = "OTHER-FILE"
			self.assertFalse(
				permissions._data_export_file_permission(
					file_doc,
					"read",
					"auditor@example.test",
				)
			)
			self.assertFalse(
				permissions._data_export_file_permission(
					file_doc,
					"write",
					"auditor@example.test",
				)
			)

	def test_scope_fields_are_required_read_only_links_in_metadata(self) -> None:
		path = (
			Path(__file__).resolve().parents[1]
			/ "ione_administration"
			/ "doctype"
			/ "ione_data_export_request"
			/ "ione_data_export_request.json"
		)
		payload = json.loads(path.read_text(encoding="utf-8"))
		fields = {field["fieldname"]: field for field in payload["fields"]}
		self.assertEqual(
			set(fields["scope_mode"]["options"].splitlines()),
			{"Department", "Campus", "Hospital", "Multiple", "Unscoped"},
		)
		self.assertEqual(fields["scope_mode"]["reqd"], 1)
		for fieldname, options in data_export._SCOPE_LINKS.items():
			self.assertEqual(fields[fieldname]["fieldtype"], "Link")
			self.assertEqual(fields[fieldname]["options"], options)
			self.assertEqual(fields[fieldname]["read_only"], 1)
			self.assertEqual(fields[fieldname]["ignore_user_permissions"], 1)


class TestDataExportDecisionConcurrency(TestCase):
	def test_requester_cannot_make_either_approval_decision(self) -> None:
		for action in (
			data_export.approve_export_request,
			data_export.reject_export_request,
		):
			with self.subTest(action=action.__name__):
				doc = _export_request(requested_by="requester@example.test")
				with (
					patch.object(
						data_export,
						"_export_request_lock",
						return_value=nullcontext(),
					),
					patch.object(data_export.frappe, "get_doc", return_value=doc),
					patch.object(data_export, "_require_export_approver"),
					patch.object(data_export, "_validate_request_as_requester") as validate,
					patch.object(
						data_export.frappe,
						"throw",
						side_effect=RuntimeError("separation"),
					),
					patch.object(
						data_export.frappe.session,
						"user",
						"requester@example.test",
					),
				):
					with self.assertRaisesRegex(RuntimeError, "separation"):
						action(doc.name, "reviewed decision comment")
				validate.assert_not_called()
				doc.save.assert_not_called()

	def test_approve_and_reject_share_lock_and_row_lock(self) -> None:
		approve_doc = _export_request()
		reject_doc = _export_request()
		lock = MagicMock(side_effect=[nullcontext(), nullcontext()])
		with (
			patch.object(data_export, "_export_request_lock", lock),
			patch.object(
				data_export.frappe,
				"get_doc",
				side_effect=[approve_doc, reject_doc],
			) as get_doc,
			patch.object(data_export, "_require_export_approver"),
			patch.object(data_export, "_validate_request_as_requester"),
			patch.object(data_export.frappe, "enqueue"),
			patch.object(data_export.frappe.session, "user", "approver@example.test"),
		):
			data_export.approve_export_request("IONE-EXP-1", "approved for scoped use")
			data_export.reject_export_request("IONE-EXP-1", "rejected after review")
		self.assertEqual(
			lock.call_args_list,
			[
				((approve_doc.name,), {}),
				((reject_doc.name,), {}),
			],
		)
		self.assertEqual(
			get_doc.call_args_list,
			[
				(("IONE Data Export Request", approve_doc.name), {"for_update": True}),
				(("IONE Data Export Request", reject_doc.name), {"for_update": True}),
			],
		)
		self.assertEqual(approve_doc.status, "Approved")
		self.assertEqual(reject_doc.status, "Rejected")
		approve_doc.save.assert_called_once_with(ignore_permissions=True)
		reject_doc.save.assert_called_once_with(ignore_permissions=True)

	def test_generation_reuses_decision_lock_and_row_lock(self) -> None:
		doc = _export_request(status="Completed")
		doc.output_file = "FILE-1"
		with (
			patch.object(
				data_export,
				"_export_request_lock",
				return_value=nullcontext(),
			) as lock,
			patch.object(data_export.frappe, "get_doc", return_value=doc) as get_doc,
		):
			result = data_export.generate_export_file(doc.name)
		lock.assert_called_once_with(doc.name)
		get_doc.assert_called_once_with(
			"IONE Data Export Request",
			doc.name,
			for_update=True,
		)
		self.assertEqual(result["file"], "FILE-1")

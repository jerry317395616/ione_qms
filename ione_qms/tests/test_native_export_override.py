from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import frappe

UPSTREAM_EXPORT_METHOD = "frappe.core.doctype.data_export.exporter.export_data"
QMS_EXPORT_METHOD = "ione_qms.overrides.data_export.export_data"


class TestNativeExportOverride(TestCase):
	def setUp(self) -> None:
		self.previous_user = frappe.session.user
		frappe.set_user("Administrator")

	def tearDown(self) -> None:
		frappe.set_user(self.previous_user)

	def test_exact_upstream_method_resolves_to_qms_guard(self) -> None:
		self.assertEqual(
			frappe.override_whitelisted_method(UPSTREAM_EXPORT_METHOD),
			QMS_EXPORT_METHOD,
		)

	def test_administrator_cannot_bypass_with_nested_or_malformed_arguments(self) -> None:
		method = frappe.get_attr(frappe.override_whitelisted_method(UPSTREAM_EXPORT_METHOD))
		with patch("ione_qms.overrides.data_export.frappe_export_data") as upstream:
			for doctype, parent_doctype in (
				([{"payload": ["IONE Medical Staff"]}], None),
				("Sales Invoice", '{"payload":"IONE Agent Release"}'),
				('["\\u0049\\u004f\\u004e\\u0045 QC Finding"', None),
			):
				with self.subTest(doctype=doctype, parent_doctype=parent_doctype):
					with self.assertRaises(frappe.PermissionError):
						method(doctype=doctype, parent_doctype=parent_doctype)
			upstream.assert_not_called()

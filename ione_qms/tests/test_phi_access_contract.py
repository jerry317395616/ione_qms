from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ione_qms.overrides.phi_query_guard import _contains_phi_field_reference
from ione_qms.privacy_registry import PROTECTED_FIELDS_BY_DOCTYPE
from ione_qms.services.phi_access import (
	PHI_FIELDS_BY_DOCTYPE,
	_normalize_requested_fields,
	_policy_family_lock,
)


class TestPHIAccessContract(IntegrationTestCase):
	TEST_USER = "ione.phi.metadata@example.test"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", cls.TEST_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.TEST_USER,
					"first_name": "IONE PHI Metadata",
					"send_welcome_email": 0,
					"roles": [
						{"role": "IONE QC Reviewer"},
						{"role": "IONE PHI Identity Reader"},
					],
				}
			).insert(ignore_permissions=True)
		else:
			user = frappe.get_doc("User", cls.TEST_USER)
			user.enabled = 1
			user.save(ignore_permissions=True)
			user.add_roles("IONE QC Reviewer", "IONE PHI Identity Reader")
		frappe.clear_cache(user=cls.TEST_USER)

	@classmethod
	def tearDownClass(cls) -> None:
		frappe.set_user("Administrator")
		if frappe.db.exists("User", cls.TEST_USER):
			frappe.db.set_value("User", cls.TEST_USER, "enabled", 0)
		frappe.clear_cache(user=cls.TEST_USER)
		super().tearDownClass()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_identity_fields_are_masked_high_permlevel_without_generic_reader(self) -> None:
		for doctype, protected_fields in PHI_FIELDS_BY_DOCTYPE.items():
			meta = frappe.get_meta(doctype)
			permitted = set(
				meta.get_permitted_fieldnames(
					user=self.TEST_USER,
					permission_type="read",
					with_virtual_fields=False,
				)
			)
			self.assertTrue(protected_fields.isdisjoint(permitted))
			for fieldname in protected_fields:
				field = meta.get_field(fieldname)
				self.assertEqual(int(field.permlevel or 0), 9)
				self.assertEqual(int(field.mask or 0), 1)
			self.assertFalse(
				any(
					int(permission.permlevel or 0) == 9 and int(permission.read or 0)
					for permission in meta.permissions
				)
			)

	def test_operational_sensitive_registry_is_also_unreadable_generically(self) -> None:
		for doctype, protected_fields in PROTECTED_FIELDS_BY_DOCTYPE.items():
			meta = frappe.get_meta(doctype)
			for fieldname in protected_fields:
				field = meta.get_field(fieldname)
				self.assertIsNotNone(field, f"{doctype}.{fieldname}")
				self.assertEqual(int(field.permlevel or 0), 9)
				self.assertEqual(int(field.mask or 0), 1)

	def test_only_pseudonymous_keys_drive_titles_and_link_search(self) -> None:
		contracts = {
			"IONE Patient Index": ("patient_key", "patient_key"),
			"IONE Encounter Index": ("encounter_key", "encounter_key"),
		}
		for doctype, (title_field, search_fields) in contracts.items():
			meta = frappe.get_meta(doctype)
			self.assertEqual(meta.title_field, title_field)
			self.assertEqual(meta.search_fields, search_fields)

	def test_requested_fields_are_minimal_registered_and_duplicate_free(self) -> None:
		self.assertEqual(
			_normalize_requested_fields(
				'["patient_name","source_patient_id"]',
				"IONE Patient Index",
			),
			["patient_name", "source_patient_id"],
		)
		for fields in (
			[],
			["patient_name", "patient_name"],
			["patient_name", "payload_json"],
		):
			with self.subTest(fields=fields), self.assertRaises(frappe.ValidationError):
				_normalize_requested_fields(fields, "IONE Patient Index")

	def test_policy_family_lock_is_deterministic_and_does_not_expose_scope_names(self) -> None:
		first = _policy_family_lock("HOSPITAL-ALPHA", "DIRECT_CARE")
		second = _policy_family_lock("HOSPITAL-ALPHA", "DIRECT_CARE")
		other = _policy_family_lock("HOSPITAL-BETA", "DIRECT_CARE")
		self.assertEqual(first, second)
		self.assertNotEqual(first, other)
		self.assertNotIn("HOSPITAL-ALPHA", first)
		self.assertNotIn("DIRECT_CARE", first)

	def test_generic_query_guard_is_target_specific_and_decodes_nested_filters(self) -> None:
		self.assertTrue(
			_contains_phi_field_reference(
				{
					"doctype": "IONE Patient Index",
					"filters": '{"source\\u005fpatient\\u005fid":"MRN-EXAMPLE"}',
				},
				root_doctypes=frozenset({"IONE Patient Index"}),
			)
		)
		self.assertFalse(
			_contains_phi_field_reference(
				{
					"doctype": "Unrelated Patient",
					"filters": {"patient_name": "Example"},
				},
				root_doctypes=frozenset({"Unrelated Patient"}),
			)
		)


if __name__ == "__main__":
	unittest.main()

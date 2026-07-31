from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from ione_qms.services.identity_keys import identity_key_contract, site_identity_key
from ione_qms.services.phi_access import (
	_activate_due_policies,
	approve_phi_disclosure_policy,
	disclose_phi_identity,
	retire_phi_disclosure_policy,
	submit_phi_disclosure_policy,
)


class TestPHIDisclosureLifecycle(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.suffix = uuid.uuid4().hex[:10].upper()
		self.author = self._user("author", "IONE QC Administrator")
		self.reviewer = self._user("reviewer", "IONE Auditor")
		self.reader = self._user(
			"reader",
			"IONE Medical Affairs",
			"IONE Physician",
			"IONE PHI Identity Reader",
		)
		self.hospital = f"PHI-H-{self.suffix}"
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "IONE Hospital",
				"hospital_code": self.hospital,
				"hospital_name": f"PHI Test Hospital {self.suffix}",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		self.source = f"PHI-SRC-{self.suffix}"
		source = frappe.get_doc(
			{
				"doctype": "IONE Source System",
				"name": self.source,
				"system_code": self.source,
				"system_name": f"PHI Test Source {self.suffix}",
				"system_type": "EMR",
				"enabled": 0,
				"read_only": 1,
				"identity_namespace": f"phi-ns-{self.suffix.lower()}",
			}
		)
		source.db_insert()
		self.patient_source_id = f"MRN-{self.suffix}"
		self.patient_key = site_identity_key(
			"patient",
			source.identity_namespace,
			self.hospital,
			self.patient_source_id,
		)
		patient = frappe.get_doc(
			{
				"doctype": "IONE Patient Index",
				"name": self.patient_key,
				"patient_key": self.patient_key,
				"identity_key_contract": identity_key_contract(),
				"tenant_hospital": self.hospital,
				"source_system": self.source,
				"source_namespace": source.identity_namespace,
				"source_patient_id": self.patient_source_id,
				"patient_name": "PHI Canary Patient",
				"gender": "Unknown",
				"status": "Active",
			}
		)
		patient.db_insert()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_role_basis_policy_replacement_schedule_and_receipt_integrity(self) -> None:
		physician_only = self._approve_policy(
			code=f"PHI.PHYS.{self.suffix}",
			version="1",
			allowed_roles=["IONE Physician"],
			effective_from=add_to_date(now_datetime(), minutes=-5),
		)
		frappe.set_user(self.reader)
		with self.assertRaises(frappe.PermissionError):
			disclose_phi_identity(
				reference_doctype="IONE Patient Index",
				reference_name=self.patient_key,
				fields=["patient_name"],
				purpose_code="DIRECT_CARE",
				request_id=f"deny-{self.suffix}",
			)

		global_reader = self._approve_policy(
			code=f"PHI.GLOBAL.{self.suffix}",
			version="1",
			allowed_roles=["IONE Medical Affairs"],
			effective_from=add_to_date(now_datetime(), minutes=-5),
		)
		self.assertEqual(
			frappe.db.get_value("IONE PHI Disclosure Policy", physician_only, "status"),
			"Retired",
		)
		frappe.set_user(self.reader)
		result = disclose_phi_identity(
			reference_doctype="IONE Patient Index",
			reference_name=self.patient_key,
			fields=["patient_name"],
			purpose_code="DIRECT_CARE",
			request_id=f"allow-{self.suffix}",
		)
		self.assertEqual(result["data"], {"patient_name": "PHI Canary Patient"})
		receipt = frappe.get_doc("IONE PHI Access Receipt", result["receipt"])
		basis = json.loads(receipt.authorization_basis_json)
		roles = json.loads(receipt.role_snapshot_json)
		self.assertEqual(basis["authorizing_roles"], ["IONE Medical Affairs"])
		self.assertEqual(roles["authorizing_roles"], ["IONE Medical Affairs"])
		self.assertEqual(basis["authorized_scope"]["hospital"], self.hospital)
		self.assertTrue(receipt.hmac_key_id)

		future = add_to_date(now_datetime(), minutes=10)
		scheduled = self._approve_policy(
			code=f"PHI.NEXT.{self.suffix}",
			version="1",
			allowed_roles=["IONE Medical Affairs"],
			effective_from=future,
		)
		self.assertEqual(
			frappe.db.get_value("IONE PHI Disclosure Policy", scheduled, "status"),
			"Scheduled",
		)
		self.assertEqual(
			frappe.db.get_value("IONE PHI Disclosure Policy", global_reader, "status"),
			"Active",
		)
		with patch(
			"ione_qms.services.phi_access.now_datetime",
			return_value=add_to_date(future, minutes=1),
		):
			_activate_due_policies(
				scope={"hospital": self.hospital, "campus": "", "department": "", "ward": ""},
				purpose_code="DIRECT_CARE",
			)
		self.assertEqual(
			frappe.db.get_value("IONE PHI Disclosure Policy", scheduled, "status"),
			"Active",
		)
		self.assertEqual(
			frappe.db.get_value("IONE PHI Disclosure Policy", global_reader, "status"),
			"Retired",
		)

		original_hash = str(receipt.record_hash)
		frappe.db.set_value(
			"IONE PHI Access Receipt",
			receipt.name,
			"record_hash",
			"0" * 64,
			update_modified=False,
		)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("IONE PHI Access Receipt", receipt.name)
		frappe.db.set_value(
			"IONE PHI Access Receipt",
			receipt.name,
			"record_hash",
			original_hash,
			update_modified=False,
		)

		frappe.set_user(self.reviewer)
		retired = retire_phi_disclosure_policy(scheduled, "End of integration lifecycle test")
		self.assertEqual(retired["status"], "Retired")

	def _approve_policy(
		self,
		*,
		code: str,
		version: str,
		allowed_roles: list[str],
		effective_from,
	) -> str:
		frappe.set_user(self.author)
		doc = frappe.get_doc(
			{
				"doctype": "IONE PHI Disclosure Policy",
				"policy_code": code,
				"policy_version": version,
				"policy_name": f"PHI lifecycle {code}",
				"purpose_code": "DIRECT_CARE",
				"purpose_description": "Named direct-care identity disclosure integration test.",
				"hospital": self.hospital,
				"allowed_fields_json": json.dumps(
					{"IONE Patient Index": ["patient_name"]},
					sort_keys=True,
				),
				"allowed_reader_roles_json": json.dumps(allowed_roles, sort_keys=True),
				"max_disclosures_per_user_per_minute": 30,
				"max_disclosures_per_record_per_minute": 5,
				"effective_from": effective_from,
				"external_approval_reference": f"GOV/PHI/{self.suffix}/{code[-4:]}",
			}
		)
		doc.insert()
		submit_phi_disclosure_policy(doc.name)
		frappe.set_user(self.reviewer)
		approved = approve_phi_disclosure_policy(
			doc.name,
			"Independent integration test approval",
		)
		return str(approved["policy"])

	def _user(self, prefix: str, *roles: str) -> str:
		user = f"ione.phi.{prefix}.{self.suffix.lower()}@example.test"
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "User",
				"email": user,
				"first_name": f"IONE PHI {prefix}",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		return user

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
QUERY_GUARD = ROOT / "ione_qms" / "overrides" / "phi_query_guard.py"

_PHI_FIELDS = {
	"IONE Patient Index": frozenset(
		{
			"source_patient_id",
			"patient_name",
			"date_of_birth",
			"identification_hash",
			"phone_hash",
		}
	),
	"IONE Encounter Index": frozenset({"source_encounter_id", "encounter_no"}),
	"IONE QC Execution": frozenset({"evidence_json"}),
}


class _GuardRejected(RuntimeError):
	pass


class _FakeDatabase:
	def __init__(self) -> None:
		self.doctypes = {
			"IONE Patient Index",
			"IONE Encounter Index",
			"IONE Clinical Event",
			"Unrelated Patient",
		}

	def exists(self, doctype: str, name: str) -> bool:
		return doctype == "DocType" and name in self.doctypes


def _load_guard():
	fake_frappe = types.ModuleType("frappe")
	fake_frappe.PermissionError = _GuardRejected
	fake_frappe.local = SimpleNamespace(request=None)
	fake_frappe.form_dict = {}
	fake_frappe.db = _FakeDatabase()
	fake_frappe.session = SimpleNamespace(user="ione.user@example.test")

	def throw(message: str, exception=None) -> None:
		raise (exception or _GuardRejected)(message)

	def get_meta(doctype: str):
		fields = {
			"IONE Clinical Event": {
				"patient": SimpleNamespace(
					fieldname="patient",
					fieldtype="Link",
					options="IONE Patient Index",
				),
				"encounter": SimpleNamespace(
					fieldname="encounter",
					fieldtype="Link",
					options="IONE Encounter Index",
				),
			}
		}
		field_map = fields.get(doctype, {})
		return SimpleNamespace(get_field=lambda fieldname: field_map.get(fieldname))

	fake_frappe.throw = throw
	fake_frappe.get_meta = get_meta

	fake_app = types.ModuleType("ione_qms")
	fake_app.__path__ = []
	fake_privacy = types.ModuleType("ione_qms.privacy_registry")
	fake_privacy.PROTECTED_FIELDS_BY_DOCTYPE = _PHI_FIELDS
	fake_privacy.IDENTITY_BEARING_DOCTYPES = frozenset(_PHI_FIELDS)
	fake_privacy.PHI_SIDECAR_DOCTYPES = frozenset(
		{"File", "Comment", "Communication", "Communication Link", "Version", "ToDo"}
	)
	module_name = "_ione_phi_query_guard_unit"
	spec = importlib.util.spec_from_file_location(module_name, QUERY_GUARD)
	if spec is None or spec.loader is None:
		raise AssertionError("Unable to load PHI query guard")
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		sys.modules,
		{
			"frappe": fake_frappe,
			"ione_qms": fake_app,
			"ione_qms.privacy_registry": fake_privacy,
		},
	):
		spec.loader.exec_module(module)
	return module, fake_frappe


class TestPHIQueryGuardUnit(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.guard, cls.frappe = _load_guard()

	def setUp(self) -> None:
		self.frappe.session.user = "ione.user@example.test"
		self.frappe.local.request = SimpleNamespace(method="GET", path="/", content_length=0)
		self.frappe.form_dict = {}

	def _request(self, method: str, path: str, **arguments) -> None:
		self.frappe.local.request = SimpleNamespace(method=method, path=path, content_length=0)
		self.frappe.form_dict = arguments
		self.guard.prevent_unsafe_phi_query_request()

	def test_resource_and_generic_method_queries_are_rejected(self) -> None:
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"GET",
				"/api/resource/IONE%20Patient%20Index",
				fields='["name","patient_name"]',
			)
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"POST",
				"/api/method/frappe.client.get_list",
				cmd="ione_qms.api.dashboard.get_command_center",
				doctype="IONE Encounter Index",
				filters='{"encounter\\u005fno":"ENC-EXAMPLE"}',
			)

	def test_unrelated_same_named_fields_do_not_trigger_a_global_false_positive(self) -> None:
		self._request(
			"GET",
			"/api/resource/Unrelated%20Patient",
			doctype="Unrelated Patient",
			filters={"patient_name": "Example"},
		)

	def test_link_traversal_into_identity_doctype_is_rejected(self) -> None:
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"GET",
				"/api/method/frappe.client.get_list",
				doctype="IONE Clinical Event",
				filters={"patient.patient_name": "Example"},
			)
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"GET",
				"/api/method/frappe.client.get_list",
				doctype="IONE Clinical Event",
				order_by="encounter.encounter_no asc",
			)

	def test_governed_configuration_exemption_is_exact_and_write_only(self) -> None:
		self._request(
			"POST",
			"/api/resource/IONE%20Integration%20Mapping",
			doctype="IONE Integration Mapping",
			doc='{"doctype":"IONE Integration Mapping","source_field":"patient_name"}',
		)
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"GET",
				"/api/resource/IONE%20Integration%20Mapping",
				doctype="IONE Patient Index",
				filters={"patient_name": "Example"},
			)
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"POST",
				"/api/method/frappe.client.get_list",
				doctype="IONE Patient Index",
				filters={"patient_name": "Example"},
			)

	def test_nested_json_and_query_size_limits_fail_closed(self) -> None:
		nested = {"doctype": "IONE Patient Index", "filters": '{"patient_name":"Example"}'}
		self.assertTrue(
			self.guard._contains_phi_field_reference(
				{"payload": nested},
				root_doctypes=self.guard._root_doctypes(nested),
			)
		)
		with self.assertRaisesRegex(_GuardRejected, "bounded input contract"):
			self.guard._contains_phi_field_reference(["safe"] * 20_001)
		oversized = (
			'{"source\\u005fpatient\\u005fid":"MRN-EXAMPLE","padding":"' + ("x" * (2 * 1024 * 1024)) + '"}'
		)
		with self.assertRaisesRegex(_GuardRejected, "unsafe request arguments"):
			self._request(
				"GET",
				"/api/resource/IONE%20Patient%20Index",
				filters=oversized,
			)
		double_encoded = '"{\\"patient\\\\u005fname\\":\\"Example\\"}"'
		with self.assertRaisesRegex(_GuardRejected, "Protected patient identity"):
			self._request(
				"GET",
				"/api/resource/IONE%20Patient%20Index",
				filters=double_encoded,
			)
		with self.assertRaisesRegex(_GuardRejected, "malformed encoded query value"):
			self._request(
				"GET",
				"/api/method/frappe.client.get_list",
				doctype="Unrelated Patient",
				filters="[not-json}",
			)

	def test_non_query_json_looking_artifact_text_is_not_rejected(self) -> None:
		class _Document(dict):
			__getattr__ = dict.get

			def as_dict(self):
				return dict(self)

		notification = _Document(
			doctype="Notification",
			name="Error Log",
			document_type="Error Log",
			subject="[Error] {{ doc.method }}",
		)
		self.guard.validate_phi_query_artifact(notification)

	def test_technical_administrator_cannot_enumerate_phi_sidecars(self) -> None:
		self.frappe.session.user = "Administrator"
		for doctype in (
			"File",
			"Comment",
			"Communication",
			"Communication Link",
			"Version",
			"ToDo",
		):
			with (
				self.subTest(doctype=doctype),
				self.assertRaisesRegex(
					_GuardRejected,
					"Technical Administrator",
				),
			):
				self._request(
					"GET",
					f"/api/resource/{doctype}",
					fields='["name"]',
				)
		with self.assertRaisesRegex(_GuardRejected, "Technical Administrator"):
			self._request(
				"GET",
				"/printview",
				doctype="IONE Patient Index",
				name="PATIENT-OPAQUE",
			)

	def test_communication_child_link_cannot_persist_a_protected_sidecar(self) -> None:
		self.assertEqual(
			set(self.guard._SIDECAR_TARGET_FIELDS_BY_DOCTYPE),
			set(self.guard.PHI_UNSTRUCTURED_SIDECAR_DOCTYPES),
		)

		class _Document(dict):
			__getattr__ = dict.get

		doc = _Document(
			doctype="Communication",
			reference_doctype="",
			links=[{"link_doctype": "IONE Patient Index", "link_name": "PATIENT-OPAQUE"}],
		)
		with self.assertRaisesRegex(_GuardRejected, "cannot persist unstructured copies"):
			self.guard.prevent_ione_phi_sidecar_persistence(doc)
		child_link = _Document(
			doctype="Communication Link",
			link_doctype="IONE Patient Index",
		)
		with self.assertRaisesRegex(_GuardRejected, "cannot persist unstructured copies"):
			self.guard.prevent_ione_phi_sidecar_persistence(child_link)

	def test_only_exact_release_controlled_report_can_target_protected_data(self) -> None:
		class _Document(dict):
			__getattr__ = dict.get

			def as_dict(self):
				return dict(self)

		base = {
			"doctype": "Report",
			"ref_doctype": "IONE QC Execution",
			"report_type": "Script Report",
			"module": "IONE Analytics",
			"is_standard": "Yes",
			"prepared_report": 0,
			"disable_prepared_report_automation": 1,
		}
		unknown = _Document(
			**base,
			name="User Script Report",
			report_name="User Script Report",
		)
		with self.assertRaisesRegex(_GuardRejected, "cannot target"):
			self.guard.validate_phi_query_artifact(unknown)
		controlled = _Document(
			**base,
			name="IONE Rule Quality",
			report_name="IONE Rule Quality",
		)
		self.guard.validate_phi_query_artifact(controlled)

	def test_options_non_api_and_governed_app_paths_are_not_intercepted(self) -> None:
		self._request(
			"OPTIONS",
			"/api/resource/IONE%20Patient%20Index",
			doctype="IONE Patient Index",
			fields=["patient_name"],
		)
		self._request(
			"POST",
			"/api/method/ione_qms.api.privacy.read_phi_identity",
			reference_doctype="IONE Patient Index",
			fields=["patient_name"],
		)
		self._request(
			"POST",
			"/login",
			doctype="IONE Patient Index",
			fields=["patient_name"],
		)


if __name__ == "__main__":
	unittest.main()

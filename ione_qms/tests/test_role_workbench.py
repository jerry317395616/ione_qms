from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = PACKAGE_ROOT / "api" / "dashboard.py"
PAGE_ROOT = PACKAGE_ROOT / "ione_analytics" / "page" / "ione_quality_workbench"
PAGE_JSON = PAGE_ROOT / "ione_quality_workbench.json"
PAGE_JS = PAGE_ROOT / "ione_quality_workbench.js"


def _load_dashboard():
	try:
		from ione_qms.api import dashboard
	except ModuleNotFoundError as exc:
		if not str(exc.name or "").startswith("frappe"):
			raise
		dashboard = _load_dashboard_with_frappe_stub()
	return dashboard


def _load_dashboard_with_frappe_stub():
	frappe = ModuleType("frappe")

	class _PermissionError(Exception):
		pass

	class _DoesNotExistError(Exception):
		pass

	def whitelist(*args, **kwargs):
		del args, kwargs

		def decorator(function):
			return function

		return decorator

	frappe.PermissionError = _PermissionError
	frappe.DoesNotExistError = _DoesNotExistError
	frappe.whitelist = whitelist
	frappe.session = SimpleNamespace(user="test@example.test")
	frappe.get_roles = lambda user=None: []
	frappe.get_meta = lambda doctype: SimpleNamespace(name=doctype)
	frappe.has_permission = lambda doctype, ptype=None: True
	frappe.get_list = lambda *args, **kwargs: []
	frappe.throw = lambda message, exception=Exception: (_ for _ in ()).throw(exception(message))

	utils = ModuleType("frappe.utils")
	utils.add_days = lambda value, days: value + timedelta(days=days)
	utils.get_datetime = lambda value: value
	utils.getdate = lambda value=None: value if isinstance(value, date) else date(2026, 7, 30)
	utils.now_datetime = lambda: datetime(2026, 7, 30, 12, 0, 0)
	utils.nowdate = lambda: date(2026, 7, 30)
	frappe.utils = utils

	permission_stub = ModuleType("ione_qms.permissions")
	permission_stub.get_access_context = lambda user=None: SimpleNamespace(
		staff_records=frozenset(),
		departments=frozenset(),
	)
	permission_stub.has_app_permission = lambda user=None: True
	permission_stub.require_department_read = lambda department, user=None: None
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	sys.modules["ione_qms.permissions"] = permission_stub

	spec = importlib.util.spec_from_file_location("_ione_dashboard_workbench_test", DASHBOARD_PATH)
	if spec is None or spec.loader is None:
		raise RuntimeError("Could not load dashboard module for workbench tests")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


dashboard = _load_dashboard()


def _raise_frappe_error(message, exception=Exception):
	raise exception(str(message))


@contextmanager
def _runtime(
	*,
	user: str,
	roles: list[str],
	get_list_side_effect=None,
	has_permission=True,
	get_meta_side_effect=None,
	staff_records: tuple[str, ...] = (),
	departments: tuple[str, ...] = (),
	app_permission: bool = True,
):
	get_list = MagicMock(side_effect=get_list_side_effect)
	get_meta = MagicMock(
		side_effect=get_meta_side_effect
		if get_meta_side_effect is not None
		else lambda doctype: SimpleNamespace(name=doctype)
	)
	with ExitStack() as stack:
		stack.enter_context(patch.object(dashboard.frappe, "session", SimpleNamespace(user=user)))
		stack.enter_context(patch.object(dashboard.frappe, "get_roles", return_value=roles))
		stack.enter_context(patch.object(dashboard.frappe, "get_list", get_list))
		stack.enter_context(patch.object(dashboard.frappe, "get_meta", get_meta))
		stack.enter_context(patch.object(dashboard.frappe, "has_permission", return_value=has_permission))
		stack.enter_context(patch.object(dashboard.frappe, "throw", side_effect=_raise_frappe_error))
		stack.enter_context(patch.object(dashboard, "has_app_permission", return_value=app_permission))
		stack.enter_context(
			patch.object(
				dashboard,
				"get_access_context",
				return_value=SimpleNamespace(
					staff_records=frozenset(staff_records),
					departments=frozenset(departments),
				),
			)
		)
		stack.enter_context(
			patch.object(
				dashboard,
				"now_datetime",
				return_value=datetime(2026, 7, 30, 12, 0, 0),
			)
		)
		yield get_list, get_meta


class TestRoleWorkbenchPermissions(TestCase):
	def test_administrator_is_rejected_before_any_business_query(self) -> None:
		with _runtime(user="Administrator", roles=["System Manager"]) as (get_list, _):
			with self.assertRaises(dashboard.frappe.PermissionError):
				dashboard.get_role_workbench()
		get_list.assert_not_called()

	def test_agent_service_is_rejected_even_with_a_business_role(self) -> None:
		with _runtime(
			user="service@example.test",
			roles=["IONE Agent Service", "IONE QC Reviewer"],
		) as (get_list, _):
			with self.assertRaises(dashboard.frappe.PermissionError):
				dashboard.get_role_workbench()
		get_list.assert_not_called()

	def test_command_center_rejects_administrator_before_any_business_query(self) -> None:
		with _runtime(user="Administrator", roles=["System Manager"]) as (get_list, _):
			with self.assertRaises(dashboard.frappe.PermissionError):
				dashboard.get_command_center()
		get_list.assert_not_called()

	def test_command_center_rejects_agent_service_before_any_business_query(self) -> None:
		with _runtime(
			user="service@example.test",
			roles=["IONE Agent Service", "IONE QC Reviewer"],
		) as (get_list, _):
			with self.assertRaises(dashboard.frappe.PermissionError):
				dashboard.get_command_center()
		get_list.assert_not_called()

	def test_physician_without_staff_mapping_fails_closed(self) -> None:
		with _runtime(
			user="physician@example.test",
			roles=["IONE Physician"],
		) as (get_list, _):
			result = dashboard.get_role_workbench()
		self.assertEqual(result["persona"], "Physician")
		self.assertFalse(result["scope"]["staff_mapped"])
		self.assertTrue(all(not rows for rows in result["sections"].values()))
		get_list.assert_not_called()

	def test_physician_queries_only_personally_owned_records(self) -> None:
		with _runtime(
			user="physician@example.test",
			roles=["IONE Physician"],
			get_list_side_effect=lambda *args, **kwargs: [],
			staff_records=("STAFF-001",),
			departments=("DEP-001",),
		) as (get_list, _):
			dashboard.get_role_workbench()

		calls = {call.args[0]: call.kwargs for call in get_list.call_args_list}
		self.assertEqual(
			calls["IONE QC Finding"]["filters"]["responsible_staff"],
			["in", ["STAFF-001"]],
		)
		self.assertEqual(
			calls["IONE QC Finding Appeal"]["filters"]["responsible_staff"],
			["in", ["STAFF-001"]],
		)
		self.assertEqual(
			calls["IONE Medical Safety Event"]["filters"]["responsible_staff"],
			["in", ["STAFF-001"]],
		)
		self.assertEqual(
			calls["IONE Indicator Alert"]["filters"]["medical_staff"],
			["in", ["STAFF-001"]],
		)
		self.assertEqual(
			calls["IONE QC Rectification"]["filters"]["assigned_to"],
			"physician@example.test",
		)
		self.assertEqual(
			calls["IONE PDCA Project"]["filters"]["project_owner"],
			"physician@example.test",
		)
		self.assertNotIn("IONE QC Verification", calls)
		self.assertNotIn("IONE AI Candidate Finding", calls)

	def test_department_persona_adds_authorized_department_filter_to_every_dataset(self) -> None:
		with _runtime(
			user="director@example.test",
			roles=["IONE Department Director"],
			get_list_side_effect=lambda *args, **kwargs: [],
			staff_records=("STAFF-002", "STAFF-003"),
			departments=("DEP-B", "DEP-A"),
		) as (get_list, _):
			result = dashboard.get_role_workbench()

		self.assertEqual(result["persona"], "Department")
		self.assertEqual(result["scope"]["department_count"], 2)
		for call in get_list.call_args_list:
			self.assertEqual(
				call.kwargs["filters"]["department"],
				["in", ["DEP-A", "DEP-B"]],
			)

	def test_auditor_persona_is_explicitly_read_only(self) -> None:
		with _runtime(
			user="auditor@example.test",
			roles=["IONE Auditor"],
			get_list_side_effect=lambda *args, **kwargs: [],
		):
			result = dashboard.get_role_workbench()
		self.assertEqual(result["persona"], "Auditor")
		self.assertTrue(result["read_only"])

	def test_every_query_and_returned_section_is_bounded_to_twenty(self) -> None:
		def oversized_rows(doctype, **kwargs):
			del kwargs
			return [{"name": f"{doctype}-{index}"} for index in range(30)]

		with _runtime(
			user="reviewer@example.test",
			roles=["IONE QC Reviewer"],
			get_list_side_effect=oversized_rows,
		) as (get_list, _):
			result = dashboard.get_role_workbench()

		self.assertTrue(all(len(rows) == 20 for rows in result["sections"].values()))
		for call in get_list.call_args_list:
			self.assertEqual(call.kwargs["limit_page_length"], 20)

	def test_missing_optional_doctype_returns_an_empty_section(self) -> None:
		def get_meta(doctype):
			if doctype == "IONE PDCA Project":
				raise dashboard.frappe.DoesNotExistError(doctype)
			return SimpleNamespace(name=doctype)

		with _runtime(
			user="reviewer@example.test",
			roles=["IONE QC Reviewer"],
			get_list_side_effect=lambda *args, **kwargs: [],
			get_meta_side_effect=get_meta,
		):
			result = dashboard.get_role_workbench()
		self.assertEqual(result["sections"]["pdca_projects"], [])

	def test_response_drops_unrequested_sensitive_mock_fields(self) -> None:
		def rows_for(doctype, **kwargs):
			del kwargs
			if doctype == "IONE QC Finding":
				return [
					{
						"name": "FINDING-001",
						"status": "Confirmed",
						"payload_json": '{"patient_name": "hidden"}',
						"patient_name": "hidden",
						"description": "hidden",
					}
				]
			return []

		with _runtime(
			user="reviewer@example.test",
			roles=["IONE QC Reviewer"],
			get_list_side_effect=rows_for,
		):
			result = dashboard.get_role_workbench()
		row = result["sections"]["findings"][0]
		self.assertEqual(row["name"], "FINDING-001")
		self.assertNotIn("payload_json", row)
		self.assertNotIn("patient_name", row)
		self.assertNotIn("description", row)


class TestRoleWorkbenchStaticContracts(TestCase):
	def test_page_roles_match_backend_roles_and_exclude_privileged_identities(self) -> None:
		metadata = json.loads(PAGE_JSON.read_text(encoding="utf-8"))
		page_roles = {row["role"] for row in metadata["roles"]}
		self.assertSetEqual(page_roles, set(dashboard._WORKBENCH_ROLES))
		self.assertTrue(
			page_roles.isdisjoint(
				{
					"Guest",
					"Administrator",
					"System Manager",
					"IONE Agent Service",
					"IONE Agent Administrator",
				}
			)
		)
		self.assertEqual(metadata["module"], "IONE Analytics")
		self.assertEqual(metadata["page_name"], "ione-quality-workbench")

	def test_backend_uses_only_permissioned_get_list_for_workbench_records(self) -> None:
		source = "\n".join(
			inspect.getsource(function)
			for function in (
				dashboard.get_role_workbench,
				dashboard._workbench_staff_scope,
				dashboard._permissioned_workbench_list,
			)
		)
		self.assertIn("frappe.get_list", source)
		self.assertNotIn("frappe.get_all", source)
		self.assertNotIn("frappe.db.", source)
		self.assertNotIn("ignore_permissions", source)

	def test_dataset_fields_exclude_raw_sensitive_content(self) -> None:
		fields = {fieldname for dataset in dashboard._WORKBENCH_DATASETS for fieldname in dataset["fields"]}
		self.assertTrue(
			fields.isdisjoint(
				{
					"patient",
					"encounter",
					"patient_name",
					"staff_name",
					"payload_json",
					"details_json",
					"description",
					"narrative",
					"immediate_action",
					"reason",
					"evidence_summary",
					"message",
					"plan",
					"rationale",
					"review_comment",
					"assigned_to",
					"responsible_staff",
					"reported_by",
				}
			)
		)

	def test_page_uses_text_rendering_and_only_authorized_form_routes(self) -> None:
		source = PAGE_JS.read_text(encoding="utf-8")
		self.assertIn('"ione_qms.api.dashboard.get_role_workbench"', source)
		self.assertIn('frappe.set_route("Form", definition.doctype, row.name)', source)
		for section in dashboard._WORKBENCH_DATASETS:
			self.assertIn(f'key: "{section["key"]}"', source)
			self.assertIn(f'doctype: "{section["doctype"]}"', source)
		for prohibited in (
			".html(",
			"innerHTML",
			"insertAdjacentHTML",
			"error.message",
			"console.",
			"payload_json",
			"details_json",
			"patient_name",
		):
			with self.subTest(prohibited=prohibited):
				self.assertNotIn(prohibited, source)

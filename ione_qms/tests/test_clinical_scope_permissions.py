from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms import hooks, permissions


def _context(
	*,
	user: str = "person@example.test",
	roles: tuple[str, ...] = (),
	departments: tuple[str, ...] = (),
	hospitals: tuple[str, ...] = (),
	campuses: tuple[str, ...] = (),
	wards: tuple[str, ...] = (),
	staff_records: tuple[str, ...] = (),
	scope_grants: tuple[tuple[str, str, str], ...] = (),
) -> permissions.AccessContext:
	return permissions.AccessContext(
		user=user,
		roles=frozenset(roles),
		hospitals=frozenset(hospitals),
		campuses=frozenset(campuses),
		departments=frozenset(departments),
		wards=frozenset(wards),
		staff_records=frozenset(staff_records),
		scope_grants=scope_grants,
	)


def _escape(value: str) -> str:
	return f"'{value}'"


class TestClinicalScopePermissions(TestCase):
	def setUp(self) -> None:
		super().setUp()
		integrity_sql = patch.object(permissions, "scope_integrity_sql", return_value="1=1")
		document_integrity = patch.object(
			permissions,
			"_scope_document_is_consistent",
			return_value=True,
		)
		integrity_sql.start()
		document_integrity.start()
		self.addCleanup(document_integrity.stop)
		self.addCleanup(integrity_sql.stop)

	def test_all_scope_bearing_business_lists_have_query_and_document_guards(self) -> None:
		expected = {
			"IONE Medical Staff",
			"IONE Patient Index",
			"IONE Encounter Index",
			"IONE Clinical Quality Event",
			"IONE QC Execution",
			"IONE QC Finding",
			"IONE QC Finding Evidence",
			"IONE QC Finding Appeal",
			"IONE QC Finding Appeal Evidence",
			"IONE Medical Record QC",
			"IONE Surgery QC",
			"IONE Medical Safety Event",
			"IONE Indicator Alert",
			"IONE QC Rectification",
			"IONE QC Verification",
			"IONE PDCA Project",
			"IONE Indicator Result",
			"IONE Indicator Result Detail",
			"IONE Daily Quality Fact",
			"IONE Monthly Quality Fact",
			"IONE Finding Analysis Fact",
			"IONE Surgery Quality Fact",
			"IONE Agent Analysis Fact",
			"IONE AI Analysis Task",
			"IONE AI Candidate Finding",
			"IONE AI Data Access Log",
			"IONE AI Execution Event",
			"IONE AI Report Draft",
			"IONE AI Tool Approval",
			"IONE Flow Run Link",
		}
		self.assertTrue(expected.issubset(hooks.permission_query_conditions))
		self.assertTrue(expected.issubset(hooks.has_permission))

	def test_agent_administrator_can_list_and_manage_governed_flow_agents(self) -> None:
		context = _context(roles=("IONE Agent Administrator",))
		agent = SimpleNamespace(name="IONE Medical Record QC Agent", title="IONE Medical Record QC Agent")
		with patch.object(permissions, "get_access_context", return_value=context):
			self.assertEqual(permissions.flow_agent_query(context.user), "")
			self.assertTrue(permissions.flow_agent_permission(agent, "read", context.user))
			self.assertTrue(permissions.flow_agent_permission(agent, "write", context.user))

	def test_physician_query_is_personal_not_department_wide(self) -> None:
		context = _context(
			roles=("IONE Physician",),
			departments=("CARDIOLOGY",),
			hospitals=("HOSPITAL-1",),
			staff_records=("STAFF-1",),
		)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(
				permissions,
				"_has_field",
				side_effect=lambda _doctype, fieldname: (
					fieldname
					in {"department", "hospital", "responsible_staff", "medical_staff", "assigned_to"}
				),
			),
			patch.object(permissions.frappe.db, "escape", side_effect=_escape),
		):
			finding_condition = permissions._clinical_condition("IONE QC Finding", context.user)
			rectification_condition = permissions._clinical_condition(
				"IONE QC Rectification",
				context.user,
			)
		self.assertIn("responsible_staff in ('STAFF-1')", finding_condition)
		self.assertIn("medical_staff in ('STAFF-1')", finding_condition)
		self.assertNotIn(".department in", finding_condition)
		self.assertNotIn(".hospital in", finding_condition)
		self.assertIn("assigned_to = 'person@example.test'", rectification_condition)

	def test_physician_without_staff_mapping_fails_closed(self) -> None:
		context = _context(
			roles=("IONE Physician",),
			departments=("CARDIOLOGY",),
		)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "_has_field", return_value=True),
			patch.object(permissions.frappe.db, "escape", side_effect=_escape),
		):
			self.assertEqual(
				permissions._clinical_condition("IONE QC Finding", context.user),
				"1=0",
			)

	def test_department_director_is_limited_to_authorized_departments(self) -> None:
		context = _context(
			roles=("IONE Department Director",),
			departments=("CARDIOLOGY",),
			staff_records=("STAFF-1",),
		)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "_has_field", return_value=True),
			patch.object(permissions.frappe.db, "escape", side_effect=_escape),
		):
			condition = permissions._clinical_condition("IONE QC Finding", context.user)
		self.assertIn(".department in ('CARDIOLOGY')", condition)
		self.assertNotIn("responsible_staff", condition)
		self.assertNotIn("medical_staff", condition)

	def test_dual_physician_director_role_uses_department_and_personal_scope(self) -> None:
		context = _context(
			roles=("IONE Physician", "IONE Department Director"),
			departments=("CARDIOLOGY",),
			staff_records=("STAFF-1",),
		)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "_has_field", return_value=True),
			patch.object(permissions.frappe.db, "escape", side_effect=_escape),
		):
			condition = permissions._clinical_condition("IONE QC Finding", context.user)
		self.assertIn(".department in ('CARDIOLOGY')", condition)
		self.assertIn("responsible_staff in ('STAFF-1')", condition)

	def test_agent_reviewer_scope_applies_only_to_ai_artifacts(self) -> None:
		context = _context(
			roles=("IONE Agent Reviewer",),
			departments=("CARDIOLOGY",),
		)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "_has_field", return_value=True),
			patch.object(permissions.frappe.db, "escape", side_effect=_escape),
		):
			self.assertEqual(
				permissions._clinical_condition("IONE QC Finding", context.user),
				"1=0",
			)
			ai_condition = permissions._ai_query("IONE AI Candidate Finding", context.user)
			self.assertIn(".department in ('CARDIOLOGY')", ai_condition)

	def test_ai_global_roles_still_receive_scope_integrity_predicate(self) -> None:
		context = _context(roles=("IONE QC Reviewer",))
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "scope_integrity_sql", return_value="scope_integrity = 1"),
		):
			self.assertEqual(
				permissions._ai_query("IONE AI Analysis Task", context.user),
				"scope_integrity = 1",
			)

	def test_direct_hospital_and_campus_staff_assignments_are_authorized(self) -> None:
		context = _context(
			roles=("IONE Department QC Officer",),
			hospitals=("HOSPITAL-1",),
			campuses=("CAMPUS-1",),
		)
		self.assertEqual(
			permissions._authorized_hospitals(context, "IONE Patient Index"),
			frozenset({"HOSPITAL-1"}),
		)
		self.assertEqual(
			permissions._authorized_campuses(context, "IONE Patient Index"),
			frozenset({"CAMPUS-1"}),
		)

	def test_personal_role_does_not_receive_department_read_scope_by_default(self) -> None:
		context = _context(
			roles=("IONE Physician",),
			departments=("CARDIOLOGY",),
			staff_records=("STAFF-1",),
		)
		self.assertEqual(
			permissions._authorized_departments(context, "IONE Patient Index"),
			frozenset(),
		)
		self.assertEqual(
			permissions._authorized_departments(
				context,
				"IONE Medical Safety Event",
				include_personal=True,
			),
			frozenset({"CARDIOLOGY"}),
		)

	def test_hospital_scoped_patient_permission_does_not_require_an_encounter(self) -> None:
		context = _context(
			roles=("IONE Department QC Officer",),
			hospitals=("HOSPITAL-1",),
		)
		patient = SimpleNamespace(
			doctype="IONE Patient Index",
			name="PATIENT-1",
			tenant_hospital="HOSPITAL-1",
		)
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions, "_scope_document_is_consistent", return_value=True),
			patch.object(permissions.frappe.db, "sql") as sql,
		):
			self.assertTrue(permissions.patient_permission(patient, "read", context.user))
			sql.assert_not_called()

	def test_administrator_list_queries_fail_closed_for_clinical_records(self) -> None:
		context = _context(
			user="Administrator",
			roles=("IONE QC Reviewer", "IONE Medical Affairs"),
		)
		with patch.object(permissions, "get_access_context", return_value=context):
			self.assertEqual(
				permissions._clinical_condition("IONE QC Finding", "Administrator"),
				"1=0",
			)
			self.assertEqual(
				permissions._ai_query("IONE AI Analysis Task", "Administrator"),
				"1=0",
			)

	def test_auditor_requires_explicit_user_permission_scope(self) -> None:
		no_scope = _context(roles=("IONE Auditor",))
		explicit_scope = _context(
			roles=("IONE Auditor",),
			scope_grants=(
				("IONE Medical Department", "CARDIOLOGY", ""),
				("IONE Medical Department", "ONCOLOGY", "IONE Indicator Result"),
			),
		)
		with (
			patch.object(permissions, "_has_field", return_value=True),
			patch.object(permissions.frappe.db, "escape", side_effect=_escape),
		):
			self.assertEqual(
				permissions._scope_clauses(
					"IONE Indicator Result",
					"`tabIONE Indicator Result`",
					no_scope,
				),
				[],
			)
			clauses = permissions._scope_clauses(
				"IONE Indicator Result",
				"`tabIONE Indicator Result`",
				explicit_scope,
			)
			self.assertIn("'CARDIOLOGY'", clauses[0])
			self.assertIn("'ONCOLOGY'", clauses[0])
			with patch.object(permissions, "get_access_context", return_value=no_scope):
				self.assertEqual(
					permissions._ai_query("IONE AI Candidate Finding", no_scope.user),
					"1=0",
				)
			with patch.object(permissions, "get_access_context", return_value=explicit_scope):
				ai_condition = permissions._ai_query(
					"IONE AI Candidate Finding",
					explicit_scope.user,
				)
			self.assertIn("'CARDIOLOGY'", ai_condition)
			# The ONCOLOGY grant is explicitly limited to Indicator Result and
			# must not broaden access to AI Candidate Finding.
			self.assertNotIn("'ONCOLOGY'", ai_condition)
			other_clauses = permissions._scope_clauses(
				"IONE Finding Analysis Fact",
				"`tabIONE Finding Analysis Fact`",
				explicit_scope,
			)
			self.assertIn("'CARDIOLOGY'", other_clauses[0])
			self.assertNotIn("'ONCOLOGY'", other_clauses[0])

	def test_document_permission_honors_personal_assignment(self) -> None:
		context = _context(
			roles=("IONE Physician",),
			departments=("CARDIOLOGY",),
			staff_records=("STAFF-1",),
		)
		own_rectification = SimpleNamespace(
			doctype="IONE QC Rectification",
			department="CARDIOLOGY",
			hospital=None,
			campus=None,
			responsible_staff=None,
			medical_staff=None,
			assigned_to=context.user,
			project_owner=None,
		)
		other_rectification = SimpleNamespace(
			**{**vars(own_rectification), "assigned_to": "other@example.test"}
		)
		with patch.object(permissions, "get_access_context", return_value=context):
			self.assertTrue(permissions._clinical_permission(own_rectification, "read", context.user))
			self.assertFalse(permissions._clinical_permission(other_rectification, "read", context.user))

	def test_medical_staff_directory_is_own_record_or_department_scoped(self) -> None:
		physician = _context(
			roles=("IONE Physician",),
			departments=("CARDIOLOGY",),
			staff_records=("STAFF-1",),
		)
		director = _context(
			roles=("IONE Department Director",),
			departments=("CARDIOLOGY",),
			staff_records=("STAFF-2",),
		)
		with patch.object(permissions.frappe.db, "escape", side_effect=_escape):
			with patch.object(permissions, "get_access_context", return_value=physician):
				self.assertEqual(
					permissions.medical_staff_query(physician.user),
					"`tabIONE Medical Staff`.user = 'person@example.test'",
				)
			with patch.object(permissions, "get_access_context", return_value=director):
				self.assertEqual(
					permissions.medical_staff_query(director.user),
					"`tabIONE Medical Staff`.department in ('CARDIOLOGY')",
				)

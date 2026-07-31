from __future__ import annotations

from unittest import TestCase

from ione_qms.services.data_export import (
	_STATUS_TRANSITIONS,
	AUDITOR_EXPORT_SCOPE_MODES,
	EXPORT_SCOPE_MODES,
	EXPORTABLE_DOCTYPES,
	MAX_EXPORT_ROWS,
	SENSITIVE_EXPORT_DOCTYPES,
	_safe_csv_cell,
	_single_scope_filter_value,
)


class TestControlledDataExportContracts(TestCase):
	def test_sensitive_doctypes_are_explicitly_exportable(self) -> None:
		self.assertTrue(SENSITIVE_EXPORT_DOCTYPES)
		self.assertTrue(SENSITIVE_EXPORT_DOCTYPES.issubset(EXPORTABLE_DOCTYPES))

	def test_row_limit_is_bounded(self) -> None:
		self.assertGreater(MAX_EXPORT_ROWS, 0)
		self.assertLessEqual(MAX_EXPORT_ROWS, 10_000)

	def test_auditor_scope_modes_exclude_hospital_wide_and_ambiguous_exports(self) -> None:
		self.assertEqual(
			EXPORT_SCOPE_MODES,
			{"Department", "Campus", "Hospital", "Multiple", "Unscoped"},
		)
		self.assertEqual(AUDITOR_EXPORT_SCOPE_MODES, {"Department", "Campus"})

	def test_only_a_single_exact_scope_filter_is_promoted(self) -> None:
		self.assertEqual(_single_scope_filter_value("DEPT-1"), "DEPT-1")
		self.assertEqual(_single_scope_filter_value(["=", "DEPT-1"]), "DEPT-1")
		self.assertEqual(_single_scope_filter_value(["in", ["DEPT-1"]]), "DEPT-1")
		for value in (
			["in", ["DEPT-1", "DEPT-2"]],
			["!=", "DEPT-1"],
			["not in", ["DEPT-1"]],
			"",
			None,
		):
			with self.subTest(value=value):
				self.assertIsNone(_single_scope_filter_value(value))

	def test_spreadsheet_formula_injection_is_neutralized(self) -> None:
		for value in ("=1+1", "+SUM(A1:A2)", "-2+3", "@cmd", "\tformula", "\rformula"):
			with self.subTest(value=value):
				self.assertTrue(_safe_csv_cell(value).startswith("'"))
		self.assertEqual(_safe_csv_cell("normal text"), "normal text")

	def test_approval_lifecycle_has_no_terminal_escape(self) -> None:
		self.assertEqual(_STATUS_TRANSITIONS["Pending"], {"Approved", "Rejected"})
		self.assertEqual(_STATUS_TRANSITIONS["Approved"], {"Processing", "Expired"})
		for terminal in ("Completed", "Rejected", "Failed", "Expired"):
			with self.subTest(status=terminal):
				self.assertNotIn("Pending", _STATUS_TRANSITIONS[terminal])

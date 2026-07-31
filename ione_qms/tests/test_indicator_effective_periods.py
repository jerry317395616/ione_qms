from __future__ import annotations

from datetime import date
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import indicators, versions
from ione_qms.tasks import indicators as indicator_tasks


class _Row(dict):
	def __getattr__(self, fieldname: str):
		return self[fieldname]


class _Document(_Row):
	def __init__(self, values: dict, previous: dict | None = None):
		super().__init__(values)
		self.name = str(values.get("name") or "IND-1-v1")
		self.doctype = "IONE QC Indicator Version"
		self._previous = previous

	def get_doc_before_save(self):
		return self._previous


def _version(
	frequency: str,
	*,
	status: str = "Published",
	effective_from: str = "2026-01-01",
	effective_to: str | None = None,
	rolling_window_days: int | None = None,
) -> _Row:
	return _Row(
		name=f"IND-{frequency}",
		indicator="IND-1",
		status=status,
		calculation_frequency=frequency,
		rolling_window_days=rolling_window_days,
		formula_json=None,
		effective_from=effective_from,
		effective_to=effective_to,
	)


class TestIndicatorEffectivePeriods(TestCase):
	def test_fixed_frequency_nominal_periods(self) -> None:
		cases = (
			("Daily", "2026-07-15", (date(2026, 7, 15), date(2026, 7, 15))),
			("Weekly", "2026-07-19", (date(2026, 7, 13), date(2026, 7, 19))),
			("Monthly", "2026-07-31", (date(2026, 7, 1), date(2026, 7, 31))),
			("Quarterly", "2026-06-30", (date(2026, 4, 1), date(2026, 6, 30))),
			("Annual", "2026-12-31", (date(2026, 1, 1), date(2026, 12, 31))),
		)
		for frequency, as_of, expected in cases:
			with self.subTest(frequency=frequency):
				self.assertEqual(indicators.nominal_indicator_period(_version(frequency), as_of), expected)

	def test_fixed_frequency_is_only_due_at_the_nominal_end(self) -> None:
		for frequency in ("Weekly", "Monthly", "Quarterly", "Annual"):
			with self.subTest(frequency=frequency):
				self.assertIsNone(indicators.nominal_indicator_period(_version(frequency), "2026-07-15"))

	def test_publication_and_retirement_boundaries_must_align(self) -> None:
		valid = (
			_version("Daily", effective_from="2026-07-02", effective_to="2026-07-18"),
			_version("Weekly", effective_from="2026-07-06", effective_to="2026-07-19"),
			_version("Monthly", effective_from="2026-07-01", effective_to="2026-08-31"),
			_version("Quarterly", effective_from="2026-04-01", effective_to="2026-09-30"),
			_version("Annual", effective_from="2026-01-01", effective_to="2026-12-31"),
		)
		for version in valid:
			with self.subTest(frequency=version.calculation_frequency):
				indicators.validate_indicator_effective_boundaries(version)
		invalid = (
			_version("Weekly", effective_from="2026-07-07"),
			_version("Weekly", effective_from="2026-07-06", effective_to="2026-07-18"),
			_version("Monthly", effective_from="2026-07-02"),
			_version("Monthly", effective_from="2026-07-01", effective_to="2026-08-30"),
			_version("Quarterly", effective_from="2026-02-01"),
			_version("Annual", effective_from="2026-02-01"),
		)
		for version in invalid:
			with (
				self.subTest(frequency=version.calculation_frequency),
				self.assertRaisesRegex(ValueError, "nominal period"),
			):
				indicators.validate_indicator_effective_boundaries(version)

	def test_rolling_waits_for_a_complete_window(self) -> None:
		version = _version(
			"Rolling",
			effective_from="2026-07-10",
			rolling_window_days=7,
		)
		self.assertIsNone(indicators.scheduled_indicator_period(version, "2026-07-15"))
		self.assertEqual(
			indicators.scheduled_indicator_period(version, "2026-07-16"),
			(date(2026, 7, 10), date(2026, 7, 16)),
		)
		with self.assertRaisesRegex(ValueError, "starts before"):
			indicators._validate_published_period(
				version,
				date(2026, 7, 9),
				date(2026, 7, 15),
			)
		with self.assertRaisesRegex(ValueError, "complete nominal"):
			indicators._validate_published_period(
				version,
				date(2026, 7, 10),
				date(2026, 7, 15),
			)

	def test_service_rejects_partial_fixed_period_even_inside_effective_dates(self) -> None:
		version = _version(
			"Monthly",
			effective_from="2026-07-01",
			effective_to="2026-08-31",
		)
		indicators._validate_published_period(
			version,
			date(2026, 7, 1),
			date(2026, 7, 31),
		)
		with self.assertRaisesRegex(ValueError, "complete nominal"):
			indicators._validate_published_period(
				version,
				date(2026, 7, 2),
				date(2026, 7, 31),
			)

	def test_retired_version_requires_end_and_remains_calculable_in_its_interval(self) -> None:
		missing_end = _version(
			"Daily",
			status="Retired",
			effective_from="2026-07-01",
		)
		with self.assertRaisesRegex(ValueError, "explicit effective_to"):
			indicators.validate_indicator_effective_boundaries(missing_end)
		retired = _version(
			"Daily",
			status="Retired",
			effective_from="2026-07-01",
			effective_to="2026-07-31",
		)
		self.assertEqual(
			indicator_tasks._period_for_version(retired, date(2026, 7, 31)),
			(date(2026, 7, 31), date(2026, 7, 31)),
		)
		self.assertIsNone(indicator_tasks._period_for_version(retired, date(2026, 8, 1)))
		indicators._validate_published_period(
			retired,
			date(2026, 7, 31),
			date(2026, 7, 31),
		)
		with self.assertRaisesRegex(ValueError, "ends after"):
			indicators._validate_published_period(
				retired,
				date(2026, 8, 1),
				date(2026, 8, 1),
			)

	def test_retirement_can_set_effective_to_once(self) -> None:
		doc = _Document(
			{
				"status": "Retired",
				"effective_from": "2026-07-01",
				"effective_to": "2026-07-31",
			},
			previous={
				"status": "Published",
				"effective_from": "2026-07-01",
				"effective_to": None,
			},
		)
		versions._protect_published_content(
			doc,
			status_field="status",
			content_fields=("effective_from", "effective_to"),
			retirement_mutable_fields=frozenset({"effective_to"}),
		)

	def test_published_and_retired_history_may_not_overlap(self) -> None:
		doc = _Document(
			{
				"name": "IND-1-v2",
				"indicator": "IND-1",
				"status": "Published",
				"effective_from": "2026-07-01",
				"effective_to": None,
			}
		)
		existing = _Row(
			name="IND-1-v1",
			effective_from="2026-01-01",
			effective_to="2026-07-15",
		)
		with (
			patch.object(versions.frappe, "get_all", return_value=[existing]),
			patch.object(
				versions.frappe,
				"throw",
				side_effect=lambda message, *args, **kwargs: (_ for _ in ()).throw(RuntimeError(message)),
			),
			self.assertRaisesRegex(RuntimeError, "overlap governed version"),
		):
			versions._require_no_effective_overlap(
				doc,
				parent_field="indicator",
				status_field="status",
				governed_statuses=("Published", "Retired"),
			)

	def test_scheduler_scans_published_and_retired_versions(self) -> None:
		with (
			patch.object(indicator_tasks, "_has_field", return_value=True),
			patch.object(indicator_tasks.frappe, "get_all", return_value=[]) as get_all,
		):
			indicator_tasks._next_version(None)
		self.assertEqual(
			get_all.call_args.kwargs["filters"]["status"],
			["in", ["Published", "Retired"]],
		)

	def test_read_only_history_audit_reports_boundary_and_overlap_errors(self) -> None:
		row = _version(
			"Monthly",
			status="Retired",
			effective_from="2026-07-02",
			effective_to="2026-07-30",
		)
		row["name"] = "IND-1-v2"
		history = [
			_Row(name="IND-1-v1", effective_from="2026-01-01", effective_to="2026-07-15"),
			_Row(name="IND-1-v2", effective_from="2026-07-02", effective_to="2026-07-30"),
		]
		with patch.object(versions.frappe, "get_all", side_effect=[[row], history]):
			report = versions.audit_indicator_effective_periods()
		self.assertEqual(report["scanned"], 1)
		self.assertEqual(
			{issue["code"] for issue in report["issues"]},
			{"INVALID_EFFECTIVE_BOUNDARY", "OVERLAPPING_EFFECTIVE_INTERVAL"},
		)

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import add_days, get_first_day, get_last_day, getdate, now_datetime

from ione_qms.indicator_engine import (
	IndicatorContext,
	IndicatorResult,
	calculator_code_hash,
	canonical_dimension,
	get_indicator,
	governed_query_contract_hash,
	normalize_dimension_names,
	normalize_dimension_values,
	physical_query_contract_hash,
	resolved_physical_query_contract,
	verify_indicator_publication_contract,
)

_GENERIC_CALCULATOR = "IONE_RECORD_AGGREGATE"
_FREQUENCY_ALIASES = {
	"daily": "Daily",
	"realtime": "Daily",
	"日": "Daily",
	"每日": "Daily",
	"实时": "Daily",
	"weekly": "Weekly",
	"week": "Weekly",
	"周": "Weekly",
	"每周": "Weekly",
	"monthly": "Monthly",
	"month": "Monthly",
	"月": "Monthly",
	"每月": "Monthly",
	"quarterly": "Quarterly",
	"quarter": "Quarterly",
	"季度": "Quarterly",
	"每季度": "Quarterly",
	"annual": "Annual",
	"yearly": "Annual",
	"year": "Annual",
	"年度": "Annual",
	"每年": "Annual",
	"rolling": "Rolling",
	"rolling window": "Rolling",
	"滚动": "Rolling",
	"滚动窗口": "Rolling",
}
_RESULT_DOCTYPE = "IONE Indicator Result"
_DETAIL_DOCTYPE = "IONE Indicator Result Detail"
_CALCULATION_DOCTYPE = "IONE Indicator Calculation"
_POINTER_DOCTYPE = "IONE Indicator Result Pointer"
_QUARANTINE_RECEIPT_DOCTYPE = "IONE Indicator Quarantine Receipt"
_QUARANTINE_DISPOSITION_DOCTYPE = "IONE Indicator Quarantine Disposition"
_SHA256_LENGTH = 64
_HISTORICAL_REPRODUCTION_FLAG = "ione_indicator_historical_reproduction"
_HISTORICAL_REPRODUCTION_CAPABILITY = object()
_INDICATOR_AUDIT_DOCTYPES = (
	_CALCULATION_DOCTYPE,
	_RESULT_DOCTYPE,
	_DETAIL_DOCTYPE,
	_POINTER_DOCTYPE,
)
_INDICATOR_MANIFEST_FORMAT = "ione-indicator-artifact-manifest-v1"
_INDICATOR_MANIFEST_CURSOR_VERSION = "v2"
_INDICATOR_MANIFEST_SCAN_PAGE_SIZE = 500
_INDICATOR_IMMUTABLE_MANIFEST_DOCTYPES = frozenset(
	{
		_CALCULATION_DOCTYPE,
		_RESULT_DOCTYPE,
		_DETAIL_DOCTYPE,
	}
)
_QUARANTINE_REVIEW_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})


def indicator_frequency(version) -> str:
	"""Return the governed calculation frequency or fail closed."""
	raw = str(version.get("frequency") or version.get("calculation_frequency") or "").strip()
	frequency = _FREQUENCY_ALIASES.get(raw.lower())
	if not frequency:
		raise ValueError("Indicator calculation_frequency must be a supported governed value.")
	return frequency


def indicator_rolling_window_days(version) -> int:
	"""Return a reviewed rolling-window size."""
	value = version.get("rolling_window_days")
	if value in (None, ""):
		raw = version.get("formula_json")
		if isinstance(raw, str):
			try:
				raw = json.loads(raw)
			except ValueError:
				raw = {}
		if isinstance(raw, dict):
			value = raw.get("rolling_window_days")
	try:
		window_days = int(value)
	except (TypeError, ValueError) as exc:
		raise ValueError("Rolling indicators require rolling_window_days from 1 to 366.") from exc
	if not 1 <= window_days <= 366:
		raise ValueError("Rolling indicators require rolling_window_days from 1 to 366.")
	return window_days


def validate_indicator_effective_boundaries(version) -> None:
	"""Validate publication and retirement boundaries without changing historical rows."""
	status = str(version.get("status") or version.get("approval_status") or "").strip()
	if status not in {"Published", "Retired"}:
		return
	effective_from = version.get("effective_from")
	effective_to = version.get("effective_to")
	if not effective_from:
		raise ValueError("Published and Retired indicator versions require effective_from.")
	if status == "Retired" and not effective_to:
		raise ValueError("Retired indicator versions require an explicit effective_to.")
	start = getdate(effective_from)
	end = getdate(effective_to) if effective_to else None
	if end and end < start:
		raise ValueError("effective_to cannot be earlier than effective_from.")
	frequency = indicator_frequency(version)
	if frequency == "Rolling":
		indicator_rolling_window_days(version)
		return
	if frequency == "Daily":
		return
	if not _is_aligned_period_start(frequency, start):
		raise ValueError(f"{frequency} indicator effective_from must align to the nominal period start.")
	if end and not _is_aligned_period_end(frequency, end):
		raise ValueError(f"{frequency} indicator effective_to must align to the nominal period end.")


def nominal_indicator_period(version, calculation_date):
	"""Return the complete nominal period ending on calculation_date, if one is due."""
	end = getdate(calculation_date)
	frequency = indicator_frequency(version)
	if frequency == "Daily":
		return end, end
	if frequency == "Weekly":
		if end.weekday() != 6:
			return None
		return add_days(end, -6), end
	if frequency == "Monthly":
		if end != getdate(get_last_day(end)):
			return None
		return getdate(get_first_day(end)), end
	if frequency == "Quarterly":
		if end.month not in {3, 6, 9, 12} or end != getdate(get_last_day(end)):
			return None
		quarter_start_month = ((end.month - 1) // 3) * 3 + 1
		return getdate(f"{end.year}-{quarter_start_month:02d}-01"), end
	if frequency == "Annual":
		if end.month != 12 or end.day != 31:
			return None
		return getdate(f"{end.year}-01-01"), end
	window_days = indicator_rolling_window_days(version)
	return add_days(end, -(window_days - 1)), end


def scheduled_indicator_period(version, calculation_date):
	"""Return a nominal period only when fully contained by the governed interval."""
	validate_indicator_effective_boundaries(version)
	period = nominal_indicator_period(version, calculation_date)
	if period is None:
		return None
	start, end = period
	effective_from = getdate(version.get("effective_from"))
	effective_to = getdate(version.get("effective_to")) if version.get("effective_to") else None
	if start < effective_from or (effective_to and end > effective_to):
		return None
	return period


def _is_aligned_period_start(frequency: str, value) -> bool:
	if frequency == "Weekly":
		return value.weekday() == 0
	if frequency == "Monthly":
		return value.day == 1
	if frequency == "Quarterly":
		return value.day == 1 and value.month in {1, 4, 7, 10}
	if frequency == "Annual":
		return value.month == 1 and value.day == 1
	return True


def _is_aligned_period_end(frequency: str, value) -> bool:
	if frequency == "Weekly":
		return value.weekday() == 6
	if frequency == "Monthly":
		return value == getdate(get_last_day(value))
	if frequency == "Quarterly":
		return value.month in {3, 6, 9, 12} and value == getdate(get_last_day(value))
	if frequency == "Annual":
		return value.month == 12 and value.day == 31
	return True


def calculate_indicator_version(
	indicator_version: str,
	period_start: str,
	period_end: str,
	dimensions: dict[str, str] | str | None = None,
	*,
	calculation: str | None = None,
) -> dict[str, Any]:
	"""Create an immutable result revision from one exact reproducible input receipt."""
	start_date = getdate(period_start)
	end_date = getdate(period_end)
	if end_date < start_date:
		raise ValueError("Indicator period_end cannot be earlier than period_start.")
	dimension_values = normalize_dimension_values(dimensions)
	version = frappe.get_doc("IONE QC Indicator Version", indicator_version)
	_validate_published_period(version, start_date, end_date)
	verify_indicator_publication_contract(version)
	indicator = frappe.get_cached_doc("IONE QC Indicator", version.indicator)
	if str(version.get("lineage_status") or "") != "Verified":
		raise ValueError(f"Indicator version {version.name} lacks verified semantic lineage.")
	indicator_code = str(version.get("indicator_code_snapshot") or "").strip().upper()
	indicator_name = str(version.get("indicator_name_snapshot") or "").strip()
	if not indicator_code or not indicator_name:
		raise ValueError(f"Indicator version {version.name} lacks immutable identity snapshots.")
	calculator_code = _calculator_code(indicator, version, indicator_code)
	calculator_class = get_indicator(calculator_code)
	if calculator_class is None:
		raise ValueError(
			f"Indicator calculator {calculator_code!r} is not registered in reviewed IONE QMS code."
		)
	formula = _formula(indicator, version)
	configured_dimensions = _configured_dimensions(version, formula)
	if tuple(dimension_values) != configured_dimensions:
		raise ValueError("Calculation dimension values must exactly cover the governed indicator dimensions.")
	calculator_class.validate_dimension_capability(formula, configured_dimensions)
	query_contract_hash = governed_query_contract_hash(
		formula,
		list(configured_dimensions),
	)
	published_query_hash = _required_digest(version, "query_contract_hash")
	if not hmac.compare_digest(query_contract_hash, published_query_hash):
		raise ValueError("Indicator query contract no longer matches its published snapshot.")
	version_checksum = _required_digest(version, "publication_checksum")
	mapping_record = _required_text(version, "source_mapping")
	mapping_version = _required_text(version, "mapping_version_snapshot")
	mapping_checksum = _required_digest(version, "mapping_checksum_snapshot")
	source_system = _required_text(version, "source_system_snapshot")
	code_hash = calculator_code_hash(calculator_class)
	published_code_hash = _required_digest(version, "calculator_code_hash")
	if not hmac.compare_digest(code_hash, published_code_hash):
		raise ValueError("Indicator calculator code no longer matches its published snapshot.")
	calculator_version = str(getattr(calculator_class, "version", "") or "")
	published_calculator_version = _required_text(version, "calculator_version_snapshot")
	if not hmac.compare_digest(calculator_version, published_calculator_version):
		raise ValueError("Indicator calculator semantic version no longer matches its published snapshot.")
	try:
		published_physical_contract = json.loads(_required_text(version, "physical_query_contract_json"))
	except ValueError as exc:
		raise ValueError("Indicator published physical query contract is invalid.") from exc
	published_physical_hash = _required_digest(version, "physical_query_contract_hash")
	if not hmac.compare_digest(
		physical_query_contract_hash(published_physical_contract),
		published_physical_hash,
	):
		raise ValueError("Indicator published physical query contract hash verification failed.")
	current_physical_contract = resolved_physical_query_contract(
		formula,
		list(configured_dimensions),
	)
	if not hmac.compare_digest(
		physical_query_contract_hash(current_physical_contract),
		published_physical_hash,
	):
		raise ValueError("Indicator physical schema no longer matches its published snapshot.")
	if not hmac.compare_digest(
		_required_digest(version, "schema_signature_hash"),
		str(current_physical_contract.get("schema_signature_hash") or ""),
	):
		raise ValueError("Indicator source schema signature no longer matches publication.")
	context = IndicatorContext(
		indicator_code=indicator_code,
		indicator_name=indicator_name,
		indicator_version=version.name,
		period_start=start_date,
		period_end=end_date,
		dimensions=dimension_values,
		configured_dimensions=configured_dimensions,
		parameters={"formula": formula},
		calculator_version=calculator_version,
		calculator_code_hash=code_hash,
		mapping_record=mapping_record,
		mapping_version=mapping_version,
		mapping_checksum=mapping_checksum,
		source_system=source_system,
		query_contract_hash=query_contract_hash,
		physical_query_contract_hash=published_physical_hash,
		physical_query_contract=published_physical_contract,
	)
	computation = calculator_class().calculate(context)
	input_receipt = build_indicator_input_receipt(
		context=context,
		computation=computation,
		indicator_version_checksum=version_checksum,
		source_system=source_system,
		published_calculator_code_hash=published_code_hash,
	)
	input_receipt_hash = _digest(input_receipt)
	dimension_hash = _digest(dimension_values)
	series_key = indicator_series_key(
		version.name,
		start_date,
		end_date,
		dimension_hash,
	)
	with frappe.db.advisory_lock(f"ione-qms:indicator-series:{series_key}", timeout=5):
		# Never reuse a historical {"input_receipt_hash": input_receipt_hash}; only
		# the locked current pointer is idempotent, so A→B→A appends revision 3.
		pointer = _lock_result_pointer(series_key)
		if pointer and hmac.compare_digest(
			str(pointer.get("current_input_receipt_hash") or ""),
			input_receipt_hash,
		):
			result_doc = frappe.get_doc(_RESULT_DOCTYPE, pointer.get("current_result"))
			verify_indicator_result_integrity(result_doc, expected_receipt=input_receipt)
			if calculation and str(result_doc.get("calculation") or "") != str(calculation):
				raise ValueError("Requested calculation conflicts with the current result revision.")
			is_current = True
		else:
			if pointer is None and frappe.db.exists(_RESULT_DOCTYPE, {"series_key": series_key}):
				raise ValueError(
					"Indicator series has result revisions but no authoritative current pointer."
				)
			revision = int(pointer.get("current_revision") or 0) + 1 if pointer else 1
			calculation_doc = _ensure_calculation_receipt(
				context,
				input_receipt,
				input_receipt_hash,
				series_key,
				revision,
				calculation,
			)
			result_doc = _insert_result_revision(
				indicator=indicator,
				version=version,
				context=context,
				computation=computation,
				input_receipt=input_receipt,
				input_receipt_hash=input_receipt_hash,
				series_key=series_key,
				revision=revision,
				calculation=calculation_doc.name,
			)
			_insert_result_details(result_doc, computation)
			_complete_calculation_receipt(calculation_doc, result_doc)
			switched_pointer = _switch_result_pointer(
				pointer=pointer,
				result_doc=result_doc,
				input_receipt_hash=input_receipt_hash,
				series_key=series_key,
				revision=revision,
			)
			verify_indicator_result_pointer(switched_pointer)
			_project_current_indicator_result(switched_pointer, result_doc)
			is_current = True
	return {
		"indicator": indicator.name,
		"indicator_code": indicator_code,
		"indicator_version": version.name,
		"result": result_doc.name,
		"result_key": result_doc.get("result_key"),
		"input_receipt_hash": input_receipt_hash,
		"revision": int(result_doc.get("result_revision") or 0),
		"is_current": is_current,
		"numerator": _json_number(computation.numerator),
		"denominator": _json_number(computation.denominator),
		"value": _json_number(computation.value),
		"status": result_doc.get("status"),
	}


def on_indicator_result(doc, method: str | None = None) -> None:
	"""Maintain rebuildable projections only after the atomic current-pointer switch."""
	del method
	if getattr(doc.flags, "ione_defer_projection", False):
		return
	if getattr(doc.flags, "ione_projection_completed", False):
		return
	with current_indicator_result_lock(doc.name) as (pointer, current):
		_project_current_indicator_result(pointer, current)
	doc.flags.ione_projection_completed = True


@contextmanager
def current_indicator_result_lock(result_name: str):
	"""Yield one integrity-verified current result while holding its series/pointer lock."""
	require_completed_batch_authority(result_name)
	candidate = frappe.get_doc(_RESULT_DOCTYPE, result_name)
	series_key = _validate_digest_value(candidate.get("series_key"), "series_key")
	with current_indicator_series_lock(series_key) as (pointer, current):
		if str(pointer.get("current_result") or "") != str(result_name):
			raise ValueError("Indicator consumers may only use the current pointer revision.")
		yield pointer, current


@contextmanager
def current_indicator_results_lock(result_names: tuple[str, ...] | list[str] | set[str]):
	"""Lock multiple current results in global series order and yield them by exact name."""
	names = tuple(sorted({str(name or "").strip() for name in result_names}))
	if any(not name for name in names):
		raise ValueError("Indicator multi-result lock requires exact non-empty result names.")
	for name in names:
		require_completed_batch_authority(name)
	candidates = {name: frappe.get_doc(_RESULT_DOCTYPE, name) for name in names}
	series_by_name = {
		name: _validate_digest_value(candidate.get("series_key"), "series_key")
		for name, candidate in candidates.items()
	}
	if len(set(series_by_name.values())) != len(series_by_name):
		raise ValueError("Indicator multi-result lock cannot bind two current revisions from one series.")
	name_by_series = {series_key: name for name, series_key in series_by_name.items()}
	with ExitStack() as stack:
		locked_by_name: dict[str, Any] = {}
		for series_key in sorted(name_by_series):
			pointer, current = stack.enter_context(current_indicator_series_lock(series_key))
			requested_name = name_by_series[series_key]
			if str(pointer.get("current_result") or "") != requested_name:
				raise ValueError("Indicator consumers may only use exact current pointer revisions.")
			locked_by_name[requested_name] = current
		yield locked_by_name


@contextmanager
def current_indicator_series_lock(series_key: str):
	"""Resolve and hold the verified current revision by stable series identity."""
	series_key = _validate_digest_value(series_key, "series_key")
	with frappe.db.advisory_lock(f"ione-qms:indicator-series:{series_key}", timeout=5):
		pointer = _lock_result_pointer(series_key)
		if not pointer:
			raise ValueError("Indicator series has no authoritative current pointer.")
		current = frappe.get_doc(_RESULT_DOCTYPE, pointer.get("current_result"))
		verify_indicator_result_pointer(pointer)
		verify_indicator_result_integrity(current)
		require_completed_batch_authority(current.name)
		yield pointer, current


def require_completed_batch_authority(result_name: str) -> None:
	"""Reject a scheduled result until its whole batch generation is complete."""
	references = int(
		frappe.db.sql(
			"""
			select count(*)
			from `tabIONE Batch Work Item`
			where result_reference = %s
			""",
			(result_name,),
		)[0][0]
	)
	if not references:
		# Results created by explicitly authorized manual calculations have no
		# scheduler work binding and retain the normal pointer contract.
		return
	authorized = frappe.db.sql(
		"select batch.name, batch.subject_key, batch.period_start, batch.period_end, "
		"batch.recovery_generation, batch.freshness_watermark_json "
		"from `tabIONE Batch Work Item` work "
		"inner join `tabIONE Batch Run` batch on batch.name = work.batch_run "
		"where work.result_reference = %s "
		"and work.status = 'Succeeded' "
		"and batch.batch_type = 'Indicator' "
		"and batch.status = 'Completed' "
		"order by batch.recovery_generation desc, batch.name desc "
		"limit 1",
		(result_name,),
		as_dict=True,
	)
	if not authorized:
		raise ValueError("Indicator result batch generation is not complete.")
	batch = authorized[0]
	if frappe.db.exists(
		"IONE Batch Run",
		{
			"batch_type": "Indicator",
			"subject_key": batch.get("subject_key"),
			"period_start": batch.get("period_start"),
			"period_end": batch.get("period_end"),
			"status": "Completed",
			"recovery_generation": [">", int(batch.get("recovery_generation") or 0)],
		},
	):
		raise ValueError("Indicator result is not from the latest completed batch generation.")
	from ione_qms.indicator_engine.calculator import source_snapshot_watermark

	version = frappe.get_doc("IONE QC Indicator Version", batch.get("subject_key"))
	formula = _formula(None, version)
	dimensions = _configured_dimensions(version, formula)
	current_watermark = source_snapshot_watermark(
		formula,
		list(dimensions),
		getdate(batch.get("period_start")),
		getdate(batch.get("period_end")),
		lineage={
			"source_system": str(version.get("source_system_snapshot") or ""),
			"mapping_record": str(version.get("source_mapping") or ""),
			"mapping_version": str(version.get("mapping_version_snapshot") or ""),
			"mapping_checksum": str(version.get("mapping_checksum_snapshot") or ""),
		},
		physical_query_contract=version.get("physical_query_contract_json"),
	)
	if not hmac.compare_digest(
		_canonical_json(current_watermark),
		str(batch.get("freshness_watermark_json") or ""),
	):
		raise ValueError("Indicator result source freshness watermark has advanced.")


def indicator_result_detail_is_governed_readable(doc) -> bool:
	"""Fail closed unless a detail is current and has no approved terminal disposition."""
	try:
		if str(getattr(doc, "doctype", "") or "") != _DETAIL_DOCTYPE:
			return False
		from ione_qms.indicator_engine.calculator import (
			indicator_result_detail_governance_schema_ready,
			indicator_result_detail_governance_sql,
		)

		if not indicator_result_detail_governance_schema_ready(frappe):
			return False
		rows = frappe.db.sql(
			"select 1 from `tabIONE Indicator Result Detail` source "  # noqa: S608
			f"where source.name = %s and {indicator_result_detail_governance_sql('source')} "
			"limit 1",
			(str(doc.name or ""),),
		)
		if not rows:
			return False
		require_completed_batch_authority(str(doc.get("indicator_result") or ""))
		return True
	except Exception:
		return False


def _project_current_indicator_result(pointer, result_doc) -> None:
	if str(pointer.get("current_result") or "") != str(result_doc.name):
		raise ValueError("Indicator projection source is not the locked current pointer revision.")
	verify_indicator_result_pointer(pointer)
	verify_indicator_result_integrity(result_doc)
	_upsert_daily_fact(result_doc)
	_resolve_indicator_alerts(result_doc, superseded_only=True)
	if _is_breach(str(result_doc.get("status") or "")):
		_upsert_indicator_alert(result_doc)
	else:
		_resolve_indicator_alerts(result_doc)
	result_doc.flags.ione_projection_completed = True


def result_status(
	value: Decimal | None,
	*,
	direction: str | None,
	target: Any = None,
	target_min: Any = None,
	target_max: Any = None,
	warning: Any = None,
	critical: Any = None,
) -> str:
	"""Classify one value using deterministic, direction-aware thresholds."""
	if value is None:
		return "No Data"
	normalized_direction = str(direction or "Higher is Better").strip().lower()
	if normalized_direction in {"target range", "range", "目标区间"}:
		lower_bound = _optional_decimal(target_min)
		upper_bound = _optional_decimal(target_max)
		if lower_bound is None or upper_bound is None:
			raise ValueError("Target Range indicators require both target_min and target_max.")
		if lower_bound > upper_bound:
			raise ValueError("Indicator target_min cannot be greater than target_max.")
		return "Met" if lower_bound <= value <= upper_bound else "Below Target"
	is_lower_better = normalized_direction in {
		"lower",
		"lower is better",
		"decrease",
		"descending",
		"越低越好",
	}
	critical_value = _optional_decimal(critical)
	warning_value = _optional_decimal(warning)
	target_value = _optional_decimal(target)
	if critical_value is not None and _threshold_breached(value, critical_value, is_lower_better):
		return "Critical"
	if warning_value is not None and _threshold_breached(value, warning_value, is_lower_better):
		return "Warning"
	if target_value is not None:
		target_met = value <= target_value if is_lower_better else value >= target_value
		return "Met" if target_met else "Below Target"
	return "Calculated"


def build_indicator_input_receipt(
	*,
	context: IndicatorContext,
	computation: IndicatorResult,
	indicator_version_checksum: str,
	source_system: str,
	published_calculator_code_hash: str,
) -> dict[str, Any]:
	"""Build the canonical receipt whose hash is the immutable idempotency key."""
	lineage = computation.lineage
	if not isinstance(lineage, dict):
		raise ValueError("Indicator calculator did not return structured lineage.")
	canonical_dimensions = normalize_dimension_values(context.dimensions)
	if canonical_dimensions != context.dimensions:
		raise ValueError("Indicator context dimensions must already be canonical.")
	if str(source_system or "").strip() != str(context.source_system or "").strip():
		raise ValueError("Indicator receipt source_system conflicts with its query context.")
	lineage_bindings = {
		"indicator_version": context.indicator_version,
		"period_start": context.period_start.isoformat(),
		"period_end": context.period_end.isoformat(),
		"dimensions": canonical_dimensions,
		"mapping_record": context.mapping_record,
		"mapping_version": context.mapping_version,
		"mapping_checksum": context.mapping_checksum,
		"source_system": context.source_system,
		"query_contract_hash": context.query_contract_hash,
		"physical_query_contract_hash": context.physical_query_contract_hash,
		"calculator_version": context.calculator_version,
		"calculator_code_hash": context.calculator_code_hash,
	}
	for fieldname, expected in lineage_bindings.items():
		if lineage.get(fieldname) != expected:
			raise ValueError(f"Indicator calculator lineage does not match context field {fieldname}.")
	sources = lineage.get("sources")
	if not isinstance(sources, dict) or not sources:
		raise ValueError("Indicator calculation requires a deterministic source-record manifest.")
	if len(sources) > 8:
		raise ValueError("Indicator calculation source manifest exceeds the reviewed source-role limit.")
	normalized_sources: dict[str, dict[str, Any]] = {}
	for role, raw in sorted(sources.items()):
		role_name = str(role or "").strip()
		if not role_name or len(role_name) > 64 or not role_name.replace("_", "").isalnum():
			raise ValueError("Indicator source manifest role must be a bounded canonical identifier.")
		if not isinstance(raw, dict):
			raise ValueError("Indicator source manifest must be a JSON object.")
		manifest_hash = _validate_digest_value(
			raw.get("source_manifest_hash"),
			"source_manifest_hash",
		)
		try:
			manifest_count = int(raw.get("source_manifest_count"))
		except (TypeError, ValueError) as exc:
			raise ValueError("Indicator source manifest count is required.") from exc
		if manifest_count < 0:
			raise ValueError("Indicator source manifest count cannot be negative.")
		dataset = str(raw.get("dataset") or "").strip().lower()
		source_doctype = str(raw.get("source_doctype") or "").strip()
		if not dataset or not source_doctype:
			raise ValueError("Indicator source manifest requires dataset and source_doctype.")
		source_lineage = {
			fieldname: _nonempty_receipt_text(raw.get(fieldname), fieldname)
			for fieldname in (
				"source_system",
				"mapping_record",
				"mapping_version",
				"mapping_checksum",
			)
		}
		source_lineage["mapping_checksum"] = _validate_digest_value(
			source_lineage["mapping_checksum"],
			"source manifest mapping_checksum",
		)
		for fieldname, expected in {
			"source_system": context.source_system,
			"mapping_record": context.mapping_record,
			"mapping_version": context.mapping_version,
			"mapping_checksum": context.mapping_checksum,
		}.items():
			if source_lineage[fieldname] != expected:
				raise ValueError(f"Indicator source manifest {role_name} does not match {fieldname}.")
		normalized_sources[role_name] = {
			"dataset": dataset,
			"source_doctype": source_doctype,
			"source_manifest_count": manifest_count,
			"source_manifest_hash": manifest_hash,
			**source_lineage,
		}
	dimension_hash = _digest(context.dimensions)
	return {
		"receipt_format": "ione-indicator-input-v1",
		"indicator_version": _nonempty_receipt_text(
			context.indicator_version,
			"indicator_version",
		),
		"indicator_version_checksum": _validate_digest_value(
			indicator_version_checksum,
			"indicator_version_checksum",
		),
		"period_start": context.period_start.isoformat(),
		"period_end": context.period_end.isoformat(),
		"dimension_json": canonical_dimensions,
		"dimension_hash": dimension_hash,
		"source_system": _nonempty_receipt_text(source_system, "source_system"),
		"source_manifests": normalized_sources,
		"source_manifest_count": sum(
			source["source_manifest_count"] for source in normalized_sources.values()
		),
		"source_manifest_hash": _digest(normalized_sources),
		"mapping_version": _nonempty_receipt_text(
			context.mapping_version,
			"mapping_version",
		),
		"mapping_record": _nonempty_receipt_text(
			context.mapping_record,
			"mapping_record",
		),
		"mapping_checksum": _validate_digest_value(
			context.mapping_checksum,
			"mapping_checksum",
		),
		"query_contract_hash": _validate_digest_value(
			context.query_contract_hash,
			"query_contract_hash",
		),
		"physical_query_contract_hash": _validate_digest_value(
			context.physical_query_contract_hash,
			"physical_query_contract_hash",
		),
		"calculator_code": _nonempty_receipt_text(
			str(lineage.get("calculator") or "").strip().upper(),
			"calculator_code",
		),
		"calculator_version": _nonempty_receipt_text(
			context.calculator_version,
			"calculator_version",
		),
		"calculator_code_hash": _validate_digest_value(
			context.calculator_code_hash,
			"calculator_code_hash",
		),
		"published_calculator_code_hash": _validate_digest_value(
			published_calculator_code_hash,
			"published_calculator_code_hash",
		),
	}


def indicator_series_key(
	indicator_version: str,
	period_start: Any,
	period_end: Any,
	dimension_hash: str,
) -> str:
	version = str(indicator_version or "").strip()
	if not version:
		raise ValueError("Indicator series requires an exact indicator_version.")
	return _digest(
		{
			"dimension_hash": _validate_digest_value(dimension_hash, "dimension_hash"),
			"indicator_version": version,
			"period_end": str(period_end),
			"period_start": str(period_start),
		}
	)


def _insert_result_revision(
	*,
	indicator,
	version,
	context: IndicatorContext,
	computation: IndicatorResult,
	input_receipt: dict[str, Any],
	input_receipt_hash: str,
	series_key: str,
	revision: int,
	calculation: str,
):
	doctype = _RESULT_DOCTYPE
	dimension_json = _canonical_json(context.dimensions)
	target = _first(version, fields=("target_value", "target"))
	target_min = _first(version, fields=("target_min",))
	target_max = _first(version, fields=("target_max",))
	warning = _first(version, fields=("warning_threshold", "warning_value"))
	critical = _first(version, fields=("critical_threshold", "alert_threshold"))
	direction = _first(version, fields=("direction", "target_direction"))
	semantic_status = result_status(
		computation.value,
		direction=direction,
		target=target,
		target_min=target_min,
		target_max=target_max,
		warning=warning,
		critical=critical,
	)
	revision_key = _series_revision_key(series_key, revision)
	values: dict[str, Any] = {
		"indicator": indicator.name,
		"indicator_version": version.name,
		"calculation": calculation,
		"result_key": revision_key,
		"series_key": series_key,
		"series_revision_key": revision_key,
		"result_revision": revision,
		"input_receipt_hash": input_receipt_hash,
		"input_receipt_json": _canonical_json(input_receipt),
		"period_start": context.period_start,
		"period_end": context.period_end,
		"period": f"{context.period_start.isoformat()}:{context.period_end.isoformat()}",
		"dimension_json": dimension_json,
		"dimension_hash": _digest(context.dimensions),
		"numerator": computation.numerator,
		"denominator": computation.denominator,
		"indicator_value": computation.value,
		"target_value": target,
		"target_min": target_min,
		"target_max": target_max,
		"status": _select_value(
			doctype,
			"status",
			_status_candidates(semantic_status),
		),
		"computed_at": now_datetime(),
		"calculator_key": _calculator_code(indicator, version, context.indicator_code),
		"calculator_version": context.calculator_version,
		"calculator_code_hash": context.calculator_code_hash,
		"query_contract_hash": context.query_contract_hash,
		"physical_query_contract_hash": context.physical_query_contract_hash,
		"source_system": context.source_system,
		"mapping_version": context.mapping_version,
		"mapping_record": context.mapping_record,
		"mapping_checksum": context.mapping_checksum,
		"source_manifest_hash": input_receipt["source_manifest_hash"],
		"source_manifest_count": input_receipt["source_manifest_count"],
		"lineage_status": "Verified",
		"lineage_json": _canonical_json(computation.lineage),
		**_dimension_projection_values(context.dimensions),
	}
	values["result_checksum"] = _result_checksum(values)
	values = _supported_values(doctype, values)
	result_doc = frappe.get_doc({"doctype": doctype, **values})
	result_doc.flags.ione_defer_projection = True
	try:
		result_doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value(
			doctype,
			{"series_revision_key": revision_key},
			"name",
		)
		if not existing:
			raise
		result_doc = frappe.get_doc(doctype, existing)
		verify_indicator_result_integrity(result_doc, expected_receipt=input_receipt)
	return result_doc


def _insert_result_details(result_doc, computation: IndicatorResult) -> None:
	doctype = _DETAIL_DOCTYPE
	if not frappe.db.exists("DocType", doctype):
		raise ValueError("Indicator Result Detail schema is unavailable.")
	parent_receipt = json.loads(result_doc.get("input_receipt_json") or "{}")
	for index, detail in enumerate(computation.details, start=1):
		dimension_type = canonical_dimension(detail.dimension_type) if detail.dimension_type else None
		detail_payload = {
			"dimension_type": dimension_type,
			"dimension_value": detail.dimension_value,
			"numerator": detail.numerator,
			"denominator": detail.denominator,
			"indicator_value": detail.value,
			"reason_code": detail.reason_code,
			"source_reference_hash": detail.source_reference_hash,
			"lineage_json": _canonical_json(detail.lineage),
		}
		detail_key = _digest(
			{
				"detail_index": index,
				"result": result_doc.name,
				"payload": detail_payload,
			}
		)
		values = {
			"indicator_result": result_doc.name,
			"result": result_doc.name,
			"detail_key": detail_key,
			"result_revision": result_doc.get("result_revision"),
			"input_receipt_hash": result_doc.get("input_receipt_hash"),
			"source_system": parent_receipt.get("source_system"),
			"mapping_record": parent_receipt.get("mapping_record"),
			"mapping_version": parent_receipt.get("mapping_version"),
			"mapping_checksum": parent_receipt.get("mapping_checksum"),
			**detail_payload,
		}
		values["detail_checksum"] = _detail_checksum(values)
		values = _supported_values(doctype, values)
		try:
			frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			existing = frappe.db.get_value(doctype, {"detail_key": detail_key}, "name")
			if not existing:
				raise
			verify_indicator_result_detail_integrity(frappe.get_doc(doctype, existing))


def _ensure_calculation_receipt(
	context: IndicatorContext,
	input_receipt: dict[str, Any],
	input_receipt_hash: str,
	series_key: str,
	revision: int,
	requested_calculation: str | None,
):
	calculation_key = _calculation_revision_key(series_key, revision, input_receipt_hash)
	existing = frappe.db.get_value(
		_CALCULATION_DOCTYPE,
		{"calculation_key": calculation_key},
		"name",
	)
	if requested_calculation and requested_calculation != calculation_key:
		raise ValueError("Requested calculation does not match this series revision.")
	if requested_calculation and existing and requested_calculation != existing:
		raise ValueError("Calculation identity conflicts with the exact input receipt.")
	if existing:
		doc = frappe.get_doc(_CALCULATION_DOCTYPE, existing)
		_verify_calculation_receipt(doc, input_receipt)
		return doc
	values = {
		"calculation_key": calculation_key,
		"input_receipt_hash": input_receipt_hash,
		"input_receipt_json": _canonical_json(input_receipt),
		"series_key": series_key,
		"result_revision": revision,
		"indicator_version": context.indicator_version,
		"period_start": context.period_start,
		"period_end": context.period_end,
		"dimension_json": _canonical_json(context.dimensions),
		"dimension_hash": _digest(context.dimensions),
		"receipt_status": "Verified",
		"status": _select_value(_CALCULATION_DOCTYPE, "status", ("Running", "Pending")),
		"started_at": now_datetime(),
		"result_count": 0,
		"error_count": 0,
	}
	values["receipt_checksum"] = _calculation_checksum(values)
	doc = frappe.get_doc(
		{
			"doctype": _CALCULATION_DOCTYPE,
			**_supported_values(_CALCULATION_DOCTYPE, values),
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value(
			_CALCULATION_DOCTYPE,
			{"calculation_key": calculation_key},
			"name",
		)
		if not existing:
			raise
		doc = frappe.get_doc(_CALCULATION_DOCTYPE, existing)
		_verify_calculation_receipt(doc, input_receipt)
	return doc


def _complete_calculation_receipt(calculation_doc, result_doc) -> None:
	calculation_doc.status = _select_value(
		_CALCULATION_DOCTYPE,
		"status",
		("Completed",),
	)
	calculation_doc.completed_at = now_datetime()
	calculation_doc.result = result_doc.name
	calculation_doc.result_count = 1
	calculation_doc.error_count = 0
	calculation_doc.error_message = None
	calculation_doc.flags.ione_receipt_completion = True
	calculation_doc.flags.ignore_permissions = True
	calculation_doc.save()


def _series_revision_key(series_key: str, revision: int) -> str:
	if int(revision or 0) < 1:
		raise ValueError("Indicator result revision must be positive.")
	return _digest(
		{
			"result_revision": int(revision),
			"series_key": _validate_digest_value(series_key, "series_key"),
		}
	)


def _calculation_revision_key(series_key: str, revision: int, input_receipt_hash: str) -> str:
	return _digest(
		{
			"artifact": "indicator-calculation-revision",
			"input_receipt_hash": _validate_digest_value(
				input_receipt_hash,
				"input_receipt_hash",
			),
			"result_revision": int(revision),
			"series_key": _validate_digest_value(series_key, "series_key"),
		}
	)


def _lock_result_pointer(series_key: str):
	name = frappe.db.get_value(_POINTER_DOCTYPE, {"series_key": series_key}, "name")
	if not name:
		return None
	pointer = _lock_result_pointer_for_repair(str(name))
	verify_indicator_result_pointer(pointer)
	return pointer


def _lock_result_pointer_for_repair(pointer_name: str):
	"""Lock a pointer without trusting fields that a governed repair must replace."""
	locked = frappe.db.sql(
		f"select name from `tab{_POINTER_DOCTYPE}` where name = %s for update",  # noqa: S608
		(pointer_name,),
	)
	if not locked:
		raise ValueError("Indicator current-result pointer disappeared while locked.")
	return frappe.get_doc(_POINTER_DOCTYPE, pointer_name)


def _switch_result_pointer(
	*,
	pointer,
	result_doc,
	input_receipt_hash: str,
	series_key: str,
	revision: int,
):
	values = {
		"series_key": series_key,
		"indicator": result_doc.get("indicator"),
		"indicator_version": result_doc.get("indicator_version"),
		"period_start": result_doc.get("period_start"),
		"period_end": result_doc.get("period_end"),
		"dimension_hash": result_doc.get("dimension_hash"),
		"dimension_json": result_doc.get("dimension_json"),
		**_dimension_projection_values(
			json.loads(result_doc.get("dimension_json") or "{}"),
		),
		"current_result": result_doc.name,
		"current_revision": revision,
		"current_input_receipt_hash": input_receipt_hash,
		"current_result_checksum": result_doc.get("result_checksum"),
		"switched_at": now_datetime(),
	}
	values["pointer_checksum"] = _pointer_checksum(values)
	if pointer:
		if revision != int(pointer.get("current_revision") or 0) + 1:
			raise ValueError("Indicator result revisions must be strictly monotonic.")
		pointer.update(_supported_values(_POINTER_DOCTYPE, values))
		pointer.flags.ione_pointer_switch = True
		pointer.flags.ignore_permissions = True
		pointer.save()
		return pointer
	doc = frappe.get_doc(
		{
			"doctype": _POINTER_DOCTYPE,
			**_supported_values(_POINTER_DOCTYPE, values),
		}
	)
	doc.flags.ione_pointer_switch = True
	doc.insert(ignore_permissions=True)
	return doc


def _upsert_daily_fact(result_doc) -> None:
	doctype = "IONE Daily Quality Fact"
	if not frappe.db.exists("DocType", doctype):
		return
	fact_key = _digest(
		{
			"series_key": result_doc.get("series_key"),
			"date": result_doc.get("period_end"),
		}
	)
	values = _supported_values(
		doctype,
		{
			"fact_key": fact_key,
			"indicator": result_doc.get("indicator"),
			"indicator_version": result_doc.get("indicator_version"),
			"indicator_result": result_doc.name,
			"indicator_result_revision": result_doc.get("result_revision"),
			"input_receipt_hash": result_doc.get("input_receipt_hash"),
			"result_checksum": result_doc.get("result_checksum"),
			"series_key": result_doc.get("series_key"),
			"fact_date": result_doc.get("period_end"),
			"date": result_doc.get("period_end"),
			"hospital": result_doc.get("hospital"),
			"campus": result_doc.get("campus"),
			"department": result_doc.get("department"),
			"ward": result_doc.get("ward"),
			"medical_staff": result_doc.get("medical_staff"),
			"medical_group": result_doc.get("medical_group"),
			"physician": result_doc.get("physician"),
			"disease": result_doc.get("disease"),
			"surgery": result_doc.get("surgery"),
			"drg": result_doc.get("drg"),
			"dip": result_doc.get("dip"),
			"dimension_json": result_doc.get("dimension_json"),
			"numerator": result_doc.get("numerator"),
			"denominator": result_doc.get("denominator"),
			"indicator_value": result_doc.get("indicator_value"),
			"target_value": result_doc.get("target_value"),
			"target_min": result_doc.get("target_min"),
			"target_max": result_doc.get("target_max"),
			"status": result_doc.get("status"),
			"source_modified": result_doc.get("modified"),
			"lineage_json": result_doc.get("lineage_json"),
		},
	)
	existing = _get_by_unique_key(
		doctype,
		"fact_key",
		fact_key,
		fallback_filters=_supported_values(
			doctype,
			{"fact_key": fact_key},
		),
	)
	_upsert_doc(doctype, existing, values)


def _upsert_indicator_alert(result_doc) -> None:
	doctype = "IONE Indicator Alert"
	if not frappe.db.exists("DocType", doctype):
		return
	alert_key = _digest({"result": result_doc.name, "status": result_doc.get("status")})
	values = _supported_values(
		doctype,
		{
			"alert_key": alert_key,
			"series_key": result_doc.get("series_key"),
			"indicator": result_doc.get("indicator"),
			"indicator_version": result_doc.get("indicator_version"),
			"indicator_result": result_doc.name,
			"hospital": result_doc.get("hospital"),
			"campus": result_doc.get("campus"),
			"department": result_doc.get("department"),
			"ward": result_doc.get("ward"),
			"medical_staff": result_doc.get("medical_staff"),
			"medical_group": result_doc.get("medical_group"),
			"physician": result_doc.get("physician"),
			"disease": result_doc.get("disease"),
			"surgery": result_doc.get("surgery"),
			"drg": result_doc.get("drg"),
			"dip": result_doc.get("dip"),
			"alert_level": _select_value(
				doctype,
				"alert_level",
				("Critical", "High") if result_doc.get("status") == "Critical" else ("Warning", "High"),
			),
			"severity": _select_value(
				doctype,
				"severity",
				("Critical", "High") if result_doc.get("status") == "Critical" else ("Warning", "Medium"),
			),
			"status": _select_value(doctype, "status", ("Open", "Active", "Pending")),
			"message": (
				f"Indicator result {result_doc.name} is {result_doc.get('status')} "
				f"with value {result_doc.get('indicator_value')}."
			),
			"triggered_at": now_datetime(),
		},
	)
	existing = _get_by_unique_key(
		doctype,
		"alert_key",
		alert_key,
		fallback_filters=_supported_values(
			doctype,
			{"indicator_result": result_doc.name, "status": ["in", ["Open", "Active", "Pending"]]},
		),
	)
	_upsert_doc(doctype, existing, values)


def _resolve_indicator_alerts(result_doc, *, superseded_only: bool = False) -> None:
	doctype = "IONE Indicator Alert"
	if not frappe.db.exists("DocType", doctype):
		return
	if not _has_field(doctype, "series_key"):
		raise ValueError("Indicator Alert schema lacks the current-series projection key.")
	link_field = next(
		(fieldname for fieldname in ("indicator_result", "result") if _has_field(doctype, fieldname)),
		None,
	)
	if not link_field:
		return
	filters: dict[str, Any] = {"series_key": result_doc.get("series_key")}
	if superseded_only:
		filters[link_field] = ["!=", result_doc.name]
	if _has_field(doctype, "status"):
		filters["status"] = ["in", ["Open", "Active", "Pending"]]
	resolved = 0
	while True:
		names = frappe.get_all(
			doctype,
			filters=filters,
			pluck="name",
			order_by="name asc",
			limit_page_length=101,
		)
		if resolved + len(names) > 10_000:
			raise ValueError("Indicator alert projection exceeds the governed resolution bound.")
		for name in names[:100]:
			doc = frappe.get_doc(doctype, name)
			values = _supported_values(
				doctype,
				{
					"status": _select_value(doctype, "status", ("Resolved", "Closed")),
					"resolved_at": now_datetime(),
					"resolution_note": (
						f"Indicator result {result_doc.name} returned to {result_doc.get('status')}."
					),
				},
			)
			doc.update(values)
			doc.flags.ignore_permissions = True
			doc.save()
			resolved += 1
		if len(names) <= 100:
			break


def _upsert_doc(doctype: str, name: str | None, values: dict[str, Any]):
	if name:
		doc = frappe.get_doc(doctype, name)
		doc.update(values)
		doc.flags.ignore_permissions = True
		doc.save()
		return doc
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.insert(ignore_permissions=True)
	return doc


def _get_by_unique_key(
	doctype: str,
	fieldname: str,
	value: str,
	*,
	fallback_filters: dict[str, Any],
) -> str | None:
	if _has_field(doctype, fieldname):
		return frappe.db.get_value(doctype, {fieldname: value}, "name")
	if not fallback_filters:
		return None
	return frappe.db.get_value(doctype, fallback_filters, "name")


def _calculator_code(indicator, version, indicator_code: str) -> str:
	del indicator
	for fieldname in ("calculator_key", "calculator_code", "calculation_method"):
		value = version.get(fieldname)
		if value and get_indicator(str(value)):
			return str(value).strip().upper()
	if get_indicator(indicator_code):
		return indicator_code
	if _formula(None, version):
		return _GENERIC_CALCULATOR
	return indicator_code


def _validate_published_period(version, period_start, period_end) -> None:
	status = version.get("status") or version.get("approval_status")
	if str(status or "").strip() not in {"Published", "Retired"}:
		raise ValueError(f"Indicator version {version.name} is not governed for calculation.")
	validate_indicator_effective_boundaries(version)
	expected = nominal_indicator_period(version, period_end)
	if expected is None or expected != (period_start, period_end):
		raise ValueError(f"Indicator version {version.name} requires a complete nominal calculation period.")
	effective_from = getdate(version.get("effective_from")) if version.get("effective_from") else None
	effective_to = getdate(version.get("effective_to")) if version.get("effective_to") else None
	if effective_from is None or period_start < effective_from:
		raise ValueError(f"Indicator version {version.name} period starts before its effective interval.")
	if effective_to and period_end > effective_to:
		raise ValueError(f"Indicator version {version.name} period ends after its effective interval.")


def _formula(indicator, version) -> dict[str, Any]:
	del indicator
	formula: dict[str, Any] = {}
	for fieldname in ("formula_json", "calculation_json", "formula", "parameters_json"):
		value = version.get(fieldname)
		if value in (None, ""):
			continue
		if isinstance(value, dict):
			formula = dict(value)
			break
		loaded = json.loads(value)
		if not isinstance(loaded, dict):
			raise ValueError("Indicator formula must be a JSON object.")
		formula = loaded
		break
	if version.get("multiplier") not in (None, ""):
		formula.setdefault("multiplier", version.get("multiplier"))
	if version.get("precision") not in (None, ""):
		formula.setdefault("precision", version.get("precision"))
	return formula


def _configured_dimensions(version, formula: dict[str, Any]) -> tuple[str, ...]:
	raw = version.get("dimensions_json")
	if raw in (None, ""):
		raw = formula.get("dimensions") or []
	return normalize_dimension_names(raw)


def _dimension_projection_values(dimensions: dict[str, str]) -> dict[str, str | None]:
	physician = dimensions.get("physician")
	return {
		"hospital": dimensions.get("hospital"),
		"campus": dimensions.get("campus"),
		"department": dimensions.get("department"),
		"ward": dimensions.get("ward"),
		"medical_group": dimensions.get("medical_group"),
		"physician": physician,
		# Compatibility projection only; canonical hashes never contain medical_staff.
		"medical_staff": physician,
		"disease": dimensions.get("disease"),
		"surgery": dimensions.get("surgery"),
		"drg": dimensions.get("drg"),
		"dip": dimensions.get("dip"),
	}


def _required_text(doc, fieldname: str) -> str:
	value = str(doc.get(fieldname) or "").strip()
	if not value:
		raise ValueError(f"Indicator version requires immutable {fieldname}.")
	return value


def _required_digest(doc, fieldname: str) -> str:
	return _validate_digest_value(doc.get(fieldname), fieldname)


def _validate_digest_value(value: Any, label: str) -> str:
	digest = str(value or "").strip().lower()
	if len(digest) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in digest):
		raise ValueError(f"Indicator {label} must be a SHA-256 digest.")
	return digest


_RESULT_INTEGRITY_FIELDS = (
	"result_key",
	"series_key",
	"series_revision_key",
	"result_revision",
	"input_receipt_hash",
	"source_system",
	"mapping_record",
	"mapping_version",
	"mapping_checksum",
	"input_receipt_json",
	"indicator",
	"indicator_version",
	"calculation",
	"period",
	"period_start",
	"period_end",
	"dimension_json",
	"dimension_hash",
	"hospital",
	"campus",
	"department",
	"ward",
	"medical_group",
	"physician",
	"medical_staff",
	"disease",
	"surgery",
	"drg",
	"dip",
	"numerator",
	"denominator",
	"indicator_value",
	"target_value",
	"target_min",
	"target_max",
	"status",
	"computed_at",
	"calculator_key",
	"calculator_version",
	"calculator_code_hash",
	"query_contract_hash",
	"physical_query_contract_hash",
	"source_manifest_hash",
	"source_manifest_count",
	"lineage_status",
	"lineage_json",
)
_RESULT_NUMERIC_FIELDS = frozenset(
	{
		"result_revision",
		"numerator",
		"denominator",
		"indicator_value",
		"target_value",
		"target_min",
		"target_max",
		"source_manifest_count",
	}
)
_DETAIL_INTEGRITY_FIELDS = (
	"detail_key",
	"indicator_result",
	"result",
	"result_revision",
	"input_receipt_hash",
	"source_system",
	"mapping_record",
	"mapping_version",
	"mapping_checksum",
	"dimension_type",
	"dimension_value",
	"numerator",
	"denominator",
	"indicator_value",
	"reason_code",
	"source_reference_hash",
	"lineage_json",
)
_CALCULATION_INTEGRITY_FIELDS = (
	"calculation_key",
	"input_receipt_hash",
	"input_receipt_json",
	"series_key",
	"result_revision",
	"indicator_version",
	"period_start",
	"period_end",
	"dimension_json",
	"dimension_hash",
	"receipt_status",
)
_POINTER_INTEGRITY_FIELDS = (
	"series_key",
	"indicator",
	"indicator_version",
	"period_start",
	"period_end",
	"dimension_hash",
	"dimension_json",
	"hospital",
	"campus",
	"department",
	"ward",
	"medical_group",
	"physician",
	"medical_staff",
	"disease",
	"surgery",
	"drg",
	"dip",
	"current_result",
	"current_revision",
	"current_input_receipt_hash",
	"current_result_checksum",
	"switched_at",
)


def _integrity_payload(
	source,
	fields: tuple[str, ...],
	*,
	numeric_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
	payload: dict[str, Any] = {}
	for fieldname in fields:
		value = source.get(fieldname)
		if fieldname in numeric_fields:
			value = _canonical_number(value)
		elif value is not None and not isinstance(value, str | bool | int | float):
			value = str(value)
		payload[fieldname] = value
	return payload


def _canonical_number(value: Any) -> str | None:
	if value in (None, ""):
		return None
	number = Decimal(str(value))
	if not number.is_finite():
		raise ValueError("Indicator integrity numbers must be finite.")
	return format(number.normalize(), "f")


def _result_checksum(source) -> str:
	return _digest(
		_integrity_payload(
			source,
			_RESULT_INTEGRITY_FIELDS,
			numeric_fields=_RESULT_NUMERIC_FIELDS,
		)
	)


def _detail_checksum(source) -> str:
	return _digest(
		_integrity_payload(
			source,
			_DETAIL_INTEGRITY_FIELDS,
			numeric_fields=frozenset({"result_revision", "numerator", "denominator", "indicator_value"}),
		)
	)


def _calculation_checksum(source) -> str:
	return _digest(_integrity_payload(source, _CALCULATION_INTEGRITY_FIELDS))


def _pointer_checksum(source) -> str:
	return _digest(
		_integrity_payload(
			source,
			_POINTER_INTEGRITY_FIELDS,
			numeric_fields=frozenset({"current_revision"}),
		)
	)


def verify_indicator_result_integrity(
	doc,
	*,
	expected_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
	if str(doc.get("lineage_status") or "") != "Verified":
		raise ValueError("Legacy or quarantined indicator results are not authoritative.")
	receipt_json = doc.get("input_receipt_json")
	try:
		receipt = json.loads(receipt_json) if isinstance(receipt_json, str) else receipt_json
	except ValueError as exc:
		raise ValueError("Indicator input receipt is not valid JSON.") from exc
	if not isinstance(receipt, dict):
		raise ValueError("Indicator input receipt is required.")
	receipt_hash = _digest(receipt)
	if not hmac.compare_digest(
		receipt_hash,
		_validate_digest_value(doc.get("input_receipt_hash"), "input_receipt_hash"),
	):
		raise ValueError("Indicator input receipt hash verification failed.")
	if (
		str(receipt.get("indicator_version") or "") != str(doc.get("indicator_version") or "")
		or str(receipt.get("period_start") or "") != str(doc.get("period_start") or "")
		or str(receipt.get("period_end") or "") != str(doc.get("period_end") or "")
		or str(receipt.get("dimension_hash") or "") != str(doc.get("dimension_hash") or "")
	):
		raise ValueError("Indicator result identity does not match its input receipt.")
	dimensions = normalize_dimension_values(receipt.get("dimension_json"))
	try:
		stored_dimensions = normalize_dimension_values(doc.get("dimension_json"))
	except ValueError as exc:
		raise ValueError("Indicator result dimension JSON is invalid.") from exc
	if dimensions != stored_dimensions or not hmac.compare_digest(
		_digest(dimensions),
		_validate_digest_value(doc.get("dimension_hash"), "dimension_hash"),
	):
		raise ValueError("Indicator result dimensions do not match their canonical receipt.")
	for fieldname, expected in _dimension_projection_values(dimensions).items():
		if str(doc.get(fieldname) or "") != str(expected or ""):
			raise ValueError("Indicator result dimension projections do not match their receipt.")
	for receipt_field, result_field in {
		"calculator_code": "calculator_key",
		"calculator_version": "calculator_version",
		"calculator_code_hash": "calculator_code_hash",
		"query_contract_hash": "query_contract_hash",
		"physical_query_contract_hash": "physical_query_contract_hash",
		"source_system": "source_system",
		"mapping_record": "mapping_record",
		"mapping_version": "mapping_version",
		"mapping_checksum": "mapping_checksum",
		"source_manifest_hash": "source_manifest_hash",
		"source_manifest_count": "source_manifest_count",
	}.items():
		if str(receipt.get(receipt_field) or "") != str(doc.get(result_field) or ""):
			raise ValueError(f"Indicator result {result_field} does not match its exact input receipt.")
	if expected_receipt is not None and not hmac.compare_digest(
		receipt_hash,
		_digest(expected_receipt),
	):
		raise ValueError("Existing indicator result does not match the requested input receipt.")
	expected_series = indicator_series_key(
		str(doc.get("indicator_version") or ""),
		doc.get("period_start"),
		doc.get("period_end"),
		str(doc.get("dimension_hash") or ""),
	)
	if not hmac.compare_digest(expected_series, str(doc.get("series_key") or "")):
		raise ValueError("Indicator result series key verification failed.")
	if int(doc.get("result_revision") or 0) < 1:
		raise ValueError("Indicator result revision must be positive.")
	expected_revision_key = _series_revision_key(
		str(doc.get("series_key") or ""),
		int(doc.get("result_revision") or 0),
	)
	if not hmac.compare_digest(
		expected_revision_key,
		_validate_digest_value(doc.get("series_revision_key"), "series_revision_key"),
	):
		raise ValueError("Indicator result series/revision identity verification failed.")
	if not hmac.compare_digest(
		expected_revision_key,
		_validate_digest_value(doc.get("result_key"), "result_key"),
	):
		raise ValueError("Indicator result key does not equal its series/revision identity.")
	if not hmac.compare_digest(
		_result_checksum(doc),
		_validate_digest_value(doc.get("result_checksum"), "result_checksum"),
	):
		raise ValueError("Indicator result checksum verification failed.")
	calculation_name = str(doc.get("calculation") or "")
	if not calculation_name:
		raise ValueError("Indicator result must reference its exact calculation receipt.")
	calculation = frappe.get_doc(_CALCULATION_DOCTYPE, calculation_name)
	_verify_calculation_receipt(calculation, receipt)
	if str(calculation.get("indicator_version") or "") != str(doc.get("indicator_version") or ""):
		raise ValueError("Indicator result and calculation version do not match.")
	version = frappe.get_doc("IONE QC Indicator Version", doc.get("indicator_version"))
	verify_indicator_publication_contract(version)
	if str(version.get("indicator") or "") != str(doc.get("indicator") or ""):
		raise ValueError("Indicator result does not match its immutable version parent.")
	if not hmac.compare_digest(
		_required_digest(version, "publication_checksum"),
		_validate_digest_value(
			receipt.get("indicator_version_checksum"),
			"indicator_version_checksum",
		),
	):
		raise ValueError("Indicator result does not match its signed publication checksum.")
	for version_field, receipt_field in {
		"calculator_version_snapshot": "calculator_version",
		"calculator_code_hash": "published_calculator_code_hash",
		"query_contract_hash": "query_contract_hash",
		"physical_query_contract_hash": "physical_query_contract_hash",
		"source_system_snapshot": "source_system",
		"source_mapping": "mapping_record",
		"mapping_version_snapshot": "mapping_version",
		"mapping_checksum_snapshot": "mapping_checksum",
	}.items():
		if str(version.get(version_field) or "") != str(receipt.get(receipt_field) or ""):
			raise ValueError(f"Indicator result receipt no longer matches publication field {version_field}.")
	return receipt


def verify_indicator_result_detail_integrity(doc) -> None:
	if not hmac.compare_digest(
		_detail_checksum(doc),
		_validate_digest_value(doc.get("detail_checksum"), "detail_checksum"),
	):
		raise ValueError("Indicator result detail checksum verification failed.")
	result_name = str(doc.get("indicator_result") or "")
	if not result_name or str(doc.get("result") or "") != result_name:
		raise ValueError("Indicator result detail must reference one exact result revision.")
	result = frappe.get_doc(_RESULT_DOCTYPE, result_name)
	receipt = verify_indicator_result_integrity(result)
	if int(doc.get("result_revision") or 0) != int(result.get("result_revision") or 0) or str(
		doc.get("input_receipt_hash") or ""
	) != str(result.get("input_receipt_hash") or ""):
		raise ValueError("Indicator result detail is not bound to its parent revision receipt.")
	for fieldname in ("source_system", "mapping_record", "mapping_version", "mapping_checksum"):
		if str(doc.get(fieldname) or "") != str(receipt.get(fieldname) or ""):
			raise ValueError(f"Indicator result detail {fieldname} does not match its parent receipt.")
	if doc.get("dimension_type"):
		if canonical_dimension(str(doc.get("dimension_type"))) != str(doc.get("dimension_type")):
			raise ValueError("Indicator result detail dimension_type must be canonical.")


def _verify_calculation_receipt(doc, expected_receipt: dict[str, Any]) -> None:
	try:
		receipt = json.loads(doc.get("input_receipt_json") or "")
	except ValueError as exc:
		raise ValueError("Indicator calculation receipt is invalid.") from exc
	receipt_hash = _digest(receipt) if isinstance(receipt, dict) else ""
	if not isinstance(receipt, dict) or receipt_hash != _digest(expected_receipt):
		raise ValueError("Indicator calculation receipt conflicts with the immutable input.")
	if receipt_hash != str(doc.get("input_receipt_hash") or ""):
		raise ValueError("Indicator calculation input hash does not match its exact receipt.")
	if not hmac.compare_digest(
		_calculation_checksum(doc),
		_validate_digest_value(doc.get("receipt_checksum"), "receipt_checksum"),
	):
		raise ValueError("Indicator calculation receipt checksum verification failed.")
	expected_series = indicator_series_key(
		str(expected_receipt.get("indicator_version") or ""),
		expected_receipt.get("period_start"),
		expected_receipt.get("period_end"),
		str(expected_receipt.get("dimension_hash") or ""),
	)
	revision = int(doc.get("result_revision") or 0)
	if revision < 1:
		raise ValueError("Indicator calculation receipt revision must be positive.")
	expected_calculation_key = _calculation_revision_key(expected_series, revision, receipt_hash)
	if not hmac.compare_digest(
		expected_calculation_key,
		_validate_digest_value(doc.get("calculation_key"), "calculation_key"),
	):
		raise ValueError("Indicator calculation key does not match its series revision.")
	for fieldname, expected in {
		"series_key": expected_series,
		"result_revision": revision,
		"indicator_version": expected_receipt.get("indicator_version"),
		"period_start": expected_receipt.get("period_start"),
		"period_end": expected_receipt.get("period_end"),
		"dimension_hash": expected_receipt.get("dimension_hash"),
		"dimension_json": _canonical_json(normalize_dimension_values(expected_receipt.get("dimension_json"))),
	}.items():
		if str(doc.get(fieldname) or "") != str(expected or ""):
			raise ValueError(f"Indicator calculation {fieldname} does not match its receipt.")


def verify_indicator_result_pointer(doc) -> None:
	if not hmac.compare_digest(
		_pointer_checksum(doc),
		_validate_digest_value(doc.get("pointer_checksum"), "pointer_checksum"),
	):
		raise ValueError("Indicator current-result pointer checksum verification failed.")
	result_name = str(doc.get("current_result") or "")
	if not result_name:
		raise ValueError("Indicator current-result pointer must reference a result revision.")
	result = frappe.get_doc(_RESULT_DOCTYPE, result_name)
	verify_indicator_result_integrity(result)
	if (
		str(result.get("series_key") or "") != str(doc.get("series_key") or "")
		or str(result.get("indicator") or "") != str(doc.get("indicator") or "")
		or str(result.get("indicator_version") or "") != str(doc.get("indicator_version") or "")
		or str(result.get("period_start") or "") != str(doc.get("period_start") or "")
		or str(result.get("period_end") or "") != str(doc.get("period_end") or "")
		or str(result.get("dimension_hash") or "") != str(doc.get("dimension_hash") or "")
		or str(result.get("dimension_json") or "") != str(doc.get("dimension_json") or "")
		or int(result.get("result_revision") or 0) != int(doc.get("current_revision") or 0)
		or str(result.get("input_receipt_hash") or "") != str(doc.get("current_input_receipt_hash") or "")
		or str(result.get("result_checksum") or "") != str(doc.get("current_result_checksum") or "")
	):
		raise ValueError("Indicator current-result pointer does not match its result revision.")
	for fieldname, expected in _dimension_projection_values(
		normalize_dimension_values(result.get("dimension_json"))
	).items():
		if str(doc.get(fieldname) or "") != str(expected or ""):
			raise ValueError("Indicator current-result pointer dimension projection is inconsistent.")


def validate_indicator_result_revision(doc, method: str | None = None) -> None:
	del method
	if doc.get_doc_before_save() is not None:
		raise frappe.ValidationError("Indicator result revisions are append-only.")
	try:
		verify_indicator_result_integrity(doc)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def validate_indicator_result_detail(doc, method: str | None = None) -> None:
	del method
	if doc.get_doc_before_save() is not None:
		raise frappe.ValidationError("Indicator result details are append-only.")
	try:
		verify_indicator_result_detail_integrity(doc)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def validate_indicator_calculation_receipt(doc, method: str | None = None) -> None:
	del method
	previous = doc.get_doc_before_save()
	if str(doc.get("receipt_status") or "") != "Verified":
		raise frappe.ValidationError("Only verified indicator calculation receipts may run.")
	try:
		receipt = json.loads(doc.get("input_receipt_json") or "")
		_verify_calculation_receipt(doc, receipt)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(str(exc)) from exc
	if previous is None:
		return
	if not getattr(doc.flags, "ione_receipt_completion", False):
		raise frappe.ValidationError("Indicator calculation receipts cannot be edited.")
	for fieldname in _CALCULATION_INTEGRITY_FIELDS:
		if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or ""):
			raise frappe.ValidationError("Indicator calculation input receipt is immutable.")
	if (
		str(previous.get("status") or "") not in {"Pending", "Running"}
		or str(doc.get("status") or "") != "Completed"
	):
		raise frappe.ValidationError("Invalid indicator calculation completion transition.")


def validate_indicator_result_pointer(doc, method: str | None = None) -> None:
	del method
	if not getattr(doc.flags, "ione_pointer_switch", False):
		raise frappe.ValidationError("Indicator current-result pointers are service managed.")
	try:
		verify_indicator_result_pointer(doc)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def prevent_indicator_revision_deletion(doc, method: str | None = None) -> None:
	del doc, method
	raise frappe.ValidationError("Indicator revisions and current pointers are immutable audit records.")


def verify_indicator_result_reproducibility(result_name: str) -> dict[str, Any]:
	"""Re-run any immutable historical revision without writes and compare its receipts."""
	context = getattr(frappe.flags, _HISTORICAL_REPRODUCTION_FLAG, None)
	if (
		not isinstance(context, dict)
		or context.get("capability") is not _HISTORICAL_REPRODUCTION_CAPABILITY
		or str(context.get("result") or "") != str(result_name)
		or str(context.get("user") or "") != str(frappe.session.user or "")
		or not context.get("authorization")
	):
		raise frappe.PermissionError(
			"Historical indicator reproduction requires a controlled dual-person authorization receipt."
		)
	candidate = frappe.get_doc(_RESULT_DOCTYPE, result_name)
	series_key = _validate_digest_value(candidate.get("series_key"), "series_key")
	with frappe.db.advisory_lock(f"ione-qms:indicator-series:{series_key}", timeout=5):
		result_doc = frappe.get_doc(_RESULT_DOCTYPE, result_name)
		if not hmac.compare_digest(series_key, str(result_doc.get("series_key") or "")):
			raise ValueError("Indicator historical result changed series during reproduction.")
		verify_indicator_result_integrity(result_doc)
		return _verify_indicator_result_reproducibility_locked(result_doc)


@contextmanager
def _authorized_historical_indicator_reproduction(result_name: str, authorization: str):
	"""Issue a request-local capability consumed by the historical audit service only."""
	previous = getattr(frappe.flags, _HISTORICAL_REPRODUCTION_FLAG, None)
	setattr(
		frappe.flags,
		_HISTORICAL_REPRODUCTION_FLAG,
		{
			"authorization": str(authorization),
			"capability": _HISTORICAL_REPRODUCTION_CAPABILITY,
			"result": str(result_name),
			"user": str(frappe.session.user or ""),
		},
	)
	try:
		yield
	finally:
		if previous is None:
			delattr(frappe.flags, _HISTORICAL_REPRODUCTION_FLAG)
		else:
			setattr(frappe.flags, _HISTORICAL_REPRODUCTION_FLAG, previous)


def _verify_indicator_result_reproducibility_locked(result_doc) -> dict[str, Any]:
	receipt = verify_indicator_result_integrity(result_doc)
	version = frappe.get_doc("IONE QC Indicator Version", result_doc.get("indicator_version"))
	verify_indicator_publication_contract(version)
	if not hmac.compare_digest(
		_required_digest(version, "publication_checksum"),
		str(receipt.get("indicator_version_checksum") or ""),
	):
		return {"verified": False, "reason_code": "INDICATOR_VERSION_DRIFT"}
	for receipt_field, expected in {
		"source_system": str(version.get("source_system_snapshot") or "").strip(),
		"mapping_record": str(version.get("source_mapping") or "").strip(),
		"mapping_version": str(version.get("mapping_version_snapshot") or "").strip(),
		"mapping_checksum": str(version.get("mapping_checksum_snapshot") or "").strip().lower(),
		"query_contract_hash": str(version.get("query_contract_hash") or "").strip().lower(),
		"physical_query_contract_hash": str(version.get("physical_query_contract_hash") or "")
		.strip()
		.lower(),
		"published_calculator_code_hash": str(version.get("calculator_code_hash") or "").strip().lower(),
	}.items():
		if not expected or not hmac.compare_digest(
			expected,
			str(receipt.get(receipt_field) or ""),
		):
			return {"verified": False, "reason_code": "PUBLISHED_EXECUTION_CONTRACT_DRIFT"}
	formula = _formula(None, version)
	dimensions = normalize_dimension_values(receipt.get("dimension_json"))
	configured_dimensions = _configured_dimensions(version, formula)
	calculator = get_indicator(str(receipt.get("calculator_code") or ""))
	if calculator is None:
		return {"verified": False, "reason_code": "CALCULATOR_UNAVAILABLE"}
	current_code_hash = calculator_code_hash(calculator)
	if not hmac.compare_digest(
		current_code_hash,
		str(receipt.get("calculator_code_hash") or ""),
	):
		return {"verified": False, "reason_code": "CALCULATOR_CODE_DRIFT"}
	if str(getattr(calculator, "version", "") or "") != str(version.get("calculator_version_snapshot") or ""):
		return {"verified": False, "reason_code": "CALCULATOR_VERSION_DRIFT"}
	query_hash = governed_query_contract_hash(formula, list(configured_dimensions))
	if not hmac.compare_digest(query_hash, str(receipt.get("query_contract_hash") or "")):
		return {"verified": False, "reason_code": "QUERY_CONTRACT_DRIFT"}
	current_physical_contract = resolved_physical_query_contract(
		formula,
		list(configured_dimensions),
	)
	current_physical_hash = physical_query_contract_hash(current_physical_contract)
	if not hmac.compare_digest(
		current_physical_hash,
		str(receipt.get("physical_query_contract_hash") or ""),
	):
		return {"verified": False, "reason_code": "PHYSICAL_QUERY_CONTRACT_DRIFT"}
	try:
		published_physical_contract = json.loads(str(version.get("physical_query_contract_json") or ""))
	except ValueError:
		return {"verified": False, "reason_code": "PHYSICAL_QUERY_CONTRACT_INVALID"}
	if not hmac.compare_digest(
		physical_query_contract_hash(published_physical_contract),
		current_physical_hash,
	):
		return {"verified": False, "reason_code": "PHYSICAL_QUERY_CONTRACT_DRIFT"}
	context = IndicatorContext(
		indicator_code=str(version.get("indicator_code_snapshot") or "").strip().upper(),
		indicator_name=str(version.get("indicator_name_snapshot") or "").strip(),
		indicator_version=version.name,
		period_start=getdate(receipt.get("period_start")),
		period_end=getdate(receipt.get("period_end")),
		dimensions=dimensions,
		configured_dimensions=configured_dimensions,
		parameters={"formula": formula},
		calculator_version=str(getattr(calculator, "version", "1")),
		calculator_code_hash=current_code_hash,
		mapping_record=str(receipt.get("mapping_record") or ""),
		mapping_version=str(receipt.get("mapping_version") or ""),
		mapping_checksum=str(receipt.get("mapping_checksum") or ""),
		source_system=str(receipt.get("source_system") or ""),
		query_contract_hash=query_hash,
		physical_query_contract_hash=current_physical_hash,
		physical_query_contract=published_physical_contract,
	)
	computation = calculator().calculate(context)
	reproduced_receipt = build_indicator_input_receipt(
		context=context,
		computation=computation,
		indicator_version_checksum=str(receipt.get("indicator_version_checksum") or ""),
		source_system=str(receipt.get("source_system") or ""),
		published_calculator_code_hash=str(receipt.get("published_calculator_code_hash") or ""),
	)
	if not hmac.compare_digest(_digest(receipt), _digest(reproduced_receipt)):
		return {
			"verified": False,
			"reason_code": "SOURCE_MANIFEST_DRIFT",
			"expected_input_receipt_hash": result_doc.get("input_receipt_hash"),
			"observed_input_receipt_hash": _digest(reproduced_receipt),
		}
	expected_values = (
		_canonical_number(result_doc.get("numerator")),
		_canonical_number(result_doc.get("denominator")),
		_canonical_number(result_doc.get("indicator_value")),
	)
	observed_values = (
		_canonical_number(computation.numerator),
		_canonical_number(computation.denominator),
		_canonical_number(computation.value),
	)
	if expected_values != observed_values:
		return {"verified": False, "reason_code": "CALCULATION_OUTPUT_DRIFT"}
	return {
		"verified": True,
		"reason_code": "REPRODUCED",
		"input_receipt_hash": result_doc.get("input_receipt_hash"),
		"result_checksum": result_doc.get("result_checksum"),
	}


def quarantine_legacy_indicator_receipts(batch_size: int = 200) -> dict[str, Any]:
	"""Boundedly verify every immutable artifact and quarantine unprovable history.

	The migration singleton stores an ordinal cursor over one frozen artifact
	manifest. A final manifest recheck prevents a lower-sorting restore from being
	skipped while the scan is in flight. Invalid records receive an append-only
	quarantine receipt and are never rewritten into authoritative lineage.
	"""
	limit = min(max(int(batch_size or 200), 1), 1_000)
	if any(not frappe.db.exists("DocType", doctype) for doctype in _INDICATOR_AUDIT_DOCTYPES):
		return {"verified": 0, "quarantined": 0, "blocked": 1, "has_more": False}
	if not _indicator_quarantine_schema_ready():
		return {"verified": 0, "quarantined": 0, "blocked": 1, "has_more": False}
	if _indicator_verification_complete():
		completed_binding = _stored_indicator_manifest_binding()
		if completed_binding and indicator_artifact_manifest_matches(completed_binding):
			return {
				"verified": 0,
				"quarantined": 0,
				"blocked": _unresolved_indicator_quarantine_count(),
				"has_more": False,
				**completed_binding,
			}
		_set_indicator_verification_state(None, complete=False)

	cursor_binding, offset = _indicator_verification_cursor()
	if cursor_binding is None:
		cursor_binding = _indicator_artifact_manifest(str(now_datetime()))
		offset = 0
	work, has_more, next_offset = _indicator_audit_page(
		str(cursor_binding["artifact_high_watermark"]),
		offset,
		limit,
	)
	verified = 0
	quarantined = 0
	for doctype, name in work:
		doc = frappe.get_doc(doctype, name)
		fingerprint = _indicator_artifact_fingerprint(doc)
		if _approved_indicator_disposition_exists(doctype, name, fingerprint):
			continue
		try:
			_verify_indicator_artifact(doc)
		except TypeError, ValueError, frappe.DoesNotExistError:
			_quarantine_indicator_artifact(
				doc,
				fingerprint=fingerprint,
				reason_code="LEGACY_INPUT_RECEIPT_UNPROVABLE",
			)
			quarantined += 1
		else:
			verified += 1
	if has_more:
		_set_indicator_verification_state(
			_indicator_verification_cursor_value(cursor_binding, next_offset),
			complete=False,
		)
		return {
			"verified": verified,
			"quarantined": quarantined,
			"blocked": 0,
			"has_more": True,
			**cursor_binding,
		}

	final_binding = _indicator_artifact_manifest(str(cursor_binding["artifact_high_watermark"]))
	if not _indicator_manifest_bindings_equal(cursor_binding, final_binding) or not (
		indicator_artifact_manifest_matches(final_binding, require_resolved=False)
	):
		restart_binding = _indicator_artifact_manifest(str(now_datetime()))
		_set_indicator_verification_state(
			_indicator_verification_cursor_value(restart_binding, 0),
			complete=False,
		)
		return {
			"verified": verified,
			"quarantined": quarantined,
			"blocked": 0,
			"has_more": True,
			**restart_binding,
		}
	_set_indicator_verification_state(None, complete=True)
	return {
		"verified": verified,
		"quarantined": quarantined,
		"blocked": _unresolved_indicator_quarantine_count(),
		"has_more": False,
		**final_binding,
	}


def request_indicator_quarantine_disposition(
	quarantine_receipt: str,
	action: str,
	request_reason: str,
	replacement_result: str | None = None,
) -> dict[str, Any]:
	"""Request a terminal legacy disposition; approval must come from another human."""
	user = _require_independent_quarantine_reviewer()
	action = str(action or "").strip()
	if action not in {"Exclude Legacy", "Replace"}:
		raise ValueError("Indicator quarantine action must be Exclude Legacy or Replace.")
	reason = str(request_reason or "").strip()
	if not reason or len(reason) > 2_000:
		raise ValueError("Indicator quarantine disposition requires a bounded reason.")
	receipt = frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, quarantine_receipt)
	verify_indicator_quarantine_receipt(receipt)
	target_doctype = str(receipt.get("target_doctype") or "")
	if target_doctype == _POINTER_DOCTYPE and action != "Replace":
		raise ValueError("A quarantined current pointer must be repaired by a verified replacement.")
	if target_doctype == _DETAIL_DOCTYPE and action != "Exclude Legacy":
		raise ValueError("A quarantined result detail may only receive an exact legacy exclusion.")
	replacement = None
	if action == "Replace":
		if not replacement_result:
			raise ValueError("Replacement dispositions require a verified result.")
		replacement = _verified_replacement_result(receipt, replacement_result)
	elif replacement_result:
		raise ValueError("Exclude Legacy dispositions cannot name a replacement result.")
	with frappe.db.advisory_lock(f"ione-qms:indicator-disposition:{receipt.name}", timeout=5):
		existing = frappe.db.get_value(
			_QUARANTINE_DISPOSITION_DOCTYPE,
			{
				"quarantine_receipt": receipt.name,
				"status": ["in", ["Pending Review", "Approved"]],
			},
			"name",
		)
		if existing:
			existing_doc = frappe.get_doc(_QUARANTINE_DISPOSITION_DOCTYPE, existing)
			verify_indicator_quarantine_disposition(existing_doc)
			return _disposition_outcome(existing_doc)
		attempt = (
			int(
				frappe.db.count(
					_QUARANTINE_DISPOSITION_DOCTYPE,
					{"quarantine_receipt": receipt.name},
				)
			)
			+ 1
		)
		key = _digest(
			{
				"action": action,
				"attempt": attempt,
				"quarantine_receipt": receipt.name,
				"replacement_result": replacement.name if replacement else None,
				"requested_by": user,
			}
		)
		values = {
			"disposition_key": key,
			"quarantine_receipt": receipt.name,
			"action": action,
			"replacement_result": replacement.name if replacement else None,
			"request_reason": reason,
			"requested_by": user,
			"requested_at": now_datetime(),
			"status": "Pending Review",
			"reviewed_by": None,
			"reviewed_at": None,
			"review_comment": None,
		}
		values["disposition_checksum"] = _disposition_checksum(values)
		doc = frappe.get_doc({"doctype": _QUARANTINE_DISPOSITION_DOCTYPE, **values})
		doc.flags.ione_quarantine_disposition_service = True
		doc.insert(ignore_permissions=True)
		return _disposition_outcome(doc)


def review_indicator_quarantine_disposition(
	disposition: str,
	approve: int,
	review_comment: str,
) -> dict[str, Any]:
	"""Record the independent terminal decision and its immutable checksum."""
	reviewer = _require_independent_quarantine_reviewer()
	comment = str(review_comment or "").strip()
	if not comment or len(comment) > 2_000:
		raise ValueError("Indicator quarantine review requires a bounded comment.")
	with frappe.db.advisory_lock(f"ione-qms:indicator-disposition-review:{disposition}", timeout=5):
		frappe.db.sql(
			f"select name from `tab{_QUARANTINE_DISPOSITION_DOCTYPE}` "  # noqa: S608
			"where name = %s for update",
			(disposition,),
		)
		doc = frappe.get_doc(_QUARANTINE_DISPOSITION_DOCTYPE, disposition)
		verify_indicator_quarantine_disposition(doc)
		if str(doc.get("status") or "") != "Pending Review":
			raise ValueError("Indicator quarantine disposition already has a terminal decision.")
		if reviewer == str(doc.get("requested_by") or ""):
			raise ValueError("Indicator quarantine requester and reviewer must be different people.")
		receipt = frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, doc.get("quarantine_receipt"))
		verify_indicator_quarantine_receipt(receipt)
		if int(approve or 0):
			_validate_approved_quarantine_disposition(doc, receipt)
		doc.status = "Approved" if int(approve or 0) else "Rejected"
		doc.reviewed_by = reviewer
		doc.reviewed_at = now_datetime()
		doc.review_comment = comment
		doc.disposition_checksum = _disposition_checksum(doc)
		doc.flags.ione_quarantine_disposition_service = True
		doc.flags.ignore_permissions = True
		doc.save()
		return _disposition_outcome(doc)


def validate_indicator_quarantine_receipt(doc, method: str | None = None) -> None:
	del method
	if doc.get_doc_before_save() is not None:
		raise frappe.ValidationError("Indicator quarantine receipts are append-only.")
	if not getattr(doc.flags, "ione_quarantine_migration", False):
		raise frappe.ValidationError("Indicator quarantine receipts are migration-service managed.")
	try:
		verify_indicator_quarantine_receipt(doc)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def verify_indicator_quarantine_receipt(doc) -> None:
	if str(doc.get("target_doctype") or "") not in _INDICATOR_AUDIT_DOCTYPES:
		raise ValueError("Indicator quarantine receipt target is outside the audited artifact set.")
	if (
		not doc.get("target_name")
		or not doc.get("target_fingerprint")
		or not doc.get("reason_code")
		or not doc.get("detected_at")
		or not doc.get("migration_key")
	):
		raise ValueError("Indicator quarantine receipt lacks immutable audit identity.")
	_validate_digest_value(doc.get("target_fingerprint"), "target_fingerprint")
	expected_key = _digest(
		{
			"target_doctype": doc.get("target_doctype"),
			"target_fingerprint": doc.get("target_fingerprint"),
			"target_name": doc.get("target_name"),
		}
	)
	if not hmac.compare_digest(expected_key, str(doc.get("quarantine_receipt_key") or "")):
		raise ValueError("Indicator quarantine receipt identity verification failed.")
	if not hmac.compare_digest(
		_quarantine_receipt_checksum(doc),
		_validate_digest_value(
			doc.get("quarantine_receipt_checksum"),
			"quarantine_receipt_checksum",
		),
	):
		raise ValueError("Indicator quarantine receipt checksum verification failed.")


def validate_indicator_quarantine_disposition(doc, method: str | None = None) -> None:
	del method
	if not getattr(doc.flags, "ione_quarantine_disposition_service", False):
		raise frappe.ValidationError("Indicator quarantine dispositions are service managed.")
	previous = doc.get_doc_before_save()
	if previous and str(previous.get("status") or "") in {"Approved", "Rejected"}:
		raise frappe.ValidationError("Terminal indicator quarantine dispositions are immutable.")
	if previous:
		for fieldname in (
			"disposition_key",
			"quarantine_receipt",
			"action",
			"replacement_result",
			"request_reason",
			"requested_by",
			"requested_at",
		):
			if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or ""):
				raise frappe.ValidationError("Indicator quarantine request identity is immutable.")
	try:
		verify_indicator_quarantine_disposition(doc)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def verify_indicator_quarantine_disposition(doc) -> None:
	status = str(doc.get("status") or "")
	action = str(doc.get("action") or "")
	requested_by = str(doc.get("requested_by") or "")
	reviewed_by = str(doc.get("reviewed_by") or "")
	if status not in {"Pending Review", "Approved", "Rejected"}:
		raise ValueError("Indicator quarantine disposition status is invalid.")
	if action not in {"Exclude Legacy", "Replace"}:
		raise ValueError("Indicator quarantine disposition action is invalid.")
	if not doc.get("quarantine_receipt") or not doc.get("request_reason") or not doc.get("requested_at"):
		raise ValueError("Indicator quarantine disposition lacks immutable request identity.")
	if requested_by in {"", "Administrator", "Guest"}:
		raise ValueError("Indicator quarantine disposition requires a named requester.")
	_validate_digest_value(doc.get("disposition_key"), "disposition_key")
	if action == "Replace" and not doc.get("replacement_result"):
		raise ValueError("Replacement quarantine disposition requires a replacement result.")
	if action == "Exclude Legacy" and doc.get("replacement_result"):
		raise ValueError("Exclude Legacy disposition cannot contain a replacement result.")
	if status == "Pending Review":
		if any(doc.get(fieldname) for fieldname in ("reviewed_by", "reviewed_at", "review_comment")):
			raise ValueError("Pending quarantine disposition cannot contain a terminal review.")
	else:
		if (
			reviewed_by in {"", "Administrator", "Guest"}
			or not doc.get("reviewed_at")
			or not doc.get("review_comment")
		):
			raise ValueError("Terminal quarantine disposition requires a named reviewer receipt.")
		if requested_by == reviewed_by:
			raise ValueError("Quarantine requester and reviewer must be independent.")
	if not hmac.compare_digest(
		_disposition_checksum(doc),
		_validate_digest_value(doc.get("disposition_checksum"), "disposition_checksum"),
	):
		raise ValueError("Indicator quarantine disposition checksum verification failed.")


def _indicator_quarantine_schema_ready() -> bool:
	return _indicator_quarantine_receipts_ready() and all(
		_has_field("IONE Migration State", fieldname)
		for fieldname in ("indicator_verification_cursor", "indicator_verification_complete")
	)


def _indicator_quarantine_receipts_ready() -> bool:
	return all(
		frappe.db.exists("DocType", doctype)
		for doctype in (_QUARANTINE_RECEIPT_DOCTYPE, _QUARANTINE_DISPOSITION_DOCTYPE)
	)


def _indicator_verification_complete() -> bool:
	return bool(
		int(
			frappe.db.get_single_value(
				"IONE Migration State",
				"indicator_verification_complete",
			)
			or 0
		)
	)


def indicator_artifact_manifest_matches(
	binding: dict[str, Any],
	*,
	require_resolved: bool = True,
) -> bool:
	"""Verify the receipt-bound legacy population and every later artifact."""
	try:
		if require_resolved and _unresolved_indicator_quarantine_count():
			return False
		expected = _normalize_indicator_manifest_binding(binding)
		current = _indicator_artifact_manifest(str(expected["artifact_high_watermark"]))
		if not _indicator_manifest_bindings_equal(expected, current):
			return False
		high_watermark = str(expected["artifact_high_watermark"])
		return _prefix_mutable_indicator_artifacts_are_governed(
			high_watermark,
			allow_unresolved_quarantine=not require_resolved,
		) and _post_watermark_indicator_artifacts_are_governed(
			high_watermark,
			allow_unresolved_quarantine=not require_resolved,
		)
	except Exception:
		return False


def _normalize_indicator_manifest_binding(binding: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(binding, dict):
		raise ValueError("Indicator artifact manifest binding is required.")
	if str(binding.get("artifact_manifest_format") or "") != _INDICATOR_MANIFEST_FORMAT:
		raise ValueError("Indicator artifact manifest format is not current.")
	count_value = binding.get("artifact_manifest_count")
	if isinstance(count_value, bool):
		raise ValueError("Indicator artifact manifest count is invalid.")
	try:
		count = int(count_value)
	except (TypeError, ValueError) as exc:
		raise ValueError("Indicator artifact manifest count is invalid.") from exc
	if count < 0:
		raise ValueError("Indicator artifact manifest count is invalid.")
	manifest_hash = _validate_digest_value(
		binding.get("artifact_manifest_hash"),
		"artifact_manifest_hash",
	)
	high_watermark = str(binding.get("artifact_high_watermark") or "").strip()
	if not high_watermark or len(high_watermark) > 64 or "|" in high_watermark:
		raise ValueError("Indicator artifact high-watermark is invalid.")
	return {
		"artifact_manifest_format": _INDICATOR_MANIFEST_FORMAT,
		"artifact_manifest_count": count,
		"artifact_manifest_hash": manifest_hash,
		"artifact_high_watermark": high_watermark,
	}


def _indicator_manifest_bindings_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
	try:
		normalized_left = _normalize_indicator_manifest_binding(left)
		normalized_right = _normalize_indicator_manifest_binding(right)
	except ValueError:
		return False
	return (
		normalized_left["artifact_manifest_format"] == normalized_right["artifact_manifest_format"]
		and normalized_left["artifact_manifest_count"] == normalized_right["artifact_manifest_count"]
		and normalized_left["artifact_high_watermark"] == normalized_right["artifact_high_watermark"]
		and hmac.compare_digest(
			str(normalized_left["artifact_manifest_hash"]),
			str(normalized_right["artifact_manifest_hash"]),
		)
	)


def _indicator_artifact_manifest(
	high_watermark: str,
) -> dict[str, Any]:
	high_watermark = str(high_watermark or "").strip()
	if not high_watermark or len(high_watermark) > 64 or "|" in high_watermark:
		raise ValueError("Indicator artifact high-watermark is invalid.")
	manifest_hash = hashlib.sha256()
	manifest_hash.update(b"[")
	count = 0
	for doctype in _INDICATOR_AUDIT_DOCTYPES:
		for row in _frozen_indicator_artifact_rows(doctype, high_watermark):
			name = str(row.get("name") or "")
			creation = str(row.get("creation") or "")
			signature = ""
			if doctype in _INDICATOR_IMMUTABLE_MANIFEST_DOCTYPES:
				doc = frappe.get_doc(doctype, name)
				if str(doc.name or "") != name:
					raise ValueError("Indicator artifact manifest identity changed during capture.")
				signature = _indicator_artifact_fingerprint(doc)
			entry = {
				"creation": creation,
				"doctype": doctype,
				"name": name,
				"signature": signature,
			}
			if count:
				manifest_hash.update(b",")
			manifest_hash.update(_canonical_json(entry).encode("utf-8"))
			count += 1
	manifest_hash.update(b"]")
	binding = {
		"artifact_manifest_format": _INDICATOR_MANIFEST_FORMAT,
		"artifact_manifest_count": count,
		"artifact_manifest_hash": manifest_hash.hexdigest(),
		"artifact_high_watermark": high_watermark,
	}
	return _normalize_indicator_manifest_binding(binding)


def _indicator_artifact_rows(
	doctype: str,
	*,
	fields: tuple[str, ...] = ("name", "creation"),
	filters: dict[str, Any] | list[list[str]] | None = None,
):
	offset = 0
	while True:
		rows = frappe.get_all(
			doctype,
			fields=list(fields),
			filters=filters,
			order_by="creation asc, name asc",
			limit_start=offset,
			limit_page_length=_INDICATOR_MANIFEST_SCAN_PAGE_SIZE,
		)
		if not rows:
			return
		yield from rows
		if len(rows) < _INDICATOR_MANIFEST_SCAN_PAGE_SIZE:
			return
		offset += len(rows)


def _frozen_indicator_artifact_filters(high_watermark: str) -> tuple[list[list[str]], ...]:
	"""Partition the frozen population without dropping NULL/empty creation values."""
	return (
		[["creation", "is", "not set"]],
		[
			["creation", "is", "set"],
			["creation", "<=", high_watermark],
		],
	)


def _frozen_indicator_artifact_rows(
	doctype: str,
	high_watermark: str,
	*,
	fields: tuple[str, ...] = ("name", "creation"),
):
	for filters in _frozen_indicator_artifact_filters(high_watermark):
		yield from _indicator_artifact_rows(doctype, fields=fields, filters=filters)


def _prefix_mutable_indicator_artifacts_are_governed(
	high_watermark: str,
	*,
	allow_unresolved_quarantine: bool = False,
) -> bool:
	for doctype in (_CALCULATION_DOCTYPE, _POINTER_DOCTYPE):
		for row in _frozen_indicator_artifact_rows(doctype, high_watermark):
			if not _indicator_artifact_is_governed_or_excluded(
				doctype,
				str(row.get("name") or ""),
				allow_unresolved_quarantine=allow_unresolved_quarantine,
			):
				return False
	return True


def _post_watermark_indicator_artifacts_are_governed(
	high_watermark: str,
	*,
	allow_unresolved_quarantine: bool = False,
) -> bool:
	for doctype in _INDICATOR_AUDIT_DOCTYPES:
		for row in _indicator_artifact_rows(
			doctype,
			filters=[
				["creation", "is", "set"],
				["creation", ">", high_watermark],
			],
		):
			if not _indicator_artifact_is_governed_or_excluded(
				doctype,
				str(row.get("name") or ""),
				allow_unresolved_quarantine=allow_unresolved_quarantine,
			):
				return False
	return True


def _indicator_artifact_is_governed_or_excluded(
	doctype: str,
	name: str,
	*,
	allow_unresolved_quarantine: bool = False,
) -> bool:
	doc = None
	try:
		doc = frappe.get_doc(doctype, name)
		_verify_indicator_artifact(doc)
		return True
	except TypeError, ValueError, frappe.DoesNotExistError:
		if doc is None:
			return False
		try:
			fingerprint = _indicator_artifact_fingerprint(doc)
		except Exception:
			return False
		if _approved_indicator_disposition_exists(doctype, doc.name, fingerprint):
			return True
		return allow_unresolved_quarantine and _exact_indicator_quarantine_receipt_exists(
			doctype,
			doc.name,
			fingerprint,
		)


def _stored_indicator_manifest_binding() -> dict[str, Any] | None:
	try:
		raw = frappe.db.get_single_value("IONE Migration State", "last_summary_json")
		summary = json.loads(str(raw or ""))
		if not isinstance(summary, dict):
			return None
		bindings = summary.get("artifact_bindings")
		if isinstance(bindings, dict) and isinstance(bindings.get("indicator_artifacts"), dict):
			return _normalize_indicator_manifest_binding(bindings["indicator_artifacts"])
		last_batch = summary.get("last_batch")
		component = last_batch.get("indicator_receipts") if isinstance(last_batch, dict) else None
		if isinstance(component, dict):
			return _normalize_indicator_manifest_binding(component)
	except TypeError, ValueError, json.JSONDecodeError:
		return None
	return None


def _indicator_verification_cursor() -> tuple[dict[str, Any] | None, int]:
	raw = str(frappe.db.get_single_value("IONE Migration State", "indicator_verification_cursor") or "")
	if not raw:
		return None, 0
	try:
		version, high_watermark, offset_text, count_text, manifest_hash = raw.split("|", 4)
		offset = int(offset_text)
		binding = _normalize_indicator_manifest_binding(
			{
				"artifact_manifest_format": _INDICATOR_MANIFEST_FORMAT,
				"artifact_manifest_count": int(count_text),
				"artifact_manifest_hash": manifest_hash,
				"artifact_high_watermark": high_watermark,
			}
		)
	except (TypeError, ValueError) as exc:
		raise ValueError("Indicator migration verification cursor is invalid.") from exc
	if (
		version != _INDICATOR_MANIFEST_CURSOR_VERSION
		or offset < 0
		or offset > int(binding["artifact_manifest_count"])
	):
		raise ValueError("Indicator migration verification cursor is outside its contract.")
	return binding, offset


def _indicator_verification_cursor_value(binding: dict[str, Any], offset: int) -> str:
	normalized = _normalize_indicator_manifest_binding(binding)
	value = "|".join(
		(
			_INDICATOR_MANIFEST_CURSOR_VERSION,
			str(normalized["artifact_high_watermark"]),
			str(int(offset)),
			str(normalized["artifact_manifest_count"]),
			str(normalized["artifact_manifest_hash"]),
		)
	)
	if len(value) > 140:
		raise ValueError("Indicator migration verification cursor exceeds its storage contract.")
	return value


def _indicator_audit_page(
	high_watermark: str,
	offset: int,
	limit: int,
) -> tuple[list[tuple[str, str]], bool, int]:
	if offset < 0:
		raise ValueError("Indicator migration verification offset is outside its manifest.")
	work: list[tuple[str, str]] = []
	remaining_offset = offset
	segments = [
		(
			doctype,
			filters,
			int(frappe.db.count(doctype, filters=filters) or 0),
		)
		for doctype in _INDICATOR_AUDIT_DOCTYPES
		for filters in _frozen_indicator_artifact_filters(high_watermark)
	]
	total = sum(count for _doctype, _filters, count in segments)
	for doctype, filters, count in segments:
		if remaining_offset >= count:
			remaining_offset -= count
			continue
		remaining = limit - len(work)
		rows = frappe.get_all(
			doctype,
			filters=filters,
			pluck="name",
			order_by="creation asc, name asc",
			limit_start=remaining_offset,
			limit_page_length=remaining,
		)
		work.extend((doctype, str(name)) for name in rows)
		remaining_offset = 0
		if len(work) >= limit:
			break
	end = offset + len(work)
	if offset > total:
		raise ValueError("Indicator migration verification offset is outside its live manifest.")
	return work, end < total, end


def _set_indicator_verification_state(cursor: str | None, *, complete: bool) -> None:
	for fieldname, value in {
		"indicator_verification_cursor": cursor,
		"indicator_verification_complete": int(complete),
	}.items():
		frappe.db.set_single_value("IONE Migration State", fieldname, value)


def _verify_indicator_artifact(doc) -> None:
	if doc.doctype == _RESULT_DOCTYPE:
		verify_indicator_result_integrity(doc)
		return
	if doc.doctype == _DETAIL_DOCTYPE:
		verify_indicator_result_detail_integrity(doc)
		return
	if doc.doctype == _POINTER_DOCTYPE:
		verify_indicator_result_pointer(doc)
		_verify_series_revision_continuity(str(doc.get("series_key") or ""), pointer=doc)
		return
	try:
		receipt = json.loads(doc.get("input_receipt_json") or "")
	except ValueError as exc:
		raise ValueError("Indicator calculation receipt is invalid.") from exc
	if not isinstance(receipt, dict):
		raise ValueError("Indicator calculation receipt is required.")
	_verify_calculation_receipt(doc, receipt)
	if str(doc.get("status") or "") == "Completed":
		result_name = str(doc.get("result") or "")
		if not result_name:
			raise ValueError("Completed indicator calculation lacks a result revision.")
		result = frappe.get_doc(_RESULT_DOCTYPE, result_name)
		if (
			str(result.get("calculation") or "") != str(doc.name)
			or int(result.get("result_revision") or 0) != int(doc.get("result_revision") or 0)
			or str(result.get("series_key") or "") != str(doc.get("series_key") or "")
		):
			raise ValueError("Indicator calculation/result relationship is invalid.")


def _verify_series_revision_continuity(series_key: str, *, pointer=None) -> list[Any]:
	"""Require exactly one immutable result for each revision in the closed interval 1..N."""
	series_key = _validate_digest_value(series_key, "series_key")
	rows = frappe.get_all(
		_RESULT_DOCTYPE,
		filters={"series_key": series_key},
		fields=["name", "result_revision", "series_revision_key", "result_key"],
		order_by="result_revision asc, name asc",
		limit_page_length=10_001,
	)
	if not rows:
		raise ValueError("Indicator result series has no immutable revisions.")
	if len(rows) > 10_000:
		raise ValueError("Indicator result series exceeds the governed 10,000-revision audit bound.")
	for expected_revision, row in enumerate(rows, start=1):
		try:
			revision = int(row.get("result_revision"))
		except (TypeError, ValueError) as exc:
			raise ValueError("Indicator result revision sequence is not an exact integer series.") from exc
		if revision != expected_revision:
			raise ValueError("Indicator result revisions must be exactly contiguous from 1 through N.")
		expected_key = _series_revision_key(series_key, expected_revision)
		if not hmac.compare_digest(
			expected_key,
			_validate_digest_value(row.get("series_revision_key"), "series_revision_key"),
		) or not hmac.compare_digest(
			expected_key,
			_validate_digest_value(row.get("result_key"), "result_key"),
		):
			raise ValueError("Indicator result revision sequence contains an invalid series identity.")
	if pointer is None:
		pointer_name = frappe.db.get_value(_POINTER_DOCTYPE, {"series_key": series_key}, "name")
		if not pointer_name:
			raise ValueError("Indicator result series has no authoritative pointer.")
		pointer = frappe.get_doc(_POINTER_DOCTYPE, pointer_name)
	last = rows[-1]
	if (
		str(pointer.get("series_key") or "") != series_key
		or int(pointer.get("current_revision") or 0) != len(rows)
		or str(pointer.get("current_result") or "") != str(last.get("name") or "")
	):
		raise ValueError("Indicator pointer does not select the terminal contiguous revision.")
	return rows


def _indicator_artifact_fingerprint(doc) -> str:
	fields = {
		_RESULT_DOCTYPE: (
			*(fieldname for fieldname in _RESULT_INTEGRITY_FIELDS if fieldname != "lineage_status"),
			"result_checksum",
		),
		_DETAIL_DOCTYPE: (*_DETAIL_INTEGRITY_FIELDS, "detail_checksum"),
		_CALCULATION_DOCTYPE: (
			*(fieldname for fieldname in _CALCULATION_INTEGRITY_FIELDS if fieldname != "receipt_status"),
			"receipt_checksum",
		),
		_POINTER_DOCTYPE: (*_POINTER_INTEGRITY_FIELDS, "pointer_checksum"),
	}[doc.doctype]
	return _digest(
		{
			"doctype": doc.doctype,
			"name": doc.name,
			"payload": _integrity_payload(doc, fields),
		}
	)


def _quarantine_indicator_artifact(
	doc,
	*,
	fingerprint: str,
	reason_code: str,
) -> str:
	if doc.doctype == _RESULT_DOCTYPE and _has_field(_RESULT_DOCTYPE, "lineage_status"):
		frappe.db.set_value(
			_RESULT_DOCTYPE,
			doc.name,
			{
				"lineage_status": "Legacy Quarantined",
				"legacy_quarantine_reason": reason_code,
			},
			update_modified=False,
		)
	elif doc.doctype == _CALCULATION_DOCTYPE and _has_field(
		_CALCULATION_DOCTYPE,
		"receipt_status",
	):
		frappe.db.set_value(
			_CALCULATION_DOCTYPE,
			doc.name,
			"receipt_status",
			"Blocked",
			update_modified=False,
		)
	key = _digest(
		{
			"target_doctype": doc.doctype,
			"target_fingerprint": fingerprint,
			"target_name": doc.name,
		}
	)
	existing = frappe.db.get_value(
		_QUARANTINE_RECEIPT_DOCTYPE,
		{"quarantine_receipt_key": key},
		"name",
	)
	if existing:
		verify_indicator_quarantine_receipt(frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, existing))
		return str(existing)
	from ione_qms.services.migration_state import MIGRATION_KEY

	values = {
		"quarantine_receipt_key": key,
		"target_doctype": doc.doctype,
		"target_name": doc.name,
		"target_fingerprint": fingerprint,
		"reason_code": reason_code,
		"detected_at": now_datetime(),
		"migration_key": MIGRATION_KEY,
	}
	values["quarantine_receipt_checksum"] = _quarantine_receipt_checksum(values)
	receipt = frappe.get_doc({"doctype": _QUARANTINE_RECEIPT_DOCTYPE, **values})
	receipt.flags.ione_quarantine_migration = True
	receipt.insert(ignore_permissions=True)
	return str(receipt.name)


def _quarantine_receipt_checksum(source) -> str:
	return _digest(
		{
			fieldname: source.get(fieldname)
			for fieldname in (
				"quarantine_receipt_key",
				"target_doctype",
				"target_name",
				"target_fingerprint",
				"reason_code",
				"detected_at",
				"migration_key",
			)
		}
	)


def _approved_indicator_disposition_exists(
	doctype: str,
	name: str,
	fingerprint: str,
) -> bool:
	receipts = frappe.get_all(
		_QUARANTINE_RECEIPT_DOCTYPE,
		filters={
			"target_doctype": doctype,
			"target_name": name,
			"target_fingerprint": fingerprint,
		},
		pluck="name",
		limit_page_length=2,
	)
	for receipt_name in receipts:
		receipt = frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, receipt_name)
		verify_indicator_quarantine_receipt(receipt)
		disposition = _approved_disposition_for_receipt(receipt.name)
		if disposition:
			_validate_applied_indicator_disposition(disposition, receipt)
			return True
	return False


def _exact_indicator_quarantine_receipt_exists(
	doctype: str,
	name: str,
	fingerprint: str,
) -> bool:
	receipts = frappe.get_all(
		_QUARANTINE_RECEIPT_DOCTYPE,
		filters={
			"target_doctype": doctype,
			"target_name": name,
			"target_fingerprint": fingerprint,
		},
		pluck="name",
		order_by="name asc",
		limit_page_length=2,
	)
	if len(receipts) > 1:
		raise ValueError("Indicator artifact has conflicting exact quarantine receipts.")
	if not receipts:
		return False
	receipt = frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, receipts[0])
	verify_indicator_quarantine_receipt(receipt)
	return (
		str(receipt.get("target_doctype") or "") == str(doctype)
		and str(receipt.get("target_name") or "") == str(name)
		and hmac.compare_digest(
			str(receipt.get("target_fingerprint") or ""),
			str(fingerprint),
		)
	)


def indicator_artifact_is_governed_excluded(doc) -> bool:
	"""Return whether an exact bad artifact has a verified terminal exclusion receipt."""
	if doc.doctype not in _INDICATOR_AUDIT_DOCTYPES or not _indicator_quarantine_receipts_ready():
		return False
	fingerprint = _indicator_artifact_fingerprint(doc)
	receipts = frappe.get_all(
		_QUARANTINE_RECEIPT_DOCTYPE,
		filters={
			"target_doctype": doc.doctype,
			"target_name": doc.name,
			"target_fingerprint": fingerprint,
		},
		pluck="name",
		order_by="name asc",
		limit_page_length=2,
	)
	if len(receipts) > 1:
		raise ValueError("Indicator artifact has conflicting exact quarantine receipts.")
	if not receipts:
		return False
	receipt = frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, receipts[0])
	verify_indicator_quarantine_receipt(receipt)
	disposition = _approved_disposition_for_receipt(receipt.name)
	if not disposition:
		return False
	_validate_applied_indicator_disposition(disposition, receipt)
	return str(disposition.get("action") or "") in {"Exclude Legacy", "Replace"}


def _unresolved_indicator_quarantine_count() -> int:
	receipt_names = frappe.get_all(
		_QUARANTINE_RECEIPT_DOCTYPE,
		pluck="name",
		order_by="name asc",
		limit_page_length=10_001,
	)
	if len(receipt_names) > 10_000:
		raise ValueError("Indicator quarantine receipts exceed the governed disposition audit bound.")
	unresolved = 0
	for receipt_name in receipt_names:
		receipt = frappe.get_doc(_QUARANTINE_RECEIPT_DOCTYPE, receipt_name)
		verify_indicator_quarantine_receipt(receipt)
		disposition = _approved_disposition_for_receipt(receipt.name)
		if not disposition:
			unresolved += 1
		else:
			_validate_applied_indicator_disposition(disposition, receipt)
	return unresolved


def _approved_disposition_for_receipt(receipt_name: str):
	names = frappe.get_all(
		_QUARANTINE_DISPOSITION_DOCTYPE,
		filters={"quarantine_receipt": receipt_name, "status": "Approved"},
		pluck="name",
		order_by="name asc",
		limit_page_length=2,
	)
	if len(names) > 1:
		raise ValueError("Indicator quarantine receipt has conflicting approved dispositions.")
	if not names:
		return None
	disposition = frappe.get_doc(_QUARANTINE_DISPOSITION_DOCTYPE, names[0])
	verify_indicator_quarantine_disposition(disposition)
	if str(disposition.get("quarantine_receipt") or "") != str(receipt_name):
		raise ValueError("Indicator quarantine disposition receipt identity is inconsistent.")
	return disposition


def _require_independent_quarantine_reviewer() -> str:
	user = str(frappe.session.user or "")
	if user in {"", "Administrator", "Guest"}:
		raise frappe.PermissionError("A named human reviewer is required.")
	if not _QUARANTINE_REVIEW_ROLES.intersection(frappe.get_roles(user)):
		raise frappe.PermissionError("Indicator quarantine review role is required.")
	return user


def _validate_approved_quarantine_disposition(doc, receipt) -> None:
	target_doctype = str(receipt.get("target_doctype") or "")
	target_name = str(receipt.get("target_name") or "")
	_assert_quarantine_target_fingerprint(receipt)
	action = str(doc.get("action") or "")
	if target_doctype == _POINTER_DOCTYPE and action != "Replace":
		raise ValueError("A quarantined current pointer must be repaired by a verified replacement.")
	if target_doctype == _DETAIL_DOCTYPE and action != "Exclude Legacy":
		raise ValueError("A quarantined result detail may only receive an exact legacy exclusion.")
	replacement = None
	replacement_series = ""
	if action == "Replace":
		replacement_name = str(doc.get("replacement_result") or "")
		if not replacement_name:
			raise ValueError("Approved replacement disposition requires a result.")
		replacement = _verified_replacement_result(receipt, replacement_name)
		if target_doctype == _RESULT_DOCTYPE and target_name == replacement.name:
			raise ValueError("A quarantined result cannot replace itself.")
		replacement_series = str(replacement.get("series_key") or "")
	target_series = _quarantined_target_series(target_doctype, target_name)
	if replacement_series and target_series and replacement_series != target_series:
		raise ValueError("A replacement result must belong to the quarantined artifact series.")
	if replacement and target_doctype in {_POINTER_DOCTYPE, _RESULT_DOCTYPE}:
		current_target = (
			target_doctype == _POINTER_DOCTYPE
			or str(
				frappe.db.get_value(
					_POINTER_DOCTYPE,
					{"series_key": target_series},
					"current_result",
				)
				or ""
			)
			== target_name
		)
		if current_target:
			_repair_quarantined_current_pointer(
				receipt,
				replacement,
				target_series=target_series,
			)
	if target_doctype == _RESULT_DOCTYPE:
		series_key = frappe.db.get_value(_RESULT_DOCTYPE, target_name, "series_key")
		if series_key:
			current = frappe.db.get_value(
				_POINTER_DOCTYPE,
				{"series_key": series_key},
				"current_result",
			)
			if str(current or "") == target_name:
				raise ValueError("A current result cannot be excluded; switch to a verified replacement.")


def _verified_replacement_result(receipt, replacement_name: str):
	replacement = frappe.get_doc(_RESULT_DOCTYPE, replacement_name)
	verify_indicator_result_integrity(replacement)
	if str(receipt.get("target_doctype") or "") == _RESULT_DOCTYPE and str(
		receipt.get("target_name") or ""
	) == str(replacement.name):
		raise ValueError("A quarantined result cannot replace itself.")
	target_series = _quarantined_target_series(
		str(receipt.get("target_doctype") or ""),
		str(receipt.get("target_name") or ""),
	)
	if target_series and str(replacement.get("series_key") or "") != target_series:
		raise ValueError("A replacement result must belong to the quarantined artifact series.")
	if indicator_artifact_is_governed_excluded(replacement):
		raise ValueError("A governed-excluded result cannot be used as a replacement.")
	return replacement


def _repair_quarantined_current_pointer(receipt, replacement, *, target_series: str) -> None:
	series_key = _validate_digest_value(replacement.get("series_key"), "series_key")
	if target_series and target_series != series_key:
		raise ValueError("A replacement result must belong to the quarantined artifact series.")
	target_doctype = str(receipt.get("target_doctype") or "")
	target_name = str(receipt.get("target_name") or "")
	with frappe.db.advisory_lock(f"ione-qms:indicator-series:{series_key}", timeout=5):
		if target_doctype == _POINTER_DOCTYPE:
			pointer_name = target_name
		else:
			pointer_name = str(
				frappe.db.get_value(_POINTER_DOCTYPE, {"series_key": series_key}, "name") or ""
			)
		if not pointer_name:
			raise ValueError("Quarantined series has no pointer available for governed replacement.")
		pointer = _lock_result_pointer_for_repair(pointer_name)
		if target_doctype == _POINTER_DOCTYPE:
			fingerprint = _indicator_artifact_fingerprint(pointer)
			if not hmac.compare_digest(
				fingerprint,
				_validate_digest_value(receipt.get("target_fingerprint"), "target_fingerprint"),
			):
				raise ValueError("Quarantined pointer changed after its exact repair receipt was issued.")
		if target_doctype == _POINTER_DOCTYPE and str(pointer.name) != series_key:
			raise ValueError("Quarantined pointer identity and replacement result series do not match.")
		if target_doctype != _POINTER_DOCTYPE and str(pointer.get("series_key") or "") != series_key:
			raise ValueError("Quarantined pointer and replacement result series do not match.")
		rows = _verify_series_revision_continuity_for_repair(series_key)
		if int(replacement.get("result_revision") or 0) != len(rows) or str(
			rows[-1].get("name") or ""
		) != str(replacement.name):
			raise ValueError("Pointer repair replacement must be the terminal contiguous revision.")
		values = _pointer_values(replacement)
		pointer.update(_supported_values(_POINTER_DOCTYPE, values))
		pointer.flags.ione_pointer_switch = True
		pointer.flags.ignore_permissions = True
		pointer.save()
		verify_indicator_result_pointer(pointer)
		_verify_series_revision_continuity(series_key, pointer=pointer)
		_project_current_indicator_result(pointer, replacement)


def _pointer_values(result_doc) -> dict[str, Any]:
	values = {
		"series_key": result_doc.get("series_key"),
		"indicator": result_doc.get("indicator"),
		"indicator_version": result_doc.get("indicator_version"),
		"period_start": result_doc.get("period_start"),
		"period_end": result_doc.get("period_end"),
		"dimension_hash": result_doc.get("dimension_hash"),
		"dimension_json": result_doc.get("dimension_json"),
		**_dimension_projection_values(
			json.loads(result_doc.get("dimension_json") or "{}"),
		),
		"current_result": result_doc.name,
		"current_revision": int(result_doc.get("result_revision") or 0),
		"current_input_receipt_hash": result_doc.get("input_receipt_hash"),
		"current_result_checksum": result_doc.get("result_checksum"),
		"switched_at": now_datetime(),
	}
	values["pointer_checksum"] = _pointer_checksum(values)
	return values


def _verify_series_revision_continuity_for_repair(series_key: str) -> list[Any]:
	"""Validate 1..N without trusting the corrupt pointer that is about to be replaced."""
	series_key = _validate_digest_value(series_key, "series_key")
	rows = frappe.get_all(
		_RESULT_DOCTYPE,
		filters={"series_key": series_key},
		fields=["name", "result_revision", "series_revision_key", "result_key"],
		order_by="result_revision asc, name asc",
		limit_page_length=10_001,
	)
	if not rows or len(rows) > 10_000:
		raise ValueError("Pointer repair requires a bounded non-empty result revision series.")
	for expected_revision, row in enumerate(rows, start=1):
		if int(row.get("result_revision") or 0) != expected_revision:
			raise ValueError("Indicator result revisions must be exactly contiguous from 1 through N.")
		expected_key = _series_revision_key(series_key, expected_revision)
		if not all(
			hmac.compare_digest(
				expected_key,
				_validate_digest_value(row.get(fieldname), fieldname),
			)
			for fieldname in ("series_revision_key", "result_key")
		):
			raise ValueError("Indicator result revision sequence contains an invalid series identity.")
	return rows


def _validate_applied_indicator_disposition(doc, receipt) -> None:
	action = str(doc.get("action") or "")
	target_doctype = str(receipt.get("target_doctype") or "")
	target_name = str(receipt.get("target_name") or "")
	if target_doctype != _POINTER_DOCTYPE:
		_assert_quarantine_target_fingerprint(receipt)
	if target_doctype == _DETAIL_DOCTYPE:
		if action != "Exclude Legacy":
			raise ValueError("Result-detail quarantine is not governed by replacement semantics.")
		return
	if target_doctype == _POINTER_DOCTYPE and action != "Replace":
		raise ValueError("Current pointers cannot be excluded from governed reads.")
	if action == "Exclude Legacy":
		if target_doctype == _RESULT_DOCTYPE:
			series_key = _quarantined_target_series(target_doctype, target_name)
			current = frappe.db.get_value(
				_POINTER_DOCTYPE,
				{"series_key": series_key},
				"current_result",
			)
			if str(current or "") == target_name:
				raise ValueError("Approved exclusion still targets the current indicator result.")
		return
	replacement = _verified_replacement_result(receipt, str(doc.get("replacement_result") or ""))
	series_key = str(replacement.get("series_key") or "")
	pointer_name = frappe.db.get_value(_POINTER_DOCTYPE, {"series_key": series_key}, "name")
	if not pointer_name:
		raise ValueError("Approved replacement has no materialized authoritative pointer.")
	pointer = frappe.get_doc(_POINTER_DOCTYPE, pointer_name)
	verify_indicator_result_pointer(pointer)
	if (
		int(pointer.get("current_revision") or 0) < int(replacement.get("result_revision") or 0)
		or str(pointer.get("current_result") or "") == target_name
	):
		raise ValueError("Approved replacement is not materialized in the authoritative pointer series.")
	_verify_series_revision_continuity(series_key, pointer=pointer)


def _assert_quarantine_target_fingerprint(receipt) -> None:
	target_doctype = str(receipt.get("target_doctype") or "")
	target_name = str(receipt.get("target_name") or "")
	target = frappe.get_doc(target_doctype, target_name)
	if not hmac.compare_digest(
		_indicator_artifact_fingerprint(target),
		_validate_digest_value(receipt.get("target_fingerprint"), "target_fingerprint"),
	):
		raise ValueError("Quarantined indicator artifact changed after its exact receipt was issued.")


def _quarantined_target_series(doctype: str, name: str) -> str:
	if doctype == _POINTER_DOCTYPE:
		# Pointer autoname is its immutable series identity even when a corrupt field is
		# the reason the pointer itself requires governed repair.
		if len(str(name)) == _SHA256_LENGTH and all(
			character in "0123456789abcdef" for character in str(name)
		):
			return str(name)
		return str(frappe.db.get_value(doctype, name, "series_key") or "")
	if doctype in {_RESULT_DOCTYPE, _CALCULATION_DOCTYPE}:
		return str(frappe.db.get_value(doctype, name, "series_key") or "")
	if doctype == _DETAIL_DOCTYPE:
		result_name = frappe.db.get_value(doctype, name, "indicator_result")
		return str(frappe.db.get_value(_RESULT_DOCTYPE, result_name, "series_key") or "")
	return ""


def _disposition_checksum(source) -> str:
	return _digest(
		{
			fieldname: source.get(fieldname)
			for fieldname in (
				"disposition_key",
				"quarantine_receipt",
				"action",
				"replacement_result",
				"request_reason",
				"requested_by",
				"requested_at",
				"status",
				"reviewed_by",
				"reviewed_at",
				"review_comment",
			)
		}
	)


def _disposition_outcome(doc) -> dict[str, Any]:
	return {
		"disposition": doc.name,
		"status": doc.get("status"),
		"action": doc.get("action"),
		"replacement_result": doc.get("replacement_result"),
		"disposition_checksum": doc.get("disposition_checksum"),
	}


def _first(*sources, fields: tuple[str, ...]) -> Any:
	for source in sources:
		for fieldname in fields:
			value = source.get(fieldname)
			if value not in (None, ""):
				return value
	return None


def _nonempty_receipt_text(value: Any, label: str) -> str:
	normalized = str(value or "").strip()
	if not normalized:
		raise ValueError(f"Indicator input receipt requires {label}.")
	return normalized


def _status_candidates(status: str) -> tuple[str, ...]:
	return {
		"No Data": ("No Data", "Insufficient Data", "Calculated"),
		"Critical": ("Critical", "Alert", "Breached", "Calculated"),
		"Warning": ("Warning", "Alert", "Breached", "Calculated"),
		"Met": ("Met", "On Target", "Calculated"),
		"Below Target": ("Below Target", "Warning", "Breached", "Calculated"),
	}.get(status, ("Calculated", status))


def _select_value(doctype: str, fieldname: str, candidates: tuple[str, ...]) -> str:
	meta = frappe.get_meta(doctype)
	field = meta.get_field(fieldname)
	if not field or field.fieldtype != "Select" or not field.options:
		return candidates[0]
	options = {option.strip() for option in str(field.options).splitlines() if option.strip()}
	for candidate in candidates:
		if candidate in options:
			return candidate
	return candidates[0]


def _supported_values(doctype: str, values: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	return {fieldname: value for fieldname, value in values.items() if meta.has_field(fieldname)}


def _has_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False


def _threshold_breached(value: Decimal, threshold: Decimal, lower_is_better: bool) -> bool:
	return value >= threshold if lower_is_better else value <= threshold


def _optional_decimal(value: Any) -> Decimal | None:
	return None if value in (None, "") else Decimal(str(value))


def _is_breach(status: str) -> bool:
	return status.strip().lower() in {"critical", "warning", "alert", "breached", "below target"}


def _json_number(value: Decimal | None) -> float | None:
	return float(value) if value is not None else None


def _digest(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)

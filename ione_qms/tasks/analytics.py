from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import frappe
from frappe.exceptions import QueryTimeoutError
from frappe.utils import (
	add_days,
	get_datetime,
	get_first_day,
	get_last_day,
	getdate,
	now_datetime,
	nowdate,
)

from ione_qms.indicator_engine import safe_rate
from ione_qms.services import batch_reliability as batches
from ione_qms.services.indicators import (
	result_status,
	verify_indicator_result_integrity,
)
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

_MAX_BATCH_SIZE = 1_000
_MAX_CLEANUP_BATCH_SIZE = 500
_MAX_CONTINUATION_BATCHES = 10_000


@dataclass(frozen=True)
class _FactSource:
	source_doctype: str
	target_doctype: str
	date_fields: tuple[str, ...]
	builder: str


_SOURCES = (
	_FactSource(
		"IONE Indicator Result",
		"IONE Monthly Quality Fact",
		("period_end", "computed_at", "creation"),
		"indicator",
	),
	_FactSource(
		"IONE QC Finding",
		"IONE Finding Analysis Fact",
		("detected_at", "creation"),
		"finding",
	),
	_FactSource(
		"IONE Surgery QC",
		"IONE Surgery Quality Fact",
		("surgery_time", "creation"),
		"surgery",
	),
	_FactSource(
		"IONE AI Analysis Task",
		"IONE Agent Analysis Fact",
		("completed_at", "creation"),
		"agent",
	),
)


def build_monthly_facts(
	month: str | None = None,
	batch_size: int = 500,
	source_index: int = 0,
	cursor: str | None = None,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
) -> dict[str, int]:
	"""Dispatch durable monthly projection runs for every governed fact source."""
	require_post_migrate_runtime_ready("build monthly analytics facts")
	if cursor:
		raise RuntimeError(
			"Legacy name-only analytics cursors are not authoritative; daily "
			"reconciliation will restart with a frozen (creation, name) high-water mark."
		)
	period_start, period_end = _month_bounds(month)
	limit = min(max(int(batch_size or 500), 1), _MAX_BATCH_SIZE)
	index = min(max(int(source_index or 0), 0), len(_SOURCES))
	remaining_batches = int(remaining_batches)
	if not 0 <= remaining_batches <= _MAX_CONTINUATION_BATCHES:
		raise ValueError("Analytics continuation budget is outside its governed bound.")
	summary = {"read": 0, "upserted": 0, "failed": 0, "dispatched": 0}
	while index < len(_SOURCES) and summary["dispatched"] < limit:
		run = _ensure_analytics_run(index, period_start, period_end)
		if run is not None and str(run.get("status") or "") not in {
			"Completed",
			"Dead Letter",
			"Superseded",
		}:
			batches.enqueue_run(run.name, enqueue_after_commit=True)
			summary["dispatched"] += 1
		index += 1
	if index < len(_SOURCES):
		if remaining_batches <= 0:
			raise RuntimeError("Analytics dispatcher continuation budget was exhausted.")
		_enqueue_dispatch(period_start, limit, index, remaining_batches - 1)
	return summary


def reconcile_monthly_facts(
	as_of: str | None = None,
	months_back: int = 13,
	month_offset: int = 0,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
) -> dict[str, int]:
	"""Daily high-water reconciliation for late inserts, updates, and deletions."""
	require_post_migrate_runtime_ready("reconcile monthly analytics facts")
	months_back = min(max(int(months_back or 13), 1), 36)
	month_offset = max(int(month_offset or 0), 0)
	remaining_batches = int(remaining_batches)
	if not 0 <= remaining_batches <= _MAX_CONTINUATION_BATCHES:
		raise ValueError("Analytics reconciliation budget is outside its governed bound.")
	anchor = getdate(as_of or add_days(get_first_day(nowdate()), -1))
	summary = {"months_scanned": 0, "sources_scanned": 0, "dispatched": 0}
	if month_offset >= months_back:
		return summary
	month_anchor = _add_months(get_first_day(anchor), -month_offset)
	period_start = getdate(get_first_day(month_anchor))
	period_end = getdate(get_last_day(month_anchor))
	for index in range(len(_SOURCES)):
		summary["sources_scanned"] += 1
		run = _ensure_analytics_run(index, period_start, period_end)
		if run is not None and str(run.get("status") or "") not in {
			"Completed",
			"Dead Letter",
			"Superseded",
		}:
			batches.enqueue_run(run.name, enqueue_after_commit=True)
			summary["dispatched"] += 1
	summary["months_scanned"] = 1
	if month_offset + 1 < months_back:
		if remaining_batches <= 0:
			raise RuntimeError("Analytics reconciliation continuation budget was exhausted.")
		_enqueue_reconciliation(
			anchor,
			months_back,
			month_offset + 1,
			remaining_batches - 1,
		)
	return summary


def process_analytics_batch_run(run_name: str) -> dict[str, int]:
	"""Project one bounded page; any builder failure rolls back the whole page."""
	require_post_migrate_runtime_ready("process monthly analytics batch")
	summary = {"read": 0, "upserted": 0, "failed": 0}
	try:
		with frappe.db.advisory_lock(f"ione-qms:batch-run:{run_name}", timeout=0):
			activated = batches.activate_run(run_name)
			if activated is None:
				return summary
			run, lease_token = activated
			index = int(run.get("source_index") or 0)
			if not 0 <= index < len(_SOURCES):
				raise ValueError("Analytics batch source index is invalid.")
			source = _SOURCES[index]
			period_start = getdate(run.get("period_start"))
			period_end = getdate(run.get("period_end"))
			date_field = _first_existing_field(source.source_doctype, source.date_fields)
			if not date_field:
				raise RuntimeError("Analytics source lacks a governed date field.")
			# Source documents acquire this row lock before every mutation.  Keeping
			# it through one bounded projection page gives each page a serializable
			# source boundary; changes between pages are detected by the frozen
			# high-water contract before completion.
			batches.lock_source_epochs((source.source_doctype,))
			if str(run.get("phase") or "") == "Cleaning":
				_clean_analytics_fact_page(
					run,
					lease_token,
					source,
					date_field,
					period_start,
					period_end,
				)
				return summary
			cursor_state = _decode_analytics_cursor(
				run.get("cursor_json"),
				expected_high_water=run.get("high_water_json"),
			)
			rows = _source_rows(
				source.source_doctype,
				date_field,
				period_start,
				period_end,
				cursor_state["position"],
				cursor_state["high_water"],
				_MAX_BATCH_SIZE + 1,
			)
			page_rows = rows[:_MAX_BATCH_SIZE]
			savepoint = f"ione_analytics_{run.name[:24]}"
			frappe.db.savepoint(savepoint)
			seen_fact_keys: set[str] = set()
			source_sequence = int(run.get("manifest_record_count") or 0)
			for offset, row in enumerate(page_rows, start=1):
				summary["read"] += 1
				try:
					fact_key = _build_fact(
						source,
						row,
						period_start,
						period_end,
						seen_fact_keys,
					)
				except Exception as exc:
					frappe.db.rollback(save_point=savepoint)
					batches.schedule_retry(run.name, exc)
					frappe.db.commit()
					summary["failed"] += 1
					_log_failure(source, str(row.get("name") or ""), type(exc).__name__)
					return summary
				if fact_key:
					seen_fact_keys.add(fact_key)
					batches.record_completed_work_item(
						run.name,
						payload={"fact_key": fact_key},
						sequence=source_sequence + offset,
						result_reference=fact_key,
					)
					summary["upserted"] += 1
			work_count = frappe.db.count(
				batches.WORK_DOCTYPE,
				{"batch_run": run.name, "status": "Succeeded"},
			)
			page_values: dict[str, Any] = {
				"heartbeat_at": now_datetime(),
				"manifest_record_count": source_sequence + len(page_rows),
				"work_completed": work_count,
				"work_total": work_count,
			}
			if len(rows) > _MAX_BATCH_SIZE:
				position = _source_cursor_value(source, page_rows[-1])
				page_values["cursor_json"] = _encode_analytics_cursor(
					position,
					cursor_state["high_water"],
				)
				batches.set_run_values(run.name, page_values)
				frappe.db.release_savepoint(savepoint)
				frappe.db.commit()
				batches.enqueue_run(run.name, enqueue_after_commit=False)
				frappe.db.commit()
				return summary
			current_high_water = _analytics_high_water(
				source,
				date_field,
				period_start,
				period_end,
			)
			if current_high_water != cursor_state["high_water"]:
				frappe.db.rollback(save_point=savepoint)
				_supersede_analytics_run(run, lease_token)
				return summary
			page_values.update({"cursor_json": "", "phase": "Cleaning"})
			batches.set_run_values(run.name, page_values)
			frappe.db.release_savepoint(savepoint)
			frappe.db.commit()
			batches.enqueue_run(run.name, enqueue_after_commit=False)
			frappe.db.commit()
			return summary
	except QueryTimeoutError:
		frappe.db.rollback()
		return summary
	except Exception as exc:
		frappe.db.rollback()
		batches.schedule_retry(run_name, exc)
		frappe.db.commit()
		summary["failed"] += 1
		return summary


def _clean_analytics_fact_page(
	run,
	lease_token: str,
	source: _FactSource,
	date_field: str,
	period_start,
	period_end,
) -> None:
	"""Remove stale derived rows in bounded pages under the source barrier."""
	current_high_water = _analytics_high_water(
		source,
		date_field,
		period_start,
		period_end,
	)
	try:
		frozen_high_water = json.loads(str(run.get("high_water_json") or ""))
	except ValueError as exc:
		raise ValueError("Analytics frozen high-water contract is invalid.") from exc
	if current_high_water != frozen_high_water:
		_supersede_analytics_run(run, lease_token)
		return
	cursor = str(run.get("cursor_json") or "")
	rows = _analytics_fact_cleanup_rows(
		run.name,
		source,
		period_start,
		period_end,
		cursor,
		_MAX_CLEANUP_BATCH_SIZE + 1,
	)
	page_rows = rows[:_MAX_CLEANUP_BATCH_SIZE]
	for row in page_rows:
		if not row.get("receipt_name"):
			frappe.delete_doc(
				source.target_doctype,
				str(row.get("name") or ""),
				ignore_permissions=True,
			)
	if len(rows) > _MAX_CLEANUP_BATCH_SIZE:
		next_cursor = str(page_rows[-1].get("name") or "")
		if not next_cursor:
			raise RuntimeError("Analytics cleanup page omitted its stable cursor.")
		batches.set_run_values(
			run.name,
			{
				"cursor_json": next_cursor,
				"heartbeat_at": now_datetime(),
			},
		)
		frappe.db.commit()
		batches.enqueue_run(run.name, enqueue_after_commit=False)
		frappe.db.commit()
		return
	batches.complete_run(run.name, lease_token)
	frappe.db.commit()


def _analytics_fact_cleanup_rows(
	run_name: str,
	source: _FactSource,
	period_start,
	period_end,
	cursor: str,
	limit: int,
) -> list[Any]:
	"""Return target facts and their optional receipt from this exact run."""
	if source not in _SOURCES:
		raise ValueError("Analytics cleanup source is not governed.")
	target = source.target_doctype
	if source.builder == "indicator":
		scope_sql = "fact.month = %s"
		scope_params: tuple[Any, ...] = (period_start,)
	else:
		scope_sql = "fact.fact_date >= %s and fact.fact_date < %s"
		scope_params = (period_start, add_days(period_end, 1))
	return frappe.db.sql(
		f"""
		select fact.name, fact.fact_key, work.name as receipt_name
		from `tab{target}` fact
		left join `tabIONE Batch Work Item` work
			on work.batch_run = %s
			and work.status = 'Succeeded'
			and work.result_reference = fact.fact_key
		where {scope_sql}
			and fact.name > %s
		order by fact.name asc
		limit %s
		""",  # noqa: S608
		(
			run_name,
			*scope_params,
			cursor,
			min(max(int(limit or 1), 1), _MAX_CLEANUP_BATCH_SIZE + 1),
		),
		as_dict=True,
	)


def _supersede_analytics_run(run, lease_token: str) -> None:
	batches.supersede_run(run.name, lease_token, "ANALYTICS_SOURCE_DRIFT")
	frappe.db.commit()
	_dispatch_replacement_analytics_run(run)


def _build_fact(
	source: _FactSource,
	row,
	period_start,
	period_end,
	seen_fact_keys: set[str],
) -> str | None:
	if source.builder == "indicator":
		return _build_indicator_fact(row, period_start, period_end, seen_fact_keys)
	if source.builder == "finding":
		return _build_finding_fact(row)
	if source.builder == "surgery":
		return _build_surgery_fact(row)
	if source.builder == "agent":
		return _build_agent_fact(row)
	raise ValueError(f"Unknown analytics fact builder: {source.builder}")


def _build_indicator_fact(row, period_start, period_end, seen_fact_keys: set[str]) -> str | None:
	doctype = "IONE Monthly Quality Fact"
	group = {
		"indicator": row.get("indicator"),
		"indicator_version": row.get("indicator_version"),
		"hospital": row.get("hospital"),
		"campus": row.get("campus"),
		"department": row.get("department"),
		"ward": row.get("ward"),
		"medical_staff": row.get("medical_staff"),
		"medical_group": row.get("medical_group"),
		"physician": row.get("physician"),
		"disease": row.get("disease"),
		"surgery": row.get("surgery"),
		"drg": row.get("drg"),
		"dip": row.get("dip"),
		"dimension_hash": row.get("dimension_hash"),
	}
	fact_key = _digest({"month": period_start, **group})
	if fact_key in seen_fact_keys:
		return None
	with _current_indicator_aggregate(group, period_start, period_end) as aggregate:
		numerator = Decimal(str(aggregate.get("numerator") or 0))
		denominator = Decimal(str(aggregate.get("denominator") or 0))
		version, formula = _indicator_semantics(row.get("indicator_version"))
		indicator = (
			frappe.get_cached_doc("IONE QC Indicator", row.get("indicator")) if row.get("indicator") else {}
		)
		indicator_value = (
			numerator
			if str(formula.get("measure") or "").strip().lower() == "count"
			else safe_rate(
				numerator,
				denominator,
				multiplier=_first_value(formula.get("multiplier"), version.get("multiplier"), 100),
				precision=int(_first_value(formula.get("precision"), version.get("precision"), 2)),
			)
		)
		target_value = _first_value(
			aggregate.get("target_value"),
			version.get("target_value"),
			indicator.get("target_value"),
		)
		target_min = _first_value(
			aggregate.get("target_min"),
			version.get("target_min"),
			indicator.get("target_min"),
		)
		target_max = _first_value(
			aggregate.get("target_max"),
			version.get("target_max"),
			indicator.get("target_max"),
		)
		direction = _first_value(version.get("direction"), indicator.get("direction"))
		semantic_status = result_status(
			indicator_value,
			direction=direction,
			target=target_value,
			target_min=target_min,
			target_max=target_max,
			warning=_first_value(version.get("warning_threshold"), indicator.get("warning_threshold")),
			critical=_first_value(version.get("critical_threshold"), indicator.get("critical_threshold")),
		)
		values = {
			"fact_key": fact_key,
			"month": period_start,
			**group,
			"dimension_json": row.get("dimension_json"),
			"numerator": numerator,
			"denominator": denominator,
			"indicator_value": indicator_value,
			"target_value": target_value,
			"target_min": target_min,
			"target_max": target_max,
			"status": semantic_status,
			"result_count": int(aggregate.get("result_count") or 0),
			"source_revision_manifest_hash": aggregate.get("source_revision_manifest_hash"),
			"source_revision_count": int(aggregate.get("source_revision_count") or 0),
			"lineage_json": json.dumps(
				{
					"projection": "IONE Monthly Quality Fact",
					"source_revision_manifest_hash": aggregate.get("source_revision_manifest_hash"),
					"source_revision_count": int(aggregate.get("source_revision_count") or 0),
				},
				ensure_ascii=False,
				sort_keys=True,
				separators=(",", ":"),
			),
			"source_modified": aggregate.get("source_modified"),
		}
		# The caller holds the Indicator Result source-epoch lock through this
		# aggregate verification and the replacement of the prior fact.
		_upsert_fact(
			doctype,
			fact_key,
			values,
			{"indicator_version": row.get("indicator_version"), "month": period_start},
		)
	return fact_key


def _build_finding_fact(row) -> str:
	doctype = "IONE Finding Analysis Fact"
	fact_key = _digest({"finding": row.name})
	values = {
		"fact_key": fact_key,
		"finding": row.name,
		"fact_date": row.get("detected_at") or row.get("creation"),
		"hospital": row.get("hospital"),
		"campus": row.get("campus"),
		"department": row.get("department"),
		"ward": row.get("ward"),
		"rule": row.get("rule"),
		"rule_version": row.get("rule_version"),
		"standard": row.get("standard"),
		"severity": row.get("severity"),
		"finding_status": row.get("status"),
		"ai_origin": row.get("ai_origin"),
		"responsible_staff": row.get("responsible_staff"),
		"source_modified": row.get("modified"),
	}
	_upsert_fact(doctype, fact_key, values, {"finding": row.name})
	return fact_key


def _build_surgery_fact(row) -> str:
	doctype = "IONE Surgery Quality Fact"
	fact_key = _digest({"surgery_qc": row.name})
	values = {
		"fact_key": fact_key,
		"surgery_qc": row.name,
		"fact_date": row.get("surgery_time") or row.get("creation"),
		"hospital": row.get("hospital"),
		"campus": row.get("campus"),
		"department": row.get("department"),
		"ward": row.get("ward"),
		"encounter": row.get("encounter"),
		"finding": row.get("finding"),
		"surgery_type": row.get("surgery_type"),
		"surgery_level": row.get("surgery_level"),
		"surgeon": row.get("surgeon"),
		"risk_level": row.get("risk_level"),
		"surgery_status": row.get("status"),
		"compliant": (
			row.get("compliant") if row.get("compliant") is not None else int(row.get("status") == "Passed")
		),
		"unplanned_return": row.get("unplanned_return"),
		"complication": row.get("complication"),
		"source_modified": row.get("modified"),
	}
	_upsert_fact(doctype, fact_key, values, {"surgery_qc": row.name})
	return fact_key


def _build_agent_fact(row) -> str:
	doctype = "IONE Agent Analysis Fact"
	fact_key = _digest({"analysis_task": row.name})
	run_link = _flow_run_link(row)
	usage = _usage_metrics(run_link.get("usage_json"))
	duration_ms = _duration_ms(
		run_link.get("started_at") or row.get("started_at"),
		run_link.get("completed_at") or row.get("completed_at"),
	)
	task_status = str(row.get("status") or "")
	values = {
		"fact_key": fact_key,
		"analysis_task": row.name,
		"flow_run_link": run_link.get("name"),
		"fact_date": row.get("completed_at") or row.get("creation"),
		"hospital": row.get("hospital"),
		"campus": row.get("campus"),
		"department": row.get("department"),
		"ward": row.get("ward"),
		"policy": run_link.get("policy") or row.get("policy"),
		"flow_agent": run_link.get("flow_agent"),
		"flow_model": run_link.get("flow_model"),
		"model_id": run_link.get("model_id"),
		"flow_run": run_link.get("flow_run") or row.get("flow_run"),
		"task_type": row.get("task_type"),
		"task_status": task_status,
		"requested_by": row.get("requested_by"),
		"requires_human_review": row.get("requires_human_review"),
		"duration_ms": duration_ms,
		"input_tokens": usage["input_tokens"],
		"output_tokens": usage["output_tokens"],
		"total_tokens": usage["total_tokens"],
		"task_count": 1,
		"success_count": int(task_status in {"Completed", "Reviewed"}),
		"failure_count": int(task_status in {"Failed", "Rejected", "Cancelled"}),
		"adopted_count": _adopted_candidate_count(row.name),
		"source_modified": row.get("modified"),
	}
	_upsert_fact(doctype, fact_key, values, {"analysis_task": row.name})
	return fact_key


def _upsert_fact(
	doctype: str,
	fact_key: str,
	values: dict[str, Any],
	fallback_filters: dict[str, Any],
) -> None:
	supported = _supported_values(doctype, values)
	existing = None
	if _has_field(doctype, "fact_key"):
		existing = frappe.db.get_value(doctype, {"fact_key": fact_key}, "name")
	else:
		fallback = _supported_values(doctype, fallback_filters)
		if fallback:
			existing = frappe.db.get_value(doctype, fallback, "name")
	if existing:
		doc = frappe.get_doc(doctype, existing)
		doc.update(supported)
		doc.flags.ignore_permissions = True
		doc.save()
		return
	frappe.get_doc({"doctype": doctype, **supported}).insert(ignore_permissions=True)


@contextmanager
def _current_indicator_aggregate(
	group: dict[str, Any],
	period_start,
	period_end,
) -> Any:
	"""Yield an aggregate from the latest *completed* batch per governed period."""
	clauses = [
		"result.period_end >= %s",
		"result.period_end < %s",
		"result.lineage_status = 'Verified'",
	]
	params: list[Any] = [period_start, add_days(period_end, 1)]
	for fieldname, value in group.items():
		if not _has_field("IONE Indicator Result", fieldname):
			continue
		if value in (None, ""):
			clauses.append(f"coalesce(result.`{fieldname}`, '') = ''")
		else:
			clauses.append(f"result.`{fieldname}` = %s")
			params.append(value)
	where = " and ".join(clauses)
	cursor = ""
	series_keys: list[str] = []
	while True:
		series_query = (
			"select result.series_key, result.name "  # noqa: S608
			"from `tabIONE Indicator Result` result "
			"inner join `tabIONE Batch Work Item` work "
			"on work.result_reference = result.name and work.status = 'Succeeded' "
			"inner join `tabIONE Batch Run` batch "
			"on batch.name = work.batch_run "
			"and batch.batch_type = 'Indicator' and batch.status = 'Completed' "
			"left join `tabIONE Batch Run` newer "
			"on newer.batch_type = batch.batch_type "
			"and newer.subject_key = batch.subject_key "
			"and newer.period_start = batch.period_start "
			"and newer.period_end = batch.period_end "
			"and newer.status = 'Completed' "
			"and newer.recovery_generation > batch.recovery_generation "
			f"where newer.name is null and {where} and result.series_key > %s "
			"order by result.series_key asc limit 1000"
		)
		rows = frappe.db.sql(
			series_query,
			(*params, cursor),
			as_dict=True,
		)
		if not rows:
			break
		for row in rows:
			series_key = str(row.get("series_key") or "")
			if not series_key or (series_keys and series_key <= series_keys[-1]):
				raise ValueError("Monthly indicator series enumeration is not strictly monotonic.")
			series_keys.append(series_key)
			if len(series_keys) > 1_000_000:
				raise ValueError("Monthly indicator revision manifest exceeds 1,000,000 rows.")
		cursor = series_keys[-1]
		if len(rows) < 1_000:
			break

	current_results = []
	for series_key in series_keys:
		rows = frappe.db.sql(
			"""
			select result.name
			from `tabIONE Indicator Result` result
			inner join `tabIONE Batch Work Item` work
				on work.result_reference = result.name and work.status = 'Succeeded'
			inner join `tabIONE Batch Run` batch
				on batch.name = work.batch_run
				and batch.batch_type = 'Indicator' and batch.status = 'Completed'
			left join `tabIONE Batch Run` newer
				on newer.batch_type = batch.batch_type
				and newer.subject_key = batch.subject_key
				and newer.period_start = batch.period_start
				and newer.period_end = batch.period_end
				and newer.status = 'Completed'
				and newer.recovery_generation > batch.recovery_generation
			where newer.name is null and result.series_key = %s
			order by result.result_revision desc, result.name desc
			limit 1
			""",
			(series_key,),
			as_dict=True,
		)
		if len(rows) != 1:
			raise ValueError("Completed indicator batch series did not resolve uniquely.")
		current = frappe.get_doc("IONE Indicator Result", rows[0].get("name"))
		verify_indicator_result_integrity(current)
		if not _indicator_result_matches_group(current, group, period_start, period_end):
			raise ValueError("Completed indicator series no longer matches the monthly aggregate.")
		current_results.append(current)

	manifest = hashlib.sha256()
	numerator = Decimal("0")
	denominator = Decimal("0")
	target_values: list[Decimal] = []
	target_mins: list[Decimal] = []
	target_maxes: list[Decimal] = []
	source_modified = None
	for current in current_results:
		numerator += Decimal(str(current.get("numerator") or 0))
		denominator += Decimal(str(current.get("denominator") or 0))
		_append_decimal(target_values, current.get("target_value"))
		_append_decimal(target_mins, current.get("target_min"))
		_append_decimal(target_maxes, current.get("target_max"))
		modified = current.get("modified")
		if modified is not None and (source_modified is None or modified > source_modified):
			source_modified = modified
		leaf = json.dumps(
			{
				"input_receipt_hash": current.get("input_receipt_hash"),
				"name": current.name,
				"result_checksum": current.get("result_checksum"),
				"result_revision": int(current.get("result_revision") or 0),
				"series_key": current.get("series_key"),
			},
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
		)
		manifest.update(hashlib.sha256(leaf.encode()).hexdigest().encode("ascii"))
		manifest.update(b"\n")

	yield {
		"numerator": numerator,
		"denominator": denominator,
		"target_value": max(target_values) if target_values else None,
		"target_min": max(target_mins) if target_mins else None,
		"target_max": max(target_maxes) if target_maxes else None,
		"result_count": len(current_results),
		"source_modified": source_modified,
		"source_revision_manifest_hash": manifest.hexdigest(),
		"source_revision_count": len(current_results),
	}


def _indicator_result_matches_group(current, group: dict[str, Any], period_start, period_end) -> bool:
	if str(current.get("lineage_status") or "") != "Verified":
		return False
	period_end_value = getdate(current.get("period_end"))
	if period_end_value < getdate(period_start) or period_end_value >= getdate(add_days(period_end, 1)):
		return False
	for fieldname, expected in group.items():
		if not _has_field("IONE Indicator Result", fieldname):
			continue
		actual = current.get(fieldname)
		if expected in (None, ""):
			if actual not in (None, ""):
				return False
		elif str(actual) != str(expected):
			return False
	return True


def _append_decimal(values: list[Decimal], value: Any) -> None:
	if value not in (None, ""):
		values.append(Decimal(str(value)))


def _source_rows(
	doctype: str,
	date_field: str,
	period_start,
	period_end,
	position: dict[str, str],
	high_water: dict[str, Any],
	limit: int,
) -> list[Any]:
	curated = {
		"name",
		"creation",
		"modified",
		"indicator",
		"indicator_version",
		"period_end",
		"computed_at",
		"numerator",
		"denominator",
		"target_value",
		"dimension_json",
		"dimension_hash",
		"detected_at",
		"hospital",
		"campus",
		"department",
		"ward",
		"medical_staff",
		"medical_group",
		"physician",
		"disease",
		"surgery",
		"drg",
		"dip",
		"series_key",
		"result_revision",
		"input_receipt_hash",
		"result_checksum",
		"lineage_status",
		"rule",
		"rule_version",
		"standard",
		"severity",
		"status",
		"ai_origin",
		"responsible_staff",
		"surgery_time",
		"encounter",
		"finding",
		"surgery_type",
		"surgery_level",
		"surgeon",
		"risk_level",
		"compliant",
		"unplanned_return",
		"complication",
		"started_at",
		"completed_at",
		"policy",
		"flow_run",
		"task_type",
		"requested_by",
		"requires_human_review",
	}
	meta = frappe.get_meta(doctype)
	fields = sorted(fieldname for fieldname in curated if fieldname == "name" or meta.has_field(fieldname))
	if not int(high_water.get("count") or 0):
		return []
	high_creation = str(high_water.get("creation") or "")
	high_name = str(high_water.get("name") or "")
	cursor_creation = str(position.get("creation") or "")
	cursor_name = str(position.get("name") or "")
	if not high_creation or not high_name:
		raise ValueError("Analytics source high-water identity is incomplete.")
	if bool(cursor_creation) != bool(cursor_name):
		raise ValueError("Analytics source cursor identity is incomplete.")
	if cursor_creation and (cursor_creation, cursor_name) > (high_creation, high_name):
		raise ValueError("Analytics source cursor exceeds its frozen high-water mark.")
	window_sql = (
		"and (source.creation > %s or (source.creation = %s and source.name > %s)) "
		if cursor_creation
		else ""
	)
	upper_sql = "and (source.creation < %s or (source.creation = %s and source.name <= %s)) "
	window_params: list[Any] = [cursor_creation, cursor_creation, cursor_name] if cursor_creation else []
	window_params.extend([high_creation, high_creation, high_name])
	date_identifier = _sql_identifier(date_field)
	if doctype == "IONE Indicator Result":
		selection = ", ".join(f"source.`{fieldname}`" for fieldname in fields)
		query = (
			f"select {selection}, source.series_key as pointer_series_key "  # noqa: S608
			"from `tabIONE Indicator Result` source "
			"inner join `tabIONE Batch Work Item` work "
			"on work.result_reference = source.name and work.status = 'Succeeded' "
			"inner join `tabIONE Batch Run` batch "
			"on batch.name = work.batch_run "
			"and batch.batch_type = 'Indicator' and batch.status = 'Completed' "
			"left join `tabIONE Batch Run` newer "
			"on newer.batch_type = batch.batch_type "
			"and newer.subject_key = batch.subject_key "
			"and newer.period_start = batch.period_start "
			"and newer.period_end = batch.period_end "
			"and newer.status = 'Completed' "
			"and newer.recovery_generation > batch.recovery_generation "
			"where newer.name is null "
			f"and source.`{date_identifier}` >= %s "
			f"and source.`{date_identifier}` < %s "
			"and source.lineage_status = 'Verified' "
			f"{window_sql}{upper_sql}"
			"order by source.creation asc, source.name asc limit %s"
		)
	else:
		selection = ", ".join(f"source.`{fieldname}`" for fieldname in fields)
		query = (
			f"select {selection} from `tab{doctype}` source "  # noqa: S608
			f"where source.`{date_identifier}` >= %s "
			f"and source.`{date_identifier}` < %s "
			f"{window_sql}{upper_sql}"
			"order by source.creation asc, source.name asc limit %s"
		)
	return frappe.db.sql(
		query,
		(
			period_start,
			add_days(period_end, 1),
			*window_params,
			min(max(int(limit or 1), 1), _MAX_BATCH_SIZE + 1),
		),
		as_dict=True,
	)


def _source_cursor_value(source: _FactSource, row) -> dict[str, str]:
	del source
	creation = str(row.get("creation") or "")
	name = str(row.get("name") or "")
	if not creation or not name:
		raise ValueError("Analytics continuation row lacks its stable cursor identity.")
	return {"creation": creation, "name": name}


def _indicator_semantics(version_name: str | None) -> tuple[Any, dict[str, Any]]:
	if not version_name:
		return {}, {}
	version = frappe.get_cached_doc("IONE QC Indicator Version", version_name)
	raw = version.get("formula_json") or version.get("calculation_json")
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			raw = {}
	return version, raw if isinstance(raw, dict) else {}


def _flow_run_link(task_row) -> Any:
	doctype = "IONE Flow Run Link"
	if not frappe.db.exists("DocType", doctype):
		return {}
	meta = frappe.get_meta(doctype)
	fields = [
		fieldname
		for fieldname in (
			"name",
			"policy",
			"flow_agent",
			"flow_model",
			"model_id",
			"flow_run",
			"usage_json",
			"started_at",
			"completed_at",
		)
		if fieldname == "name" or meta.has_field(fieldname)
	]
	filters: dict[str, Any] = {"task": task_row.name}
	if task_row.get("flow_run") and meta.has_field("flow_run"):
		filters["flow_run"] = task_row.get("flow_run")
	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by=(
			"run_iteration desc, creation desc, name desc"
			if meta.has_field("run_iteration")
			else "modified desc, name desc"
		),
		limit_page_length=1,
	)
	return rows[0] if rows else {}


def _usage_metrics(raw: Any) -> dict[str, int]:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			raw = {}
	if not isinstance(raw, dict):
		raw = {}
	input_tokens = _first_int(
		raw,
		"input_tokens",
		"prompt_tokens",
		"prompt_token_count",
		"input_token_count",
	)
	output_tokens = _first_int(
		raw,
		"output_tokens",
		"completion_tokens",
		"completion_token_count",
		"output_token_count",
	)
	total_tokens = _first_int(raw, "total_tokens", "total_token_count")
	if not total_tokens:
		total_tokens = input_tokens + output_tokens
	return {
		"input_tokens": input_tokens,
		"output_tokens": output_tokens,
		"total_tokens": total_tokens,
	}


def _first_int(values: dict[str, Any], *fieldnames: str) -> int:
	for fieldname in fieldnames:
		value = values.get(fieldname)
		if value in (None, ""):
			continue
		try:
			return max(int(value), 0)
		except TypeError, ValueError:
			continue
	return 0


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
	if not started_at or not completed_at:
		return None
	duration = (get_datetime(completed_at) - get_datetime(started_at)).total_seconds()
	return max(round(duration * 1000), 0)


def _adopted_candidate_count(task_name: str) -> int:
	doctype = "IONE AI Candidate Finding"
	if not frappe.db.exists("DocType", doctype):
		return 0
	return int(frappe.db.count(doctype, {"task": task_name, "status": "Accepted"}))


def _first_value(*values: Any) -> Any:
	return next((value for value in values if value not in (None, "")), None)


def _ensure_analytics_run(index: int, period_start, period_end):
	if not 0 <= index < len(_SOURCES):
		raise ValueError("Analytics source index is invalid.")
	source = _SOURCES[index]
	if not _doctype_exists(source.source_doctype) or not _doctype_exists(source.target_doctype):
		return None
	date_field = _first_existing_field(source.source_doctype, source.date_fields)
	if not date_field:
		return None
	high_water = _analytics_high_water(source, date_field, period_start, period_end)
	high_water_json = batches.canonical_json(high_water)
	subject_key = f"{index}:{source.builder}:{source.source_doctype}"
	latest_rows = frappe.get_all(
		batches.RUN_DOCTYPE,
		filters={
			"batch_type": "Analytics",
			"subject_key": subject_key,
			"period_start": period_start,
			"period_end": period_end,
		},
		fields=["name", "status", "recovery_generation", "high_water_json"],
		order_by="recovery_generation desc, creation desc, name desc",
		limit_page_length=1,
	)
	if latest_rows:
		latest = latest_rows[0]
		if (
			str(latest.get("high_water_json") or "") == high_water_json
			and str(latest.get("status") or "") != "Superseded"
		):
			return frappe.get_doc(batches.RUN_DOCTYPE, latest.name)
		if str(latest.get("status") or "") in {"Pending", "Running", "Retry Waiting"}:
			# The running page will compare its frozen high water before
			# completion and create the next generation on drift.
			return frappe.get_doc(batches.RUN_DOCTYPE, latest.name)
		generation = int(latest.get("recovery_generation") or 0) + 1
	else:
		generation = 1
	run = batches.ensure_run(
		batch_type="Analytics",
		subject_key=subject_key,
		period_start=period_start,
		period_end=period_end,
		task_path="ione_qms.tasks.analytics.process_analytics_batch_run",
		task_args={},
		generation=generation,
		source_doctype=source.source_doctype,
		source_index=index,
	)
	if str(run.get("phase") or "") == "Dispatch":
		batches.set_run_values(
			run.name,
			{
				"cursor_json": _encode_analytics_cursor(
					{"creation": "", "name": ""},
					high_water,
				),
				"high_water_json": high_water_json,
				"phase": "Projecting",
			},
		)
		run = frappe.get_doc(batches.RUN_DOCTYPE, run.name)
	return run


def _analytics_high_water(
	source: _FactSource,
	date_field: str,
	period_start,
	period_end,
) -> dict[str, Any]:
	date_identifier = _sql_identifier(date_field)
	if source.builder == "indicator":
		from_sql = (
			"from `tabIONE Indicator Result` source "
			"inner join `tabIONE Batch Work Item` work "
			"on work.result_reference = source.name and work.status = 'Succeeded' "
			"inner join `tabIONE Batch Run` batch "
			"on batch.name = work.batch_run "
			"and batch.batch_type = 'Indicator' and batch.status = 'Completed' "
			"left join `tabIONE Batch Run` newer "
			"on newer.batch_type = batch.batch_type "
			"and newer.subject_key = batch.subject_key "
			"and newer.period_start = batch.period_start "
			"and newer.period_end = batch.period_end "
			"and newer.status = 'Completed' "
			"and newer.recovery_generation > batch.recovery_generation "
		)
		extra_where = "and newer.name is null "
	else:
		from_sql = f"from `tab{source.source_doctype}` source "
		extra_where = ""
	params = (period_start, add_days(period_end, 1))
	stats = frappe.db.sql(
		"select count(distinct source.name) as row_count, "
		"max(source.modified) as max_modified "
		f"{from_sql}"
		f"where source.`{date_identifier}` >= %s "
		f"and source.`{date_identifier}` < %s {extra_where}",
		params,
		as_dict=True,
	)[0]
	count = int(stats.get("row_count") or 0)
	if not count:
		return {
			"count": 0,
			"creation": "",
			"modified": "",
			"name": "",
			"version": "ione-analytics-high-water-v1",
		}
	rows = frappe.db.sql(
		"select source.name, source.creation "
		f"{from_sql}"
		f"where source.`{date_identifier}` >= %s "
		f"and source.`{date_identifier}` < %s {extra_where}"
		"order by source.creation desc, source.name desc limit 1",
		params,
		as_dict=True,
	)
	if len(rows) != 1:
		raise RuntimeError("Analytics high-water identity could not be resolved.")
	return {
		"count": count,
		"creation": str(rows[0].get("creation") or ""),
		"modified": str(stats.get("max_modified") or ""),
		"name": str(rows[0].get("name") or ""),
		"version": "ione-analytics-high-water-v1",
	}


def _encode_analytics_cursor(
	position: dict[str, str],
	high_water: dict[str, Any],
) -> str:
	unsigned = {
		"high_water": high_water,
		"position": {
			"creation": str(position.get("creation") or ""),
			"name": str(position.get("name") or ""),
		},
		"version": "ione-analytics-cursor-v1",
	}
	return batches.canonical_json(
		{
			**unsigned,
			"signature": _analytics_cursor_signature(unsigned),
		}
	)


def _decode_analytics_cursor(
	value: Any,
	*,
	expected_high_water: Any,
) -> dict[str, Any]:
	try:
		payload = json.loads(str(value or ""))
		expected = json.loads(str(expected_high_water or ""))
	except ValueError as exc:
		raise ValueError("Analytics frozen cursor is invalid.") from exc
	if not isinstance(payload, dict) or set(payload) != {
		"high_water",
		"position",
		"signature",
		"version",
	}:
		raise ValueError("Analytics frozen cursor contract is invalid.")
	unsigned = {key: payload[key] for key in payload if key != "signature"}
	if (
		payload.get("version") != "ione-analytics-cursor-v1"
		or not hmac.compare_digest(
			str(payload.get("signature") or ""),
			_analytics_cursor_signature(unsigned),
		)
		or batches.canonical_json(payload.get("high_water")) != batches.canonical_json(expected)
	):
		raise ValueError("Analytics frozen cursor verification failed.")
	position = payload.get("position")
	high_water = payload.get("high_water")
	if (
		not isinstance(position, dict)
		or set(position) != {"creation", "name"}
		or not isinstance(high_water, dict)
		or set(high_water) != {"count", "creation", "modified", "name", "version"}
	):
		raise ValueError("Analytics frozen cursor fields are invalid.")
	return {"position": position, "high_water": high_water}


def _analytics_cursor_signature(payload: dict[str, Any]) -> str:
	signing_key = str((getattr(frappe, "conf", {}) or {}).get("encryption_key") or "")
	if not signing_key:
		raise ValueError("Site encryption_key is required for analytics cursors.")
	return hmac.new(
		signing_key.encode("utf-8"),
		batches.canonical_json(payload).encode("utf-8"),
		hashlib.sha256,
	).hexdigest()


def _dispatch_replacement_analytics_run(run) -> None:
	replacement = _ensure_analytics_run(
		int(run.get("source_index") or 0),
		getdate(run.get("period_start")),
		getdate(run.get("period_end")),
	)
	if replacement is None or replacement.name == run.name:
		raise RuntimeError("Analytics drift replacement did not advance generation.")
	batches.enqueue_run(replacement.name, enqueue_after_commit=True)


def _month_bounds(value: str | None):
	if value:
		try:
			anchor = getdate(f"{value[:7]}-01")
		except (TypeError, ValueError) as exc:
			raise ValueError("month must use YYYY-MM format.") from exc
	else:
		anchor = add_days(get_first_day(nowdate()), -1)
	return getdate(get_first_day(anchor)), getdate(get_last_day(anchor))


def _add_months(value: Any, offset: int) -> date:
	anchor = getdate(value)
	ordinal = anchor.year * 12 + anchor.month - 1 + int(offset)
	year, month_index = divmod(ordinal, 12)
	return date(year, month_index + 1, 1)


def _enqueue_dispatch(
	period_start,
	batch_size: int,
	source_index: int,
	remaining_batches: int,
) -> None:
	if source_index >= len(_SOURCES):
		return
	job_key = _digest(
		{
			"month": period_start,
			"source_index": source_index,
			"remaining": remaining_batches,
		}
	)[:24]
	frappe.enqueue(
		"ione_qms.tasks.analytics.build_monthly_facts",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:analytics:{job_key}",
		deduplicate=True,
		month=period_start.strftime("%Y-%m"),
		batch_size=batch_size,
		source_index=source_index,
		cursor=None,
		remaining_batches=remaining_batches,
	)


def _enqueue_reconciliation(
	as_of,
	months_back: int,
	month_offset: int,
	remaining_batches: int,
) -> None:
	job_key = _digest(
		{
			"as_of": as_of,
			"month_offset": month_offset,
			"months_back": months_back,
			"remaining": remaining_batches,
		}
	)[:24]
	frappe.enqueue(
		"ione_qms.tasks.analytics.reconcile_monthly_facts",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:analytics-reconcile:{job_key}",
		deduplicate=True,
		as_of=str(as_of),
		months_back=months_back,
		month_offset=month_offset,
		remaining_batches=remaining_batches,
	)


def _first_existing_field(doctype: str, candidates: tuple[str, ...]) -> str | None:
	return next((fieldname for fieldname in candidates if _has_field(doctype, fieldname)), None)


def _supported_values(doctype: str, values: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	return {fieldname: value for fieldname, value in values.items() if meta.has_field(fieldname)}


def _has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _doctype_exists(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def _sql_identifier(value: str) -> str:
	identifier = str(value or "")
	if not identifier or not identifier.replace("_", "").isalnum():
		raise ValueError("Analytics SQL identifier is invalid.")
	return identifier


def _digest(value: Any) -> str:
	encoded = json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	).encode()
	return hashlib.sha256(encoded).hexdigest()


def _log_failure(source: _FactSource, reference_name: str, failure_type: str) -> None:
	frappe.log_error(
		title=f"IONE QMS {source.builder} analytics projection failed",
		message=f"{failure_type}: analytics projection failed; source payload was not logged.",
		reference_doctype=source.source_doctype,
		reference_name=reference_name,
	)

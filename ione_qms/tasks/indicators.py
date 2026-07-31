from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import frappe
from frappe.exceptions import QueryTimeoutError
from frappe.utils import add_days, getdate, nowdate

from ione_qms.indicator_engine import (
	normalize_dimension_names,
	read_dimension_snapshot_page,
	source_snapshot_watermark,
	start_dimension_snapshot,
)
from ione_qms.services import batch_reliability as batches
from ione_qms.services.indicators import (
	calculate_indicator_version,
	scheduled_indicator_period,
)
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

_MAX_BATCH_SIZE = 500
_MAX_MANIFEST_PAGE_SIZE = 5_000
_MAX_CONTINUATION_BATCHES = 50_000
_MAX_VERSIONS_SCANNED = 1_000
_MAX_RECONCILIATION_LOOKBACK_DAYS = 400
_INDICATOR_TASK = "ione_qms.tasks.indicators.process_indicator_batch_run"


def calculate_daily_indicators(
	as_of: str | None = None,
	batch_size: int = 100,
	version_cursor: str | None = None,
	dimension_cursor: str | None = None,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
) -> dict[str, int]:
	"""Dispatch bounded, independently recoverable runs for one calculation date.

	The scheduler cursor advances only after a durable run exists.  Enumeration
	and calculation progress live inside that run, so a failed page or work item
	can never be skipped by advancing this dispatcher cursor.
	"""
	require_post_migrate_runtime_ready("calculate scheduled indicators")
	if dimension_cursor:
		raise RuntimeError(
			"Legacy dimension continuations are not authoritative; the durable "
			"double-scan manifest scheduler will restart this governed period."
		)
	limit = min(max(int(batch_size or 100), 1), _MAX_BATCH_SIZE)
	remaining_batches = _bounded_continuation_budget(remaining_batches)
	calculation_date = getdate(as_of or add_days(nowdate(), -1))
	summary = {"attempted": 0, "completed": 0, "failed": 0, "dispatched": 0}
	current_cursor = version_cursor
	versions_scanned = 0
	with frappe.db.advisory_lock(
		f"ione-qms:indicator-dispatch:{calculation_date.isoformat()}",
		timeout=0,
	):
		while summary["attempted"] < limit and versions_scanned < _MAX_VERSIONS_SCANNED:
			version = _next_version(current_cursor)
			if version is None:
				break
			versions_scanned += 1
			period = _period_for_version(version, calculation_date)
			if period is None:
				current_cursor = version.name
				continue
			summary["attempted"] += 1
			period_start, period_end = period
			try:
				run = _dispatch_indicator_run(
					version.name,
					period_start,
					period_end,
					generation=1,
				)
			except Exception as exc:
				summary["failed"] += 1
				_log_calculation_failure(version.name, exc)
				# Do not advance: the next scheduler invocation must retry this
				# exact version if durable dispatch itself did not complete.
				raise
			current_cursor = version.name
			summary["dispatched"] += 1
			if str(run.get("status") or "") == "Completed":
				summary["completed"] += 1
		next_version = _next_version(current_cursor)
		if next_version is not None:
			if remaining_batches <= 0:
				raise RuntimeError(
					"Indicator dispatcher continuation budget was exhausted before "
					"all governed versions had durable runs."
				)
			_enqueue_dispatch_continuation(
				calculation_date,
				limit,
				current_cursor or "",
				remaining_batches - 1,
			)
	return summary


def reconcile_indicator_backlog(
	as_of: str | None = None,
	batch_size: int = 100,
	version_cursor: str | None = None,
	period_cursor: str | None = None,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
	lookback_days: int = _MAX_RECONCILIATION_LOOKBACK_DAYS,
) -> dict[str, int]:
	"""Oldest-first catch-up of *every* missing nominal period in the lookback."""
	require_post_migrate_runtime_ready("reconcile scheduled indicator backlog")
	limit = min(max(int(batch_size or 100), 1), _MAX_BATCH_SIZE)
	remaining_batches = _bounded_continuation_budget(remaining_batches)
	lookback_days = min(
		max(int(lookback_days or _MAX_RECONCILIATION_LOOKBACK_DAYS), 1),
		_MAX_RECONCILIATION_LOOKBACK_DAYS,
	)
	anchor = getdate(as_of or add_days(nowdate(), -1))
	summary = {"versions_scanned": 0, "periods_scanned": 0, "missing": 0, "dispatched": 0}
	current_version_cursor = version_cursor
	current_period_cursor = getdate(period_cursor) if period_cursor else None
	continuation_version: str | None = None
	continuation_period: date | None = None
	with frappe.db.advisory_lock(
		f"ione-qms:indicator-reconciliation:{anchor.isoformat()}",
		timeout=0,
	):
		while summary["dispatched"] < limit:
			version = _next_version(
				current_version_cursor,
				include_cursor=bool(current_period_cursor),
			)
			if version is None:
				break
			summary["versions_scanned"] += 1
			due_periods = _due_periods(version, anchor, lookback_days=lookback_days)
			for period_start, period_end in due_periods:
				if current_period_cursor and period_end <= current_period_cursor:
					continue
				summary["periods_scanned"] += 1
				if _completed_indicator_run_exists(version.name, period_start, period_end):
					continue
				summary["missing"] += 1
				_dispatch_indicator_run(
					version.name,
					period_start,
					period_end,
					generation=1,
				)
				summary["dispatched"] += 1
				if summary["dispatched"] >= limit:
					continuation_version = version.name
					continuation_period = period_end
					break
			if continuation_version:
				break
			current_version_cursor = version.name
			current_period_cursor = None
		next_version = _next_version(current_version_cursor) if not continuation_version else version
		if next_version is not None:
			if remaining_batches <= 0:
				raise RuntimeError("Indicator reconciliation continuation budget was exhausted.")
			_enqueue_reconciliation_continuation(
				anchor,
				limit,
				continuation_version or current_version_cursor or "",
				continuation_period,
				remaining_batches - 1,
				lookback_days,
			)
	return summary


def revalidate_completed_indicator_snapshots(
	as_of: str | None = None,
	batch_size: int = 50,
	run_cursor: str | None = None,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
	lookback_days: int = _MAX_RECONCILIATION_LOOKBACK_DAYS,
) -> dict[str, int]:
	"""Detect post-completion backfills/corrections and create a new generation."""
	require_post_migrate_runtime_ready("revalidate completed indicator snapshots")
	limit = min(max(int(batch_size or 50), 1), 200)
	remaining_batches = _bounded_continuation_budget(remaining_batches)
	lookback_days = min(
		max(int(lookback_days or _MAX_RECONCILIATION_LOOKBACK_DAYS), 1),
		_MAX_RECONCILIATION_LOOKBACK_DAYS,
	)
	anchor = getdate(as_of or add_days(nowdate(), -1))
	filters: dict[str, Any] = {
		"batch_type": "Indicator",
		"status": "Completed",
		"period_end": [">=", add_days(anchor, -(lookback_days - 1))],
	}
	if run_cursor:
		filters["name"] = [">", run_cursor]
	rows = frappe.get_all(
		batches.RUN_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"subject_key",
			"period_start",
			"period_end",
			"recovery_generation",
			"freshness_watermark_json",
		],
		order_by="name asc",
		limit_page_length=limit + 1,
	)
	summary = {"scanned": 0, "fresh": 0, "drifted": 0, "dispatched": 0}
	for run in rows[:limit]:
		summary["scanned"] += 1
		if not _is_latest_completed_generation(run):
			continue
		version = _version_by_name(str(run.get("subject_key") or ""))
		current = _completion_watermark(
			version,
			getdate(run.get("period_start")),
			getdate(run.get("period_end")),
		)
		if current == str(run.get("freshness_watermark_json") or ""):
			summary["fresh"] += 1
			continue
		summary["drifted"] += 1
		if _newer_active_generation_exists(run):
			continue
		_dispatch_indicator_run(
			version.name,
			getdate(run.get("period_start")),
			getdate(run.get("period_end")),
			generation=int(run.get("recovery_generation") or 1) + 1,
		)
		summary["dispatched"] += 1
	if len(rows) > limit:
		if remaining_batches <= 0:
			raise RuntimeError("Indicator freshness sweep continuation budget was exhausted.")
		_enqueue_freshness_continuation(
			anchor,
			limit,
			str(rows[limit - 1].get("name") or ""),
			remaining_batches - 1,
			lookback_days,
		)
	return summary


def process_indicator_batch_run(run_name: str) -> dict[str, int]:
	"""Advance exactly one bounded phase/page of an indicator batch run."""
	require_post_migrate_runtime_ready("process governed indicator batch")
	summary = {"manifest_rows": 0, "work_attempted": 0, "completed": 0, "failed": 0}
	try:
		with frappe.db.advisory_lock(f"ione-qms:batch-run:{run_name}", timeout=0):
			activated = batches.activate_run(run_name)
			if activated is None:
				return summary
			run, lease_token = activated
			version = _version_by_name(str(run.get("subject_key") or ""))
			period_start = getdate(run.get("period_start"))
			period_end = getdate(run.get("period_end"))
			dimensions = _configured_dimensions(version)
			phase = str(run.get("phase") or "Dispatch")
			if phase == "Dispatch":
				initial_watermark = _completion_watermark(
					version,
					period_start,
					period_end,
				)
				if dimensions:
					start_cursor = start_dimension_snapshot(
						version.get("formula_json"),
						list(dimensions),
						period_start,
						period_end,
						lineage=_version_lineage(version),
						physical_query_contract=version.get("physical_query_contract_json"),
					)
					batches.set_run_values(
						run.name,
						{
							"cursor_json": start_cursor,
							"freshness_watermark_json": initial_watermark,
							"manifest_hash": batches.EMPTY_MANIFEST_HASH,
							"manifest_record_count": 0,
							"phase": "Materializing",
							"snapshot_start_cursor": start_cursor,
							"verification_hash": batches.EMPTY_MANIFEST_HASH,
							"verification_record_count": 0,
						},
					)
				else:
					batches.ensure_work_item(run.name, payload={"dimensions": {}}, sequence=0)
					batches.set_run_values(
						run.name,
						{
							"freshness_watermark_json": initial_watermark,
							"phase": "Executing",
							"work_total": 1,
						},
					)
				frappe.db.commit()
				batches.enqueue_run(run.name, enqueue_after_commit=False)
				frappe.db.commit()
				return summary
			if phase == "Materializing":
				_materialize_dimension_page(run, lease_token, version, dimensions, summary)
				return summary
			if phase in {"Verifying", "Final Verifying"}:
				_verify_dimension_page(run, lease_token, version, dimensions, summary)
				return summary
			if phase in {"Ready", "Executing"}:
				_execute_indicator_work_page(run, lease_token, version, summary)
				return summary
			raise RuntimeError("Indicator batch run has an unsupported phase.")
	except QueryTimeoutError:
		# An existing worker owns the authoritative database advisory lock.
		frappe.db.rollback()
		return summary
	except Exception as exc:
		frappe.db.rollback()
		try:
			batches.schedule_retry(run_name, exc)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			raise
		summary["failed"] += 1
		_log_calculation_failure(run_name, exc)
		return summary


def _materialize_dimension_page(run, lease_token, version, dimensions, summary) -> None:
	batches.assert_run_lease(run.name, lease_token)
	page = read_dimension_snapshot_page(
		version.get("formula_json"),
		list(dimensions),
		getdate(run.get("period_start")),
		getdate(run.get("period_end")),
		record_cursor=str(run.get("cursor_json") or ""),
		limit=_MAX_MANIFEST_PAGE_SIZE,
		lineage=_version_lineage(version),
		physical_query_contract=version.get("physical_query_contract_json"),
	)
	sequence = int(run.get("work_total") or 0)
	for offset, dimension_values in enumerate(page.dimensions, start=1):
		batches.ensure_work_item(
			run.name,
			payload={"dimensions": dimension_values},
			sequence=sequence + offset,
		)
	work_total = frappe.db.count(batches.WORK_DOCTYPE, {"batch_run": run.name})
	manifest_hash = batches.advance_manifest_hash(
		str(run.get("manifest_hash") or batches.EMPTY_MANIFEST_HASH),
		page.record_hashes,
	)
	values: dict[str, Any] = {
		"heartbeat_at": frappe.utils.now_datetime(),
		"manifest_hash": manifest_hash,
		"manifest_record_count": int(run.get("manifest_record_count") or 0) + len(page.record_hashes),
		"work_total": work_total,
	}
	summary["manifest_rows"] += len(page.record_hashes)
	if page.has_more:
		if not page.next_cursor:
			raise RuntimeError("Dimension manifest page omitted its signed continuation.")
		values["cursor_json"] = page.next_cursor
	else:
		values.update(
			{
				"cursor_json": str(run.get("snapshot_start_cursor") or ""),
				"phase": "Verifying",
				"verification_hash": batches.EMPTY_MANIFEST_HASH,
				"verification_record_count": 0,
			}
		)
	batches.set_run_values(run.name, values)
	frappe.db.commit()
	batches.enqueue_run(run.name, enqueue_after_commit=False)
	frappe.db.commit()


def _verify_dimension_page(run, lease_token, version, dimensions, summary) -> None:
	batches.assert_run_lease(run.name, lease_token)
	if str(run.get("phase") or "") == "Final Verifying":
		# Source writers take the same ordered epoch locks in ``before_save``.
		# Holding them before the final-page read closes the read-to-completion
		# race without blocking ingestion during materialization or execution.
		batches.lock_source_epochs(_source_doctypes_from_watermark(run.get("freshness_watermark_json")))
	page = read_dimension_snapshot_page(
		version.get("formula_json"),
		list(dimensions),
		getdate(run.get("period_start")),
		getdate(run.get("period_end")),
		record_cursor=str(run.get("cursor_json") or ""),
		limit=_MAX_MANIFEST_PAGE_SIZE,
		lineage=_version_lineage(version),
		physical_query_contract=version.get("physical_query_contract_json"),
	)
	verification_hash = batches.advance_manifest_hash(
		str(run.get("verification_hash") or batches.EMPTY_MANIFEST_HASH),
		page.record_hashes,
	)
	verification_count = int(run.get("verification_record_count") or 0) + len(page.record_hashes)
	summary["manifest_rows"] += len(page.record_hashes)
	if page.has_more:
		if not page.next_cursor:
			raise RuntimeError("Dimension verification page omitted its signed continuation.")
		batches.set_run_values(
			run.name,
			{
				"cursor_json": page.next_cursor,
				"heartbeat_at": frappe.utils.now_datetime(),
				"verification_hash": verification_hash,
				"verification_record_count": verification_count,
			},
		)
		frappe.db.commit()
		batches.enqueue_run(run.name, enqueue_after_commit=False)
		frappe.db.commit()
		return
	if verification_count != int(run.get("manifest_record_count") or 0) or verification_hash != str(
		run.get("manifest_hash") or ""
	):
		batches.supersede_run(run.name, lease_token, "DIMENSION_SNAPSHOT_DRIFT")
		frappe.db.commit()
		replacement = _dispatch_indicator_run(
			version.name,
			getdate(run.get("period_start")),
			getdate(run.get("period_end")),
			generation=int(run.get("recovery_generation") or 1) + 1,
		)
		if replacement.name == run.name:
			raise RuntimeError("Dimension snapshot drift replacement did not advance generation.")
		return
	if str(run.get("phase") or "") == "Final Verifying":
		batches.set_run_values(
			run.name,
			{
				"cursor_json": None,
				"heartbeat_at": frappe.utils.now_datetime(),
				"verification_hash": verification_hash,
				"verification_record_count": verification_count,
			},
		)
		_complete_or_supersede_for_freshness(run, lease_token, version, summary)
		return
	batches.set_run_values(
		run.name,
		{
			"cursor_json": None,
			"heartbeat_at": frappe.utils.now_datetime(),
			"phase": "Executing",
			"verification_hash": verification_hash,
			"verification_record_count": verification_count,
		},
	)
	frappe.db.commit()
	batches.enqueue_run(run.name, enqueue_after_commit=False)
	frappe.db.commit()


def _execute_indicator_work_page(run, lease_token, version, summary) -> None:
	items = batches.pending_work_items(run.name, limit=_MAX_BATCH_SIZE)
	if not items:
		_begin_final_verification(run, lease_token, version, summary)
		return
	for item in items:
		batches.assert_run_lease(run.name, lease_token)
		summary["work_attempted"] += 1
		savepoint: str | None = None
		try:
			payload = json.loads(str(item.get("payload_json") or ""))
			if not isinstance(payload, dict) or set(payload) != {"dimensions"}:
				raise ValueError("Indicator work-item payload contract is invalid.")
			dimensions = payload.get("dimensions")
			if not isinstance(dimensions, dict):
				raise ValueError("Indicator work-item dimensions are invalid.")
			savepoint = f"ione_indicator_{item.name[:24]}"
			frappe.db.savepoint(savepoint)
			outcome = _calculate_work_unit(
				version,
				getdate(run.get("period_start")),
				getdate(run.get("period_end")),
				{str(key): str(value) for key, value in dimensions.items()},
			)
			batches.mark_work_succeeded(
				item.name,
				result_reference=str(outcome.get("result") or ""),
			)
			frappe.db.release_savepoint(savepoint)
			frappe.db.commit()
			batches.heartbeat(run.name, lease_token)
		except Exception as exc:
			if savepoint:
				frappe.db.rollback(save_point=savepoint)
			batches.mark_work_failed(item.name, exc)
			frappe.db.commit()
			summary["failed"] += 1
			_log_calculation_failure(version.name, exc)
			return
	remaining = frappe.db.count(
		batches.WORK_DOCTYPE,
		{"batch_run": run.name, "status": ["!=", "Succeeded"]},
	)
	if remaining:
		frappe.db.commit()
		batches.enqueue_run(run.name, enqueue_after_commit=False)
		frappe.db.commit()
	else:
		_begin_final_verification(run, lease_token, version, summary)


def _begin_final_verification(run, lease_token, version, summary) -> None:
	"""Close the post-materialization mutation window before completion."""
	dimensions = _configured_dimensions(version)
	if not dimensions:
		_complete_or_supersede_for_freshness(run, lease_token, version, summary)
		return
	start_cursor = str(run.get("snapshot_start_cursor") or "")
	if not start_cursor:
		raise RuntimeError("Indicator run cannot final-verify without its signed snapshot start.")
	batches.set_run_values(
		run.name,
		{
			"cursor_json": start_cursor,
			"heartbeat_at": frappe.utils.now_datetime(),
			"phase": "Final Verifying",
			"verification_hash": batches.EMPTY_MANIFEST_HASH,
			"verification_record_count": 0,
		},
	)
	frappe.db.commit()
	batches.enqueue_run(run.name, enqueue_after_commit=False)
	frappe.db.commit()


def _complete_or_supersede_for_freshness(run, lease_token, version, summary) -> None:
	batches.lock_source_epochs(_source_doctypes_from_watermark(run.get("freshness_watermark_json")))
	current_watermark = _completion_watermark(
		version,
		getdate(run.get("period_start")),
		getdate(run.get("period_end")),
	)
	if current_watermark != str(run.get("freshness_watermark_json") or ""):
		batches.supersede_run(run.name, lease_token, "INDICATOR_SOURCE_WATERMARK_DRIFT")
		frappe.db.commit()
		_dispatch_indicator_run(
			version.name,
			getdate(run.get("period_start")),
			getdate(run.get("period_end")),
			generation=int(run.get("recovery_generation") or 1) + 1,
		)
		return
	batches.complete_run(run.name, lease_token)
	frappe.db.commit()
	summary["completed"] += 1


def _calculate_work_unit(version, period_start, period_end, dimensions: dict[str, str]) -> dict[str, Any]:
	outcome = calculate_indicator_version(
		version.name,
		str(period_start),
		str(period_end),
		dimensions,
	)
	if not outcome.get("result"):
		raise ValueError("Indicator calculation completed without an immutable result revision.")
	return outcome


def _dispatch_indicator_run(
	version_name: str,
	period_start,
	period_end,
	*,
	generation: int,
):
	subject_key = str(version_name or "")
	if not subject_key:
		raise ValueError("Indicator batch run requires a version identity.")
	for candidate_generation in range(max(int(generation or 1), 1), int(generation or 1) + 100):
		run = batches.ensure_run(
			batch_type="Indicator",
			subject_key=subject_key,
			period_start=period_start,
			period_end=period_end,
			task_path=_INDICATOR_TASK,
			task_args={},
			generation=candidate_generation,
		)
		if str(run.get("status") or "") != "Superseded":
			break
	else:
		raise RuntimeError("Indicator batch generation retry bound was exhausted.")
	if str(run.get("status") or "") not in {"Completed", "Dead Letter", "Superseded"}:
		batches.enqueue_run(run.name, enqueue_after_commit=True)
	return run


def _period_for_version(version, calculation_date):
	try:
		return scheduled_indicator_period(version, calculation_date)
	except TypeError, ValueError:
		# Invalid historical boundaries are reported by the read-only governance audit.
		return None


def _due_periods(version, anchor: date, *, lookback_days: int) -> list[tuple[date, date]]:
	periods: dict[tuple[date, date], tuple[date, date]] = {}
	for offset in range(max(int(lookback_days), 1)):
		period = _period_for_version(version, add_days(anchor, -offset))
		if period is not None:
			periods[(getdate(period[0]), getdate(period[1]))] = (
				getdate(period[0]),
				getdate(period[1]),
			)
	return [periods[key] for key in sorted(periods, key=lambda item: (item[1], item[0]))]


def _completed_indicator_run_exists(version_name: str, period_start, period_end) -> bool:
	return bool(
		frappe.db.exists(
			batches.RUN_DOCTYPE,
			{
				"batch_type": "Indicator",
				"subject_key": version_name,
				"period_start": period_start,
				"period_end": period_end,
				"status": "Completed",
			},
		)
	)


def _is_latest_completed_generation(run) -> bool:
	return not bool(
		frappe.db.exists(
			batches.RUN_DOCTYPE,
			{
				"batch_type": "Indicator",
				"subject_key": run.get("subject_key"),
				"period_start": run.get("period_start"),
				"period_end": run.get("period_end"),
				"status": "Completed",
				"recovery_generation": [">", int(run.get("recovery_generation") or 0)],
			},
		)
	)


def _newer_active_generation_exists(run) -> bool:
	return bool(
		frappe.db.exists(
			batches.RUN_DOCTYPE,
			{
				"batch_type": "Indicator",
				"subject_key": run.get("subject_key"),
				"period_start": run.get("period_start"),
				"period_end": run.get("period_end"),
				"status": ["in", ["Pending", "Running", "Retry Waiting", "Dead Letter"]],
				"recovery_generation": [">", int(run.get("recovery_generation") or 0)],
			},
		)
	)


def _next_version(cursor: str | None, *, include_cursor: bool = False):
	doctype = "IONE QC Indicator Version"
	filters: dict[str, Any] = {}
	if _has_field(doctype, "status"):
		filters["status"] = ["in", ["Published", "Retired"]]
	elif _has_field(doctype, "approval_status"):
		filters["approval_status"] = ["in", ["Published", "Retired"]]
	elif _has_field(doctype, "is_published"):
		filters["is_published"] = 1
	if cursor:
		filters["name"] = [">=" if include_cursor else ">", cursor]
	fields = ["name", "indicator"]
	for candidate in (
		"status",
		"approval_status",
		"is_published",
		"frequency",
		"calculation_frequency",
		"dimensions_json",
		"dimension_json",
		"formula_json",
		"rolling_window_days",
		"effective_from",
		"effective_to",
		"calculator_key",
		"source_system_snapshot",
		"source_mapping",
		"mapping_version_snapshot",
		"mapping_checksum_snapshot",
		"physical_query_contract_json",
	):
		if _has_field(doctype, candidate):
			fields.append(candidate)
	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by="name asc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


def _version_by_name(version_name: str):
	if not version_name:
		raise ValueError("Indicator batch run lacks its governed version.")
	return frappe.get_doc("IONE QC Indicator Version", version_name)


def _version_lineage(version) -> dict[str, str]:
	return {
		"source_system": str(version.get("source_system_snapshot") or ""),
		"mapping_record": str(version.get("source_mapping") or ""),
		"mapping_version": str(version.get("mapping_version_snapshot") or ""),
		"mapping_checksum": str(version.get("mapping_checksum_snapshot") or ""),
	}


def _completion_watermark(version, period_start, period_end) -> str:
	return batches.canonical_json(
		source_snapshot_watermark(
			version.get("formula_json"),
			list(_configured_dimensions(version)),
			period_start,
			period_end,
			lineage=_version_lineage(version),
			physical_query_contract=version.get("physical_query_contract_json"),
		)
	)


def _source_doctypes_from_watermark(value: Any) -> tuple[str, ...]:
	try:
		watermark = json.loads(str(value or ""))
	except ValueError as exc:
		raise ValueError("Indicator source watermark is invalid.") from exc
	roles = watermark.get("roles") if isinstance(watermark, dict) else None
	if not isinstance(roles, dict) or not roles:
		raise ValueError("Indicator source watermark contains no governed source roles.")
	doctypes = tuple(
		sorted({str(role.get("source_doctype") or "") for role in roles.values() if isinstance(role, dict)})
	)
	if not doctypes or any(not doctype for doctype in doctypes):
		raise ValueError("Indicator source watermark contains an invalid source DocType.")
	return doctypes


def _configured_dimensions(version) -> tuple[str, ...]:
	raw = version.get("dimensions_json") or version.get("dimension_json")
	if raw in (None, ""):
		formula = version.get("formula_json")
		if isinstance(formula, str):
			try:
				formula = frappe.parse_json(formula)
			except ValueError:
				formula = {}
		if isinstance(formula, dict):
			raw = formula.get("dimensions")
	if isinstance(raw, str):
		try:
			raw = frappe.parse_json(raw)
		except ValueError:
			raw = [item.strip() for item in raw.split(",")]
	if isinstance(raw, dict):
		raw = list(raw)
	if not isinstance(raw, (list, tuple, set, frozenset)):
		return ()
	return normalize_dimension_names(raw)


def _enqueue_dispatch_continuation(
	as_of,
	batch_size: int,
	version_cursor: str,
	remaining_batches: int,
) -> None:
	job_key = _digest(f"{as_of}|{version_cursor}|{remaining_batches}")[:24]
	frappe.enqueue(
		"ione_qms.tasks.indicators.calculate_daily_indicators",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:indicator-dispatch:{job_key}",
		deduplicate=True,
		as_of=str(as_of),
		batch_size=batch_size,
		version_cursor=version_cursor,
		dimension_cursor=None,
		remaining_batches=remaining_batches,
	)


def _enqueue_reconciliation_continuation(
	as_of,
	batch_size: int,
	version_cursor: str,
	period_cursor,
	remaining_batches: int,
	lookback_days: int,
) -> None:
	job_key = _digest(f"{as_of}|{version_cursor}|{period_cursor}|{remaining_batches}|{lookback_days}")[:24]
	frappe.enqueue(
		"ione_qms.tasks.indicators.reconcile_indicator_backlog",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:indicator-reconcile:{job_key}",
		deduplicate=True,
		as_of=str(as_of),
		batch_size=batch_size,
		version_cursor=version_cursor,
		period_cursor=str(period_cursor) if period_cursor else None,
		remaining_batches=remaining_batches,
		lookback_days=lookback_days,
	)


def _enqueue_freshness_continuation(
	as_of,
	batch_size: int,
	run_cursor: str,
	remaining_batches: int,
	lookback_days: int,
) -> None:
	job_key = _digest(f"{as_of}|{run_cursor}|{remaining_batches}|{lookback_days}")[:24]
	frappe.enqueue(
		"ione_qms.tasks.indicators.revalidate_completed_indicator_snapshots",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:indicator-freshness:{job_key}",
		deduplicate=True,
		as_of=str(as_of),
		batch_size=batch_size,
		run_cursor=run_cursor,
		remaining_batches=remaining_batches,
		lookback_days=lookback_days,
	)


def _bounded_continuation_budget(value: int) -> int:
	value = int(value)
	if not 0 <= value <= _MAX_CONTINUATION_BATCHES:
		raise ValueError("Indicator continuation budget is outside its governed bound.")
	return value


def _log_calculation_failure(indicator_version: str, exc: Exception) -> None:
	"""Log only bounded technical identity; no source row or dimension value."""
	frappe.log_error(
		message=f"indicator_version={indicator_version}; error_type={type(exc).__name__}",
		title="IONE governed indicator calculation failed",
	)


def _has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _digest(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()

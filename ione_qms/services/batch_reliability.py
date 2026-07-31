from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

import frappe
from frappe.exceptions import QueryTimeoutError
from frappe.utils import get_datetime, now_datetime

RUN_DOCTYPE = "IONE Batch Run"
WORK_DOCTYPE = "IONE Batch Work Item"
RECOVERY_DOCTYPE = "IONE Batch Recovery Receipt"
SOURCE_EPOCH_DOCTYPE = "IONE Batch Source Epoch"

EMPTY_MANIFEST_HASH = hashlib.sha256(b"ione-batch-manifest-v1").hexdigest()
MAX_RUN_ATTEMPTS = 8
MAX_WORK_ATTEMPTS = 8
LEASE_MINUTES = 15
MAX_RECOVERY_ROWS = 200

_TASK_ALLOWLIST = frozenset(
	{
		"ione_qms.tasks.analytics.process_analytics_batch_run",
		"ione_qms.tasks.indicators.process_indicator_batch_run",
	}
)
_TERMINAL_STATUSES = frozenset({"Completed", "Dead Letter", "Superseded"})
SOURCE_EPOCH_DOCTYPES = frozenset(
	{
		"IONE AI Analysis Task",
		"IONE Clinical Quality Event",
		"IONE Encounter Index",
		"IONE Indicator Result",
		"IONE Indicator Result Detail",
		"IONE Medical Record QC",
		"IONE QC Finding",
		"IONE Medical Safety Event",
		"IONE Surgery QC",
	}
)
_MUTABLE_RUN_FIELDS = frozenset(
	{
		"attempt_count",
		"completed_at",
		"continuation_sequence",
		"cursor_json",
		"failure_work_item",
		"freshness_watermark_json",
		"heartbeat_at",
		"high_water_json",
		"last_error_code",
		"lease_expires_at",
		"lease_token",
		"manifest_hash",
		"manifest_record_count",
		"manual_recovery_count",
		"next_retry_at",
		"phase",
		"recovered_at",
		"recovered_by",
		"recovery_reason",
		"snapshot_start_cursor",
		"status",
		"verification_hash",
		"verification_record_count",
		"work_completed",
		"work_receipt_count",
		"work_receipt_hash",
		"work_total",
	}
)
_MUTABLE_WORK_FIELDS = frozenset(
	{
		"attempt_count",
		"completed_at",
		"last_error_code",
		"next_retry_at",
		"result_reference",
		"status",
	}
)


def canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def bump_indicator_source_epoch(doc, method: str | None = None) -> None:
	"""Lock and advance the transaction-coupled barrier before a source mutation."""
	del method
	doctype = str(getattr(doc, "doctype", "") or "")
	if doctype not in SOURCE_EPOCH_DOCTYPES:
		return
	if not frappe.db.exists("DocType", SOURCE_EPOCH_DOCTYPE):
		return
	advance_source_epoch(doctype)


def advance_source_epoch(doctype: str) -> None:
	"""Advance one governed barrier in the caller's current transaction."""
	if doctype not in SOURCE_EPOCH_DOCTYPES:
		raise ValueError("Cannot advance an ungoverned source epoch.")
	_ensure_source_epoch_row(doctype)
	rows = frappe.db.sql(
		"select name, source_epoch from `tabIONE Batch Source Epoch` where source_doctype = %s for update",
		(doctype,),
		as_dict=True,
	)
	if len(rows) != 1:
		raise RuntimeError("Governed source-epoch row is missing or ambiguous.")
	frappe.db.set_value(
		SOURCE_EPOCH_DOCTYPE,
		rows[0].name,
		{
			"last_changed_at": now_datetime(),
			"source_epoch": int(rows[0].get("source_epoch") or 0) + 1,
		},
		update_modified=True,
	)


def ensure_source_epoch_rows() -> None:
	"""Materialize every fixed barrier row before governed source writes begin."""
	if not frappe.db.exists("DocType", SOURCE_EPOCH_DOCTYPE):
		return
	for doctype in sorted(SOURCE_EPOCH_DOCTYPES):
		_ensure_source_epoch_row(doctype)


def lock_source_epochs(doctypes: Iterable[str]) -> dict[str, int]:
	"""Acquire ordered current-read locks held until the caller commits or rolls back."""
	normalized = tuple(sorted({str(doctype or "") for doctype in doctypes}))
	if not normalized or any(doctype not in SOURCE_EPOCH_DOCTYPES for doctype in normalized):
		raise ValueError("Source-epoch barrier requested for an ungoverned DocType.")
	for doctype in normalized:
		_ensure_source_epoch_row(doctype)
	observed: dict[str, int] = {}
	for doctype in normalized:
		rows = frappe.db.sql(
			"select source_doctype, source_epoch from `tabIONE Batch Source Epoch` "
			"where source_doctype = %s for update",
			(doctype,),
			as_dict=True,
		)
		if len(rows) == 1:
			observed[str(rows[0].get("source_doctype") or "")] = int(rows[0].get("source_epoch") or 0)
	if tuple(observed) != normalized:
		raise RuntimeError("Source-epoch barrier set is incomplete.")
	return observed


def source_epoch(doctype: str) -> int:
	if doctype not in SOURCE_EPOCH_DOCTYPES:
		raise ValueError("Source epoch requested for an ungoverned DocType.")
	if not frappe.db.exists("DocType", SOURCE_EPOCH_DOCTYPE):
		raise RuntimeError("Indicator source-epoch write barrier is unavailable.")
	return int(
		frappe.db.get_value(
			SOURCE_EPOCH_DOCTYPE,
			{"source_doctype": doctype},
			"source_epoch",
		)
		or 0
	)


def digest(value: Any) -> str:
	return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def advance_manifest_hash(previous_hash: str | None, record_hashes: Iterable[str]) -> str:
	"""Advance a page-size-independent hash chain over ordered source-row hashes."""
	state = _validated_digest(previous_hash or EMPTY_MANIFEST_HASH, "previous manifest hash")
	for record_hash in record_hashes:
		item = _validated_digest(record_hash, "source record hash")
		state = hashlib.sha256(f"ione-batch-manifest-link-v1\0{state}\0{item}".encode("ascii")).hexdigest()
	return state


def ensure_run(
	*,
	batch_type: str,
	subject_key: str,
	period_start: Any,
	period_end: Any,
	task_path: str,
	task_args: dict[str, Any],
	generation: int = 1,
	source_doctype: str | None = None,
	source_index: int | None = None,
) -> Any:
	"""Return one durable run identity, creating it idempotently when absent."""
	if task_path not in _TASK_ALLOWLIST:
		raise ValueError("Batch recovery task is not allowlisted.")
	generation = max(int(generation or 1), 1)
	run_key = digest(
		{
			"batch_type": batch_type,
			"generation": generation,
			"period_end": str(period_end),
			"period_start": str(period_start),
			"subject_key": subject_key,
			"version": "ione-batch-run-v1",
		}
	)
	if frappe.db.exists(RUN_DOCTYPE, run_key):
		return frappe.get_doc(RUN_DOCTYPE, run_key)
	args = dict(task_args)
	args["run_name"] = run_key
	doc = frappe.get_doc(
		{
			"doctype": RUN_DOCTYPE,
			"run_key": run_key,
			"batch_type": batch_type,
			"subject_key": subject_key,
			"period_start": period_start,
			"period_end": period_end,
			"source_doctype": source_doctype,
			"source_index": source_index,
			"status": "Pending",
			"phase": "Dispatch",
			"attempt_count": 0,
			"max_attempts": MAX_RUN_ATTEMPTS,
			"continuation_sequence": 0,
			"recovery_generation": generation,
			"task_path": task_path,
			"task_args_json": canonical_json(args),
			"manifest_hash": EMPTY_MANIFEST_HASH,
			"verification_hash": EMPTY_MANIFEST_HASH,
			"manifest_record_count": 0,
			"verification_record_count": 0,
			"work_total": 0,
			"work_completed": 0,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return frappe.get_doc(RUN_DOCTYPE, run_key)
	return doc


def activate_run(run_name: str) -> tuple[Any, str] | None:
	"""Lease a non-terminal run while its caller holds the per-run advisory lock."""
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	status = str(run.get("status") or "")
	if status in _TERMINAL_STATUSES:
		return None
	if status == "Retry Waiting" and run.get("next_retry_at"):
		if get_datetime(run.get("next_retry_at")) > now_datetime():
			return None
	lease_token = secrets.token_hex(32)
	now = now_datetime()
	set_run_values(
		run.name,
		{
			"heartbeat_at": now,
			"lease_expires_at": now + timedelta(minutes=LEASE_MINUTES),
			"lease_token": lease_token,
			"next_retry_at": None,
			"status": "Running",
		},
	)
	return frappe.get_doc(RUN_DOCTYPE, run.name), lease_token


def assert_run_lease(run_name: str, lease_token: str) -> Any:
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	if str(run.get("status") or "") != "Running" or not secrets.compare_digest(
		str(run.get("lease_token") or ""), str(lease_token or "")
	):
		raise RuntimeError("Batch run lease is no longer authoritative.")
	return run


def heartbeat(run_name: str, lease_token: str) -> None:
	assert_run_lease(run_name, lease_token)
	now = now_datetime()
	set_run_values(
		run_name,
		{
			"heartbeat_at": now,
			"lease_expires_at": now + timedelta(minutes=LEASE_MINUTES),
		},
	)


def set_run_values(run_name: str, values: dict[str, Any]) -> None:
	unknown = set(values) - _MUTABLE_RUN_FIELDS
	if unknown:
		raise ValueError(f"Unsupported batch-run state fields: {', '.join(sorted(unknown))}.")
	frappe.db.set_value(RUN_DOCTYPE, run_name, values, update_modified=True)


def enqueue_run(run_name: str, *, enqueue_after_commit: bool = True) -> None:
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	if str(run.get("status") or "") in _TERMINAL_STATUSES:
		return
	task_path = str(run.get("task_path") or "")
	if task_path not in _TASK_ALLOWLIST:
		raise ValueError("Stored batch recovery task is not allowlisted.")
	try:
		args = json.loads(str(run.get("task_args_json") or "{}"))
	except ValueError as exc:
		raise ValueError("Stored batch recovery arguments are invalid.") from exc
	if not isinstance(args, dict) or set(args) - {"run_name"}:
		raise ValueError("Stored batch recovery arguments exceed the governed contract.")
	args["run_name"] = run.name
	sequence = int(run.get("continuation_sequence") or 0) + 1
	set_run_values(
		run.name,
		{
			"continuation_sequence": sequence,
			"lease_expires_at": None,
			"lease_token": None,
			"status": "Pending",
		},
	)
	frappe.enqueue(
		task_path,
		queue="long",
		enqueue_after_commit=enqueue_after_commit,
		job_id=f"ione-qms:batch:{run.name}:{sequence}",
		deduplicate=True,
		**args,
	)


def schedule_retry(
	run_name: str,
	error: Exception | str,
	*,
	failure_work_item: str | None = None,
	force_dead_letter: bool = False,
) -> str:
	"""Persist a bounded retry without moving any source/work cursor."""
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	attempt = int(run.get("attempt_count") or 0) + 1
	max_attempts = max(int(run.get("max_attempts") or MAX_RUN_ATTEMPTS), 1)
	dead_letter = force_dead_letter or attempt >= max_attempts
	values: dict[str, Any] = {
		"attempt_count": attempt,
		"failure_work_item": failure_work_item,
		"heartbeat_at": now_datetime(),
		"last_error_code": _error_code(error),
		"lease_expires_at": None,
		"lease_token": None,
		"status": "Dead Letter" if dead_letter else "Retry Waiting",
	}
	if dead_letter:
		values["next_retry_at"] = None
	else:
		values["next_retry_at"] = now_datetime() + timedelta(seconds=min(30 * (2 ** (attempt - 1)), 3_600))
	set_run_values(run.name, values)
	return str(values["status"])


def complete_run(run_name: str, lease_token: str) -> None:
	run = assert_run_lease(run_name, lease_token)
	incomplete = frappe.db.count(
		WORK_DOCTYPE,
		{"batch_run": run_name, "status": ["!=", "Succeeded"]},
	)
	if incomplete:
		raise RuntimeError("Batch run cannot complete while durable work remains unfinished.")
	work_receipt_hash = EMPTY_MANIFEST_HASH
	work_receipt_count = 0
	cursor = ""
	while True:
		rows = frappe.get_all(
			WORK_DOCTYPE,
			filters={
				"batch_run": run_name,
				"name": [">", cursor] if cursor else ["!=", ""],
			},
			fields=["name", "item_key", "payload_hash", "result_reference"],
			order_by="name asc",
			limit_page_length=1_000,
		)
		if not rows:
			break
		work_receipt_hash = advance_manifest_hash(
			work_receipt_hash,
			[
				digest(
					{
						"item_key": row.get("item_key"),
						"payload_hash": row.get("payload_hash"),
						"result_reference": row.get("result_reference"),
						"version": "ione-batch-work-receipt-v1",
					}
				)
				for row in rows
			],
		)
		work_receipt_count += len(rows)
		cursor = str(rows[-1].get("name") or "")
	if str(run.get("batch_type") or "") == "Indicator":
		# Indicator results become analytics-visible only when their owning run
		# becomes Completed. Use the reader's source barrier for that transition.
		advance_source_epoch("IONE Indicator Result")
	set_run_values(
		run_name,
		{
			"completed_at": now_datetime(),
			"heartbeat_at": now_datetime(),
			"lease_expires_at": None,
			"lease_token": None,
			"phase": "Complete",
			"status": "Completed",
			"work_receipt_count": work_receipt_count,
			"work_receipt_hash": work_receipt_hash,
		},
	)


def supersede_run(run_name: str, lease_token: str, reason: str) -> None:
	assert_run_lease(run_name, lease_token)
	set_run_values(
		run_name,
		{
			"heartbeat_at": now_datetime(),
			"last_error_code": _error_code(reason),
			"lease_expires_at": None,
			"lease_token": None,
			"phase": "Snapshot Drift",
			"status": "Superseded",
		},
	)


def ensure_work_item(
	run_name: str,
	*,
	payload: dict[str, Any],
	sequence: int,
) -> Any:
	payload_json = canonical_json(payload)
	payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
	item_key = digest(
		{
			"batch_run": run_name,
			"payload_hash": payload_hash,
			"version": "ione-batch-work-v1",
		}
	)
	if frappe.db.exists(WORK_DOCTYPE, item_key):
		return frappe.get_doc(WORK_DOCTYPE, item_key)
	doc = frappe.get_doc(
		{
			"doctype": WORK_DOCTYPE,
			"item_key": item_key,
			"batch_run": run_name,
			"sequence": max(int(sequence or 0), 0),
			"payload_json": payload_json,
			"payload_hash": payload_hash,
			"status": "Pending",
			"attempt_count": 0,
			"max_attempts": MAX_WORK_ATTEMPTS,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return frappe.get_doc(WORK_DOCTYPE, item_key)
	return doc


def pending_work_items(run_name: str, *, limit: int) -> list[Any]:
	"""Return due work using SQL so NULL/empty retry timestamps are unambiguous."""
	target = min(max(int(limit or 1), 1), 1_000)
	return frappe.db.sql(
		"select name, item_key, payload_json, payload_hash, attempt_count, "
		"max_attempts, status from `tabIONE Batch Work Item` "
		"where batch_run = %s and status in ('Pending', 'Retry Waiting') "
		"and (next_retry_at is null or next_retry_at = '' or next_retry_at <= %s) "
		"order by sequence asc, name asc limit %s",
		(run_name, now_datetime(), target),
		as_dict=True,
	)


def mark_work_succeeded(item_name: str, *, result_reference: str | None = None) -> None:
	item = frappe.get_doc(WORK_DOCTYPE, item_name)
	if str(item.get("status") or "") == "Succeeded":
		return
	set_work_values(
		item.name,
		{
			"completed_at": now_datetime(),
			"last_error_code": None,
			"next_retry_at": None,
			"result_reference": result_reference,
			"status": "Succeeded",
		},
	)
	run_name = str(item.get("batch_run") or "")
	completed = frappe.db.count(WORK_DOCTYPE, {"batch_run": run_name, "status": "Succeeded"})
	set_run_values(run_name, {"work_completed": completed})


def record_completed_work_item(
	run_name: str,
	*,
	payload: dict[str, Any],
	sequence: int,
	result_reference: str,
) -> Any:
	"""Persist an idempotent success receipt for work completed in this transaction."""
	item = ensure_work_item(run_name, payload=payload, sequence=sequence)
	if str(item.get("status") or "") != "Succeeded" or str(item.get("result_reference") or "") != str(
		result_reference or ""
	):
		set_work_values(
			item.name,
			{
				"completed_at": now_datetime(),
				"last_error_code": None,
				"next_retry_at": None,
				"result_reference": result_reference,
				"status": "Succeeded",
			},
		)
	return frappe.get_doc(WORK_DOCTYPE, item.name)


def mark_work_failed(item_name: str, error: Exception | str) -> str:
	item = frappe.get_doc(WORK_DOCTYPE, item_name)
	attempt = int(item.get("attempt_count") or 0) + 1
	max_attempts = max(int(item.get("max_attempts") or MAX_WORK_ATTEMPTS), 1)
	dead_letter = attempt >= max_attempts
	values: dict[str, Any] = {
		"attempt_count": attempt,
		"completed_at": None,
		"last_error_code": _error_code(error),
		"status": "Dead Letter" if dead_letter else "Retry Waiting",
	}
	values["next_retry_at"] = (
		None if dead_letter else now_datetime() + timedelta(seconds=min(30 * (2 ** (attempt - 1)), 3_600))
	)
	set_work_values(item.name, values)
	schedule_retry(
		str(item.get("batch_run") or ""),
		error,
		failure_work_item=item.name,
		force_dead_letter=dead_letter,
	)
	return str(values["status"])


def set_work_values(item_name: str, values: dict[str, Any]) -> None:
	unknown = set(values) - _MUTABLE_WORK_FIELDS
	if unknown:
		raise ValueError(f"Unsupported batch-work state fields: {', '.join(sorted(unknown))}.")
	frappe.db.set_value(WORK_DOCTYPE, item_name, values, update_modified=True)


def recover_due_batch_runs() -> dict[str, int]:
	"""Requeue due retries and recover workers whose DB advisory lock disappeared."""
	from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

	require_post_migrate_runtime_ready("recover durable batch runs")
	summary = {"scanned": 0, "requeued": 0, "dead_lettered": 0}
	now = now_datetime()
	rows = frappe.db.sql(
		"""
		select name, status, lease_expires_at
		from `tabIONE Batch Run`
		where (
			status = 'Pending'
			or (status = 'Retry Waiting' and next_retry_at <= %s)
			or (status = 'Running' and lease_expires_at <= %s)
		)
		order by coalesce(next_retry_at, lease_expires_at, creation) asc, name asc
		limit %s
		""",
		(now, now, MAX_RECOVERY_ROWS),
		as_dict=True,
	)
	for row in rows:
		summary["scanned"] += 1
		try:
			with frappe.db.advisory_lock(f"ione-qms:batch-run:{row.name}", timeout=0):
				current = frappe.get_doc(RUN_DOCTYPE, row.name)
				status = str(current.get("status") or "")
				if status == "Running":
					new_status = schedule_retry(current.name, "STALE_WORKER_LEASE")
					if new_status == "Dead Letter":
						summary["dead_lettered"] += 1
						continue
				elif status == "Retry Waiting":
					if (
						current.get("next_retry_at")
						and get_datetime(current.get("next_retry_at")) > now_datetime()
					):
						continue
				elif status != "Pending":
					continue
				enqueue_run(current.name, enqueue_after_commit=True)
				summary["requeued"] += 1
		except QueryTimeoutError:
			# A worker still owns the database lock; a stale wall-clock lease
			# alone never authorizes a concurrent replacement.
			continue
	return summary


@frappe.whitelist(methods=["POST"])
def recover_dead_letter(run_name: str, reason: str) -> dict[str, Any]:
	"""Human-authorized replay of a dead-letter run and its failed work item."""
	user = str(frappe.session.user or "")
	roles = set(frappe.get_roles(user))
	if user in {"", "Guest", "Administrator"} or "IONE Agent Service" in roles or "Guest" in roles:
		frappe.throw(
			"Batch dead-letter recovery requires a named human administrator.",
			frappe.PermissionError,
		)
	if not roles.intersection({"System Manager", "IONE QC Administrator"}):
		frappe.throw("Batch dead-letter recovery requires an administrator role.", frappe.PermissionError)
	reason = str(reason or "").strip()
	if len(reason) < 20 or len(reason) > 500:
		raise ValueError("Batch dead-letter recovery reason must contain 20-500 characters.")
	with frappe.db.advisory_lock(f"ione-qms:batch-run:{run_name}", timeout=5):
		run = frappe.get_doc(RUN_DOCTYPE, run_name)
		if str(run.get("status") or "") != "Dead Letter":
			raise ValueError("Only a dead-letter batch run can be manually recovered.")
		recovery_sequence = int(run.get("manual_recovery_count") or 0) + 1
		requested_at = now_datetime()
		reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
		receipt_key = digest(
			{
				"batch_run": run.name,
				"recovery_sequence": recovery_sequence,
				"reason_hash": reason_hash,
				"requested_at": requested_at,
				"requested_by": user,
				"version": "ione-batch-recovery-receipt-v1",
			}
		)
		frappe.get_doc(
			{
				"doctype": RECOVERY_DOCTYPE,
				"receipt_key": receipt_key,
				"batch_run": run.name,
				"recovery_sequence": recovery_sequence,
				"prior_status": "Dead Letter",
				"reason": reason,
				"reason_hash": reason_hash,
				"requested_by": user,
				"requested_at": requested_at,
			}
		).insert(ignore_permissions=True)
		failed_item = str(run.get("failure_work_item") or "")
		if failed_item:
			item = frappe.get_doc(WORK_DOCTYPE, failed_item)
			if str(item.get("status") or "") == "Dead Letter":
				set_work_values(
					item.name,
					{
						"attempt_count": 0,
						"completed_at": None,
						"last_error_code": None,
						"next_retry_at": None,
						"status": "Pending",
					},
				)
		set_run_values(
			run.name,
			{
				"attempt_count": 0,
				"failure_work_item": None,
				"last_error_code": None,
				"lease_expires_at": None,
				"lease_token": None,
				"next_retry_at": None,
				"recovered_at": requested_at,
				"recovered_by": user,
				"manual_recovery_count": recovery_sequence,
				"recovery_reason": reason,
				"status": "Pending",
			},
		)
		enqueue_run(run.name, enqueue_after_commit=True)
	return {"run": run_name, "status": "Pending"}


def validate_batch_run(doc, method: str | None = None) -> None:
	del method
	run_key = _validated_digest(str(doc.get("run_key") or ""), "batch run key")
	if str(doc.get("name") or doc.get("run_key") or "") != str(doc.get("run_key") or ""):
		raise ValueError("Batch run name must equal its deterministic run key.")
	expected_run_key = digest(
		{
			"batch_type": doc.get("batch_type"),
			"generation": max(int(doc.get("recovery_generation") or 1), 1),
			"period_end": str(doc.get("period_end")),
			"period_start": str(doc.get("period_start")),
			"subject_key": doc.get("subject_key"),
			"version": "ione-batch-run-v1",
		}
	)
	if not secrets.compare_digest(run_key, expected_run_key):
		raise ValueError("Batch run key is not bound to its immutable identity.")
	if str(doc.get("task_path") or "") not in _TASK_ALLOWLIST:
		raise ValueError("Batch recovery task is not allowlisted.")
	try:
		args = json.loads(str(doc.get("task_args_json") or "{}"))
	except ValueError as exc:
		raise ValueError("Batch recovery arguments are invalid.") from exc
	if not isinstance(args, dict) or set(args) != {"run_name"}:
		raise ValueError("Batch recovery arguments must contain only the bound run identity.")
	if str(args.get("run_name") or "") != str(doc.get("run_key") or ""):
		raise ValueError("Batch recovery arguments are not bound to this run.")
	previous = doc.get_doc_before_save()
	if previous is not None:
		_protect_fields(doc, previous, set(doc.as_dict()) - _MUTABLE_RUN_FIELDS)


def validate_batch_work_item(doc, method: str | None = None) -> None:
	del method
	item_key = _validated_digest(str(doc.get("item_key") or ""), "batch work-item key")
	_validated_digest(str(doc.get("payload_hash") or ""), "batch work-item payload hash")
	try:
		payload = json.loads(str(doc.get("payload_json") or ""))
	except ValueError as exc:
		raise ValueError("Batch work-item payload is invalid.") from exc
	if not isinstance(payload, dict):
		raise ValueError("Batch work-item payload must be a JSON object.")
	observed = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
	if not secrets.compare_digest(observed, str(doc.get("payload_hash") or "")):
		raise ValueError("Batch work-item payload hash verification failed.")
	expected_item_key = digest(
		{
			"batch_run": doc.get("batch_run"),
			"payload_hash": doc.get("payload_hash"),
			"version": "ione-batch-work-v1",
		}
	)
	if not secrets.compare_digest(item_key, expected_item_key):
		raise ValueError("Batch work-item key is not bound to its immutable payload.")
	previous = doc.get_doc_before_save()
	if previous is not None:
		_protect_fields(doc, previous, set(doc.as_dict()) - _MUTABLE_WORK_FIELDS)


def validate_batch_recovery_receipt(doc, method: str | None = None) -> None:
	del method
	receipt_key = _validated_digest(str(doc.get("receipt_key") or ""), "batch recovery receipt key")
	_validated_digest(str(doc.get("reason_hash") or ""), "batch recovery reason hash")
	if hashlib.sha256(str(doc.get("reason") or "").encode("utf-8")).hexdigest() != str(
		doc.get("reason_hash") or ""
	):
		raise ValueError("Batch recovery receipt reason hash verification failed.")
	if int(doc.get("recovery_sequence") or 0) < 1:
		raise ValueError("Batch recovery receipt sequence must be positive.")
	expected_receipt_key = digest(
		{
			"batch_run": doc.get("batch_run"),
			"recovery_sequence": int(doc.get("recovery_sequence") or 0),
			"reason_hash": doc.get("reason_hash"),
			"requested_at": doc.get("requested_at"),
			"requested_by": doc.get("requested_by"),
			"version": "ione-batch-recovery-receipt-v1",
		}
	)
	if not secrets.compare_digest(receipt_key, expected_receipt_key):
		raise ValueError("Batch recovery receipt key is not bound to its immutable evidence.")
	if doc.get_doc_before_save() is not None:
		raise ValueError("Batch recovery receipts are append-only.")


def validate_batch_source_epoch(doc, method: str | None = None) -> None:
	del method
	if str(doc.get("source_doctype") or "") not in SOURCE_EPOCH_DOCTYPES:
		raise ValueError("Batch source epoch is not bound to a governed source DocType.")
	if int(doc.get("source_epoch") or 0) < 0:
		raise ValueError("Batch source epoch cannot be negative.")
	if doc.get_doc_before_save() is not None:
		raise ValueError("Batch source epochs may only advance through the write barrier.")


def prevent_batch_record_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Durable batch audit records cannot be deleted.", frappe.PermissionError)


def _protect_fields(doc, previous, protected_fields: set[str]) -> None:
	for fieldname in protected_fields:
		if fieldname.startswith("_") or fieldname in {
			"docstatus",
			"idx",
			"modified",
			"modified_by",
		}:
			continue
		if doc.get(fieldname) != previous.get(fieldname):
			raise ValueError(f"Durable batch field {fieldname} is immutable.")


def _validated_digest(value: str, label: str) -> str:
	normalized = str(value or "").strip().lower()
	if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
		raise ValueError(f"{label} must be a SHA-256 digest.")
	return normalized


def _ensure_source_epoch_row(doctype: str) -> None:
	if doctype not in SOURCE_EPOCH_DOCTYPES:
		raise ValueError("Cannot materialize an ungoverned source epoch.")
	if frappe.db.exists(SOURCE_EPOCH_DOCTYPE, {"source_doctype": doctype}):
		return
	try:
		frappe.get_doc(
			{
				"doctype": SOURCE_EPOCH_DOCTYPE,
				"source_doctype": doctype,
				"source_epoch": 0,
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		if not frappe.db.exists(SOURCE_EPOCH_DOCTYPE, {"source_doctype": doctype}):
			raise


def _error_code(error: Exception | str) -> str:
	if isinstance(error, Exception):
		name = type(error).__name__
	else:
		name = str(error or "UNKNOWN")
	normalized = "".join(character if character.isalnum() or character in "_-" else "_" for character in name)
	return (normalized or "UNKNOWN")[:140]

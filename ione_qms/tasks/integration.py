from __future__ import annotations

import hashlib
import json
from importlib import import_module
from typing import Any

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

from ione_qms.integration.connectors import get_connector, registered_connectors
from ione_qms.integration.service import (
	quarantine_invalid_clinical_event,
	receive_clinical_event,
	reject_retained_message_scope,
	retry_clinical_event_message,
)
from ione_qms.services.integration_config import (
	assert_integration_endpoint_runtime,
	trusted_integration_runtime_updates,
	update_endpoint_runtime_state,
)
from ione_qms.services.projections import materialize_data_reconciliation
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

_MAX_BATCH_SIZE = 1_000
_MAX_ENDPOINTS_PER_RUN = 50
_MAX_RETRIES_PER_RUN = 500


def sync_incremental_data(
	endpoint_name: str | None = None,
	batch_size: int | None = None,
) -> dict[str, int]:
	"""Pull one bounded page per enabled endpoint through reviewed connector classes."""
	require_post_migrate_runtime_ready("run incremental integration")
	_ensure_builtin_connectors()
	limit = _batch_size(batch_size)
	endpoints = _enabled_endpoints(endpoint_name)
	summary = {
		"endpoints": 0,
		"received": 0,
		"succeeded": 0,
		"duplicates": 0,
		"quarantined": 0,
		"failed": 0,
	}
	for endpoint in endpoints:
		failure_type: str | None = None
		try:
			with frappe.db.advisory_lock(
				f"ione-qms:integration:{endpoint.name}",
				timeout=0,
			):
				endpoint_limit = min(
					limit,
					max(int(endpoint.get("batch_size") or limit), 1),
					_MAX_BATCH_SIZE,
				)
				result = _sync_endpoint(endpoint, endpoint_limit)
		except frappe.QueryTimeoutError:
			continue
		except Exception as exc:
			failure_type = type(exc).__name__
		if failure_type is not None:
			summary["failed"] += 1
			_log_failure("IONE QMS integration endpoint failed", endpoint.name, failure_type)
			continue
		summary["endpoints"] += 1
		for key in ("received", "succeeded", "duplicates", "quarantined", "failed"):
			summary[key] += result[key]
	return summary


def retry_due_messages(batch_size: int = 200) -> dict[str, int]:
	"""Retry a bounded due batch through the same idempotent ingestion path."""
	require_post_migrate_runtime_ready("retry integration messages")
	limit = min(max(int(batch_size or 200), 1), _MAX_RETRIES_PER_RUN)
	names = frappe.get_all(
		"IONE Integration Message",
		filters={
			"status": "Error",
			"next_retry_at": ["<=", now_datetime()],
		},
		pluck="name",
		order_by="next_retry_at asc, name asc",
		limit_page_length=limit,
	)
	summary = {"due": len(names), "processed": 0, "deferred": 0, "failed": 0, "dead_letter": 0}
	for name in names:
		failure_type: str | None = None
		try:
			outcome = retry_integration_message(name)
		except Exception as exc:
			failure_type = type(exc).__name__
		if failure_type is not None:
			summary["failed"] += 1
			_log_message_failure(name, failure_type)
			continue
		status = str(outcome.get("status") or "")
		if status == "Processed":
			summary["processed"] += 1
		elif status == "Dead Letter":
			summary["dead_letter"] += 1
		elif outcome.get("deferred"):
			summary["deferred"] += 1
		else:
			summary["failed"] += 1
	return summary


def retry_integration_message(message_name: str) -> dict[str, Any]:
	"""Retry one Error message without changing its idempotency identity."""
	require_post_migrate_runtime_ready("retry an integration message")
	message = frappe.get_doc("IONE Integration Message", message_name)
	if message.status not in {"Error", "Received"}:
		return {
			"message": message.name,
			"status": message.status,
			"processed": message.status == "Processed",
			"skipped": True,
		}
	payload = _message_payload(message)
	outcome = retry_clinical_event_message(message.name, payload)
	if outcome.get("scope_rejected") and outcome.get("message") != message.name:
		reject_retained_message_scope(message)
		message.reload()
		return {
			**outcome,
			"message": message.name,
			"status": message.status,
			"quarantined": True,
		}
	return outcome


def replay_dead_letter(
	message_name: str,
	*,
	reason: str,
	requested_by: str,
) -> dict[str, Any]:
	"""Re-open one exact dead letter with an accountable manual audit stamp."""
	require_post_migrate_runtime_ready("replay an integration dead letter")
	reason = str(reason or "").strip()
	if len(reason) < 10 or len(reason) > 1_000:
		frappe.throw("Dead-letter replay reason must contain 10-1,000 characters")
	if not requested_by or requested_by in {"Guest", "Administrator"}:
		frappe.throw("Dead-letter replay requires a named accountable user", frappe.PermissionError)
	with frappe.db.advisory_lock(f"ione-qms:dead-letter-replay:{message_name}", timeout=5):
		message = frappe.get_doc("IONE Integration Message", message_name)
		if message.status != "Dead Letter":
			frappe.throw("Only a Dead Letter integration message can be replayed")
		if str(message.get("error_message") or "").startswith("OversizedPayloadError:"):
			frappe.throw(
				"An oversized payload was not retained and cannot be replayed; "
				"correct the source and send a new source version"
			)
		if str(message.get("error_message") or "").startswith("EndpointScopeViolationError:"):
			frappe.throw(
				"A scope-rejected payload was not retained and cannot be replayed; "
				"correct the source and send a new source version"
			)
		# Validate the retained body before mutating operational state.
		_message_payload(message)
		message.db_set(
			{
				"status": "Error",
				"attempts": 0,
				"next_retry_at": now_datetime(),
				"error_message": "Manual replay requested after operator review",
				"replay_count": int(message.get("replay_count") or 0) + 1,
				"last_replayed_by": requested_by,
				"last_replayed_at": now_datetime(),
				"last_replay_reason": reason,
			},
			update_modified=True,
		)
	return retry_integration_message(message_name)


def reconcile_integrations(
	period_start: str | None = None,
	period_end: str | None = None,
	endpoint_name: str | None = None,
) -> dict[str, int]:
	"""Compare QMS intake to a reviewed source aggregate when one is configured."""
	require_post_migrate_runtime_ready("reconcile integrations")
	_ensure_builtin_connectors()
	end = get_datetime(period_end) if period_end else get_datetime(now_datetime().date())
	start = get_datetime(period_start) if period_start else get_datetime(add_days(end, -1))
	if end <= start:
		frappe.throw("Reconciliation period_end must be after period_start")
	filters: dict[str, Any] = {"enabled": 1}
	if endpoint_name:
		filters["name"] = endpoint_name
	endpoints = frappe.get_all(
		"IONE Integration Endpoint",
		filters=filters,
		fields=["name", "source_system"],
		order_by="name asc",
		limit_page_length=_MAX_ENDPOINTS_PER_RUN,
	)
	summary = {
		"endpoints": 0,
		"matched": 0,
		"mismatch": 0,
		"pending": 0,
		"failed": 0,
	}
	for index, endpoint in enumerate(endpoints):
		lock_key = f"ione-qms:reconciliation:{endpoint.name}:{start.isoformat()}:{end.isoformat()}"
		savepoint = f"ione_reconciliation_endpoint_{index}"
		frappe.db.savepoint(savepoint)
		failure_type: str | None = None
		try:
			with frappe.db.advisory_lock(lock_key, timeout=5):
				status = _reconcile_endpoint(endpoint, start, end)
		except Exception as exc:
			failure_type = type(exc).__name__
		if failure_type is not None:
			frappe.db.rollback(save_point=savepoint)
			# This call intentionally occurs after leaving the active exception context.
			# Frappe's telemetry integration otherwise captures the original exception.
			_log_failure("IONE QMS reconciliation endpoint failed", endpoint.name, failure_type)
			status = "Failed"
		else:
			frappe.db.release_savepoint(savepoint)
		summary["endpoints"] += 1
		summary[status.lower()] += 1
	return summary


def _reconcile_endpoint(endpoint_row, start, end) -> str:
	started_at = now_datetime()
	counts = _ingestion_counts(endpoint_row.name, start, end)
	endpoint = frappe.get_doc("IONE Integration Endpoint", endpoint_row.name)
	assert_integration_endpoint_runtime(endpoint)
	source_snapshot = None
	source_error_type: str | None = None
	try:
		source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
		if not int(source.get("enabled") or 0):
			raise frappe.ValidationError("The reconciliation source system is disabled")
		if not int(source.get("read_only") if source.get("read_only") is not None else 1):
			raise frappe.ValidationError("The reconciliation source system must be read-only")
		connector_key = str(endpoint.get("connector_key") or "").strip()
		if connector_key:
			source_snapshot = get_connector(endpoint).reconcile(start, end)
	except Exception as exc:
		source_error_type = type(exc).__name__
	if source_error_type is not None:
		# Keep this outside the except block so Frappe/Sentry only sees the
		# deliberately sanitized message, not Oracle exception locals.
		_log_failure("IONE QMS source reconciliation failed", endpoint.name, source_error_type)

	if source_error_type is not None:
		status = "Failed"
		source_count = 0
		source_hash = None
		source_truth_verified = False
		source_count_status = (
			f"{source_error_type}: reviewed source aggregate failed; "
			"SQL, binds, credentials, and source data were not logged"
		)
	elif source_snapshot is None:
		status = "Pending"
		source_count = 0
		source_hash = None
		source_truth_verified = False
		source_count_status = "Unavailable until a reviewed source reconciliation query is configured"
	else:
		source_count = source_snapshot.source_count
		source_hash = source_snapshot.source_hash
		source_truth_verified = True
		source_count_status = "Verified by reviewed connector aggregate"
		status = (
			"Matched"
			if source_count == counts["message_count"] == counts["event_count"] and counts["error_count"] == 0
			else "Mismatch"
		)

	details: dict[str, Any] = {
		"basis": "authoritative source aggregate versus QMS messages and materialized events",
		"source_truth_verified": source_truth_verified,
		"source_count_status": source_count_status,
		"period_semantics": "[period_start, period_end)",
		"ingress_difference": counts["message_count"] - counts["event_count"],
	}
	if source_snapshot and source_snapshot.details:
		details["source_query"] = source_snapshot.details
	qms_hash = hashlib.sha256(
		json.dumps(
			{
				"endpoint": endpoint.name,
				"period_start": start.isoformat(),
				"period_end": end.isoformat(),
				**counts,
			},
			sort_keys=True,
			separators=(",", ":"),
		).encode()
	).hexdigest()
	reconciliation_name = materialize_data_reconciliation(
		{
			"source_system": endpoint_row.source_system,
			"endpoint": endpoint.name,
			"period_start": start,
			"period_end": end,
			"status": status,
			"source_count": source_count,
			"message_count": counts["message_count"],
			"event_count": counts["event_count"],
			"execution_count": counts["execution_count"],
			"error_count": counts["error_count"],
			"difference_count": (source_count - counts["event_count"] if source_truth_verified else 0),
			"source_hash": source_hash,
			"qms_hash": qms_hash,
			"started_at": started_at,
			"completed_at": now_datetime(),
			"details_json": json.dumps(
				details,
				ensure_ascii=False,
				sort_keys=True,
				separators=(",", ":"),
				default=str,
			),
		}
	)
	_sync_reconciliation_issue(
		endpoint=endpoint,
		reconciliation_name=reconciliation_name,
		period_start=start,
		period_end=end,
		status=status,
		counts=counts,
		source_count=source_count,
	)
	return status


def _sync_reconciliation_issue(
	*,
	endpoint,
	reconciliation_name: str,
	period_start,
	period_end,
	status: str,
	counts: dict[str, int],
	source_count: int,
) -> None:
	"""Open or resolve one non-clinical DQI for an exact reconciliation window."""
	if status == "Pending":
		return
	issue_key = hashlib.sha256(
		(
			f"Integration Reconciliation|{endpoint.name}|{period_start.isoformat()}|{period_end.isoformat()}"
		).encode()
	).hexdigest()
	name = frappe.db.get_value(
		"IONE Data Quality Issue",
		{"issue_key": issue_key},
		"name",
	)
	if status == "Matched":
		if not name:
			return
		with frappe.db.advisory_lock(f"ione-qms:data-quality-review:{name}", timeout=5):
			doc = frappe.get_doc("IONE Data Quality Issue", name, for_update=True)
			if doc.status in {"Resolved", "Accepted"}:
				return
			doc.update(
				{
					"status": "Resolved",
					"resolution": (
						"Automated reconciliation verified that authoritative source, QMS message, "
						f"and QMS event counts match for reconciliation {reconciliation_name}."
					),
					"resolved_at": now_datetime(),
					"resolved_by": "Administrator",
				}
			)
			doc.save(ignore_permissions=True)
		return

	description = (
		"Reviewed source reconciliation failed; inspect the governed endpoint and "
		f"reconciliation {reconciliation_name}."
		if status == "Failed"
		else (
			"Authoritative source, QMS message, and QMS event counts differ; inspect "
			f"reconciliation {reconciliation_name}."
		)
	)
	values = {
		"issue_key": issue_key,
		"source_system": endpoint.source_system,
		"source_record_type": "Reconciliation Window",
		"source_record_id": endpoint.name,
		"issue_type": "Integration Reconciliation",
		"severity": "High" if status == "Failed" else "Medium",
		"status": "Open",
		"expected_value": (
			f"source_count={source_count}" if status == "Mismatch" else "reviewed source query succeeds"
		),
		"actual_value": (
			"message_count="
			f"{counts['message_count']}, event_count={counts['event_count']}, "
			f"error_count={counts['error_count']}"
		),
		"description": description,
		"detected_at": now_datetime(),
	}
	if not name:
		frappe.get_doc({"doctype": "IONE Data Quality Issue", **values}).insert(ignore_permissions=True)
		return
	with frappe.db.advisory_lock(f"ione-qms:data-quality-review:{name}", timeout=5):
		doc = frappe.get_doc("IONE Data Quality Issue", name, for_update=True)
		if doc.status == "Accepted":
			return
		doc.update(
			{
				**values,
				"resolution": None,
				"resolved_by": None,
				"resolved_at": None,
			}
		)
		doc.save(ignore_permissions=True)


def _ingestion_counts(endpoint: str, start, end) -> dict[str, int]:
	base_filters = [
		["endpoint", "=", endpoint],
		["received_at", ">=", start],
		["received_at", "<", end],
	]
	message_count = int(frappe.db.count("IONE Integration Message", filters=base_filters))
	event_count = int(
		frappe.db.count(
			"IONE Integration Message",
			filters=[
				*base_filters,
				["status", "=", "Processed"],
				["event", "is", "set"],
			],
		)
	)
	error_count = int(
		frappe.db.count(
			"IONE Integration Message",
			filters=[
				*base_filters,
				["status", "in", ["Error", "Dead Letter"]],
			],
		)
	)
	# A fixed aggregate join avoids loading millions of event names into memory.
	query = (
		"select count(q.name) from `tabIONE Integration Message` m "
		"inner join `tabIONE QC Execution` q on q.event = m.event "
		"where m.endpoint = %(endpoint)s "
		"and m.received_at >= %(period_start)s "
		"and m.received_at < %(period_end)s"
	)
	execution_count = int(
		frappe.db.sql(
			query,
			{"endpoint": endpoint, "period_start": start, "period_end": end},
		)[0][0]
		or 0
	)
	return {
		"message_count": message_count,
		"event_count": event_count,
		"execution_count": execution_count,
		"error_count": error_count,
	}


def _message_payload(message) -> dict[str, Any]:
	try:
		payload = json.loads(str(message.get("payload_json") or ""))
	except ValueError as exc:
		raise frappe.ValidationError(
			"Integration message payload is unavailable or invalid for retry"
		) from exc
	if not isinstance(payload, dict):
		frappe.throw("Integration message payload must be a JSON object")
	canonical = json.dumps(
		payload,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
	actual_hash = hashlib.sha256(canonical.encode()).hexdigest()
	if not message.get("payload_hash") or actual_hash != str(message.payload_hash):
		frappe.throw("Integration message payload hash mismatch; replay is blocked")
	return payload


def _log_message_failure(message_name: str, error_type: str) -> None:
	frappe.log_error(
		title="IONE QMS integration retry failed",
		message=f"{error_type}: retry failed; payload and credentials were not logged.",
		reference_doctype="IONE Integration Message",
		reference_name=message_name,
	)


def _ensure_builtin_connectors() -> None:
	"""Import the fixed, code-reviewed connector module so decorators populate the registry."""
	import_module("ione_qms.integration.builtin_connectors")


def _sync_endpoint(endpoint, batch_size: int) -> dict[str, int]:
	assert_integration_endpoint_runtime(endpoint)
	source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
	if not int(source.get("enabled") or 0):
		raise ValueError(f"Source system {source.name} is disabled.")
	if not int(source.get("read_only") if source.get("read_only") is not None else 1):
		raise ValueError("Incremental clinical connectors must use read-only source systems.")
	if str(endpoint.get("connector_key") or "").strip().lower() not in registered_connectors():
		raise ValueError(
			f"Connector {endpoint.get('connector_key')!r} is not in the reviewed connector allowlist."
		)
	connector = get_connector(endpoint)
	cursor_field = _cursor_field(endpoint.doctype)
	cursor = str(endpoint.get(cursor_field) or "") if cursor_field else None
	job = _start_job(endpoint, cursor, batch_size)
	try:
		items, next_cursor = connector.fetch(cursor or None, batch_size)
		if not isinstance(items, list):
			raise TypeError("A connector fetch result must contain a list of event payloads.")
		if len(items) > batch_size:
			raise ValueError("Connector returned more records than the requested bounded batch.")
		if len(items) >= batch_size and cursor_field and str(next_cursor or "") == str(cursor or ""):
			raise ValueError("Connector filled the requested page without advancing its incremental cursor.")
	except Exception as exc:
		_finish_job(
			job,
			"Failed",
			{
				"received": 0,
				"succeeded": 0,
				"duplicates": 0,
				"quarantined": 0,
				"failed": 1,
			},
			cursor,
			exc,
		)
		raise
	result = {
		"received": len(items),
		"succeeded": 0,
		"duplicates": 0,
		"quarantined": 0,
		"failed": 0,
	}
	for item in items:
		failure_type: str | None = None
		try:
			outcome = (
				receive_clinical_event(endpoint.name, item)
				if isinstance(item, dict)
				else quarantine_invalid_clinical_event(
					endpoint.name,
					item,
					failure_type="InvalidConnectorRecord",
				)
			)
		except Exception as exc:
			failure_type = type(exc).__name__
		if failure_type is not None:
			# Discard any partial state and release transaction-held identity
			# locks before the next bounded connector item is attempted.
			frappe.db.rollback()
			result["failed"] += 1
			_log_failure("IONE QMS integration record failed", endpoint.name, failure_type)
			continue
		# Each event is an idempotent durable receipt.  Committing here bounds
		# session locks and crash loss; the source cursor advances only after the
		# complete page has no failures, so a crash safely replays duplicates.
		frappe.db.commit()
		if str(outcome.get("status") or "") == "Processed":
			result["succeeded"] += 1
			result["duplicates"] += int(bool(outcome.get("duplicate")))
		elif str(outcome.get("status") or "") == "Dead Letter" and outcome.get("quarantined"):
			result["quarantined"] += 1
		else:
			result["failed"] += 1
	if result["failed"] == 0:
		advance_values: dict[str, Any] = {"last_sync_at": now_datetime()}
		if cursor_field and next_cursor is not None:
			advance_values[cursor_field] = str(next_cursor)
		update_endpoint_runtime_state(endpoint, advance_values)
		_finish_job(
			job,
			"Partial" if result["quarantined"] else "Completed",
			result,
			next_cursor,
		)
	else:
		_finish_job(job, "Partial", result, cursor)
	return result


def _start_job(endpoint, cursor: str | None, batch_size: int):
	doctype = "IONE Integration Job"
	if not frappe.db.exists("DocType", doctype):
		return None
	values = _supported_values(
		doctype,
		{
			"endpoint": endpoint.name,
			"source_system": endpoint.source_system,
			"job_type": _select_value(doctype, "job_type", ("Incremental Sync", "Pull", "Scheduled")),
			"status": _select_value(doctype, "status", ("Running", "Started", "Pending")),
			"cursor_before_hash": _cursor_fingerprint(cursor),
			"started_at": now_datetime(),
			"requested_batch_size": batch_size,
		},
	)
	doc = frappe.get_doc({"doctype": doctype, **values})
	with trusted_integration_runtime_updates():
		doc.insert(ignore_permissions=True)
	return doc


def _finish_job(
	job,
	status: str,
	result: dict[str, int],
	cursor: str | None,
	exc: Exception | None = None,
) -> None:
	if job is None:
		return
	values = _supported_values(
		job.doctype,
		{
			"status": _select_value(
				job.doctype,
				"status",
				(status, "Completed" if status == "Completed" else "Failed"),
			),
			"cursor_after_hash": _cursor_fingerprint(cursor),
			"received_count": result["received"],
			"success_count": result["succeeded"],
			"duplicate_count": result["duplicates"],
			"quarantined_count": result.get("quarantined", 0),
			"error_count": result["failed"] + result.get("quarantined", 0),
			"completed_at": now_datetime(),
			"error_message": f"{type(exc).__name__}: connector operation failed" if exc else None,
		},
	)
	job.update(values)
	with trusted_integration_runtime_updates():
		job.flags.ignore_permissions = True
		job.save()


def _enabled_endpoints(endpoint_name: str | None) -> list[Any]:
	doctype = "IONE Integration Endpoint"
	filters: dict[str, Any] = {"enabled": 1}
	if endpoint_name:
		filters["name"] = endpoint_name
	if _has_field(doctype, "direction"):
		filters["direction"] = ["in", ["Pull", "Bidirectional", "Inbound", "Read Only"]]
	order_by = "name asc"
	if _has_field(doctype, "last_attempt_at"):
		order_by = "last_attempt_at asc, name asc"
	elif _has_field(doctype, "last_sync_at"):
		order_by = "last_sync_at asc, name asc"
	names = frappe.get_all(
		doctype,
		filters=filters,
		pluck="name",
		order_by=order_by,
		limit_page_length=_MAX_ENDPOINTS_PER_RUN,
	)
	now = now_datetime()
	endpoints = []
	for name in names:
		endpoint = frappe.get_doc(doctype, name)
		update_endpoint_runtime_state(endpoint, {"last_attempt_at": now})
		endpoints.append(endpoint)
	return endpoints


def _batch_size(value: int | None) -> int:
	if value is None and frappe.db.exists("DocType", "IONE Integration Settings"):
		value = frappe.db.get_single_value("IONE Integration Settings", "default_batch_size")
	return min(max(int(value or 500), 1), _MAX_BATCH_SIZE)


def _cursor_field(doctype: str) -> str | None:
	for fieldname in ("incremental_cursor", "last_cursor", "cursor"):
		if _has_field(doctype, fieldname):
			return fieldname
	return None


def _cursor_fingerprint(cursor: str | None) -> str | None:
	if cursor in (None, ""):
		return None
	return hashlib.sha256(str(cursor).encode()).hexdigest()


def _select_value(doctype: str, fieldname: str, candidates: tuple[str, ...]) -> str:
	field = frappe.get_meta(doctype).get_field(fieldname)
	if not field or field.fieldtype != "Select" or not field.options:
		return candidates[0]
	options = {option.strip() for option in str(field.options).splitlines() if option.strip()}
	return next((candidate for candidate in candidates if candidate in options), candidates[0])


def _supported_values(doctype: str, values: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	return {fieldname: value for fieldname, value in values.items() if meta.has_field(fieldname)}


def _has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _log_failure(title: str, reference_name: str, error_type: str) -> None:
	frappe.log_error(
		title=title,
		message=f"{error_type}: connector operation failed; payload was not logged.",
		reference_doctype="IONE Integration Endpoint",
		reference_name=reference_name,
	)

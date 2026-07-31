from __future__ import annotations

import uuid
from typing import Any

import frappe

from ione_qms.services.identity_keys import backfill_site_identity_keys
from ione_qms.services.indicators import quarantine_legacy_indicator_receipts
from ione_qms.services.migration_state import (
	SCHEMA_REVISION,
	complete_migration,
	current_migration_completion_receipt,
	initialize_migration_state,
	mark_migration_blocked,
	mark_migration_failed,
	mark_migration_pending,
	mark_migration_running,
	migration_is_complete,
	record_migration_batch,
)
from ione_qms.services.scope_hierarchy import audit_scope_integrity
from ione_qms.services.versions import (
	backfill_active_version_keys,
	backfill_definition_lineage,
)
from ione_qms.setup.install import backfill_finding_due_dates

MAX_BACKFILL_BATCH_SIZE = 1_000
MAX_PASSES_PER_JOB = 5


def run_post_migrate_backfills(
	batch_size: int = 200,
	max_passes: int = MAX_PASSES_PER_JOB,
) -> dict[str, Any]:
	"""Converge bounded repair batches and issue a verifiable completion receipt."""
	limit = min(max(int(batch_size or 200), 1), MAX_BACKFILL_BATCH_SIZE)
	pass_limit = min(max(int(max_passes or MAX_PASSES_PER_JOB), 1), 20)
	lock_acquired = False
	try:
		with frappe.db.advisory_lock(
			f"ione-qms:post-migrate-backfills:{SCHEMA_REVISION}",
			timeout=0,
		):
			lock_acquired = True
			if not initialize_migration_state():
				return {
					"status": "Schema Unavailable",
					"schema_revision": SCHEMA_REVISION,
				}
			if migration_is_complete():
				scope_summary = audit_scope_integrity(batch_size=limit)
				frappe.db.commit()
				return {
					"status": "Completed",
					"schema_revision": SCHEMA_REVISION,
					"completion_receipt": current_migration_completion_receipt(),
					"scope_integrity": scope_summary,
				}
			mark_migration_running()
			frappe.db.commit()

			last_summary: dict[str, Any] = {}
			for pass_number in range(1, pass_limit + 1):
				last_summary = _run_repair_pass(
					limit,
					include_scope_audit=pass_number == 1,
				)
				record_migration_batch(last_summary)
				blocked = _blocked_count(last_summary)
				has_more = _has_more(last_summary)
				if blocked:
					mark_migration_blocked()
					frappe.db.commit()
					return {
						"status": "Blocked",
						"schema_revision": SCHEMA_REVISION,
						"blocked": blocked,
						"passes": pass_number,
						"summary": last_summary,
					}
				if not has_more:
					receipt = complete_migration()
					frappe.db.commit()
					return {
						"status": "Completed",
						"schema_revision": SCHEMA_REVISION,
						"completion_receipt": receipt,
						"passes": pass_number,
						"summary": last_summary,
					}
				# Bound every transaction independently. The advisory lock is
				# connection-scoped and therefore remains held across commits.
				frappe.db.commit()

			sequence = mark_migration_pending()
			_enqueue_continuation(sequence, limit, pass_limit)
			frappe.db.commit()
			return {
				"status": "Pending",
				"schema_revision": SCHEMA_REVISION,
				"continuation_sequence": sequence,
				"passes": pass_limit,
				"summary": last_summary,
			}
	except Exception as exc:
		if not lock_acquired and exc.__class__.__name__ in {
			"QueryTimeoutError",
			"LockTimeoutError",
		}:
			return {
				"status": "Already Running",
				"schema_revision": SCHEMA_REVISION,
			}
		frappe.db.rollback()
		try:
			mark_migration_failed(_migration_failure_code(exc))
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
		raise


def _run_repair_pass(limit: int, *, include_scope_audit: bool) -> dict[str, Any]:
	version_summary = backfill_active_version_keys(batch_size=limit)
	summary = {
		"definition_lineage": backfill_definition_lineage(batch_size=limit),
		"integration_tenant_boundary": enforce_integration_tenant_fail_closed(limit),
		"identity_surrogates": backfill_site_identity_keys(batch_size=limit),
		"finding_due_dates": _finding_due_date_summary(limit),
		"version_keys": version_summary,
		"rectification_keys": _backfill_active_key(
			"IONE QC Rectification",
			"finding",
			terminal_statuses={"Closed", "Cancelled", "Rejected"},
			batch_size=limit,
		),
		"verification_keys": _backfill_active_key(
			"IONE QC Verification",
			"rectification",
			terminal_statuses={"Verified", "Rejected"},
			batch_size=limit,
		),
		"indicator_receipts": quarantine_legacy_indicator_receipts(batch_size=limit),
	}
	if include_scope_audit:
		# Scope audit is an ongoing cursor-based safety control, not a finite
		# release migration. Its has_more/quarantine telemetry cannot prevent
		# finite repair convergence or completion-receipt issuance.
		summary["scope_integrity"] = audit_scope_integrity(batch_size=limit)
	return summary


def _finding_due_date_summary(limit: int) -> dict[str, int | bool]:
	updated = backfill_finding_due_dates(limit)
	return {"updated": updated, "has_more": updated >= limit, "blocked": 0}


def _has_more(summary: dict[str, Any]) -> bool:
	return any(
		bool(values.get("has_more"))
		for component, values in summary.items()
		if component != "scope_integrity" and isinstance(values, dict)
	)


def _blocked_count(summary: dict[str, Any]) -> int:
	return sum(
		int(values.get("blocked") or 0)
		for component, values in summary.items()
		if component != "scope_integrity" and isinstance(values, dict)
	)


def _enqueue_continuation(sequence: int, batch_size: int, max_passes: int) -> None:
	frappe.enqueue(
		"ione_qms.tasks.migrations.run_post_migrate_backfills",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:post-migrate:{SCHEMA_REVISION}:{sequence}",
		deduplicate=True,
		batch_size=batch_size,
		max_passes=max_passes,
	)


def _migration_failure_code(exc: Exception) -> str:
	name = "".join(character if character.isalnum() else "_" for character in type(exc).__name__)
	return f"MIGRATION_{name.upper()}"[:64]


def enforce_integration_tenant_fail_closed(batch_size: int = 200) -> dict[str, int | bool]:
	"""Never infer clinical scope: mint stable namespaces, disable every unscoped legacy source."""
	limit = min(max(int(batch_size or 200), 1), MAX_BACKFILL_BATCH_SIZE)
	if not frappe.db.exists("DocType", "IONE Integration Allowed Scope"):
		return {"namespaced": 0, "sources_disabled": 0, "endpoints_disabled": 0, "has_more": False}
	namespaced = 0
	sources_disabled = 0
	endpoints_disabled = 0
	sources = frappe.db.sql(
		"""
		select name, enabled
		from `tabIONE Source System`
		where coalesce(trim(identity_namespace), '') = ''
		order by name asc
		limit %s
		""",
		(limit,),
		as_dict=True,
	)
	for source in sources:
		frappe.db.set_value(
			"IONE Source System",
			source.name,
			"identity_namespace",
			uuid.uuid4().hex,
			update_modified=False,
		)
		namespaced += 1
		if int(source.get("enabled") or 0):
			frappe.db.set_value(
				"IONE Source System",
				source.name,
				"enabled",
				0,
				update_modified=False,
			)
			sources_disabled += 1
	enabled_sources = frappe.db.sql(
		"""
		select source.name
		from `tabIONE Source System` source
		where source.enabled = 1
		and not exists (
		select 1
		from `tabIONE Integration Allowed Scope` allowed
		where allowed.parenttype = 'IONE Source System'
		and allowed.parent = source.name
		and allowed.parentfield = 'allowed_scopes'
		)
		order by source.name asc
		limit %s
		""",
		(limit,),
		as_dict=True,
	)
	for source in enabled_sources:
		frappe.db.set_value(
			"IONE Source System",
			source.name,
			"enabled",
			0,
			update_modified=False,
		)
		sources_disabled += 1
	enabled_endpoints = frappe.db.sql(
		"""
		select endpoint.name,
		case
		when coalesce(source.enabled, 0) = 0 then 'SOURCE_DISABLED'
		when coalesce(trim(source.identity_namespace), '') = '' then 'SOURCE_NAMESPACE_MISSING'
		when not exists (
			select 1
			from `tabIONE Integration Allowed Scope` allowed
			where allowed.parenttype = 'IONE Integration Endpoint'
			and allowed.parent = endpoint.name
			and allowed.parentfield = 'allowed_scopes'
		) then 'ENDPOINT_SCOPE_MISSING'
		when coalesce(trim(endpoint.mapping), '') <> '' then 'LEGACY_INLINE_MAPPING_PRESENT'
		when coalesce(endpoint.connector_options, '')
			regexp '"master_data"[[:space:]]*:'
			then 'LEGACY_INLINE_MASTER_DATA_PRESENT'
		when coalesce(trim(endpoint.mapping_record), '') = '' then 'MAPPING_RECORD_MISSING'
		when mapping.name is null then 'MAPPING_RECORD_MISSING'
		when mapping.source_system <> endpoint.source_system then 'MAPPING_SOURCE_MISMATCH'
		when mapping.endpoint <> endpoint.name then 'MAPPING_ENDPOINT_MISMATCH'
		when mapping.status <> 'Active' then 'MAPPING_NOT_ACTIVE'
		when mapping.effective_from is null
			or mapping.effective_from > current_date
			or (mapping.effective_to is not null and mapping.effective_to < current_date)
			then 'MAPPING_NOT_EFFECTIVE'
		when coalesce(mapping.mapping_json, '') = ''
			or coalesce(mapping.master_data_json, '') = ''
			or coalesce(mapping.canonical_snapshot, '') = ''
			or coalesce(mapping.mapping_code, '') = ''
			or coalesce(mapping.version, '') = ''
			then 'MAPPING_DEFINITION_INCOMPLETE'
		when json_valid(mapping.mapping_json) = 0
			or json_valid(mapping.master_data_json) = 0
			or json_valid(mapping.canonical_snapshot) = 0
			then 'MAPPING_DEFINITION_INVALID'
		when binary mapping.canonical_snapshot <> binary concat(
			'{"master_data":',
			mapping.master_data_json,
			',"scope":',
			mapping.mapping_json,
			'}'
			) then 'MAPPING_SNAPSHOT_INVALID'
		when coalesce(mapping.checksum, '') not regexp '^[0-9a-f]{64}$'
			or sha2(mapping.canonical_snapshot, 256) <> mapping.checksum
			then 'MAPPING_CHECKSUM_INVALID'
		when coalesce(mapping.mapping_version_key, '') not regexp '^[0-9a-f]{64}$'
			then 'MAPPING_VERSION_IDENTITY_INVALID'
		when sha2(concat(
			'[',
			json_quote(coalesce(mapping.source_system, '')),
			',',
			json_quote(coalesce(mapping.endpoint, '')),
			',',
			json_quote(coalesce(mapping.mapping_code, '')),
			',',
			json_quote(coalesce(mapping.version, '')),
			']'
			), 256) <> mapping.mapping_version_key
			then 'MAPPING_VERSION_IDENTITY_INVALID'
		else 'ENDPOINT_CONFIGURATION_INVALID'
		end as block_reason
		from `tabIONE Integration Endpoint` endpoint
		left join `tabIONE Source System` source
		on source.name = endpoint.source_system
		left join `tabIONE Integration Mapping` mapping
		on mapping.name = endpoint.mapping_record
		where endpoint.enabled = 1
		and (
		coalesce(source.enabled, 0) = 0
		or coalesce(trim(source.identity_namespace), '') = ''
		or not exists (
		select 1
		from `tabIONE Integration Allowed Scope` allowed
		where allowed.parenttype = 'IONE Integration Endpoint'
		and allowed.parent = endpoint.name
		and allowed.parentfield = 'allowed_scopes'
		)
		or coalesce(trim(endpoint.mapping), '') <> ''
		or coalesce(endpoint.connector_options, '')
			regexp '"master_data"[[:space:]]*:'
		or coalesce(trim(endpoint.mapping_record), '') = ''
		or mapping.name is null
		or mapping.source_system <> endpoint.source_system
		or mapping.endpoint <> endpoint.name
		or mapping.status <> 'Active'
		or mapping.effective_from is null
		or mapping.effective_from > current_date
		or (mapping.effective_to is not null and mapping.effective_to < current_date)
		or coalesce(mapping.mapping_json, '') = ''
		or coalesce(mapping.master_data_json, '') = ''
		or coalesce(mapping.canonical_snapshot, '') = ''
		or coalesce(mapping.mapping_code, '') = ''
		or coalesce(mapping.version, '') = ''
		or json_valid(mapping.mapping_json) = 0
		or json_valid(mapping.master_data_json) = 0
		or json_valid(mapping.canonical_snapshot) = 0
		or binary mapping.canonical_snapshot <> binary concat(
			'{"master_data":',
			mapping.master_data_json,
			',"scope":',
			mapping.mapping_json,
			'}'
			)
		or coalesce(mapping.checksum, '') not regexp '^[0-9a-f]{64}$'
		or sha2(mapping.canonical_snapshot, 256) <> mapping.checksum
		or coalesce(mapping.mapping_version_key, '') not regexp '^[0-9a-f]{64}$'
		or sha2(concat(
			'[',
			json_quote(coalesce(mapping.source_system, '')),
			',',
			json_quote(coalesce(mapping.endpoint, '')),
			',',
			json_quote(coalesce(mapping.mapping_code, '')),
			',',
			json_quote(coalesce(mapping.version, '')),
			']'
			), 256) <> mapping.mapping_version_key
		)
		order by endpoint.name asc
		limit %s
		""",
		(limit,),
		as_dict=True,
	)
	for endpoint in enabled_endpoints:
		frappe.db.set_value(
			"IONE Integration Endpoint",
			endpoint.name,
			"enabled",
			0,
			update_modified=False,
		)
		endpoints_disabled += 1
		_log_integration_endpoint_migration_block(endpoint.name, endpoint.block_reason)
	return {
		"namespaced": namespaced,
		"sources_disabled": sources_disabled,
		"endpoints_disabled": endpoints_disabled,
		"has_more": bool(
			len(sources) >= limit or len(enabled_sources) >= limit or len(enabled_endpoints) >= limit
		),
	}


def _log_integration_endpoint_migration_block(endpoint_name: str, reason: str) -> None:
	allowed_reasons = {
		"SOURCE_DISABLED",
		"SOURCE_NAMESPACE_MISSING",
		"ENDPOINT_SCOPE_MISSING",
		"LEGACY_INLINE_MAPPING_PRESENT",
		"LEGACY_INLINE_MASTER_DATA_PRESENT",
		"MAPPING_RECORD_MISSING",
		"MAPPING_SOURCE_MISMATCH",
		"MAPPING_ENDPOINT_MISMATCH",
		"MAPPING_NOT_ACTIVE",
		"MAPPING_NOT_EFFECTIVE",
		"MAPPING_DEFINITION_INCOMPLETE",
		"MAPPING_DEFINITION_INVALID",
		"MAPPING_SNAPSHOT_INVALID",
		"MAPPING_CHECKSUM_INVALID",
		"MAPPING_VERSION_IDENTITY_INVALID",
		"ENDPOINT_CONFIGURATION_INVALID",
	}
	safe_reason = reason if reason in allowed_reasons else "ENDPOINT_CONFIGURATION_INVALID"
	frappe.log_error(
		title="IONE QMS disabled unsafe legacy integration endpoint",
		message=(
			f"{safe_reason}: endpoint was disabled during a bounded fail-closed migration; "
			"mapping content and clinical data were not logged."
		),
		reference_doctype="IONE Integration Endpoint",
		reference_name=endpoint_name,
	)


def _backfill_active_key(
	doctype: str,
	parent_field: str,
	*,
	terminal_statuses: set[str],
	batch_size: int,
) -> dict[str, int | bool]:
	if not frappe.db.exists("DocType", doctype) or not frappe.get_meta(doctype).has_field("active_key"):
		return {"updated": 0, "cleared": 0, "blocked": 0, "has_more": False}
	limit = min(max(int(batch_size), 1), MAX_BACKFILL_BATCH_SIZE)
	cleared_rows = frappe.get_all(
		doctype,
		filters={
			"status": ["in", sorted(terminal_statuses)],
			"active_key": ["is", "set"],
		},
		pluck="name",
		order_by="name asc",
		limit_page_length=limit,
	)
	for name in cleared_rows:
		frappe.db.set_value(doctype, name, "active_key", None, update_modified=False)

	active_rows = frappe.get_all(
		doctype,
		filters={
			"status": ["not in", sorted(terminal_statuses)],
			"active_key": ["is", "not set"],
		},
		fields=["name", parent_field],
		order_by="name asc",
		limit_page_length=limit,
	)
	updated = 0
	blocked = 0
	for row in active_rows:
		parent = str(row.get(parent_field) or "")
		if not parent:
			blocked += 1
			_log_migration_issue(doctype, row.name, "MISSING_ACTIVE_PARENT")
			continue
		duplicates = frappe.get_all(
			doctype,
			filters={
				parent_field: parent,
				"status": ["not in", sorted(terminal_statuses)],
				"name": ["!=", row.name],
			},
			pluck="name",
			order_by="name asc",
			limit_page_length=2,
		)
		if duplicates:
			blocked += 1
			_log_migration_issue(doctype, row.name, "DUPLICATE_ACTIVE_RECORD")
			continue
		try:
			frappe.db.set_value(
				doctype,
				row.name,
				"active_key",
				parent,
				update_modified=False,
			)
		except frappe.DuplicateEntryError, frappe.UniqueValidationError:
			blocked += 1
			_log_migration_issue(doctype, row.name, "ACTIVE_KEY_UNIQUE_CONFLICT")
			continue
		updated += 1
	return {
		"updated": updated,
		"cleared": len(cleared_rows),
		"blocked": blocked,
		"has_more": len(active_rows) >= limit or len(cleared_rows) >= limit,
	}


def _log_migration_issue(doctype: str, name: str, code: str) -> None:
	cache_key = frappe.cache.make_key(f"ione_qms:migration_issue:{doctype}:{name}:{code}")
	if not frappe.cache.set(name=cache_key, value=b"1", ex=86_400, nx=True):
		return
	frappe.log_error(
		title=f"IONE bounded migration requires review: {code}",
		message=(
			f"{doctype} {name} could not be repaired automatically. "
			"No clinical content was copied into this log."
		),
		reference_doctype=doctype,
		reference_name=name,
	)

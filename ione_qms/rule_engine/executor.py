from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from typing import Any

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from ione_qms.ai.release import current_app_commit_sha
from ione_qms.permissions import get_access_context
from ione_qms.rule_engine.evaluator import evaluate_rule_definition
from ione_qms.rule_engine.models import RuleEvaluation, RuleResult
from ione_qms.rule_engine.registry import get_rule
from ione_qms.services.rule_receipts import prepare_signed_receipt
from ione_qms.services.runtime_settings import (
	realtime_rules_enabled,
	require_post_migrate_runtime_ready,
	require_realtime_rules_enabled,
)

RULE_EXECUTOR_CONTRACT = "IONE_RULE_EVALUATOR_V2"


def enqueue_event_rules(doc, method: str | None = None) -> None:
	if not getattr(doc, "name", None) or getattr(doc.flags, "skip_rule_enqueue", False):
		return
	if not realtime_rules_enabled():
		return
	frappe.enqueue(
		"ione_qms.rule_engine.executor.evaluate_event_job",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"ione-qms:event:{doc.name}",
		deduplicate=True,
		event_name=doc.name,
		realtime=True,
	)


def evaluate_event(
	event_name: str,
	rule_codes: list[str] | str | None = None,
	save: bool = True,
) -> list[dict[str, Any]]:
	require_post_migrate_runtime_ready("evaluate clinical quality rules")
	event = frappe.get_doc("IONE Clinical Quality Event", event_name)
	event.check_permission("read")
	if not save:
		return _evaluate_event(event, rule_codes=rule_codes, save=False)
	with frappe.db.advisory_lock(f"ione-qms:event-evaluation:{event.name}", timeout=10):
		event.reload()
		return _evaluate_event(event, rule_codes=rule_codes, save=True)


def evaluate_event_job(
	event_name: str,
	rule_codes: list[str] | str | None = None,
	save: bool = True,
	realtime: bool = False,
) -> list[dict[str, Any]]:
	"""Internal queue entry point; never expose through Frappe RPC."""
	require_post_migrate_runtime_ready("evaluate queued clinical quality rules")
	if realtime:
		require_realtime_rules_enabled()
	event = frappe.get_doc("IONE Clinical Quality Event", event_name)
	if not save:
		return _evaluate_event(event, rule_codes=rule_codes, save=False)
	with frappe.db.advisory_lock(f"ione-qms:event-evaluation:{event.name}", timeout=10):
		event.reload()
		return _evaluate_event(event, rule_codes=rule_codes, save=True)


def _evaluate_event(
	event,
	*,
	rule_codes: list[str] | str | None,
	save: bool,
) -> list[dict[str, Any]]:
	context = _build_context(event)
	rules = _get_published_rules(
		event.event_type,
		rule_codes,
		effective_date=getdate(event.event_time),
	)
	observed_at = now_datetime()
	shadow_rules = (
		_get_shadow_rules(
			event.event_type,
			rule_codes,
			effective_date=getdate(event.event_time),
			observed_at=observed_at,
		)
		if save and str(event.event_type or "") != "ManualEncounterEvaluation"
		else []
	)
	results: list[dict[str, Any]] = []
	has_error = False
	for rule_version in rules:
		started_at = now_datetime()
		timer = time.perf_counter()
		evaluation = execute_rule_version(rule_version, context)
		duration_ms = max(round((time.perf_counter() - timer) * 1000), 0)
		execution_name = None
		if save:
			execution_name = _save_execution(
				event,
				rule_version,
				context,
				evaluation,
				started_at=started_at,
				duration_ms=duration_ms,
			)
			if evaluation.result == RuleResult.ERROR:
				has_error = True
				_upsert_rule_data_quality_issue(
					event,
					rule_version,
					execution_name,
					evaluation,
				)
				notification_failure_type: str | None = None
				try:
					_notify_rule_error(event, rule_version, execution_name)
				except Exception as exc:
					notification_failure_type = type(exc).__name__
				if notification_failure_type is not None:
					frappe.log_error(
						title="IONE QMS rule error notification failed",
						message=(
							f"{notification_failure_type}: notification failed; "
							"clinical payload was not logged."
						),
						reference_doctype="IONE QC Execution",
						reference_name=execution_name,
					)
			elif evaluation.result == RuleResult.INSUFFICIENT_DATA:
				_upsert_rule_data_quality_issue(
					event,
					rule_version,
					execution_name,
					evaluation,
				)
			elif evaluation.result == RuleResult.FAILED and not int(rule_version.get("shadow_mode") or 0):
				action = str(rule_version.get("action") or "Create Finding")
				if action == "Create Finding":
					_create_candidate_finding(event, rule_version, execution_name, evaluation)
				elif action == "Alert Only":
					_notify_rule_alert(event, rule_version, execution_name)
		results.append(
			{
				"rule": rule_version.rule,
				"rule_version": rule_version.name,
				"execution": execution_name,
				"mode": "Published",
				"action": rule_version.get("action") or "Create Finding",
				**evaluation.as_dict(),
			}
		)
	for rule_version in shadow_rules:
		timer = time.perf_counter()
		evaluation_savepoint = f"ione_shadow_eval_{_hash_payload(rule_version.name)[:16]}"
		frappe.db.savepoint(evaluation_savepoint)
		try:
			evaluation = _execute_shadow_rule_version(rule_version, context)
		finally:
			# Shadow evaluation is rollback-only. The immutable receipt is
			# written separately after all plug-in writes have been discarded.
			frappe.db.rollback(save_point=evaluation_savepoint)
			frappe.db.release_savepoint(evaluation_savepoint)
		duration_ms = max(round((time.perf_counter() - timer) * 1000), 0)
		execution_name = None
		shadow_persistence_error = False
		shadow_persistence_error_code = ""
		if save:
			savepoint = f"ione_shadow_{_hash_payload(rule_version.name)[:16]}"
			frappe.db.savepoint(savepoint)
			try:
				execution_name = _save_shadow_execution(
					event,
					rule_version,
					context,
					evaluation,
					observed_at=observed_at,
					completed_at=now_datetime(),
					duration_ms=duration_ms,
				)
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				frappe.db.release_savepoint(savepoint)
				shadow_persistence_error = True
				shadow_persistence_error_code = type(exc).__name__
			else:
				frappe.db.release_savepoint(savepoint)
			if shadow_persistence_error_code:
				frappe.log_error(
					title="IONE QMS shadow receipt persistence failed",
					message=(
						f"{shadow_persistence_error_code}: online shadow receipt was not persisted; "
						"clinical payload was not logged and published-rule processing was unaffected."
					),
					reference_doctype="IONE QC Rule Version",
					reference_name=rule_version.name,
				)
		results.append(
			{
				"rule": rule_version.rule,
				"rule_version": rule_version.name,
				"execution": execution_name,
				"mode": "Shadow",
				"action": "Measure Only",
				"shadow_persistence_error": shadow_persistence_error,
				**evaluation.as_dict(),
			}
		)
	if save:
		frappe.db.set_value(
			event.doctype,
			event.name,
			{
				"processing_status": "Error" if has_error else "Completed",
				"error_message": (
					"One or more deterministic rule executions failed; retry is required."
					if has_error
					else None
				),
			},
			update_modified=False,
		)
	return results


def _execute_shadow_rule_version(rule_version, context: dict[str, Any]) -> RuleEvaluation:
	# Historical Replay and Shadow Run are deliberately narrower than the
	# published execution path.  A Python declaration (or a monkeypatch around a
	# plug-in) cannot prove absence of commits, enqueue calls, or network I/O.
	# Reject before registry lookup/instantiation so no plug-in-controlled code
	# can execute in either production validation mode.
	if str(rule_version.get("rule_type_snapshot") or "") != "Deterministic":
		return RuleEvaluation(
			result=RuleResult.ERROR,
			reason=(
				"Historical Replay and Shadow Run accept only the built-in deterministic expression DSL."
			),
			error="Non-DSL shadow execution blocked",
		)
	return execute_rule_version(rule_version, context)


def execute_rule_version(rule_version, context: dict[str, Any]) -> RuleEvaluation:
	try:
		rule_type = str(rule_version.get("rule_type_snapshot") or "")
		if rule_type not in {"Deterministic", "Python Plugin"}:
			return RuleEvaluation(
				result=RuleResult.ERROR,
				reason="The governed rule-version semantic snapshot is unavailable.",
				error="Rule version snapshot missing",
			)
		if rule_type == "Python Plugin":
			plugin_key = str(rule_version.get("plugin_key") or "")
			rule_class = get_rule(plugin_key)
			if not rule_class:
				return RuleEvaluation(
					result=RuleResult.ERROR,
					reason="The registered clinical rule plug-in was not found.",
					error=f"Unregistered rule plug-in: {plugin_key}",
				)
			return rule_class().evaluate(context)
		condition = _json_object(rule_version.condition_json)
		exclusions = _json_object(rule_version.exclusion_json, empty=None)
		return evaluate_rule_definition(condition, context, exclusions)
	except Exception as exc:
		return RuleEvaluation(
			result=RuleResult.ERROR,
			reason="The deterministic rule could not be evaluated.",
			error=f"{type(exc).__name__}: deterministic evaluation failed",
		)


def _get_published_rules(
	event_type: str,
	rule_codes: list[str] | str | None,
	*,
	effective_date,
):
	query = """
		select rv.*
		from `tabIONE QC Rule Version` rv
		where rv.status in ('Published', 'Retired')
			and rv.lineage_status = 'Verified'
			and (rv.trigger_event = %(event_type)s or rv.trigger_event = '*')
			and rv.effective_from <= %(effective_date)s
			and (rv.effective_to is null or rv.effective_to >= %(effective_date)s)
	"""
	values: dict[str, Any] = {
		"event_type": event_type,
		"effective_date": effective_date,
	}
	normalized = _normalize_rule_codes(rule_codes)
	if normalized:
		query += " and rv.rule_code_snapshot in %(rule_codes)s"
		values["rule_codes"] = normalized
	query += """
		order by case rv.risk_level_snapshot
			when 'Critical' then 4
			when 'High' then 3
			when 'Medium' then 2
			when 'Low' then 1
			else 0
		end desc, rv.rule_code_snapshot asc
	"""
	return frappe.db.sql(query, values=values, as_dict=True)


def _get_shadow_rules(
	event_type: str,
	rule_codes: list[str] | str | None,
	*,
	effective_date,
	observed_at,
):
	"""Select only online shadow versions whose governed observation window is open."""
	query = """
		select rv.*
		from `tabIONE QC Rule Version` rv
		where rv.status = 'Shadow Run'
			and rv.shadow_mode = 1
			and rv.lineage_status = 'Verified'
			and rv.shadow_observation_start <= %(observed_at)s
			and rv.shadow_observation_end >= %(observed_at)s
			and (rv.trigger_event = %(event_type)s or rv.trigger_event = '*')
			and rv.effective_from <= %(effective_date)s
			and (rv.effective_to is null or rv.effective_to >= %(effective_date)s)
	"""
	values: dict[str, Any] = {
		"event_type": event_type,
		"effective_date": effective_date,
		"observed_at": observed_at,
	}
	normalized = _normalize_rule_codes(rule_codes)
	if normalized:
		query += " and rv.rule_code_snapshot in %(rule_codes)s"
		values["rule_codes"] = normalized
	query += """
		order by case rv.risk_level_snapshot
			when 'Critical' then 4
			when 'High' then 3
			when 'Medium' then 2
			when 'Low' then 1
			else 0
		end desc, rv.rule_code_snapshot asc
	"""
	return frappe.db.sql(query, values=values, as_dict=True)


def _save_execution(
	event,
	rule_version,
	context: dict[str, Any],
	evaluation: RuleEvaluation,
	*,
	started_at,
	duration_ms: int,
) -> str:
	context_hash = _hash_payload(context)
	execution_key = _hash_payload(
		{
			"event": event.name,
			"rule_version": rule_version.name,
			"context_hash": context_hash,
			"evaluation_hash": _hash_payload(evaluation.as_dict()),
		}
	)
	existing = frappe.db.get_value(
		"IONE QC Execution",
		{"execution_key": execution_key},
		"name",
	)
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "IONE QC Execution",
			"execution_key": execution_key,
			"event": event.name,
			"rule": rule_version.rule,
			"rule_version": rule_version.name,
			"hospital": event.get("hospital"),
			"campus": event.get("campus"),
			"department": event.get("department"),
			"patient": event.get("patient_index"),
			"encounter": event.get("encounter_index"),
			"result": evaluation.result.value,
			"context_hash": context_hash,
			"evidence_json": json.dumps(
				[item.as_dict() for item in evaluation.evidence],
				ensure_ascii=False,
				default=str,
			),
			"error_message": evaluation.error,
			"started_at": started_at,
			"completed_at": now_datetime(),
			"duration_ms": duration_ms,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value(
			"IONE QC Execution",
			{"execution_key": execution_key},
			"name",
		)
		if existing:
			return existing
		raise
	return doc.name


def _save_shadow_execution(
	event,
	rule_version,
	context: dict[str, Any],
	evaluation: RuleEvaluation,
	*,
	observed_at,
	completed_at,
	duration_ms: int,
) -> str | None:
	"""Append an online shadow receipt without invoking any clinical action path."""
	frappe.db.sql(
		"select name from `tabIONE QC Rule Version` where name = %s for update",
		(rule_version.name,),
	)
	current = frappe.db.get_value(
		"IONE QC Rule Version",
		rule_version.name,
		[
			"status",
			"shadow_mode",
			"lineage_status",
			"checksum",
			"shadow_observation_start",
			"shadow_observation_end",
		],
		as_dict=True,
	)
	if (
		not current
		or str(current.get("status") or "") != "Shadow Run"
		or not int(current.get("shadow_mode") or 0)
		or str(current.get("lineage_status") or "") != "Verified"
		or str(current.get("checksum") or "") != str(rule_version.get("checksum") or "")
		or not current.get("shadow_observation_start")
		or not current.get("shadow_observation_end")
		or get_datetime(observed_at) < get_datetime(current.shadow_observation_start)
		or get_datetime(observed_at) > get_datetime(current.shadow_observation_end)
	):
		return None
	context_hash = _hash_payload(context)
	evaluation_hash = _hash_payload(evaluation.as_dict())
	rule_checksum = str(rule_version.get("checksum") or "")
	executor_version = _rule_executor_version()
	event_hash = _hash_payload(
		{
			"event": event.name,
			"idempotency_key": event.get("idempotency_key"),
			"source_system": event.get("source_system"),
			"source_record_type": event.get("source_record_type"),
			"source_version": event.get("source_version"),
			"mapping_checksum": event.get("mapping_checksum"),
		}
	)
	execution_key = _hash_payload(
		{
			"contract_version": 1,
			"event_hash": event_hash,
			"rule_version": rule_version.name,
			"rule_checksum": rule_checksum,
			"context_hash": context_hash,
			"evaluation_hash": evaluation_hash,
			"executor_version": executor_version,
		}
	)
	existing = frappe.db.get_value(
		"IONE QC Rule Shadow Execution",
		{"execution_key": execution_key},
		"name",
	)
	if existing:
		return existing
	values = {
		"doctype": "IONE QC Rule Shadow Execution",
		"execution_key": execution_key,
		"event": event.name,
		"rule": rule_version.rule,
		"rule_version": rule_version.name,
		"rule_checksum": rule_checksum,
		"hospital": event.get("hospital"),
		"campus": event.get("campus"),
		"department": event.get("department"),
		"ward": event.get("ward"),
		"result": evaluation.result.value,
		"event_hash": event_hash,
		"context_hash": context_hash,
		"evaluation_hash": evaluation_hash,
		"executor_version": executor_version,
		"observed_at": observed_at,
		"completed_at": completed_at,
		"duration_ms": duration_ms,
	}
	prepare_signed_receipt("IONE QC Rule Shadow Execution", values)
	try:
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value(
			"IONE QC Rule Shadow Execution",
			{"execution_key": execution_key},
			"name",
		)
		if existing:
			return existing
		raise
	return doc.name


def _create_candidate_finding(
	event,
	rule_version,
	execution_name: str,
	evaluation: RuleEvaluation,
) -> str:
	deduplication_key = _hash_payload(
		{
			"event": event.name,
			"rule_version": rule_version.name,
			"result": evaluation.result.value,
		}
	)
	existing = frappe.db.get_value(
		"IONE QC Finding",
		{"deduplication_key": deduplication_key},
		"name",
	)
	if existing:
		return existing
	finding = frappe.get_doc(
		{
			"doctype": "IONE QC Finding",
			"deduplication_key": deduplication_key,
			"status": "Candidate",
			"event": event.name,
			"execution": execution_name,
			"rule": rule_version.rule,
			"rule_version": rule_version.name,
			"standard": rule_version.get("standard_snapshot"),
			"standard_clause": rule_version.get("standard_clause_snapshot"),
			"severity": rule_version.get("severity") or rule_version.get("risk_level_snapshot"),
			"title": rule_version.get("rule_name_snapshot"),
			"description": evaluation.reason,
			"hospital": event.get("hospital"),
			"campus": event.get("campus"),
			"department": event.get("department"),
			"patient": event.get("patient_index"),
			"encounter": event.get("encounter_index"),
			"responsible_staff": event.get("responsible_staff"),
			"medical_group": event.get("medical_group"),
			"disease": event.get("disease"),
			"surgery": event.get("surgery"),
			"drg": event.get("drg"),
			"dip": event.get("dip"),
			"source_system": event.get("source_system"),
			"mapping_record": event.get("mapping_record"),
			"mapping_version": event.get("mapping_version"),
			"mapping_checksum": event.get("mapping_checksum"),
			"detected_at": now_datetime(),
			"ai_origin": 0,
		}
	)
	try:
		finding.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value(
			"IONE QC Finding",
			{"deduplication_key": deduplication_key},
			"name",
		)
		if existing:
			return existing
		raise
	_persist_finding_evidence(
		finding,
		event,
		rule_version,
		execution_name,
		evaluation,
	)
	return finding.name


def _persist_finding_evidence(
	finding,
	event,
	rule_version,
	execution_name: str,
	evaluation: RuleEvaluation,
) -> None:
	"""Persist source-linked rule evidence before the finding transaction commits."""
	doctype = "IONE QC Finding Evidence"
	if not frappe.db.exists("DocType", doctype):
		frappe.throw("Finding evidence DocType is unavailable")
	items: list[dict[str, Any]] = [item.as_dict() for item in evaluation.evidence]
	if not items:
		items = [
			{
				"field": "(rule result)",
				"operator": "evaluate",
				"expected": RuleResult.PASSED.value,
				"actual": evaluation.result.value,
				"matched": False,
			}
		]
	for item in items:
		evidence_payload = {
			"contract_version": 1,
			"result": evaluation.result.value,
			"reason": evaluation.reason,
			"missing_fields": sorted(set(evaluation.missing_fields)),
			"field": item["field"],
			"operator": item["operator"],
			"expected_summary": _value_summary(item.get("expected")),
			"actual_summary": _value_summary(item.get("actual")),
			"matched": bool(item.get("matched")),
			"source": {
				"system": event.get("source_system"),
				"record_type": event.get("source_record_type"),
				"record_id": event.get("source_record_id"),
				"version": event.get("source_version"),
			},
			"rule_version": rule_version.name,
			"rule_checksum": rule_version.get("checksum"),
			"standard_clause": rule_version.get("standard_clause_snapshot"),
		}
		values = {
			"doctype": doctype,
			"finding": finding.name,
			"execution": execution_name,
			"evidence_type": "Rule Evidence",
			"source_system_link": event.get("source_system"),
			"source_system": str(event.get("source_system") or ""),
			"source_record_type": event.get("source_record_type"),
			"source_record_id": event.get("source_record_id"),
			"source_version": event.get("source_version"),
			"source_reference": event.get("payload_reference"),
			"field_path": str(item["field"]),
			"operator": str(item["operator"]),
			"expected_json": _canonical_json(_value_summary(item.get("expected"))),
			"actual_json": _canonical_json(_value_summary(item.get("actual"))),
			"matched": int(bool(item.get("matched"))),
			"evidence_text": evaluation.reason,
			"evidence_json": _canonical_json(evidence_payload),
			"context_summary": "Deterministic rule evidence captured from the immutable source event.",
			"captured_at": event.get("event_time"),
			"recorded_at": now_datetime(),
			"rule_version": rule_version.name,
			"standard_clause": rule_version.get("standard_clause_snapshot"),
		}
		evidence = frappe.get_doc(values)
		evidence_hash = _document_content_hash(evidence, "evidence_hash")
		if frappe.db.exists(doctype, {"evidence_hash": evidence_hash}):
			continue
		evidence.evidence_hash = evidence_hash
		try:
			evidence.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			if not frappe.db.exists(doctype, {"evidence_hash": evidence_hash}):
				raise


def _upsert_rule_data_quality_issue(
	event,
	rule_version,
	execution_name: str,
	evaluation: RuleEvaluation,
) -> str | None:
	doctype = "IONE Data Quality Issue"
	if not frappe.db.exists("DocType", doctype):
		return None
	issue_type = (
		"Insufficient Rule Data"
		if evaluation.result == RuleResult.INSUFFICIENT_DATA
		else "Rule Execution Error"
	)
	issue_key = _hash_payload(
		{
			"event": event.name,
			"rule_version": rule_version.name,
			"issue_type": issue_type,
			"missing_fields": sorted(set(evaluation.missing_fields)),
		}
	)
	existing = frappe.db.get_value(doctype, {"issue_key": issue_key}, "name")
	if existing:
		return existing
	description = (
		"Required fields were unavailable for deterministic evaluation. No quality finding was created."
		if evaluation.result == RuleResult.INSUFFICIENT_DATA
		else "The deterministic rule could not be evaluated. The source event remains retryable."
	)
	values = {
		"doctype": doctype,
		"issue_key": issue_key,
		"source_system": event.get("source_system"),
		"event": event.name,
		"execution": execution_name,
		"rule_version": rule_version.name,
		"hospital": event.get("hospital"),
		"campus": event.get("campus"),
		"department": event.get("department"),
		"ward": event.get("ward"),
		"source_record_type": event.get("source_record_type"),
		"source_record_id": event.get("source_record_id"),
		"issue_type": issue_type,
		"severity": "High" if evaluation.result == RuleResult.ERROR else "Medium",
		"status": "Open",
		"field_path": ",".join(sorted(set(evaluation.missing_fields)))[:140],
		"description": description,
		"detected_at": now_datetime(),
	}
	try:
		issue = frappe.get_doc(values)
		issue.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return frappe.db.get_value(doctype, {"issue_key": issue_key}, "name")
	return issue.name


def _notify_rule_alert(event, rule_version, execution_name: str) -> None:
	rule_identity = rule_version.get("rule_code_snapshot") or rule_version.name
	_notify_execution(
		event,
		execution_name,
		subject=f"IONE rule alert: {rule_identity}",
		content=(
			"An approved IONE rule produced an alert-only result. "
			"Review the linked execution within your authorized scope."
		),
	)


def _notify_rule_error(event, rule_version, execution_name: str) -> None:
	rule_identity = rule_version.get("rule_code_snapshot") or rule_version.name
	_notify_execution(
		event,
		execution_name,
		subject=f"IONE rule execution requires retry: {rule_identity}",
		content=(
			"A deterministic rule execution ended in Error. The source event remains "
			"retryable and a data-quality issue was recorded. Review the linked execution."
		),
	)


def _notify_execution(
	event,
	execution_name: str,
	*,
	subject: str,
	content: str,
) -> None:
	recipients = set(
		frappe.get_all(
			"Has Role",
			filters={
				"role": ["in", ["IONE QC Reviewer", "IONE Medical Affairs"]],
				"parenttype": "User",
			},
			pluck="parent",
			limit_page_length=500,
		)
	)
	if event.get("department"):
		department_users = set(
			frappe.get_all(
				"IONE Medical Staff",
				filters={
					"department": event.department,
					"user": ["is", "set"],
					"practice_status": "Active",
				},
				pluck="user",
				limit_page_length=100,
			)
		)
		department_reviewers = set(
			frappe.get_all(
				"Has Role",
				filters={
					"role": ["in", ["IONE Department Director", "IONE Department QC Officer"]],
					"parenttype": "User",
				},
				pluck="parent",
				limit_page_length=500,
			)
		)
		recipients.update(
			user
			for user in department_users.intersection(department_reviewers)
			if event.department in get_access_context(user).departments
		)
	for user in sorted(recipients):
		if not user or user in {"Administrator", "Guest"}:
			continue
		if not frappe.db.get_value("User", user, "enabled"):
			continue
		if frappe.db.exists(
			"Notification Log",
			{
				"for_user": user,
				"document_type": "IONE QC Execution",
				"document_name": execution_name,
				"subject": subject,
			},
		):
			continue
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"from_user": "Administrator",
				"type": "Alert",
				"document_type": "IONE QC Execution",
				"document_name": execution_name,
				"subject": subject,
				"email_content": content,
			}
		).insert(ignore_permissions=True)


def _build_context(event) -> dict[str, Any]:
	payload = _json_object(event.get("payload_json"), empty={})
	if not isinstance(payload, dict):
		raise ValueError("Clinical event payload_json must be a JSON object")
	return {
		**payload,
		"payload": payload,
		"event": {
			"name": event.name,
			"event_type": event.get("event_type"),
			"event_time": event.get("event_time"),
			"source_system": event.get("source_system"),
			"source_record_type": event.get("source_record_type"),
			"source_record_id": event.get("source_record_id"),
		},
		"patient_index": event.get("patient_index"),
		"encounter_index": event.get("encounter_index"),
		"hospital": event.get("hospital"),
		"campus": event.get("campus"),
		"department": event.get("department"),
		"responsible_staff": event.get("responsible_staff"),
		"medical_group": event.get("medical_group"),
		"disease": event.get("disease"),
		"surgery": event.get("surgery"),
		"drg": event.get("drg"),
		"dip": event.get("dip"),
	}


def _json_object(value: Any, empty: Any = None) -> Any:
	if value in (None, ""):
		return empty if empty is not None else {}
	if isinstance(value, (dict, list)):
		return value
	parsed = json.loads(value)
	if not isinstance(parsed, (dict, list)):
		raise ValueError("Rule and event JSON must be an object or array")
	return parsed


def _normalize_rule_codes(rule_codes: list[str] | str | None) -> tuple[str, ...]:
	if not rule_codes:
		return ()
	if isinstance(rule_codes, str):
		try:
			parsed = json.loads(rule_codes)
			rule_codes = parsed if isinstance(parsed, list) else [rule_codes]
		except ValueError:
			rule_codes = [item.strip() for item in rule_codes.split(",")]
	return tuple(sorted({str(item).strip().upper() for item in rule_codes if str(item).strip()}))


def _hash_payload(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


@lru_cache(maxsize=1)
def _rule_executor_version() -> str:
	return f"{RULE_EXECUTOR_CONTRACT}:{current_app_commit_sha()}"


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _value_summary(value: Any) -> Any:
	"""Return a bounded value summary suitable for scoped immutable evidence."""
	serialized = _canonical_json(value)
	if len(serialized) <= 2_000:
		return value
	return {
		"truncated": True,
		"value_type": type(value).__name__,
		"sha256": hashlib.sha256(serialized.encode()).hexdigest(),
		"serialized_length": len(serialized),
	}


def _document_content_hash(doc, hash_field: str) -> str:
	excluded = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"idx",
		"docstatus",
		hash_field,
		"record_hash",
	}
	payload = {
		field.fieldname: doc.get(field.fieldname)
		for field in doc.meta.fields
		if field.fieldname not in excluded
	}
	return _hash_payload(payload)

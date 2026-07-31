from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any

import frappe
from frappe.utils import now_datetime

from ione_qms import __version__
from ione_qms.services.immutability import canonical_record_hash, validate_append_only

STATE_DOCTYPE = "IONE Migration State"
RECEIPT_DOCTYPE = "IONE Migration Completion Receipt"
FINGERPRINT_FORMAT = "ione-qms-full-fingerprint-v1"
MIGRATION_PLAN = (
	{
		"id": "definition-lineage",
		"runner": "ione_qms.services.versions.backfill_definition_lineage",
		"completion": "has_more=false",
		"blocking": True,
	},
	{
		"id": "integration-tenant-fail-closed",
		"runner": "ione_qms.tasks.migrations.enforce_integration_tenant_fail_closed",
		"completion": "has_more=false",
		"blocking": False,
	},
	{
		"id": "site-keyed-identity-surrogates",
		"runner": "ione_qms.services.identity_keys.backfill_site_identity_keys",
		"completion": "has_more=false and blocked=0",
		"blocking": True,
	},
	{
		"id": "finding-due-dates",
		"runner": "ione_qms.setup.install.backfill_finding_due_dates",
		"completion": "updated<batch_size",
		"blocking": False,
	},
	{
		"id": "version-active-keys",
		"runner": "ione_qms.services.versions.backfill_active_version_keys",
		"completion": "has_more=false",
		"blocking": True,
	},
	{
		"id": "definition-authority-lineage",
		"runner": "ione_qms.services.versions.backfill_definition_lineage",
		"completion": "has_more=false and blocked=0",
		"blocking": True,
	},
	{
		"id": "rectification-active-keys",
		"runner": "ione_qms.tasks.migrations._backfill_active_key:IONE QC Rectification",
		"completion": "has_more=false",
		"blocking": True,
	},
	{
		"id": "verification-active-keys",
		"runner": "ione_qms.tasks.migrations._backfill_active_key:IONE QC Verification",
		"completion": "has_more=false",
		"blocking": True,
	},
	{
		"id": "indicator-immutable-input-receipts",
		"runner": "ione_qms.services.indicators.quarantine_legacy_indicator_receipts",
		"completion": "has_more=false and blocked=0 and artifact-manifest=current",
		"blocking": True,
	},
	{
		"id": "scope-integrity-audit",
		"runner": "ione_qms.services.scope_hierarchy.audit_scope_integrity",
		"completion": "ongoing-nonblocking-audit",
		"blocking": False,
	},
)
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INDICATOR_MANIFEST_FORMAT = "ione-indicator-artifact-manifest-v1"
_INDICATOR_MANIFEST_SUMMARY_KEYS = frozenset(
	{
		"artifact_manifest_format",
		"artifact_manifest_count",
		"artifact_manifest_hash",
		"artifact_high_watermark",
	}
)
_INDICATOR_MANIFEST_STRING_KEYS = _INDICATOR_MANIFEST_SUMMARY_KEYS - {"artifact_manifest_count"}


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def compute_full_fingerprint(
	package_root: Path | None = None,
	*,
	app_version: str = __version__,
	migration_plan: tuple[dict[str, Any], ...] = MIGRATION_PLAN,
) -> str:
	"""Hash normalized shipped Python, DocType metadata, plan, and app version."""
	root = (package_root or Path(__file__).resolve().parents[1]).resolve()
	python_files = {
		path
		for path in root.rglob("*.py")
		if path.is_file() and "__pycache__" not in path.relative_to(root).parts
	}
	doctype_json_files = {
		path for path in root.rglob("*.json") if path.is_file() and "doctype" in path.relative_to(root).parts
	}
	file_manifest = []
	for path in sorted(python_files | doctype_json_files, key=lambda item: item.as_posix()):
		relative_path = f"{root.name}/{path.relative_to(root).as_posix()}"
		content = _normalized_fingerprint_content(path)
		file_manifest.append(
			{
				"path": relative_path,
				"sha256": hashlib.sha256(content).hexdigest(),
			}
		)
	payload = {
		"app_version": str(app_version),
		"files": file_manifest,
		"format": FINGERPRINT_FORMAT,
		"migration_plan": migration_plan,
	}
	return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalized_fingerprint_content(path: Path) -> bytes:
	raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
	if path.suffix.lower() != ".json":
		return raw
	document = json.loads(raw.decode("utf-8-sig"))
	return _canonical_json(document).encode("utf-8")


FULL_FINGERPRINT = compute_full_fingerprint()
SCHEMA_REVISION = f"ione-qms-schema-{FULL_FINGERPRINT[:24]}"
MIGRATION_KEY = f"ione-qms:{FULL_FINGERPRINT}"


def initialize_migration_state() -> bool:
	"""Create/reset the operational state for this schema revision without touching receipts."""
	if not _migration_doctypes_exist():
		return False
	current_revision = str(frappe.db.get_single_value(STATE_DOCTYPE, "schema_revision") or "")
	current_fingerprint = str(frappe.db.get_single_value(STATE_DOCTYPE, "full_fingerprint") or "")
	if current_revision == SCHEMA_REVISION and hmac.compare_digest(
		current_fingerprint,
		FULL_FINGERPRINT,
	):
		return True
	_set_state_values(
		{
			"schema_revision": SCHEMA_REVISION,
			"full_fingerprint": FULL_FINGERPRINT,
			"status": "Pending",
			"requested_at": now_datetime(),
			"started_at": None,
			"last_batch_at": None,
			"completed_at": None,
			"batch_count": 0,
			"continuation_sequence": 0,
			"last_summary_json": _canonical_json({}),
			"completion_receipt": None,
			"last_error_code": None,
			"indicator_verification_cursor": None,
			"indicator_verification_complete": 0,
		}
	)
	return True


def migration_is_complete() -> bool:
	"""Return true only for a state/receipt pair whose canonical hash still verifies."""
	try:
		if not _migration_doctypes_exist():
			return False
		if str(frappe.db.get_single_value(STATE_DOCTYPE, "schema_revision") or "") != SCHEMA_REVISION:
			return False
		state_fingerprint = str(frappe.db.get_single_value(STATE_DOCTYPE, "full_fingerprint") or "")
		if not hmac.compare_digest(state_fingerprint, FULL_FINGERPRINT):
			return False
		if str(frappe.db.get_single_value(STATE_DOCTYPE, "status") or "") != "Completed":
			return False
		receipt_name = str(frappe.db.get_single_value(STATE_DOCTYPE, "completion_receipt") or "")
		if not receipt_name or not frappe.db.exists(RECEIPT_DOCTYPE, receipt_name):
			return False
		receipt = frappe.get_doc(RECEIPT_DOCTYPE, receipt_name)
		if not verify_completion_receipt(receipt):
			return False
		if int(frappe.db.get_single_value(STATE_DOCTYPE, "batch_count") or 0) != int(
			receipt.get("batch_count") or 0
		):
			return False
		state_summary_object = _parsed_json_object(
			frappe.db.get_single_value(STATE_DOCTYPE, "last_summary_json")
		)
		state_summary = _canonical_json(state_summary_object)
		if not hmac.compare_digest(state_summary, str(receipt.get("summary_json") or "")):
			return False
		state_completed_at = str(frappe.db.get_single_value(STATE_DOCTYPE, "completed_at") or "")
		if not state_completed_at or state_completed_at != str(receipt.get("completed_at") or ""):
			return False
		return _completion_artifact_bindings_are_current(state_summary_object)
	except Exception:
		return False


def assert_migration_complete(operation: str = "enable production workloads") -> None:
	if migration_is_complete():
		return
	frappe.throw(
		f"IONE post-migrate repair is not complete; cannot {operation}. "
		f"Wait for schema revision {SCHEMA_REVISION} to reach Completed with a valid receipt."
	)


def current_migration_completion_receipt() -> str:
	return str(frappe.db.get_single_value(STATE_DOCTYPE, "completion_receipt") or "")


def mark_migration_running() -> None:
	if not initialize_migration_state():
		return
	started_at = frappe.db.get_single_value(STATE_DOCTYPE, "started_at") or now_datetime()
	_set_state_values(
		{
			"status": "Running",
			"started_at": started_at,
			"completed_at": None,
			"completion_receipt": None,
			"last_error_code": None,
		}
	)


def record_migration_batch(summary: dict[str, Any]) -> int:
	"""Persist bounded counters/flags plus non-PHI manifest bindings, never row identifiers."""
	if not initialize_migration_state():
		return 0
	current_count = int(frappe.db.get_single_value(STATE_DOCTYPE, "batch_count") or 0)
	previous_summary = _parsed_json_object(frappe.db.get_single_value(STATE_DOCTYPE, "last_summary_json"))
	accumulated = _accumulate_summary(previous_summary, _sanitized_summary(summary), current_count + 1)
	_set_state_values(
		{
			"status": "Running",
			"last_batch_at": now_datetime(),
			"batch_count": current_count + 1,
			"last_summary_json": _canonical_json(accumulated),
			"last_error_code": None,
		}
	)
	return current_count + 1


def mark_migration_pending() -> int:
	if not initialize_migration_state():
		return 0
	sequence = int(frappe.db.get_single_value(STATE_DOCTYPE, "continuation_sequence") or 0) + 1
	_set_state_values(
		{
			"status": "Pending",
			"continuation_sequence": sequence,
			"last_error_code": None,
		}
	)
	return sequence


def mark_migration_blocked() -> None:
	if not initialize_migration_state():
		return
	_set_state_values(
		{
			"status": "Blocked",
			"last_error_code": "DATA_REVIEW_REQUIRED",
		}
	)


def mark_migration_failed(error_code: str) -> None:
	if not initialize_migration_state():
		return
	safe_code = str(error_code or "UNEXPECTED_MIGRATION_FAILURE").strip().upper()
	if not _SAFE_ERROR_CODE.fullmatch(safe_code):
		safe_code = "UNEXPECTED_MIGRATION_FAILURE"
	_set_state_values({"status": "Failed", "last_error_code": safe_code})


def complete_migration() -> str:
	"""Atomically materialize the immutable receipt and bind state to it."""
	if not initialize_migration_state():
		frappe.throw("IONE migration metadata schema is unavailable.")
	if migration_is_complete():
		return str(frappe.db.get_single_value(STATE_DOCTYPE, "completion_receipt") or "")
	batch_count = int(frappe.db.get_single_value(STATE_DOCTYPE, "batch_count") or 0)
	if batch_count < 1:
		frappe.throw("IONE migration completion requires at least one committed repair batch.")
	summary = _parsed_json_object(frappe.db.get_single_value(STATE_DOCTYPE, "last_summary_json"))
	summary_json = _canonical_json(summary)
	completed_at = now_datetime()
	if not _completion_artifact_bindings_are_current(summary):
		frappe.throw("IONE migration completion requires a current indicator artifact manifest.")
	receipt_key = _completion_receipt_key(summary)
	if frappe.db.exists(RECEIPT_DOCTYPE, receipt_key):
		receipt = frappe.get_doc(RECEIPT_DOCTYPE, receipt_key)
		if not verify_completion_receipt(receipt):
			frappe.throw("Existing IONE migration completion receipt failed verification.")
		receipt_summary = _parsed_json_object(receipt.get("summary_json"))
		if not _completion_artifact_bindings_are_current(receipt_summary):
			frappe.throw("Existing IONE migration artifact manifest is no longer current.")
		completed_at = receipt.get("completed_at")
		batch_count = int(receipt.get("batch_count") or 0)
		summary_json = str(receipt.get("summary_json") or "")
	else:
		previous_receipt = frappe.get_all(
			RECEIPT_DOCTYPE,
			filters={"migration_key": ["!=", receipt_key]},
			pluck="name",
			order_by="completed_at desc, name desc",
			limit_page_length=1,
		)
		receipt = frappe.get_doc(
			{
				"doctype": RECEIPT_DOCTYPE,
				"migration_key": receipt_key,
				"schema_revision": SCHEMA_REVISION,
				"full_fingerprint": FULL_FINGERPRINT,
				"app_version": __version__,
				"completed_at": completed_at,
				"batch_count": batch_count,
				"summary_json": summary_json,
				"previous_receipt": previous_receipt[0] if previous_receipt else None,
			}
		)
		receipt.flags.ione_migration_receipt_maintenance = receipt_key
		receipt.insert(ignore_permissions=True)
		if not verify_completion_receipt(receipt):
			frappe.throw("New IONE migration completion receipt failed verification.")
	_set_state_values(
		{
			"status": "Completed",
			"completed_at": completed_at,
			"completion_receipt": receipt_key,
			"batch_count": batch_count,
			"last_summary_json": summary_json,
			"last_error_code": None,
		}
	)
	return receipt_key


def validate_completion_receipt(doc, method: str | None = None) -> None:
	del method
	if doc.is_new() and getattr(
		doc.flags,
		"ione_migration_receipt_maintenance",
		None,
	) != str(doc.get("migration_key") or ""):
		frappe.throw("Migration completion receipts may only be created by the bounded migration worker.")
	if str(doc.get("schema_revision") or "") != SCHEMA_REVISION:
		frappe.throw("Migration receipt schema revision is not current.")
	if not hmac.compare_digest(
		str(doc.get("full_fingerprint") or ""),
		FULL_FINGERPRINT,
	):
		frappe.throw("Migration receipt full fingerprint is not current.")
	if not hmac.compare_digest(str(doc.get("app_version") or ""), __version__):
		frappe.throw("Migration receipt app version is not current.")
	if int(doc.get("batch_count") or 0) < 1:
		frappe.throw("Migration receipt requires a positive committed batch count.")
	summary = _parsed_json_object(doc.get("summary_json"))
	if _canonical_json(summary) != str(doc.get("summary_json") or ""):
		frappe.throw("Migration receipt summary must be canonical JSON.")
	try:
		binding = _indicator_manifest_binding_from_summary(summary)
	except ValueError as exc:
		frappe.throw(str(exc))
	if str(doc.get("migration_key") or "") != _completion_receipt_key(summary):
		frappe.throw("Migration receipt key does not match its fingerprint and artifact epoch.")
	if str(binding["artifact_high_watermark"]) > str(doc.get("completed_at") or ""):
		frappe.throw("Indicator artifact high-watermark cannot be after migration completion.")
	validate_append_only(doc)


def verify_completion_receipt(doc) -> bool:
	try:
		if str(doc.get("schema_revision") or "") != SCHEMA_REVISION:
			return False
		if not hmac.compare_digest(
			str(doc.get("full_fingerprint") or ""),
			FULL_FINGERPRINT,
		):
			return False
		if not hmac.compare_digest(str(doc.get("app_version") or ""), __version__):
			return False
		if int(doc.get("batch_count") or 0) < 1:
			return False
		summary_json = str(doc.get("summary_json") or "")
		summary = _parsed_json_object(summary_json)
		if _canonical_json(summary) != summary_json:
			return False
		binding = _indicator_manifest_binding_from_summary(summary)
		expected_key = _completion_receipt_key(summary)
		if str(doc.get("name") or "") != expected_key:
			return False
		if str(doc.get("migration_key") or "") != expected_key:
			return False
		if str(binding["artifact_high_watermark"]) > str(doc.get("completed_at") or ""):
			return False
		record_hash = str(doc.get("record_hash") or "")
		computed = canonical_record_hash(doc)
		return bool(record_hash) and hmac.compare_digest(record_hash, computed)
	except Exception:
		return False


def _migration_doctypes_exist() -> bool:
	return bool(frappe.db.exists("DocType", STATE_DOCTYPE) and frappe.db.exists("DocType", RECEIPT_DOCTYPE))


def _set_state_values(values: dict[str, Any]) -> None:
	for fieldname, value in values.items():
		frappe.db.set_single_value(STATE_DOCTYPE, fieldname, value)


def _accumulate_summary(
	previous: dict[str, Any],
	current: dict[str, Any],
	batch_count: int,
) -> dict[str, Any]:
	totals = previous.get("totals")
	if not isinstance(totals, dict):
		totals = {}
	for component, values in current.items():
		if not isinstance(values, dict):
			continue
		component_totals = totals.setdefault(component, {})
		if not isinstance(component_totals, dict):
			component_totals = {}
			totals[component] = component_totals
		for key, value in values.items():
			if (
				key in {"has_more", "blocked"}
				or key in _INDICATOR_MANIFEST_SUMMARY_KEYS
				or isinstance(value, bool)
			):
				continue
			if isinstance(value, int):
				component_totals[key] = int(component_totals.get(key) or 0) + value
	artifact_bindings: dict[str, Any] = {}
	previous_bindings = previous.get("artifact_bindings")
	if isinstance(previous_bindings, dict) and isinstance(
		previous_bindings.get("indicator_artifacts"),
		dict,
	):
		try:
			artifact_bindings["indicator_artifacts"] = _normalize_indicator_manifest_binding(
				previous_bindings["indicator_artifacts"]
			)
		except ValueError:
			pass
	indicator_summary = current.get("indicator_receipts")
	if isinstance(indicator_summary, dict):
		try:
			artifact_bindings["indicator_artifacts"] = _normalize_indicator_manifest_binding(
				indicator_summary
			)
		except ValueError:
			pass
	return {
		"batches": batch_count,
		"last_batch": current,
		"totals": totals,
		**({"artifact_bindings": artifact_bindings} if artifact_bindings else {}),
	}


def _sanitized_summary(summary: dict[str, Any]) -> dict[str, Any]:
	clean: dict[str, Any] = {}
	for component, values in sorted(summary.items()):
		if not isinstance(values, dict):
			continue
		clean_values: dict[str, int | bool | str] = {}
		for key, value in sorted(values.items()):
			if isinstance(value, bool):
				clean_values[str(key)] = value
			elif isinstance(value, int):
				clean_values[str(key)] = value
			elif key in _INDICATOR_MANIFEST_STRING_KEYS and isinstance(value, str):
				clean_values[str(key)] = value
		clean[str(component)] = clean_values
	return clean


def _normalize_indicator_manifest_binding(binding: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(binding, dict):
		raise ValueError("Migration receipt requires an indicator artifact manifest.")
	if str(binding.get("artifact_manifest_format") or "") != _INDICATOR_MANIFEST_FORMAT:
		raise ValueError("Migration receipt indicator artifact manifest format is not current.")
	count_value = binding.get("artifact_manifest_count")
	if isinstance(count_value, bool):
		raise ValueError("Migration receipt indicator artifact manifest count is invalid.")
	try:
		count = int(count_value)
	except (TypeError, ValueError) as exc:
		raise ValueError("Migration receipt indicator artifact manifest count is invalid.") from exc
	manifest_hash = str(binding.get("artifact_manifest_hash") or "")
	high_watermark = str(binding.get("artifact_high_watermark") or "").strip()
	if (
		count < 0
		or not _SHA256.fullmatch(manifest_hash)
		or not high_watermark
		or len(high_watermark) > 64
		or "|" in high_watermark
	):
		raise ValueError("Migration receipt indicator artifact manifest binding is invalid.")
	return {
		"artifact_manifest_format": _INDICATOR_MANIFEST_FORMAT,
		"artifact_manifest_count": count,
		"artifact_manifest_hash": manifest_hash,
		"artifact_high_watermark": high_watermark,
	}


def _indicator_manifest_binding_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
	bindings = summary.get("artifact_bindings") if isinstance(summary, dict) else None
	binding = bindings.get("indicator_artifacts") if isinstance(bindings, dict) else None
	return _normalize_indicator_manifest_binding(binding)


def _completion_receipt_key(summary: dict[str, Any]) -> str:
	binding = _indicator_manifest_binding_from_summary(summary)
	epoch_hash = hashlib.sha256(_canonical_json(binding).encode("utf-8")).hexdigest()
	return f"{MIGRATION_KEY}:{epoch_hash}"


def _completion_artifact_bindings_are_current(summary: dict[str, Any]) -> bool:
	try:
		binding = _indicator_manifest_binding_from_summary(summary)
		from ione_qms.services.indicators import indicator_artifact_manifest_matches

		return bool(indicator_artifact_manifest_matches(binding))
	except Exception:
		return False


def _parsed_json_object(value) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	try:
		parsed = json.loads(str(value or ""))
	except TypeError, ValueError, json.JSONDecodeError:
		return {}
	return parsed if isinstance(parsed, dict) else {}

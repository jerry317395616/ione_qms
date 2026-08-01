"""Fail-closed evidence and separation-of-duty gate for production activation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from datetime import timedelta
from functools import lru_cache
from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime

from ione_qms.production_contracts import (
	MANIFEST_FORMAT,
	MAX_PRODUCTION_APPROVAL_DAYS,
	RELEASE_MANIFEST_FORMAT,
	REQUIRED_GATE_CODES,
)
from ione_qms.services.crypto_keys import active_hmac_key, verification_hmac_key
from ione_qms.services.immutability import canonical_record_hash
from ione_qms.services.migration_state import (
	FULL_FINGERPRINT,
	SCHEMA_REVISION,
	current_migration_completion_receipt,
	migration_is_complete,
)

ASSESSMENT_DOCTYPE = "IONE Production Readiness Assessment"
EVENT_DOCTYPE = "IONE Production Activation Event"
RELEASE_DOCTYPE = "IONE Release Record"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_APPROVAL_DAYS = MAX_PRODUCTION_APPROVAL_DAYS
ASSESSOR_ROLES = frozenset({"IONE QC Administrator", "IONE Medical Affairs"})
REVIEWER_ROLES = frozenset({"IONE QMS Auditor"})
ACTIVATOR_ROLES = frozenset({"IONE QC Administrator", "IONE Medical Affairs"})
REVOCATION_ROLES = frozenset({"IONE QMS Auditor", "IONE Medical Affairs"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REFERENCE = re.compile(r"^[^\x00-\x1f\x7f]{1,240}$")
_ASSESSMENT_IMMUTABLE_FIELDS = frozenset(
	{
		"release_record",
		"site_name",
		"schema_revision",
		"full_fingerprint",
		"migration_completion_receipt",
		"runtime_commit_manifest_json",
		"valid_until",
		"evidence_manifest_file",
		"evidence_manifest_sha256",
		"gate_evidence",
		"requested_by",
		"requested_at",
		"submitted_by",
		"submitted_at",
		"assessment_checksum",
		"hmac_key_id",
		"assessment_signature",
	}
)
_ACTIVATION_CACHE_SECONDS = 30.0
_activation_cache: dict[str, tuple[float, bool]] = {}


def validate_production_readiness_assessment(doc, method: str | None = None) -> None:
	del method
	actor = str(getattr(frappe.session, "user", "") or "")
	if doc.is_new():
		if str(doc.get("status") or "Draft") != "Draft":
			frappe.throw("Production readiness assessments must start in Draft status.")
		_require_named_actor(ASSESSOR_ROLES)
		doc.status = "Draft"
		doc.requested_by = actor
		doc.requested_at = now_datetime()
		doc.site_name = _site_name()
		doc.schema_revision = SCHEMA_REVISION
		doc.full_fingerprint = FULL_FINGERPRINT
		doc.migration_completion_receipt = current_migration_completion_receipt()
		doc.runtime_commit_manifest_json = _canonical_json(current_runtime_commit_manifest())
		_clear_review_fields(doc)
		return

	previous = doc.get_doc_before_save()
	if previous is None:
		frappe.throw("Production readiness assessment history is unavailable.")
	if str(previous.get("requested_by") or "") != str(doc.get("requested_by") or ""):
		frappe.throw("The accountable production-readiness assessor is immutable.")
	trusted = getattr(frappe.flags, "ione_production_readiness_mutation", None) == str(doc.name)
	if not trusted:
		if str(previous.get("status") or "") != "Draft":
			frappe.throw("Submitted production readiness assessments are immutable.")
		if str(doc.get("status") or "") != "Draft":
			frappe.throw("Use the governed production-readiness APIs for status transitions.")
		if actor != str(doc.get("requested_by") or ""):
			frappe.throw("Only the accountable assessor may edit this Draft assessment.")
		_assert_context_fields_unchanged(doc, previous)
		return

	if str(previous.get("status") or "") != "Draft":
		_assert_assessment_evidence_unchanged(doc, previous)
	if str(doc.get("status") or "") in {"Approved", "Rejected", "Revoked"}:
		_assert_governance_signature(doc)


def validate_production_activation_event(doc, method: str | None = None) -> None:
	del method
	event_key = str(doc.get("event_key") or "")
	if not doc.is_new():
		frappe.throw("Production activation events are append-only.")
	if getattr(frappe.flags, "ione_production_activation_event", None) != event_key:
		frappe.throw("Production activation events may only be created by the governed API.")
	if not _SHA256.fullmatch(event_key):
		frappe.throw("Production activation event key is invalid.")
	if str(doc.get("action") or "") not in {"Activated", "Deactivated", "Revoked"}:
		frappe.throw("Production activation event action is invalid.")
	if str(doc.get("actor") or "") in {"", "Guest", "Administrator"}:
		frappe.throw("Production activation events require a named accountable actor.")
	if len(str(doc.get("reason") or "").strip()) < 10:
		frappe.throw("Production activation events require a reason of at least ten characters.")
	for fieldname in (
		"assessment_checksum",
		"release_record_hash",
		"full_fingerprint",
		"runtime_commit_manifest_hash",
		"event_signature",
	):
		if not _SHA256.fullmatch(str(doc.get(fieldname) or "")):
			frappe.throw(f"Production activation event {fieldname} is invalid.")
	if str(doc.get("schema_revision") or "") != SCHEMA_REVISION:
		frappe.throw("Production activation event schema revision is not current.")
	if str(doc.get("full_fingerprint") or "") != FULL_FINGERPRINT:
		frappe.throw("Production activation event application fingerprint is not current.")
	if str(doc.get("migration_completion_receipt") or "") != current_migration_completion_receipt():
		frappe.throw("Production activation event migration receipt is not current.")
	_verify_event_signature(doc)
	if str(doc.get("action") or "") == "Activated" and not int(doc.get("production_mode") or 0):
		frappe.throw("An Activated event must enable production_mode.")
	if str(doc.get("action") or "") != "Activated" and any(
		int(doc.get(fieldname) or 0)
		for fieldname in ("production_mode", "realtime_rules_enabled", "ai_enabled")
	):
		frappe.throw("Deactivated and Revoked events must disable every production workload switch.")


def validate_production_evidence_manifest_file(doc) -> None:
	if str(doc.get("attached_to_doctype") or "") != ASSESSMENT_DOCTYPE:
		return
	if not int(doc.get("is_private") or 0) or not str(doc.get("file_url") or "").startswith(
		"/private/files/"
	):
		frappe.throw("Production evidence manifests must be private files.")
	if not str(doc.get("file_name") or "").lower().endswith(".json"):
		frappe.throw("Production evidence manifests must use a .json filename.")
	parent_name = str(doc.get("attached_to_name") or "")
	if not parent_name or not frappe.db.exists(ASSESSMENT_DOCTYPE, parent_name):
		frappe.throw("Production evidence manifests must be attached to an existing assessment.")
	status = str(frappe.db.get_value(ASSESSMENT_DOCTYPE, parent_name, "status") or "")
	if status != "Draft":
		frappe.throw("Submitted production evidence manifest files are immutable.")


def prevent_production_evidence_manifest_deletion(doc) -> None:
	if str(doc.get("attached_to_doctype") or "") != ASSESSMENT_DOCTYPE:
		return
	parent_name = str(doc.get("attached_to_name") or "")
	if not parent_name:
		frappe.throw("Orphaned production evidence manifests cannot be modified or deleted.")
	status = str(frappe.db.get_value(ASSESSMENT_DOCTYPE, parent_name, "status") or "")
	if status != "Draft":
		frappe.throw("Submitted production evidence manifest files cannot be modified or deleted.")


def submit_production_readiness_assessment(assessment: str) -> dict[str, str]:
	actor = _require_named_actor(ASSESSOR_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:production-assessment:{assessment}", timeout=10):
		doc = frappe.get_doc(ASSESSMENT_DOCTYPE, assessment, for_update=True)
		if str(doc.get("status") or "") != "Draft":
			frappe.throw("Only a Draft production readiness assessment can be submitted.")
		if str(doc.get("requested_by") or "") != actor:
			frappe.throw("Only the accountable assessor can submit this assessment.")
		_assert_context_current(doc)
		_validate_release_record(doc.get("release_record"), expected_manifest=_runtime_manifest(doc))
		manifest_hash = _validate_manifest_and_gates(doc)
		doc.evidence_manifest_sha256 = manifest_hash
		doc.submitted_by = actor
		doc.submitted_at = now_datetime()
		doc.status = "Under Review"
		key = active_hmac_key("ione_production_hmac")
		doc.hmac_key_id = key.key_id
		doc.assessment_checksum = _assessment_checksum(doc)
		doc.assessment_signature = _assessment_signature(doc, key.secret)
		with _trusted_assessment_mutation(str(doc.name)):
			doc.save(ignore_permissions=True)
	return _assessment_result(doc)


def approve_production_readiness_assessment(
	assessment: str,
	review_comment: str,
) -> dict[str, str]:
	actor = _require_named_actor(REVIEWER_ROLES)
	comment = _required_reason(review_comment, "Review comment")
	with frappe.db.advisory_lock(f"ione-qms:production-assessment:{assessment}", timeout=10):
		doc = frappe.get_doc(ASSESSMENT_DOCTYPE, assessment, for_update=True)
		if str(doc.get("status") or "") != "Under Review":
			frappe.throw("Only an Under Review production readiness assessment can be approved.")
		if actor in {str(doc.get("requested_by") or ""), str(doc.get("submitted_by") or "")}:
			frappe.throw("The assessor cannot independently approve the same assessment.")
		_assert_approved_assessment_material(doc, allowed_statuses={"Under Review"})
		doc.status = "Approved"
		doc.reviewed_by = actor
		doc.reviewed_at = now_datetime()
		doc.review_comment = comment
		_set_governance_signature(doc)
		with _trusted_assessment_mutation(str(doc.name)):
			doc.save(ignore_permissions=True)
	return _assessment_result(doc)


def reject_production_readiness_assessment(
	assessment: str,
	review_comment: str,
) -> dict[str, str]:
	actor = _require_named_actor(REVIEWER_ROLES)
	comment = _required_reason(review_comment, "Review comment")
	with frappe.db.advisory_lock(f"ione-qms:production-assessment:{assessment}", timeout=10):
		doc = frappe.get_doc(ASSESSMENT_DOCTYPE, assessment, for_update=True)
		if str(doc.get("status") or "") != "Under Review":
			frappe.throw("Only an Under Review production readiness assessment can be rejected.")
		if actor in {str(doc.get("requested_by") or ""), str(doc.get("submitted_by") or "")}:
			frappe.throw("The assessor cannot independently review the same assessment.")
		_assert_assessment_checksum(doc)
		doc.status = "Rejected"
		doc.reviewed_by = actor
		doc.reviewed_at = now_datetime()
		doc.review_comment = comment
		_set_governance_signature(doc)
		with _trusted_assessment_mutation(str(doc.name)):
			doc.save(ignore_permissions=True)
	return _assessment_result(doc)


def activate_production_mode(
	assessment: str,
	reason: str,
	*,
	enable_realtime_rules: bool | int = False,
	enable_ai: bool | int = False,
) -> dict[str, Any]:
	actor = _require_named_actor(ACTIVATOR_ROLES)
	reason_text = _required_reason(reason, "Activation reason")
	with frappe.db.advisory_lock(f"ione-qms:production-site:{_site_name()}", timeout=10):
		doc = frappe.get_doc(ASSESSMENT_DOCTYPE, assessment, for_update=True)
		if actor in {
			str(doc.get("requested_by") or ""),
			str(doc.get("submitted_by") or ""),
			str(doc.get("reviewed_by") or ""),
		}:
			frappe.throw(
				"Production activation requires an accountable operator independent of the assessor and reviewer."
			)
		_assert_approved_assessment_material(doc)
		realtime = _as_bool(enable_realtime_rules)
		ai_enabled = _as_bool(enable_ai)
		if ai_enabled:
			from ione_qms.ai.readiness import check_qwen_readiness

			check_qwen_readiness()
		event = _create_activation_event(
			doc,
			action="Activated",
			actor=actor,
			reason=reason_text,
			production_mode=True,
			realtime_rules_enabled=realtime,
			ai_enabled=ai_enabled,
		)
		_write_runtime_settings(
			event_name=str(event.name),
			production_mode=True,
			realtime_rules_enabled=realtime,
			ai_enabled=ai_enabled,
		)
	return {
		"event": str(event.name),
		"assessment": str(doc.name),
		"production_mode": 1,
		"enable_realtime_rules": int(realtime),
		"enable_ai": int(ai_enabled),
	}


def deactivate_production_mode(reason: str) -> dict[str, Any]:
	actor = _require_named_actor(ACTIVATOR_ROLES | REVOCATION_ROLES)
	reason_text = _required_reason(reason, "Deactivation reason")
	with frappe.db.advisory_lock(f"ione-qms:production-site:{_site_name()}", timeout=10):
		current_event = str(
			frappe.db.get_single_value("IONE System Settings", "production_activation_event") or ""
		)
		if not current_event:
			_write_runtime_settings(
				event_name="",
				production_mode=False,
				realtime_rules_enabled=False,
				ai_enabled=False,
			)
			return {"event": "", "production_mode": 0}
		prior = frappe.get_doc(EVENT_DOCTYPE, current_event)
		assessment = frappe.get_doc(ASSESSMENT_DOCTYPE, prior.assessment)
		event = _create_activation_event(
			assessment,
			action="Deactivated",
			actor=actor,
			reason=reason_text,
			production_mode=False,
			realtime_rules_enabled=False,
			ai_enabled=False,
			previous_event=current_event,
		)
		_write_runtime_settings(
			event_name=str(event.name),
			production_mode=False,
			realtime_rules_enabled=False,
			ai_enabled=False,
		)
	return {"event": str(event.name), "production_mode": 0}


def revoke_production_readiness_assessment(assessment: str, reason: str) -> dict[str, str]:
	actor = _require_named_actor(REVOCATION_ROLES)
	reason_text = _required_reason(reason, "Revocation reason")
	with frappe.db.advisory_lock(f"ione-qms:production-site:{_site_name()}", timeout=10):
		with frappe.db.advisory_lock(f"ione-qms:production-assessment:{assessment}", timeout=10):
			doc = frappe.get_doc(ASSESSMENT_DOCTYPE, assessment, for_update=True)
			if str(doc.get("status") or "") != "Approved":
				frappe.throw("Only an Approved production readiness assessment can be revoked.")
			_assert_assessment_checksum(doc)
			doc.status = "Revoked"
			doc.revoked_by = actor
			doc.revoked_at = now_datetime()
			doc.revocation_reason = reason_text
			_set_governance_signature(doc)
			with _trusted_assessment_mutation(str(doc.name)):
				doc.save(ignore_permissions=True)
			current_event = str(
				frappe.db.get_single_value("IONE System Settings", "production_activation_event") or ""
			)
			if current_event:
				prior = frappe.get_doc(EVENT_DOCTYPE, current_event)
				if str(prior.get("assessment") or "") == str(doc.name):
					event = _create_activation_event(
						doc,
						action="Revoked",
						actor=actor,
						reason=reason_text,
						production_mode=False,
						realtime_rules_enabled=False,
						ai_enabled=False,
						previous_event=current_event,
					)
					_write_runtime_settings(
						event_name=str(event.name),
						production_mode=False,
						realtime_rules_enabled=False,
						ai_enabled=False,
					)
	return _assessment_result(doc)


def production_activation_is_current(event_name: str | None = None) -> bool:
	try:
		name = str(
			event_name
			or frappe.db.get_single_value("IONE System Settings", "production_activation_event")
			or ""
		)
		if not name:
			return False
		cache_key = "|".join(
			(
				_site_name(),
				name,
				str(frappe.db.get_single_value("IONE System Settings", "production_mode") or 0),
				str(frappe.db.get_single_value("IONE System Settings", "enable_realtime_rules") or 0),
				str(frappe.db.get_single_value("IONE System Settings", "enable_ai") or 0),
			)
		)
		cached = _activation_cache.get(cache_key)
		if cached and cached[0] > time.monotonic():
			return cached[1]
		event = frappe.get_doc(EVENT_DOCTYPE, name)
		_assert_activation_event_current(event)
		if not int(event.get("production_mode") or 0):
			return False
		if not int(frappe.db.get_single_value("IONE System Settings", "production_mode") or 0):
			return False
		current_realtime = int(
			frappe.db.get_single_value("IONE System Settings", "enable_realtime_rules") or 0
		)
		current_ai = int(frappe.db.get_single_value("IONE System Settings", "enable_ai") or 0)
		if current_realtime > int(event.get("realtime_rules_enabled") or 0):
			return False
		if current_ai > int(event.get("ai_enabled") or 0):
			return False
		_activation_cache.clear()
		_activation_cache[cache_key] = (time.monotonic() + _ACTIVATION_CACHE_SECONDS, True)
		return True
	except Exception:
		return False


def assert_activation_event_authorizes(event_name: str) -> None:
	event = frappe.get_doc(EVENT_DOCTYPE, event_name)
	_assert_activation_event_current(event)


def _assert_activation_event_current(event) -> None:
	if str(event.get("action") or "") != "Activated":
		frappe.throw("The selected production activation event is not an Activated event.")
	if not int(event.get("production_mode") or 0):
		frappe.throw("The selected production activation event does not authorize production mode.")
	stored_hash = str(event.get("record_hash") or "")
	if not stored_hash or not hmac.compare_digest(stored_hash, canonical_record_hash(event)):
		frappe.throw("The production activation event record hash is invalid.")
	if str(event.get("schema_revision") or "") != SCHEMA_REVISION:
		frappe.throw("The production activation event schema revision is stale.")
	if str(event.get("full_fingerprint") or "") != FULL_FINGERPRINT:
		frappe.throw("The production activation event application fingerprint is stale.")
	if not migration_is_complete():
		frappe.throw("The current application migration receipt is not complete.")
	if str(event.get("migration_completion_receipt") or "") != current_migration_completion_receipt():
		frappe.throw("The production activation event migration receipt is stale.")
	current_manifest_hash = _runtime_commit_manifest_hash(current_runtime_commit_manifest())
	if not hmac.compare_digest(
		str(event.get("runtime_commit_manifest_hash") or ""),
		current_manifest_hash,
	):
		frappe.throw("The production activation event does not match the running app commits.")
	assessment = frappe.get_doc(ASSESSMENT_DOCTYPE, event.assessment)
	_assert_approved_assessment_material(assessment)
	if not hmac.compare_digest(
		str(event.get("assessment_checksum") or ""),
		str(assessment.get("assessment_checksum") or ""),
	):
		frappe.throw("The production activation event assessment checksum is stale.")
	release = _validate_release_record(
		assessment.get("release_record"),
		expected_manifest=_runtime_manifest(assessment),
	)
	if str(event.get("release_record") or "") != str(release.name):
		frappe.throw("The production activation event release binding is stale.")
	if not hmac.compare_digest(
		str(event.get("release_record_hash") or ""),
		str(release.get("record_hash") or ""),
	):
		frappe.throw("The production activation event release record hash is stale.")


def _assert_approved_assessment_material(
	doc,
	*,
	allowed_statuses: set[str] | frozenset[str] = frozenset({"Approved"}),
) -> None:
	if str(doc.get("status") or "") not in allowed_statuses:
		frappe.throw("The production readiness assessment is not approvable or active.")
	_assert_context_current(doc)
	_assert_assessment_checksum(doc)
	if str(doc.get("status") or "") == "Approved":
		_assert_governance_signature(doc)
	manifest_hash = _validate_manifest_and_gates(doc)
	if not hmac.compare_digest(str(doc.get("evidence_manifest_sha256") or ""), manifest_hash):
		frappe.throw("The production evidence manifest hash has changed.")
	_validate_release_record(doc.get("release_record"), expected_manifest=_runtime_manifest(doc))


def _assert_context_current(doc) -> None:
	if str(doc.get("site_name") or "") != _site_name():
		frappe.throw("The production readiness assessment belongs to another site.")
	if str(doc.get("schema_revision") or "") != SCHEMA_REVISION:
		frappe.throw("The production readiness assessment schema revision is stale.")
	if str(doc.get("full_fingerprint") or "") != FULL_FINGERPRINT:
		frappe.throw("The production readiness assessment application fingerprint is stale.")
	if not migration_is_complete():
		frappe.throw("A verified current migration completion receipt is required.")
	if str(doc.get("migration_completion_receipt") or "") != current_migration_completion_receipt():
		frappe.throw("The production readiness assessment migration receipt is stale.")
	manifest = _runtime_manifest(doc)
	if manifest != current_runtime_commit_manifest():
		frappe.throw("The production readiness assessment does not match the running app commits.")
	if not doc.get("valid_until"):
		frappe.throw("The production readiness assessment requires a validity deadline.")
	valid_until = get_datetime(doc.get("valid_until"))
	now = now_datetime()
	if valid_until <= now:
		frappe.throw("The production readiness assessment has expired.")
	if valid_until > now + timedelta(days=MAX_APPROVAL_DAYS):
		frappe.throw(f"Production readiness approval cannot exceed {MAX_APPROVAL_DAYS} days.")


def _validate_manifest_and_gates(doc) -> str:
	content = _read_private_manifest(doc)
	try:
		manifest = json.loads(content.decode("utf-8"))
	except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
		raise frappe.ValidationError("Production evidence manifest must be valid UTF-8 JSON.") from exc
	if not isinstance(manifest, dict):
		frappe.throw("Production evidence manifest must be a JSON object.")
	required_top_keys = {
		"format",
		"site",
		"release_record",
		"schema_revision",
		"migration_completion_receipt",
		"generated_at",
		"artifacts",
	}
	if set(manifest) != required_top_keys:
		frappe.throw("Production evidence manifest keys do not match the governed format.")
	if str(manifest.get("format") or "") != MANIFEST_FORMAT:
		frappe.throw("Production evidence manifest format is not supported.")
	for key, expected in (
		("site", doc.get("site_name")),
		("release_record", doc.get("release_record")),
		("schema_revision", doc.get("schema_revision")),
		("migration_completion_receipt", doc.get("migration_completion_receipt")),
	):
		if str(manifest.get(key) or "") != str(expected or ""):
			frappe.throw(f"Production evidence manifest {key} does not match the assessment.")
	if not manifest.get("generated_at"):
		frappe.throw("Production evidence manifest requires generated_at.")
	generated_at = get_datetime(manifest.get("generated_at"))
	if generated_at > now_datetime():
		frappe.throw("Production evidence manifest generated_at cannot be in the future.")
	artifacts = manifest.get("artifacts")
	if not isinstance(artifacts, list) or not (1 <= len(artifacts) <= 128):
		frappe.throw("Production evidence manifest must contain 1-128 artifacts.")
	artifact_index: dict[str, dict[str, Any]] = {}
	for artifact in artifacts:
		_normalize_manifest_artifact(artifact)
		artifact_id = str(artifact["id"])
		if artifact_id in artifact_index:
			frappe.throw("Production evidence manifest artifact IDs must be unique.")
		artifact_index[artifact_id] = artifact

	rows = list(doc.get("gate_evidence") or [])
	if len(rows) != len(REQUIRED_GATE_CODES):
		frappe.throw(f"Production readiness requires exactly {len(REQUIRED_GATE_CODES)} gate rows.")
	seen: set[str] = set()
	used_artifacts: set[str] = set()
	for row in rows:
		gate_code = str(row.get("gate_code") or "")
		if gate_code not in REQUIRED_GATE_CODES or gate_code in seen:
			frappe.throw("Production readiness gate codes must be complete and unique.")
		seen.add(gate_code)
		reference = str(row.get("evidence_reference") or "").strip()
		if not _SAFE_REFERENCE.fullmatch(reference):
			frappe.throw(f"Production gate {gate_code} has an invalid evidence reference.")
		artifact = artifact_index.get(reference)
		if artifact is None:
			frappe.throw(f"Production gate {gate_code} references an unknown manifest artifact.")
		used_artifacts.add(reference)
		if gate_code not in artifact["gate_codes"]:
			frappe.throw(f"Production gate {gate_code} is not bound by its manifest artifact.")
		if not hmac.compare_digest(
			str(row.get("evidence_sha256") or "").lower(),
			str(artifact["sha256"]),
		):
			frappe.throw(f"Production gate {gate_code} evidence hash does not match its artifact.")
		if str(row.get("signed_by") or "").strip() != str(artifact["owner"]):
			frappe.throw(f"Production gate {gate_code} evidence owner does not match its artifact.")
		if str(get_datetime(row.get("signed_at"))) != str(get_datetime(artifact["signed_at"])):
			frappe.throw(f"Production gate {gate_code} signed_at does not match its artifact.")
		row_expires = get_datetime(row.get("expires_at")) if row.get("expires_at") else None
		artifact_expires = get_datetime(artifact["expires_at"]) if artifact.get("expires_at") else None
		if row_expires != artifact_expires:
			frappe.throw(f"Production gate {gate_code} expiry does not match its artifact.")
		if artifact_expires and get_datetime(doc.get("valid_until")) > artifact_expires:
			frappe.throw(f"Production gate {gate_code} expires before the assessment validity window.")
	if seen != set(REQUIRED_GATE_CODES):
		frappe.throw("Production readiness is missing one or more mandatory gate codes.")
	if used_artifacts != set(artifact_index):
		frappe.throw("Production evidence manifest contains an artifact that is not bound to a gate row.")
	return hashlib.sha256(content).hexdigest()


def _normalize_manifest_artifact(artifact: Any) -> None:
	if not isinstance(artifact, dict):
		frappe.throw("Production evidence manifest artifacts must be JSON objects.")
	allowed_keys = {"id", "sha256", "gate_codes", "owner", "signed_at", "expires_at"}
	if set(artifact) - allowed_keys or not {"id", "sha256", "gate_codes", "owner", "signed_at"}.issubset(
		artifact
	):
		frappe.throw("Production evidence manifest artifact keys are invalid.")
	artifact_id = str(artifact.get("id") or "").strip()
	owner = str(artifact.get("owner") or "").strip()
	if not _SAFE_REFERENCE.fullmatch(artifact_id):
		frappe.throw("Production evidence manifest artifact ID is invalid.")
	if not _SAFE_REFERENCE.fullmatch(owner) or owner in {"Guest", "Administrator"}:
		frappe.throw("Production evidence manifest artifact owner must be a named signatory.")
	digest = str(artifact.get("sha256") or "").lower()
	if not _SHA256.fullmatch(digest):
		frappe.throw("Production evidence manifest artifact SHA-256 is invalid.")
	gate_codes = artifact.get("gate_codes")
	if (
		not isinstance(gate_codes, list)
		or not gate_codes
		or len(gate_codes) > len(REQUIRED_GATE_CODES)
		or len(set(map(str, gate_codes))) != len(gate_codes)
		or not set(map(str, gate_codes)).issubset(REQUIRED_GATE_CODES)
	):
		frappe.throw("Production evidence manifest artifact gate_codes are invalid.")
	if not artifact.get("signed_at"):
		frappe.throw("Production evidence manifest artifact requires signed_at.")
	signed_at = get_datetime(artifact.get("signed_at"))
	if signed_at > now_datetime():
		frappe.throw("Production evidence artifact signed_at cannot be in the future.")
	if artifact.get("expires_at") and get_datetime(artifact["expires_at"]) <= now_datetime():
		frappe.throw("Production evidence artifact is expired.")
	artifact["id"] = artifact_id
	artifact["owner"] = owner
	artifact["sha256"] = digest
	artifact["gate_codes"] = list(map(str, gate_codes))


def _read_private_manifest(doc) -> bytes:
	file_url = str(doc.get("evidence_manifest_file") or "")
	if not file_url:
		frappe.throw("Production readiness requires a private evidence manifest file.")
	rows = frappe.get_all(
		"File",
		filters={
			"file_url": file_url,
			"attached_to_doctype": ASSESSMENT_DOCTYPE,
			"attached_to_name": doc.name,
		},
		fields=["name", "file_name", "file_size", "file_url", "is_private"],
		order_by="name asc",
		limit_page_length=2,
	)
	if len(rows) != 1:
		frappe.throw("Evidence manifest must resolve to exactly one File attached to this assessment.")
	row = rows[0]
	if not int(row.get("is_private") or 0) or not str(row.get("file_url") or "").startswith(
		"/private/files/"
	):
		frappe.throw("Production evidence manifest must be stored as a private File.")
	if not str(row.get("file_name") or "").lower().endswith(".json"):
		frappe.throw("Production evidence manifest must use a .json filename.")
	stored_size = int(row.get("file_size") or 0)
	if stored_size <= 0 or stored_size > MAX_MANIFEST_BYTES:
		frappe.throw("Production evidence manifest size is outside the governed limit.")
	try:
		content = frappe.get_doc("File", row.name).get_content()
	except Exception as exc:
		raise frappe.ValidationError("Production evidence manifest content could not be read.") from exc
	if isinstance(content, str):
		content = content.encode("utf-8")
	if not isinstance(content, bytes) or not content or len(content) > MAX_MANIFEST_BYTES:
		frappe.throw("Production evidence manifest content is invalid or too large.")
	if len(content) != stored_size:
		frappe.throw("Production evidence manifest size metadata does not match its bytes.")
	return content


def _validate_release_record(release_name: Any, *, expected_manifest: dict[str, str]):
	name = str(release_name or "")
	if not name or not frappe.db.exists(RELEASE_DOCTYPE, name):
		frappe.throw("A deployed immutable IONE Release Record is required.")
	release = frappe.get_doc(RELEASE_DOCTYPE, name)
	stored_hash = str(release.get("record_hash") or "")
	if not stored_hash or not hmac.compare_digest(stored_hash, canonical_record_hash(release)):
		frappe.throw("The IONE Release Record hash is invalid.")
	if str(release.get("status") or "") != "Deployed":
		frappe.throw("The IONE Release Record must have Deployed status.")
	for fieldname in ("commit_sha", "frappe_commit", "flow_commit"):
		if not _COMMIT_SHA.fullmatch(str(release.get(fieldname) or "").lower()):
			frappe.throw(f"The IONE Release Record {fieldname} must be a full Git SHA.")
	for fieldname in (
		"deploy_candidate",
		"image",
		"database_backup",
		"files_backup",
		"test_report",
		"deployed_at",
	):
		if not str(release.get(fieldname) or "").strip():
			frappe.throw(f"The deployed IONE Release Record requires {fieldname}.")
	if str(release.get("deployed_by") or "") in {"", "Guest", "Administrator"}:
		frappe.throw("The deployed IONE Release Record requires a named deployer.")
	try:
		manifest = json.loads(str(release.get("manifest_json") or ""))
	except (TypeError, ValueError, json.JSONDecodeError) as exc:
		raise frappe.ValidationError("IONE Release Record manifest_json must be valid JSON.") from exc
	if not isinstance(manifest, dict) or str(manifest.get("format") or "") != RELEASE_MANIFEST_FORMAT:
		frappe.throw("IONE Release Record manifest format is not current.")
	apps = manifest.get("apps")
	if not isinstance(apps, dict) or dict(sorted((str(k), str(v).lower()) for k, v in apps.items())) != dict(
		sorted(expected_manifest.items())
	):
		frappe.throw("IONE Release Record app commits do not match the running release.")
	for fieldname in ("sbom_sha256", "ci_evidence_sha256", "deployment_evidence_sha256"):
		if not _SHA256.fullmatch(str(manifest.get(fieldname) or "").lower()):
			frappe.throw(f"IONE Release Record manifest requires a valid {fieldname}.")
	if str(release.get("commit_sha") or "").lower() != expected_manifest.get("ione_qms"):
		frappe.throw("IONE Release Record does not match the running IONE QMS commit.")
	if str(release.get("frappe_commit") or "").lower() != expected_manifest.get("frappe"):
		frappe.throw("IONE Release Record does not match the running Frappe commit.")
	if str(release.get("flow_commit") or "").lower() != expected_manifest.get("flow"):
		frappe.throw("IONE Release Record does not match the running Flow commit.")
	return release


def _create_activation_event(
	assessment,
	*,
	action: str,
	actor: str,
	reason: str,
	production_mode: bool,
	realtime_rules_enabled: bool,
	ai_enabled: bool,
	previous_event: str | None = None,
):
	release = _validate_release_record(
		assessment.get("release_record"),
		expected_manifest=_runtime_manifest(assessment),
	)
	manifest_hash = _runtime_commit_manifest_hash(_runtime_manifest(assessment))
	if previous_event is None:
		previous_event = str(
			frappe.db.get_single_value("IONE System Settings", "production_activation_event") or ""
		)
	event_at = now_datetime()
	event_key = hashlib.sha256(
		f"{assessment.name}|{action}|{actor}|{event_at}|{secrets.token_hex(16)}".encode()
	).hexdigest()
	key = active_hmac_key("ione_production_hmac")
	event = frappe.get_doc(
		{
			"doctype": EVENT_DOCTYPE,
			"event_key": event_key,
			"action": action,
			"assessment": assessment.name,
			"assessment_checksum": assessment.assessment_checksum,
			"release_record": release.name,
			"release_record_hash": release.record_hash,
			"schema_revision": SCHEMA_REVISION,
			"full_fingerprint": FULL_FINGERPRINT,
			"migration_completion_receipt": current_migration_completion_receipt(),
			"runtime_commit_manifest_hash": manifest_hash,
			"actor": actor,
			"event_at": event_at,
			"reason": reason,
			"production_mode": int(production_mode),
			"realtime_rules_enabled": int(realtime_rules_enabled),
			"ai_enabled": int(ai_enabled),
			"previous_event": previous_event or None,
			"hmac_key_id": key.key_id,
		}
	)
	event.event_signature = _event_signature(event, key.secret)
	previous_flag = getattr(frappe.flags, "ione_production_activation_event", None)
	frappe.flags.ione_production_activation_event = event_key
	try:
		event.insert(ignore_permissions=True)
	finally:
		frappe.flags.ione_production_activation_event = previous_flag
	return event


def _write_runtime_settings(
	*,
	event_name: str,
	production_mode: bool,
	realtime_rules_enabled: bool,
	ai_enabled: bool,
) -> None:
	previous_flag = getattr(frappe.flags, "ione_production_activation", None)
	frappe.flags.ione_production_activation = event_name
	try:
		if frappe.db.exists("DocType", "IONE AI Settings"):
			ai_settings = frappe.get_single("IONE AI Settings")
			ai_settings.enabled = int(ai_enabled)
			ai_settings.save(ignore_permissions=True)
		settings = frappe.get_single("IONE System Settings")
		settings.production_activation_event = event_name or None
		settings.production_mode = int(production_mode)
		settings.enable_realtime_rules = int(realtime_rules_enabled)
		settings.enable_ai = int(ai_enabled)
		settings.save(ignore_permissions=True)
	finally:
		frappe.flags.ione_production_activation = previous_flag


def current_runtime_commit_manifest() -> dict[str, str]:
	return dict(_cached_runtime_commit_manifest(_site_name()))


@lru_cache(maxsize=32)
def _cached_runtime_commit_manifest(site_name: str) -> tuple[tuple[str, str], ...]:
	del site_name
	try:
		from git import Repo

		apps = {"frappe", *(str(app) for app in frappe.get_installed_apps())}
		manifest: dict[str, str] = {}
		for app_name in sorted(apps):
			sha = str(Repo(frappe.get_app_source_path(app_name)).head.commit.hexsha).lower()
			if not _COMMIT_SHA.fullmatch(sha):
				frappe.throw(f"The running {app_name} checkout did not provide a full Git SHA.")
			manifest[app_name] = sha
		if not {"frappe", "flow", "ione_qms"}.issubset(manifest):
			frappe.throw("The running production app set is missing frappe, flow, or ione_qms.")
		return tuple(sorted(manifest.items()))
	except Exception as exc:
		if isinstance(exc, frappe.ValidationError):
			raise
		raise frappe.ValidationError("Cannot determine the running production app commits.") from exc


def _runtime_commit_manifest_hash(manifest: dict[str, str]) -> str:
	return hashlib.sha256(_canonical_json(manifest).encode()).hexdigest()


def _runtime_manifest(doc) -> dict[str, str]:
	try:
		parsed = json.loads(str(doc.get("runtime_commit_manifest_json") or ""))
	except (TypeError, ValueError, json.JSONDecodeError) as exc:
		raise frappe.ValidationError("Assessment runtime commit manifest is invalid JSON.") from exc
	if not isinstance(parsed, dict) or not parsed:
		frappe.throw("Assessment runtime commit manifest must be a non-empty JSON object.")
	normalized = {str(key): str(value).lower() for key, value in parsed.items()}
	if any(not _COMMIT_SHA.fullmatch(value) for value in normalized.values()):
		frappe.throw("Assessment runtime commit manifest contains an invalid Git SHA.")
	return dict(sorted(normalized.items()))


def _assessment_checksum(doc) -> str:
	rows = sorted(
		(
			{
				"gate_code": str(row.get("gate_code") or ""),
				"evidence_reference": str(row.get("evidence_reference") or ""),
				"evidence_sha256": str(row.get("evidence_sha256") or "").lower(),
				"signed_by": str(row.get("signed_by") or ""),
				"signed_at": str(row.get("signed_at") or ""),
				"expires_at": str(row.get("expires_at") or ""),
			}
			for row in (doc.get("gate_evidence") or [])
		),
		key=lambda row: row["gate_code"],
	)
	payload = {
		"release_record": str(doc.get("release_record") or ""),
		"site_name": str(doc.get("site_name") or ""),
		"schema_revision": str(doc.get("schema_revision") or ""),
		"full_fingerprint": str(doc.get("full_fingerprint") or ""),
		"migration_completion_receipt": str(doc.get("migration_completion_receipt") or ""),
		"runtime_commit_manifest_json": str(doc.get("runtime_commit_manifest_json") or ""),
		"valid_until": str(doc.get("valid_until") or ""),
		"evidence_manifest_file": str(doc.get("evidence_manifest_file") or ""),
		"evidence_manifest_sha256": str(doc.get("evidence_manifest_sha256") or ""),
		"gate_evidence": rows,
		"requested_by": str(doc.get("requested_by") or ""),
		"requested_at": str(doc.get("requested_at") or ""),
		"submitted_by": str(doc.get("submitted_by") or ""),
		"submitted_at": str(doc.get("submitted_at") or ""),
		"hmac_key_id": str(doc.get("hmac_key_id") or ""),
	}
	return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _assert_assessment_checksum(doc) -> None:
	expected = _assessment_checksum(doc)
	stored = str(doc.get("assessment_checksum") or "")
	if not stored or not hmac.compare_digest(stored, expected):
		frappe.throw("Production readiness assessment checksum is invalid.")
	key = verification_hmac_key("ione_production_hmac", str(doc.get("hmac_key_id") or ""))
	expected_signature = _assessment_signature(doc, key.secret)
	if not hmac.compare_digest(
		str(doc.get("assessment_signature") or ""),
		expected_signature,
	):
		frappe.throw("Production readiness assessment HMAC signature is invalid.")


def _assert_context_fields_unchanged(doc, previous) -> None:
	for fieldname in (
		"site_name",
		"schema_revision",
		"full_fingerprint",
		"migration_completion_receipt",
		"runtime_commit_manifest_json",
		"requested_by",
		"requested_at",
	):
		if doc.get(fieldname) != previous.get(fieldname):
			frappe.throw(f"Production readiness assessment {fieldname} is system-controlled.")


def _assert_assessment_evidence_unchanged(doc, previous) -> None:
	for fieldname in _ASSESSMENT_IMMUTABLE_FIELDS:
		if fieldname == "gate_evidence":
			if _gate_rows_for_compare(doc) != _gate_rows_for_compare(previous):
				frappe.throw("Submitted production readiness gate evidence is immutable.")
		elif doc.get(fieldname) != previous.get(fieldname):
			frappe.throw(f"Submitted production readiness assessment {fieldname} is immutable.")


def _gate_rows_for_compare(doc) -> list[dict[str, str]]:
	return sorted(
		[
			{
				fieldname: str(row.get(fieldname) or "")
				for fieldname in (
					"gate_code",
					"evidence_reference",
					"evidence_sha256",
					"signed_by",
					"signed_at",
					"expires_at",
				)
			}
			for row in (doc.get("gate_evidence") or [])
		],
		key=lambda row: row["gate_code"],
	)


def _clear_review_fields(doc) -> None:
	for fieldname in (
		"submitted_by",
		"submitted_at",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"assessment_checksum",
		"hmac_key_id",
		"assessment_signature",
		"governance_hmac_key_id",
		"governance_signature",
		"revoked_by",
		"revoked_at",
		"revocation_reason",
	):
		doc.set(fieldname, None)


def _assessment_result(doc) -> dict[str, str]:
	return {
		"assessment": str(doc.name),
		"status": str(doc.status),
		"assessment_checksum": str(doc.get("assessment_checksum") or ""),
	}


def _required_reason(value: Any, label: str) -> str:
	text = str(value or "").strip()
	if len(text) < 10 or len(text) > 500 or any(ord(char) < 32 and char not in "\r\n\t" for char in text):
		frappe.throw(f"{label} must contain 10-500 safe characters.")
	return text


def _require_named_actor(roles: frozenset[str] | set[str]) -> str:
	user = str(getattr(frappe.session, "user", "") or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Production readiness governance requires a named accountable user.", frappe.PermissionError
		)
	if not set(frappe.get_roles(user)).intersection(roles):
		frappe.throw("User lacks the required production-readiness role.", frappe.PermissionError)
	return user


def _site_name() -> str:
	name = str(getattr(getattr(frappe, "local", None), "site", "") or "").strip()
	if not name:
		frappe.throw("Current Frappe site identity is unavailable.")
	return name


def _as_bool(value: bool | int | str) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in {0, 1}:
		return bool(value)
	if isinstance(value, str) and value.strip().lower() in {"0", "1", "false", "true"}:
		return value.strip().lower() in {"1", "true"}
	frappe.throw("Production workload switch values must be boolean.")
	return False


def _canonical_json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _assessment_signature(doc, secret: bytes) -> str:
	message = "|".join(
		(
			"ione-production-assessment-v1",
			str(doc.name),
			str(doc.get("assessment_checksum") or ""),
			str(doc.get("hmac_key_id") or ""),
		)
	)
	return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def _set_governance_signature(doc) -> None:
	key = active_hmac_key("ione_production_hmac")
	doc.governance_hmac_key_id = key.key_id
	doc.governance_signature = _governance_signature(doc, key.secret)


def _governance_signature(doc, secret: bytes) -> str:
	payload = {
		"assessment": str(doc.name),
		"assessment_checksum": str(doc.get("assessment_checksum") or ""),
		"status": str(doc.get("status") or ""),
		"reviewed_by": str(doc.get("reviewed_by") or ""),
		"reviewed_at": str(doc.get("reviewed_at") or ""),
		"review_comment": str(doc.get("review_comment") or ""),
		"revoked_by": str(doc.get("revoked_by") or ""),
		"revoked_at": str(doc.get("revoked_at") or ""),
		"revocation_reason": str(doc.get("revocation_reason") or ""),
		"governance_hmac_key_id": str(doc.get("governance_hmac_key_id") or ""),
	}
	message = "ione-production-governance-v1|" + _canonical_json(payload)
	return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def _assert_governance_signature(doc) -> None:
	status = str(doc.get("status") or "")
	if status not in {"Approved", "Rejected", "Revoked"}:
		frappe.throw("Production readiness governance status is not independently decided.")
	key = verification_hmac_key(
		"ione_production_hmac",
		str(doc.get("governance_hmac_key_id") or ""),
	)
	expected = _governance_signature(doc, key.secret)
	if not hmac.compare_digest(str(doc.get("governance_signature") or ""), expected):
		frappe.throw("Production readiness governance HMAC signature is invalid.")


def _event_signature(doc, secret: bytes) -> str:
	payload = {
		"event_key": str(doc.get("event_key") or ""),
		"action": str(doc.get("action") or ""),
		"assessment": str(doc.get("assessment") or ""),
		"assessment_checksum": str(doc.get("assessment_checksum") or ""),
		"release_record": str(doc.get("release_record") or ""),
		"release_record_hash": str(doc.get("release_record_hash") or ""),
		"schema_revision": str(doc.get("schema_revision") or ""),
		"full_fingerprint": str(doc.get("full_fingerprint") or ""),
		"migration_completion_receipt": str(doc.get("migration_completion_receipt") or ""),
		"runtime_commit_manifest_hash": str(doc.get("runtime_commit_manifest_hash") or ""),
		"actor": str(doc.get("actor") or ""),
		"event_at": str(doc.get("event_at") or ""),
		"reason": str(doc.get("reason") or ""),
		"production_mode": int(doc.get("production_mode") or 0),
		"realtime_rules_enabled": int(doc.get("realtime_rules_enabled") or 0),
		"ai_enabled": int(doc.get("ai_enabled") or 0),
		"previous_event": str(doc.get("previous_event") or ""),
		"hmac_key_id": str(doc.get("hmac_key_id") or ""),
	}
	message = "ione-production-event-v1|" + _canonical_json(payload)
	return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def _verify_event_signature(doc) -> None:
	key = verification_hmac_key("ione_production_hmac", str(doc.get("hmac_key_id") or ""))
	expected = _event_signature(doc, key.secret)
	if not hmac.compare_digest(str(doc.get("event_signature") or ""), expected):
		frappe.throw("Production activation event HMAC signature is invalid.")


@contextmanager
def _trusted_assessment_mutation(assessment: str):
	previous = getattr(frappe.flags, "ione_production_readiness_mutation", None)
	frappe.flags.ione_production_readiness_mutation = assessment
	try:
		yield
	finally:
		frappe.flags.ione_production_readiness_mutation = previous

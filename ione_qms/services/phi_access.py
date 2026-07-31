from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime

from ione_qms.constants import APP_ROLES
from ione_qms.permissions import resolve_phi_identity_authorization
from ione_qms.privacy_registry import PHI_DISCLOSURE_FIELDS_BY_DOCTYPE
from ione_qms.services.crypto_keys import active_hmac_key, verification_hmac_key
from ione_qms.services.immutability import prevent_delete, validate_append_only
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready
from ione_qms.services.scope_hierarchy import clinical_scope_errors

POLICY_DOCTYPE = "IONE PHI Disclosure Policy"
ACCESS_RECEIPT_DOCTYPE = "IONE PHI Access Receipt"
PHI_READER_ROLE = "IONE PHI Identity Reader"

POLICY_AUTHOR_ROLES = frozenset({"IONE QC Administrator", "IONE Medical Affairs"})
POLICY_REVIEWER_ROLES = frozenset({"IONE Medical Affairs", "IONE QMS Auditor"})
POLICY_STATUSES = frozenset({"Draft", "Under Review", "Scheduled", "Active", "Retired"})
PHI_FIELDS_BY_DOCTYPE = PHI_DISCLOSURE_FIELDS_BY_DOCTYPE
PHI_BUSINESS_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE QMS Auditor",
		"IONE QMS Medical Record Coder",
		"IONE Medical Record Expert Reviewer",
	}
)

MAX_ACTIVE_POLICIES_PER_PURPOSE = 100
MAX_POLICY_FIELDS = sum(len(fields) for fields in PHI_FIELDS_BY_DOCTYPE.values())
MAX_POLICY_ROLES = len(PHI_BUSINESS_ROLES)

_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_.-]{2,63}")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_APPROVAL_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,139}")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_HEX_64_PATTERN = re.compile(r"[0-9a-f]{64}")
_PHI_AUTHORIZATION_GLOBAL_LOCK = "ione-qms:phi-authorization-global:v1"
_POLICY_MUTATION_TOKEN = object()
_RECEIPT_INSERT_TOKEN = object()

_POLICY_SEMANTIC_FIELDS = (
	"policy_code",
	"policy_version",
	"policy_name",
	"purpose_code",
	"purpose_description",
	"hospital",
	"campus",
	"department",
	"ward",
	"allowed_fields_json",
	"allowed_reader_roles_json",
	"max_disclosures_per_user_per_minute",
	"max_disclosures_per_record_per_minute",
	"effective_from",
	"effective_to",
	"external_approval_reference",
	"hmac_key_id",
	"policy_key",
	"policy_scope_key",
)
_POLICY_MANAGED_FIELDS = (
	"status",
	"active_slot_key",
	"requested_by",
	"requested_at",
	"submitted_by",
	"submitted_at",
	"reviewed_by",
	"reviewed_at",
	"review_comment",
	"policy_checksum",
	"hmac_key_id",
	"retired_by",
	"retired_at",
	"retirement_reason",
)


def prepare_phi_disclosure_policy(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new():
		return
	user = _require_named_actor(POLICY_AUTHOR_ROLES)
	doc.status = "Draft"
	doc.requested_by = user
	doc.requested_at = now_datetime()
	for fieldname in _POLICY_MANAGED_FIELDS:
		if fieldname not in {"status", "requested_by", "requested_at"}:
			doc.set(fieldname, None)


def validate_phi_disclosure_policy(doc, method: str | None = None) -> None:
	del method
	_normalize_policy_configuration(doc)
	_validate_policy_scope(doc)
	_validate_policy_dates(doc)
	_set_policy_keys(doc)
	_validate_policy_lifecycle(doc)


def submit_phi_disclosure_policy(policy: str) -> dict[str, str]:
	user = _require_named_actor(POLICY_AUTHOR_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:phi-policy:{policy}", timeout=10):
		doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
		if str(doc.get("status") or "") != "Draft":
			frappe.throw("Only a Draft PHI disclosure policy can be submitted.")
		if str(doc.get("requested_by") or "") != user:
			frappe.throw(
				"Only the accountable policy author can submit this PHI disclosure policy.",
				frappe.PermissionError,
			)
		_normalize_policy_configuration(doc)
		_validate_policy_scope(doc)
		_validate_policy_dates(doc)
		_set_policy_keys(doc)
		doc.hmac_key_id = active_hmac_key("ione_phi_hmac").key_id
		doc.status = "Under Review"
		doc.submitted_by = user
		doc.submitted_at = now_datetime()
		doc.policy_checksum = _policy_checksum(doc)
		with _trusted_policy_mutation():
			doc.save(ignore_permissions=True)
	return {
		"policy": str(doc.name),
		"status": str(doc.status),
		"policy_checksum": str(doc.policy_checksum),
	}


def approve_phi_disclosure_policy(policy: str, review_comment: str) -> dict[str, str]:
	user = _require_named_actor(POLICY_REVIEWER_ROLES)
	comment = _required_comment(review_comment, "Review comment")
	family_lock = _policy_family_lock_from_name(policy)
	with frappe.db.advisory_lock(family_lock, timeout=10):
		with frappe.db.advisory_lock(f"ione-qms:phi-policy:{policy}", timeout=10):
			doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
			_assert_policy_family_lock_is_current(doc, family_lock)
			if str(doc.get("status") or "") != "Under Review":
				frappe.throw("Only an Under Review PHI disclosure policy can be approved.")
			if user in {
				str(doc.get("requested_by") or ""),
				str(doc.get("submitted_by") or ""),
			}:
				frappe.throw(
					"The PHI disclosure policy author cannot approve the same policy.",
					frappe.PermissionError,
				)
			_assert_policy_checksum(doc)
			_assert_policy_not_expired(doc)
			_assert_policy_schedule_slot_available(doc)
			doc.reviewed_by = user
			doc.reviewed_at = now_datetime()
			doc.review_comment = comment
			if get_datetime(doc.get("effective_from")) > now_datetime():
				doc.status = "Scheduled"
				doc.active_slot_key = None
			else:
				_retire_active_exact_scope(doc, replacing_policy=str(doc.name))
				doc.status = "Active"
				doc.active_slot_key = doc.policy_scope_key
			with _trusted_policy_mutation():
				doc.save(ignore_permissions=True)
	return {
		"policy": str(doc.name),
		"status": str(doc.status),
		"policy_checksum": str(doc.policy_checksum),
	}


def retire_phi_disclosure_policy(policy: str, retirement_reason: str) -> dict[str, str]:
	user = _require_named_actor(POLICY_REVIEWER_ROLES)
	reason = _required_comment(retirement_reason, "Retirement reason")
	family_lock = _policy_family_lock_from_name(policy)
	with frappe.db.advisory_lock(family_lock, timeout=10):
		with frappe.db.advisory_lock(f"ione-qms:phi-policy:{policy}", timeout=10):
			doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
			_assert_policy_family_lock_is_current(doc, family_lock)
			if str(doc.get("status") or "") not in {"Active", "Scheduled"}:
				frappe.throw("Only an Active or Scheduled PHI disclosure policy can be retired.")
			_assert_policy_checksum(doc)
			doc.status = "Retired"
			doc.active_slot_key = None
			doc.retired_by = user
			doc.retired_at = now_datetime()
			doc.retirement_reason = reason
			with _trusted_policy_mutation():
				doc.save(ignore_permissions=True)
	return {"policy": str(doc.name), "status": str(doc.status)}


def disclose_phi_identity(
	*,
	reference_doctype: str,
	reference_name: str,
	fields: list[str] | str,
	purpose_code: str,
	request_id: str,
) -> dict[str, Any]:
	"""Acquire the global PHI governance lock before any mutable authorization row."""
	with frappe.db.advisory_lock(_PHI_AUTHORIZATION_GLOBAL_LOCK, timeout=10):
		return _disclose_phi_identity_locked(
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			fields=fields,
			purpose_code=purpose_code,
			request_id=request_id,
		)


def _disclose_phi_identity_locked(
	*,
	reference_doctype: str,
	reference_name: str,
	fields: list[str] | str,
	purpose_code: str,
	request_id: str,
) -> dict[str, Any]:
	"""Return a minimal PHI identity view only after a durable-in-transaction audit insert."""
	require_post_migrate_runtime_ready("disclose governed patient identity")
	user = _require_named_actor(frozenset({PHI_READER_ROLE}))
	if "IONE Agent Service" in frappe.get_roles(user):
		frappe.throw("Service identities cannot disclose patient identity fields.", frappe.PermissionError)
	doctype = str(reference_doctype or "").strip()
	if doctype not in PHI_FIELDS_BY_DOCTYPE:
		frappe.throw("The requested record type is not eligible for PHI identity disclosure.")
	name = str(reference_name or "").strip()
	if not name or len(name) > 255:
		frappe.throw("A bounded reference_name is required.")
	purpose = _normalized_code(purpose_code, "purpose_code")
	opaque_request_id = str(request_id or "").strip()
	if not _REQUEST_ID_PATTERN.fullmatch(opaque_request_id):
		frappe.throw("request_id must be an opaque 8-128 character identifier.")
	requested_fields = _normalize_requested_fields(fields, doctype)
	_document, authorization_basis, role_snapshot = _authorized_identity_document(
		doctype,
		name,
		user,
	)
	scope = dict(authorization_basis["authorized_scope"])
	with frappe.db.advisory_lock(_policy_family_lock(scope["hospital"], purpose), timeout=10):
		_activate_due_policies(scope=scope, purpose_code=purpose)
		resolved_policy = _resolve_active_policy(scope=scope, purpose_code=purpose)
		with frappe.db.advisory_lock(
			f"ione-qms:phi-policy:{resolved_policy.name}",
			timeout=10,
		):
			policy = frappe.get_doc(POLICY_DOCTYPE, resolved_policy.name, for_update=True)
			_assert_policy_live_for_disclosure(policy, scope, purpose)
			_assert_policy_authorizes(
				policy,
				doctype,
				requested_fields,
				set(authorization_basis["authorizing_roles"]),
			)
			policy_name = str(policy.name)
			policy_checksum = str(policy.policy_checksum)
			user_rate_limit = _bounded_integer(
				policy.get("max_disclosures_per_user_per_minute"),
				"max_disclosures_per_user_per_minute",
				minimum=1,
				maximum=120,
			)
			record_rate_limit = _bounded_integer(
				policy.get("max_disclosures_per_record_per_minute"),
				"max_disclosures_per_record_per_minute",
				minimum=1,
				maximum=20,
			)
			if record_rate_limit > user_rate_limit:
				frappe.throw("The approved PHI disclosure policy has invalid rate limits.")

	receipt_hmac_key_id = active_hmac_key("ione_phi_hmac").key_id
	request_id_hash = _site_hmac(
		f"request|{user}|{opaque_request_id}",
		key_id=receipt_hmac_key_id,
	)
	receipt_key = _site_hmac(
		f"receipt-hash|{user}|{request_id_hash}",
		key_id=receipt_hmac_key_id,
	)
	field_manifest_json = _canonical_json(requested_fields)
	field_manifest_hash = hashlib.sha256(field_manifest_json.encode()).hexdigest()
	authorization_basis_json = _canonical_json(authorization_basis)
	authorization_basis_hash = hashlib.sha256(authorization_basis_json.encode()).hexdigest()
	role_snapshot_json = _canonical_json(role_snapshot)
	role_snapshot_hash = hashlib.sha256(role_snapshot_json.encode()).hexdigest()
	reference_hash = _site_hmac(
		f"reference|{doctype}|{name}",
		key_id=receipt_hmac_key_id,
	)
	session_hash = _site_hmac(
		f"session|{user}|{_session_id()}",
		key_id=receipt_hmac_key_id,
	)
	scope_json = _canonical_json(scope)
	_enforce_disclosure_rate_limit(
		user=user,
		reference_hash=reference_hash,
		user_limit=user_rate_limit,
		record_limit=record_rate_limit,
		hmac_key_id=receipt_hmac_key_id,
	)

	with frappe.db.advisory_lock(f"ione-qms:phi-access:{receipt_key}", timeout=10):
		if frappe.db.exists(ACCESS_RECEIPT_DOCTYPE, receipt_key):
			frappe.throw(
				"request_id has already been consumed; use a new opaque request identifier.",
				frappe.ValidationError,
			)
		(
			revalidated_document,
			revalidated_basis,
			revalidated_role_snapshot,
		) = _authorized_identity_document(doctype, name, user)
		if not hmac.compare_digest(
			authorization_basis_hash,
			hashlib.sha256(_canonical_json(revalidated_basis).encode()).hexdigest(),
		) or not hmac.compare_digest(
			role_snapshot_hash,
			hashlib.sha256(_canonical_json(revalidated_role_snapshot).encode()).hexdigest(),
		):
			frappe.throw(
				"PHI authorization changed during disclosure; retry the request.",
				frappe.PermissionError,
			)
		values = {fieldname: revalidated_document.get(fieldname) for fieldname in requested_fields}
		receipt = frappe.get_doc(
			{
				"doctype": ACCESS_RECEIPT_DOCTYPE,
				"receipt_key": receipt_key,
				"request_id_hash": request_id_hash,
				"hmac_key_id": receipt_hmac_key_id,
				"reader_user": user,
				"purpose_code": purpose,
				"reference_doctype": doctype,
				"reference_hash": reference_hash,
				"field_manifest_json": field_manifest_json,
				"field_manifest_hash": field_manifest_hash,
				"disclosure_policy": policy_name,
				"policy_checksum": policy_checksum,
				"authorized_scope_json": scope_json,
				"authorization_basis_json": authorization_basis_json,
				"authorization_basis_hash": authorization_basis_hash,
				"role_snapshot_json": role_snapshot_json,
				"role_snapshot_hash": role_snapshot_hash,
				"session_hash": session_hash,
				"accessed_at": now_datetime(),
				"result_code": "AUTHORIZED",
			}
		)
		receipt.flags.ione_phi_access_receipt = _RECEIPT_INSERT_TOKEN
		receipt.insert(ignore_permissions=True)

	_set_no_store_response_headers()
	return {
		"receipt": str(receipt.name),
		"disclosed_fields": requested_fields,
		"data": {fieldname: values.get(fieldname) for fieldname in requested_fields},
	}


def validate_phi_access_receipt(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new() or getattr(doc.flags, "ione_phi_access_receipt", None) is not _RECEIPT_INSERT_TOKEN:
		frappe.throw(
			"PHI access receipts are system-managed append-only records.",
			frappe.PermissionError,
		)
	for fieldname in (
		"receipt_key",
		"request_id_hash",
		"reference_hash",
		"field_manifest_hash",
		"policy_checksum",
		"session_hash",
		"authorization_basis_hash",
		"role_snapshot_hash",
	):
		if not _HEX_64_PATTERN.fullmatch(str(doc.get(fieldname) or "")):
			frappe.throw(f"PHI access receipt {fieldname} is invalid.")
	if str(doc.get("result_code") or "") != "AUTHORIZED":
		frappe.throw("PHI access receipt result code is invalid.")
	if str(doc.get("reader_user") or "") != str(frappe.session.user or ""):
		frappe.throw("PHI access receipt reader does not match the active session.")
	manifest = _parse_json(doc.get("field_manifest_json"), list, "field_manifest_json")
	if (
		not manifest
		or any(not isinstance(fieldname, str) for fieldname in manifest)
		or manifest != sorted(set(manifest))
	):
		frappe.throw("PHI access receipt field manifest is invalid.")
	manifest_json = _canonical_json(manifest)
	if not hmac.compare_digest(
		str(doc.get("field_manifest_hash") or ""),
		hashlib.sha256(manifest_json.encode()).hexdigest(),
	):
		frappe.throw("PHI access receipt field manifest hash does not match.")
	scope = _parse_json(doc.get("authorized_scope_json"), dict, "authorized_scope_json")
	if set(scope) != {"hospital", "campus", "department", "ward"}:
		frappe.throw("PHI access receipt scope is invalid.")
	if any(not isinstance(value, str) for value in scope.values()):
		frappe.throw("PHI access receipt scope values are invalid.")
	basis = _parse_json(
		doc.get("authorization_basis_json"),
		dict,
		"authorization_basis_json",
	)
	basis_json = _canonical_json(basis)
	if not hmac.compare_digest(
		str(doc.get("authorization_basis_hash") or ""),
		hashlib.sha256(basis_json.encode()).hexdigest(),
	):
		frappe.throw("PHI access receipt authorization basis hash does not match.")
	if basis.get("authorized_scope") != scope:
		frappe.throw("PHI access receipt authorization basis scope does not match.")
	role_snapshot = _parse_json(
		doc.get("role_snapshot_json"),
		dict,
		"role_snapshot_json",
	)
	role_snapshot_json = _canonical_json(role_snapshot)
	if not hmac.compare_digest(
		str(doc.get("role_snapshot_hash") or ""),
		hashlib.sha256(role_snapshot_json.encode()).hexdigest(),
	):
		frappe.throw("PHI access receipt role snapshot hash does not match.")
	if sorted(basis.get("authorizing_roles") or []) != sorted(role_snapshot.get("authorizing_roles") or []):
		frappe.throw("PHI access receipt authorizing roles do not match.")
	doctype = str(doc.get("reference_doctype") or "")
	if doctype not in PHI_FIELDS_BY_DOCTYPE or not set(manifest).issubset(PHI_FIELDS_BY_DOCTYPE[doctype]):
		frappe.throw("PHI access receipt field manifest is outside the governed registry.")
	policy = frappe.get_doc(POLICY_DOCTYPE, doc.get("disclosure_policy"))
	if not hmac.compare_digest(
		str(doc.get("policy_checksum") or ""),
		str(policy.get("policy_checksum") or ""),
	):
		frappe.throw("PHI access receipt policy checksum does not match.")
	hmac_key_id = str(doc.get("hmac_key_id") or "")
	expected_receipt_key = _site_hmac(
		f"receipt-hash|{doc.get('reader_user')}|{doc.get('request_id_hash')}",
		key_id=hmac_key_id,
	)
	if not hmac.compare_digest(
		str(doc.get("receipt_key") or ""),
		expected_receipt_key,
	):
		frappe.throw("PHI access receipt key does not match its request binding.")
	expected_session_hash = _site_hmac(
		f"session|{doc.get('reader_user')}|{_session_id()}",
		key_id=hmac_key_id,
	)
	if not hmac.compare_digest(str(doc.get("session_hash") or ""), expected_session_hash):
		frappe.throw("PHI access receipt session binding does not match.")
	validate_append_only(doc)
	doc.record_hash = _phi_receipt_auth_tag(doc)


def verify_phi_access_receipt_integrity(doc, method: str | None = None) -> None:
	del method
	if doc.is_new():
		return
	expected = _phi_receipt_auth_tag(doc)
	if not hmac.compare_digest(str(doc.get("record_hash") or ""), expected):
		frappe.throw("PHI access receipt HMAC integrity verification failed.")


def prevent_phi_record_deletion(doc, method: str | None = None) -> None:
	prevent_delete(doc, method)


def _normalize_policy_configuration(doc) -> None:
	doc.policy_code = _normalized_code(doc.get("policy_code"), "policy_code")
	version = str(doc.get("policy_version") or "").strip()
	if not _VERSION_PATTERN.fullmatch(version):
		frappe.throw("policy_version must contain 1-32 identifier characters.")
	doc.policy_version = version
	name = str(doc.get("policy_name") or "").strip()
	if not 3 <= len(name) <= 140:
		frappe.throw("policy_name must contain 3-140 characters.")
	doc.policy_name = name
	doc.purpose_code = _normalized_code(doc.get("purpose_code"), "purpose_code")
	description = str(doc.get("purpose_description") or "").strip()
	if not 10 <= len(description) <= 500:
		frappe.throw("purpose_description must contain 10-500 characters.")
	doc.purpose_description = description
	reference = str(doc.get("external_approval_reference") or "").strip()
	if not _APPROVAL_REFERENCE_PATTERN.fullmatch(reference) or "://" in reference or ".." in reference:
		frappe.throw("external_approval_reference must be an 8-140 character approved governance identifier.")
	doc.external_approval_reference = reference
	allowed_fields = _normalize_policy_fields(doc.get("allowed_fields_json"))
	allowed_roles = _normalize_policy_roles(doc.get("allowed_reader_roles_json"))
	doc.allowed_fields_json = _canonical_json(allowed_fields)
	doc.allowed_reader_roles_json = _canonical_json(allowed_roles)
	user_limit = _bounded_integer(
		doc.get("max_disclosures_per_user_per_minute"),
		"max_disclosures_per_user_per_minute",
		minimum=1,
		maximum=120,
	)
	record_limit = _bounded_integer(
		doc.get("max_disclosures_per_record_per_minute"),
		"max_disclosures_per_record_per_minute",
		minimum=1,
		maximum=20,
	)
	if record_limit > user_limit:
		frappe.throw("The per-record PHI disclosure limit cannot exceed the per-user limit.")
	doc.max_disclosures_per_user_per_minute = user_limit
	doc.max_disclosures_per_record_per_minute = record_limit


def _normalize_policy_fields(value: Any) -> dict[str, list[str]]:
	parsed = _parse_json(value, dict, "allowed_fields_json")
	if not parsed or len(parsed) > len(PHI_FIELDS_BY_DOCTYPE):
		frappe.throw("allowed_fields_json must authorize at least one supported record type.")
	normalized: dict[str, list[str]] = {}
	total = 0
	for doctype, fields in parsed.items():
		if doctype not in PHI_FIELDS_BY_DOCTYPE or not isinstance(fields, list) or not fields:
			frappe.throw("allowed_fields_json contains an unsupported record type or empty field list.")
		if any(not isinstance(fieldname, str) for fieldname in fields):
			frappe.throw("allowed_fields_json field names must be strings.")
		canonical_fields = sorted(set(fields))
		if len(canonical_fields) != len(fields):
			frappe.throw("allowed_fields_json must not contain duplicate field names.")
		if not set(canonical_fields).issubset(PHI_FIELDS_BY_DOCTYPE[doctype]):
			frappe.throw("allowed_fields_json contains an unsupported PHI identity field.")
		normalized[doctype] = canonical_fields
		total += len(canonical_fields)
	if total > MAX_POLICY_FIELDS:
		frappe.throw("allowed_fields_json exceeds the bounded PHI field registry.")
	return dict(sorted(normalized.items()))


def _normalize_policy_roles(value: Any) -> list[str]:
	parsed = _parse_json(value, list, "allowed_reader_roles_json")
	if not parsed or len(parsed) > MAX_POLICY_ROLES or any(not isinstance(role, str) for role in parsed):
		frappe.throw("allowed_reader_roles_json must contain a bounded non-empty role list.")
	roles = sorted(set(parsed))
	if len(roles) != len(parsed):
		frappe.throw("allowed_reader_roles_json must not contain duplicate roles.")
	if not set(roles).issubset(PHI_BUSINESS_ROLES):
		frappe.throw("allowed_reader_roles_json contains a role that cannot receive patient identity.")
	return roles


def _validate_policy_scope(doc) -> None:
	if not str(doc.get("hospital") or ""):
		frappe.throw("PHI disclosure policies require an exact hospital.")
	errors = clinical_scope_errors(doc)
	if errors:
		frappe.throw(
			"PHI disclosure policy scope is inconsistent: " + ", ".join(errors),
			frappe.ValidationError,
		)


def _validate_policy_dates(doc) -> None:
	try:
		effective_from = get_datetime(doc.get("effective_from"))
		effective_to = get_datetime(doc.get("effective_to")) if doc.get("effective_to") else None
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("PHI disclosure policy effective dates are invalid.") from exc
	if not effective_from:
		frappe.throw("PHI disclosure policies require effective_from.")
	if effective_to and effective_to <= effective_from:
		frappe.throw("effective_to must be later than effective_from.")


def _set_policy_keys(doc) -> None:
	doc.policy_key = hashlib.sha256(
		f"{doc.get('policy_code')}|{doc.get('policy_version')}".encode()
	).hexdigest()
	doc.policy_scope_key = hashlib.sha256(
		_canonical_json(
			{
				"purpose_code": str(doc.get("purpose_code") or ""),
				**_document_scope(doc),
			}
		).encode()
	).hexdigest()
	doc.active_slot_key = doc.policy_scope_key if str(doc.get("status") or "") == "Active" else None


def _validate_policy_lifecycle(doc) -> None:
	status = str(doc.get("status") or "")
	if status not in POLICY_STATUSES:
		frappe.throw("PHI disclosure policy status is invalid.")
	if doc.is_new():
		if status != "Draft" or _managed_policy_mutation():
			frappe.throw("New PHI disclosure policies must start in Draft.")
		if not doc.get("requested_by") or not doc.get("requested_at"):
			frappe.throw("New PHI disclosure policies require author provenance.")
		doc.policy_checksum = None
		doc.active_slot_key = None
		return
	previous = doc.get_doc_before_save()
	if not previous:
		frappe.throw("PHI disclosure policy prior state is unavailable.")
	previous_status = str(previous.get("status") or "")
	if not _managed_policy_mutation():
		user = _require_named_actor(POLICY_AUTHOR_ROLES)
		if user != str(previous.get("requested_by") or ""):
			frappe.throw(
				"Only the accountable policy author can edit this Draft policy.",
				frappe.PermissionError,
			)
		if previous_status != "Draft" or status != "Draft":
			frappe.throw(
				"PHI disclosure policy lifecycle changes require the governed API.",
				frappe.PermissionError,
			)
		for fieldname in _POLICY_MANAGED_FIELDS:
			if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or ""):
				frappe.throw("PHI disclosure policy managed fields cannot be edited directly.")
	else:
		allowed_transitions = {
			("Draft", "Under Review"),
			("Under Review", "Active"),
			("Under Review", "Scheduled"),
			("Scheduled", "Active"),
			("Scheduled", "Retired"),
			("Active", "Retired"),
		}
		if (previous_status, status) not in allowed_transitions:
			frappe.throw(f"Invalid PHI disclosure policy transition: {previous_status} -> {status}")
	if previous_status != "Draft":
		changed = [
			fieldname
			for fieldname in _POLICY_SEMANTIC_FIELDS
			if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
		]
		if changed:
			frappe.throw(
				"Submitted PHI disclosure policy semantics are immutable; create a new policy version."
			)
	if status in {"Under Review", "Scheduled", "Active", "Retired"}:
		_assert_policy_checksum(doc)
	if status in {"Scheduled", "Active"}:
		if (
			not doc.get("reviewed_by")
			or not doc.get("reviewed_at")
			or not str(doc.get("review_comment") or "").strip()
		):
			frappe.throw(
				"Scheduled and Active PHI disclosure policies require independent review provenance."
			)
	if status == "Active":
		if doc.get("active_slot_key") != doc.get("policy_scope_key"):
			frappe.throw("Active PHI disclosure policy slot identity is invalid.")
	else:
		doc.active_slot_key = None


def _policy_checksum(doc) -> str:
	payload = {
		fieldname: doc.get(fieldname)
		for fieldname in _POLICY_SEMANTIC_FIELDS
		if fieldname not in {"policy_key", "policy_scope_key"}
	}
	payload["policy_key"] = str(doc.get("policy_key") or "")
	payload["policy_scope_key"] = str(doc.get("policy_scope_key") or "")
	return _site_hmac(
		f"phi-policy|{_canonical_json(payload)}",
		key_id=str(doc.get("hmac_key_id") or ""),
	)


def _assert_policy_checksum(doc) -> None:
	expected = _policy_checksum(doc)
	if not hmac.compare_digest(str(doc.get("policy_checksum") or ""), expected):
		frappe.throw("PHI disclosure policy checksum does not match its approved semantics.")


def _assert_policy_not_expired(doc) -> None:
	now = now_datetime()
	effective_to = get_datetime(doc.get("effective_to")) if doc.get("effective_to") else None
	if effective_to and effective_to <= now:
		frappe.throw("An already expired PHI disclosure policy cannot be activated.")


def _assert_policy_schedule_slot_available(doc) -> None:
	rows = frappe.db.sql(
		f"select name, status, effective_from, effective_to from `tab{POLICY_DOCTYPE}` "  # noqa: S608
		"where policy_scope_key = %s and status in ('Active', 'Scheduled') "
		"and name != %s for update",
		(str(doc.get("policy_scope_key") or ""), str(doc.name or "")),
		as_dict=True,
	)
	scheduled = [row for row in rows if str(row.get("status") or "") == "Scheduled"]
	if scheduled:
		frappe.throw(
			"Another independently approved PHI disclosure policy is already scheduled for this exact scope."
		)


def _retire_active_exact_scope(doc, *, replacing_policy: str) -> None:
	rows = frappe.db.sql(
		f"select name from `tab{POLICY_DOCTYPE}` "  # noqa: S608
		"where policy_scope_key = %s and status = 'Active' and name != %s for update",
		(str(doc.get("policy_scope_key") or ""), str(doc.name or "")),
		as_dict=True,
	)
	for row in rows:
		current = frappe.get_doc(POLICY_DOCTYPE, row.name, for_update=True)
		_assert_policy_checksum(current)
		current.status = "Retired"
		current.active_slot_key = None
		current.retired_by = str(doc.get("reviewed_by") or "")
		current.retired_at = now_datetime()
		current.retirement_reason = f"Superseded by independently approved policy {replacing_policy}."
		with _trusted_policy_mutation():
			current.save(ignore_permissions=True)


def _activate_due_policies(*, scope: Mapping[str, str], purpose_code: str) -> None:
	filters = _matching_policy_filters(
		scope=scope,
		purpose_code=purpose_code,
		status="Scheduled",
	)
	rows = frappe.get_all(
		POLICY_DOCTYPE,
		filters=filters,
		fields=["name", "effective_from"],
		order_by="effective_from asc, name asc",
		limit_page_length=9,
	)
	if len(rows) > 8:
		frappe.throw("Scheduled PHI policy resolution violated exact scope cardinality.")
	now = now_datetime()
	for row in rows:
		if get_datetime(row.get("effective_from")) > now:
			continue
		policy = frappe.get_doc(POLICY_DOCTYPE, row.name, for_update=True)
		if str(policy.get("status") or "") != "Scheduled":
			continue
		_assert_policy_checksum(policy)
		effective_to = get_datetime(policy.get("effective_to")) if policy.get("effective_to") else None
		if effective_to and effective_to <= now:
			policy.status = "Retired"
			policy.active_slot_key = None
			policy.retired_by = str(policy.get("reviewed_by") or "")
			policy.retired_at = now
			policy.retirement_reason = "Approved schedule elapsed before activation."
			with _trusted_policy_mutation():
				policy.save(ignore_permissions=True)
			continue
		_retire_active_exact_scope(policy, replacing_policy=str(policy.name))
		policy.status = "Active"
		policy.active_slot_key = policy.policy_scope_key
		with _trusted_policy_mutation():
			policy.save(ignore_permissions=True)


def activate_due_phi_disclosure_policies(batch_size: int = 100) -> dict[str, int | bool]:
	"""Bounded scheduler; request-time activation remains the no-gap safety net."""
	require_post_migrate_runtime_ready("activate scheduled PHI disclosure policies")
	limit = min(max(int(batch_size or 100), 1), 500)
	rows = frappe.get_all(
		POLICY_DOCTYPE,
		filters={
			"status": "Scheduled",
			"effective_from": ["<=", now_datetime()],
		},
		fields=["name", "hospital", "purpose_code"],
		order_by="effective_from asc, name asc",
		limit_page_length=limit + 1,
	)
	activated = 0
	for row in rows[:limit]:
		with frappe.db.advisory_lock(
			_policy_family_lock(str(row.hospital), str(row.purpose_code)),
			timeout=10,
		):
			before = frappe.db.get_value(POLICY_DOCTYPE, row.name, "status")
			doc = frappe.get_doc(POLICY_DOCTYPE, row.name)
			_activate_due_policies(
				scope=_document_scope(doc),
				purpose_code=str(doc.get("purpose_code") or ""),
			)
			after = frappe.db.get_value(POLICY_DOCTYPE, row.name, "status")
			activated += int(before == "Scheduled" and after == "Active")
	return {"activated": activated, "has_more": len(rows) > limit}


def _policy_family_lock_from_name(policy: str) -> str:
	row = frappe.db.get_value(
		POLICY_DOCTYPE,
		policy,
		["hospital", "purpose_code"],
		as_dict=True,
	)
	if not row or not row.get("hospital") or not row.get("purpose_code"):
		frappe.throw("The PHI disclosure policy is unavailable.")
	return _policy_family_lock(str(row.hospital), str(row.purpose_code))


def _policy_family_lock(hospital: str, purpose_code: str) -> str:
	family_hash = hashlib.sha256(
		_canonical_json(
			{
				"hospital": str(hospital or ""),
				"purpose_code": str(purpose_code or ""),
			}
		).encode()
	).hexdigest()
	return f"ione-qms:phi-policy-family:{family_hash}"


def _assert_policy_family_lock_is_current(doc, family_lock: str) -> None:
	current = _policy_family_lock(
		str(doc.get("hospital") or ""),
		str(doc.get("purpose_code") or ""),
	)
	if not hmac.compare_digest(current, family_lock):
		frappe.throw(
			"PHI disclosure policy scope changed while acquiring its governance lock; retry the action."
		)


def _authorized_identity_document(doctype: str, name: str, user: str):
	return resolve_phi_identity_authorization(doctype, name, user)


def _assert_policy_live_for_disclosure(
	policy,
	scope: Mapping[str, str],
	purpose_code: str,
) -> None:
	now = now_datetime()
	effective_from = get_datetime(policy.get("effective_from"))
	effective_to = get_datetime(policy.get("effective_to")) if policy.get("effective_to") else None
	if (
		str(policy.get("status") or "") != "Active"
		or str(policy.get("purpose_code") or "") != purpose_code
		or not _policy_matches_scope(policy, scope)
		or not effective_from
		or effective_from > now
		or (effective_to and effective_to <= now)
	):
		frappe.throw(
			"The approved PHI disclosure policy is no longer effective for this request.",
			frappe.PermissionError,
		)
	_assert_policy_checksum(policy)
	if str(policy.get("active_slot_key") or "") != str(policy.get("policy_scope_key") or ""):
		frappe.throw("Active PHI disclosure policy slot identity is invalid.")


def _resolve_active_policy(*, scope: dict[str, str], purpose_code: str):
	rows = frappe.get_all(
		POLICY_DOCTYPE,
		filters=_matching_policy_filters(
			scope=scope,
			purpose_code=purpose_code,
			status="Active",
		),
		fields=["name"],
		order_by="name asc",
		limit_page_length=9,
	)
	if len(rows) > 8:
		frappe.throw("PHI disclosure policy resolution violated its exact scope cardinality.")
	now = now_datetime()
	matches: list[tuple[int, Any]] = []
	for row in rows:
		policy = frappe.get_doc(POLICY_DOCTYPE, row.name)
		if not _policy_matches_scope(policy, scope):
			continue
		effective_from = get_datetime(policy.get("effective_from"))
		effective_to = get_datetime(policy.get("effective_to")) if policy.get("effective_to") else None
		if not effective_from or effective_from > now or (effective_to and effective_to <= now):
			continue
		_assert_policy_checksum(policy)
		if str(policy.get("active_slot_key") or "") != str(policy.get("policy_scope_key") or ""):
			frappe.throw("Active PHI disclosure policy slot identity is invalid.")
		specificity = sum(
			bool(str(policy.get(fieldname) or "")) for fieldname in ("campus", "department", "ward")
		)
		matches.append((specificity, policy))
	if not matches:
		frappe.throw(
			"No approved effective PHI disclosure policy covers this purpose and clinical scope.",
			frappe.PermissionError,
		)
	maximum = max(specificity for specificity, _policy in matches)
	winners = [policy for specificity, policy in matches if specificity == maximum]
	if len(winners) != 1:
		frappe.throw("PHI disclosure policy resolution is ambiguous; access is denied.")
	return winners[0]


def _matching_policy_filters(
	*,
	scope: Mapping[str, str],
	purpose_code: str,
	status: str,
) -> dict[str, Any]:
	filters: dict[str, Any] = {
		"hospital": str(scope.get("hospital") or ""),
		"purpose_code": purpose_code,
		"status": status,
	}
	for fieldname in ("campus", "department", "ward"):
		value = str(scope.get(fieldname) or "")
		filters[fieldname] = ["in", ["", value]] if value else ""
	return filters


def _policy_matches_scope(policy, scope: Mapping[str, str]) -> bool:
	return all(
		not str(policy.get(fieldname) or "")
		or str(policy.get(fieldname) or "") == str(scope.get(fieldname) or "")
		for fieldname in ("hospital", "campus", "department", "ward")
	)


def _assert_policy_authorizes(
	policy,
	doctype: str,
	requested_fields: list[str],
	authorizing_roles: set[str],
) -> None:
	allowed_fields = _normalize_policy_fields(policy.get("allowed_fields_json"))
	if doctype not in allowed_fields or not set(requested_fields).issubset(allowed_fields[doctype]):
		frappe.throw(
			"The active PHI disclosure policy does not authorize the requested fields.",
			frappe.PermissionError,
		)
	allowed_roles = set(_normalize_policy_roles(policy.get("allowed_reader_roles_json")))
	if not authorizing_roles.intersection(allowed_roles):
		frappe.throw(
			"The active PHI disclosure policy does not authorize this reader's business role.",
			frappe.PermissionError,
		)


def _normalize_requested_fields(value: list[str] | str, doctype: str) -> list[str]:
	parsed = _parse_json(value, list, "fields") if isinstance(value, str) else value
	if (
		not isinstance(parsed, list)
		or not parsed
		or len(parsed) > len(PHI_FIELDS_BY_DOCTYPE[doctype])
		or any(not isinstance(fieldname, str) for fieldname in parsed)
	):
		frappe.throw("fields must contain a bounded non-empty PHI field list.")
	normalized = sorted(set(parsed))
	if len(normalized) != len(parsed):
		frappe.throw("fields must not contain duplicates.")
	if not set(normalized).issubset(PHI_FIELDS_BY_DOCTYPE[doctype]):
		frappe.throw("The request contains a field outside the governed PHI identity registry.")
	return normalized


def _enforce_disclosure_rate_limit(
	*,
	user: str,
	reference_hash: str,
	user_limit: int,
	record_limit: int,
	hmac_key_id: str,
) -> None:
	window = int(time.time()) // 60
	reader_hash = _site_hmac(f"reader|{user}", key_id=hmac_key_id)
	cache = frappe.cache
	keys_and_limits = (
		(f"ione_qms:phi-rate:user:{reader_hash}:{window}", user_limit),
		(f"ione_qms:phi-rate:record:{reference_hash}:{window}", record_limit),
	)
	for raw_key, limit in keys_and_limits:
		key = cache.make_key(raw_key)
		count = int(cache.incr(key))
		if count == 1:
			cache.expire(key, 120)
		if count > limit:
			frappe.local.response["http_status_code"] = 429
			frappe.throw(
				"PHI identity disclosure rate limit exceeded.",
				frappe.RateLimitExceededError,
			)


def _document_scope(doc) -> dict[str, str]:
	doctype = str(doc.get("doctype") or "")
	hospital = (
		str(doc.get("tenant_hospital") or "")
		if doctype == "IONE Patient Index"
		else str(doc.get("hospital") or "")
	)
	return {
		"hospital": hospital,
		"campus": str(doc.get("campus") or ""),
		"department": str(doc.get("department") or ""),
		"ward": str(doc.get("ward") or ""),
	}


def _parse_json(value: Any, expected: type, label: str) -> Any:
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{label} must contain valid JSON.") from exc
	if not isinstance(parsed, expected):
		frappe.throw(f"{label} has an invalid JSON shape.")
	return parsed


def _normalized_code(value: Any, label: str) -> str:
	code = str(value or "").strip().upper()
	if not _CODE_PATTERN.fullmatch(code):
		frappe.throw(f"{label} must contain 3-64 uppercase identifier characters.")
	return code


def _required_comment(value: Any, label: str) -> str:
	comment = str(value or "").strip()
	if not 5 <= len(comment) <= 500:
		frappe.throw(f"{label} must contain 5-500 characters.")
	return comment


def _bounded_integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
	try:
		number = int(value)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{label} must be an integer.") from exc
	if number < minimum or number > maximum:
		frappe.throw(f"{label} must be between {minimum} and {maximum}.")
	return number


def _require_named_actor(roles: frozenset[str]) -> str:
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw("This governed action requires a named accountable user.", frappe.PermissionError)
	user_roles = set(frappe.get_roles(user))
	if not user_roles.intersection(roles) or not user_roles.intersection(APP_ROLES):
		frappe.throw("The current user lacks the required IONE QMS role.", frappe.PermissionError)
	return user


def _session_id() -> str:
	session_id = str(getattr(frappe.session, "sid", "") or "")
	if not session_id or session_id == "Guest":
		frappe.throw(
			"PHI identity disclosure requires an authenticated user session.", frappe.PermissionError
		)
	return session_id


def _site_hmac(value: str, *, key_id: str | None = None) -> str:
	key = verification_hmac_key("ione_phi_hmac", key_id) if key_id else active_hmac_key("ione_phi_hmac")
	return hmac.new(key.secret, value.encode(), hashlib.sha256).hexdigest()


def _phi_receipt_auth_tag(doc) -> str:
	payload = {
		fieldname: doc.get(fieldname)
		for fieldname in (
			"receipt_key",
			"request_id_hash",
			"hmac_key_id",
			"reader_user",
			"purpose_code",
			"reference_doctype",
			"reference_hash",
			"field_manifest_json",
			"field_manifest_hash",
			"disclosure_policy",
			"policy_checksum",
			"authorized_scope_json",
			"authorization_basis_json",
			"authorization_basis_hash",
			"role_snapshot_json",
			"role_snapshot_hash",
			"session_hash",
			"accessed_at",
			"result_code",
		)
	}
	return _site_hmac(
		f"phi-access-receipt|{_canonical_json(payload)}",
		key_id=str(doc.get("hmac_key_id") or ""),
	)


def _canonical_json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _set_no_store_response_headers() -> None:
	headers = getattr(frappe.local, "response_headers", None)
	if headers is None:
		headers = {}
		frappe.local.response_headers = headers
	headers["Cache-Control"] = "no-store, private, max-age=0"
	headers["Pragma"] = "no-cache"
	headers["Expires"] = "0"
	headers["Vary"] = "Cookie"


@contextmanager
def _trusted_policy_mutation():
	attribute = "ione_phi_policy_mutation"
	previous_exists = attribute in frappe.flags
	previous = frappe.flags.get(attribute)
	setattr(frappe.flags, attribute, _POLICY_MUTATION_TOKEN)
	try:
		yield
	finally:
		if not previous_exists:
			frappe.flags.pop(attribute, None)
		else:
			setattr(frappe.flags, attribute, previous)


def _managed_policy_mutation() -> bool:
	return getattr(frappe.flags, "ione_phi_policy_mutation", None) is _POLICY_MUTATION_TOKEN

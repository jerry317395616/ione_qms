from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from ione_qms.permissions import require_scope_read
from ione_qms.services.crypto_keys import active_hmac_key, verification_hmac_key
from ione_qms.services.indicators import (
	_authorized_historical_indicator_reproduction,
	_verify_series_revision_continuity,
	current_indicator_series_lock,
	verify_indicator_result_integrity,
	verify_indicator_result_reproducibility,
)

AUTHORIZATION_DOCTYPE = "IONE Indicator Historical Audit Authorization"
ACCESS_RECEIPT_DOCTYPE = "IONE Indicator Historical Audit Access Receipt"

HISTORICAL_AUDIT_REQUESTER_ROLES = frozenset({"IONE QMS Auditor", "IONE QC Reviewer", "IONE Medical Affairs"})
HISTORICAL_AUDIT_REVIEWER_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
MAX_HISTORICAL_AUDIT_SOURCE_RECORDS = 100_000
MAX_HISTORICAL_AUDIT_REPRODUCTIONS = 5
MAX_HISTORICAL_AUDIT_TTL_HOURS = 24

_DIGEST_LENGTH = 64
_REQUEST_FIELDS = (
	"authorization_key",
	"request_hmac_key_id",
	"indicator_result",
	"result_series_key",
	"result_revision",
	"result_checksum",
	"input_receipt_hash",
	"source_manifest_count",
	"purpose",
	"hospital",
	"campus",
	"department",
	"ward",
	"max_source_records",
	"max_reproductions",
	"requested_ttl_hours",
	"requested_by",
	"requested_at",
)
_AUTHORIZATION_FIELDS = (
	"request_checksum",
	"authorization_hmac_key_id",
	"status",
	"reviewed_by",
	"reviewed_at",
	"review_comment",
	"expires_at",
	"used_reproductions",
	"last_accessed_at",
)
_ACCESS_RECEIPT_FIELDS = (
	"access_key",
	"hmac_key_id",
	"authorization",
	"indicator_result",
	"accessed_by",
	"accessed_at",
	"purpose_hash",
	"sequence_no",
	"source_manifest_count",
	"result_checksum",
	"verified",
	"reason_code",
	"outcome_hash",
)


def request_historical_indicator_audit(
	result: str,
	purpose: str,
	max_source_records: int = 10_000,
	max_reproductions: int = 1,
	ttl_hours: int = 24,
) -> dict[str, Any]:
	"""Create an exact, scoped historical-reproduction request for independent review."""
	user = _require_named_historical_auditor(HISTORICAL_AUDIT_REQUESTER_ROLES)
	purpose = _bounded_text(purpose, "Historical audit purpose", minimum=10, maximum=1_000)
	record_limit = _exact_int(
		max_source_records,
		"Historical audit source-record limit",
		1,
		MAX_HISTORICAL_AUDIT_SOURCE_RECORDS,
	)
	reproduction_limit = _exact_int(
		max_reproductions,
		"Historical audit reproduction limit",
		1,
		MAX_HISTORICAL_AUDIT_REPRODUCTIONS,
	)
	ttl = _exact_int(
		ttl_hours,
		"Historical audit authorization lifetime",
		1,
		MAX_HISTORICAL_AUDIT_TTL_HOURS,
	)
	result_doc, source_receipt = _validated_historical_result(result, user=user)
	source_count = _source_manifest_count(source_receipt)
	if source_count > record_limit:
		raise ValueError("Historical result exceeds the requested source-record resource limit.")
	requested_at = now_datetime()
	signing_key = active_hmac_key("ione_phi_hmac")
	authorization_key = _keyed_digest(
		{
			"indicator_result": result_doc.name,
			"nonce": frappe.generate_hash(length=32),
			"requested_at": requested_at,
			"requested_by": user,
		},
		key_id=signing_key.key_id,
	)
	values = {
		"authorization_key": authorization_key,
		"request_hmac_key_id": signing_key.key_id,
		"authorization_hmac_key_id": signing_key.key_id,
		"indicator_result": result_doc.name,
		"result_series_key": result_doc.get("series_key"),
		"result_revision": int(result_doc.get("result_revision") or 0),
		"result_checksum": result_doc.get("result_checksum"),
		"input_receipt_hash": result_doc.get("input_receipt_hash"),
		"source_manifest_count": source_count,
		"purpose": purpose,
		"hospital": result_doc.get("hospital"),
		"campus": result_doc.get("campus"),
		"department": result_doc.get("department"),
		"ward": result_doc.get("ward"),
		"max_source_records": record_limit,
		"max_reproductions": reproduction_limit,
		"requested_ttl_hours": ttl,
		"requested_by": user,
		"requested_at": requested_at,
		"status": "Pending Review",
		"reviewed_by": None,
		"reviewed_at": None,
		"review_comment": None,
		"expires_at": None,
		"used_reproductions": 0,
		"last_accessed_at": None,
	}
	values["request_checksum"] = _request_checksum(values)
	values["authorization_checksum"] = _authorization_checksum(values)
	doc = frappe.get_doc({"doctype": AUTHORIZATION_DOCTYPE, **values})
	doc.flags.ione_indicator_historical_audit_service = True
	doc.insert(ignore_permissions=True)
	return _authorization_outcome(doc)


def review_historical_indicator_audit(
	authorization: str,
	approve: int,
	review_comment: str,
) -> dict[str, Any]:
	"""Record the independent approval or rejection for one immutable request."""
	reviewer = _require_named_historical_auditor(HISTORICAL_AUDIT_REVIEWER_ROLES)
	decision = _exact_int(approve, "Historical audit review decision", 0, 1)
	comment = _bounded_text(review_comment, "Historical audit review comment", minimum=10, maximum=1_000)
	with frappe.db.advisory_lock(f"ione-qms:indicator-audit-review:{authorization}", timeout=5):
		frappe.db.sql(
			f"select name from `tab{AUTHORIZATION_DOCTYPE}` where name = %s for update",  # noqa: S608
			(authorization,),
		)
		doc = frappe.get_doc(AUTHORIZATION_DOCTYPE, authorization)
		verify_historical_audit_authorization(doc)
		if str(doc.get("status") or "") != "Pending Review":
			raise ValueError("Historical audit authorization already has a terminal review.")
		if reviewer == str(doc.get("requested_by") or ""):
			raise ValueError("Historical audit requester and reviewer must be different named users.")
		_validated_historical_result(str(doc.get("indicator_result") or ""), user=reviewer)
		now = now_datetime()
		doc.status = "Approved" if decision else "Rejected"
		doc.reviewed_by = reviewer
		doc.reviewed_at = now
		doc.review_comment = comment
		doc.expires_at = (
			add_to_date(now, hours=int(doc.get("requested_ttl_hours") or 0)) if decision else None
		)
		doc.authorization_hmac_key_id = active_hmac_key("ione_phi_hmac").key_id
		doc.authorization_checksum = _authorization_checksum(doc)
		doc.flags.ione_indicator_historical_audit_service = True
		doc.flags.ione_indicator_historical_audit_review = True
		doc.flags.ignore_permissions = True
		doc.save()
		return _authorization_outcome(doc)


def reproduce_historical_indicator_result(
	result: str,
	authorization: str,
) -> dict[str, Any]:
	"""Consume one approved use and append a tamper-evident access receipt."""
	user = _require_named_historical_auditor(HISTORICAL_AUDIT_REQUESTER_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:indicator-audit-use:{authorization}", timeout=5):
		frappe.db.sql(
			f"select name from `tab{AUTHORIZATION_DOCTYPE}` where name = %s for update",  # noqa: S608
			(authorization,),
		)
		doc = frappe.get_doc(AUTHORIZATION_DOCTYPE, authorization)
		verify_historical_audit_authorization(doc)
		if str(doc.get("requested_by") or "") != user:
			raise frappe.PermissionError(
				"Historical audit authorization may only be consumed by its named requester."
			)
		if str(doc.get("indicator_result") or "") != str(result):
			raise frappe.PermissionError("Historical audit authorization does not bind this result.")
		if str(doc.get("status") or "") != "Approved":
			raise frappe.PermissionError("Historical audit authorization is not approved for use.")
		now = now_datetime()
		if not doc.get("expires_at") or get_datetime(doc.expires_at) <= now:
			raise frappe.PermissionError("Historical audit authorization has expired.")
		used = int(doc.get("used_reproductions") or 0)
		max_uses = int(doc.get("max_reproductions") or 0)
		if used < 0 or max_uses < 1 or used >= max_uses:
			raise frappe.PermissionError("Historical audit authorization resource limit is exhausted.")

		result_doc, source_receipt = _validated_historical_result(result, user=user)
		_assert_authorization_binding(doc, result_doc, source_receipt)
		try:
			with _authorized_historical_indicator_reproduction(result, doc.name):
				outcome = verify_indicator_result_reproducibility(result)
		except TypeError, ValueError, frappe.ValidationError, frappe.DoesNotExistError:
			outcome = {
				"verified": False,
				"reason_code": "HISTORICAL_REPRODUCTION_VERIFICATION_ERROR",
			}

		sequence = used + 1
		doc.used_reproductions = sequence
		doc.last_accessed_at = now
		if sequence >= max_uses:
			doc.status = "Exhausted"
		doc.authorization_hmac_key_id = active_hmac_key("ione_phi_hmac").key_id
		doc.authorization_checksum = _authorization_checksum(doc)
		doc.flags.ione_indicator_historical_audit_service = True
		doc.flags.ione_indicator_historical_audit_use = True
		doc.flags.ignore_permissions = True
		doc.save()
		access_receipt = _append_access_receipt(
			doc,
			result_doc,
			outcome,
			accessed_at=now,
			sequence=sequence,
		)
		return {
			**outcome,
			"authorization": doc.name,
			"authorization_status": doc.status,
			"access_receipt": access_receipt.name,
			"used_reproductions": sequence,
			"max_reproductions": max_uses,
		}


def validate_historical_audit_authorization(doc, method: str | None = None) -> None:
	del method
	if not getattr(doc.flags, "ione_indicator_historical_audit_service", False):
		raise frappe.ValidationError("Historical indicator audit authorizations are service managed.")
	previous = doc.get_doc_before_save()
	try:
		verify_historical_audit_authorization(doc)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(str(exc)) from exc
	if previous is None:
		if str(doc.get("status") or "") != "Pending Review":
			raise frappe.ValidationError("New historical audit authorizations must await review.")
		return
	for fieldname in _REQUEST_FIELDS:
		if _canonical(previous.get(fieldname)) != _canonical(doc.get(fieldname)):
			raise frappe.ValidationError("Historical audit request identity is immutable.")
	before_status = str(previous.get("status") or "")
	after_status = str(doc.get("status") or "")
	if getattr(doc.flags, "ione_indicator_historical_audit_review", False):
		if before_status != "Pending Review" or after_status not in {"Approved", "Rejected"}:
			raise frappe.ValidationError("Historical audit review transition is invalid.")
		return
	if getattr(doc.flags, "ione_indicator_historical_audit_use", False):
		if before_status != "Approved" or after_status not in {"Approved", "Exhausted"}:
			raise frappe.ValidationError("Historical audit use transition is invalid.")
		if int(doc.get("used_reproductions") or 0) != int(previous.get("used_reproductions") or 0) + 1:
			raise frappe.ValidationError("Historical audit use count must advance exactly once.")
		for fieldname in ("reviewed_by", "reviewed_at", "review_comment", "expires_at"):
			if _canonical(previous.get(fieldname)) != _canonical(doc.get(fieldname)):
				raise frappe.ValidationError("Historical audit approval receipt is immutable.")
		return
	raise frappe.ValidationError("Historical audit authorization mutation is not a governed transition.")


def verify_historical_audit_authorization(doc) -> None:
	status = str(doc.get("status") or "")
	if status not in {"Pending Review", "Approved", "Rejected", "Exhausted"}:
		raise ValueError("Historical audit authorization status is invalid.")
	requested_by = str(doc.get("requested_by") or "")
	if requested_by in {"", "Administrator", "Guest"}:
		raise ValueError("Historical audit authorization requires a named requester.")
	_validate_digest(doc.get("authorization_key"), "authorization_key")
	verification_hmac_key("ione_phi_hmac", str(doc.get("request_hmac_key_id") or ""))
	verification_hmac_key("ione_phi_hmac", str(doc.get("authorization_hmac_key_id") or ""))
	_validate_digest(doc.get("result_series_key"), "result_series_key")
	_validate_digest(doc.get("result_checksum"), "result_checksum")
	_validate_digest(doc.get("input_receipt_hash"), "input_receipt_hash")
	if not hmac.compare_digest(
		_request_checksum(doc),
		_validate_digest(doc.get("request_checksum"), "request_checksum"),
	):
		raise ValueError("Historical audit request checksum verification failed.")
	if not hmac.compare_digest(
		_authorization_checksum(doc),
		_validate_digest(doc.get("authorization_checksum"), "authorization_checksum"),
	):
		raise ValueError("Historical audit authorization checksum verification failed.")
	_exact_int(
		doc.get("source_manifest_count"),
		"Historical audit source manifest count",
		0,
		MAX_HISTORICAL_AUDIT_SOURCE_RECORDS,
	)
	max_records = _exact_int(
		doc.get("max_source_records"),
		"Historical audit source-record limit",
		1,
		MAX_HISTORICAL_AUDIT_SOURCE_RECORDS,
	)
	if int(doc.get("source_manifest_count") or 0) > max_records:
		raise ValueError("Historical audit request exceeds its source-record limit.")
	max_uses = _exact_int(
		doc.get("max_reproductions"),
		"Historical audit reproduction limit",
		1,
		MAX_HISTORICAL_AUDIT_REPRODUCTIONS,
	)
	used = _exact_int(
		doc.get("used_reproductions"),
		"Historical audit used-reproduction count",
		0,
		max_uses,
	)
	_exact_int(
		doc.get("requested_ttl_hours"),
		"Historical audit authorization lifetime",
		1,
		MAX_HISTORICAL_AUDIT_TTL_HOURS,
	)
	_bounded_text(doc.get("purpose"), "Historical audit purpose", minimum=10, maximum=1_000)
	if status == "Pending Review":
		if used or any(
			doc.get(fieldname)
			for fieldname in (
				"reviewed_by",
				"reviewed_at",
				"review_comment",
				"expires_at",
				"last_accessed_at",
			)
		):
			raise ValueError("Pending historical audit request contains terminal receipt fields.")
		return
	reviewed_by = str(doc.get("reviewed_by") or "")
	if (
		reviewed_by in {"", "Administrator", "Guest"}
		or reviewed_by == requested_by
		or not doc.get("reviewed_at")
		or not doc.get("review_comment")
	):
		raise ValueError("Historical audit terminal review is incomplete or not independent.")
	if status in {"Approved", "Exhausted"} and not doc.get("expires_at"):
		raise ValueError("Approved historical audit authorization requires an expiry.")
	if status == "Rejected" and (doc.get("expires_at") or used or doc.get("last_accessed_at")):
		raise ValueError("Rejected historical audit authorization cannot contain use fields.")
	if status == "Exhausted" and used != max_uses:
		raise ValueError("Exhausted historical audit authorization has an inconsistent use count.")
	if status == "Approved" and used >= max_uses:
		raise ValueError("Approved historical audit authorization has already exhausted its use limit.")
	if used and not doc.get("last_accessed_at"):
		raise ValueError("Consumed historical audit authorization lacks its last-access timestamp.")


def validate_historical_audit_access_receipt(doc, method: str | None = None) -> None:
	del method
	if doc.get_doc_before_save() is not None:
		raise frappe.ValidationError("Historical indicator audit access receipts are append-only.")
	if not getattr(doc.flags, "ione_indicator_historical_audit_service", False):
		raise frappe.ValidationError("Historical indicator audit access receipts are service managed.")
	try:
		verify_historical_audit_access_receipt(doc)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(str(exc)) from exc


def verify_historical_audit_access_receipt(doc) -> None:
	for fieldname in ("access_key", "purpose_hash", "result_checksum", "outcome_hash"):
		_validate_digest(doc.get(fieldname), fieldname)
	verification_hmac_key("ione_phi_hmac", str(doc.get("hmac_key_id") or ""))
	if (
		not doc.get("authorization")
		or not doc.get("indicator_result")
		or str(doc.get("accessed_by") or "") in {"", "Administrator", "Guest"}
		or not doc.get("accessed_at")
		or not doc.get("reason_code")
	):
		raise ValueError("Historical audit access receipt identity is incomplete.")
	_exact_int(
		doc.get("sequence_no"),
		"Historical audit access sequence",
		1,
		MAX_HISTORICAL_AUDIT_REPRODUCTIONS,
	)
	_exact_int(
		doc.get("source_manifest_count"),
		"Historical audit access source count",
		0,
		MAX_HISTORICAL_AUDIT_SOURCE_RECORDS,
	)
	if int(doc.get("verified") or 0) not in {0, 1}:
		raise ValueError("Historical audit access verification flag is invalid.")
	if not hmac.compare_digest(
		_access_receipt_checksum(doc),
		_validate_digest(doc.get("receipt_checksum"), "receipt_checksum"),
	):
		raise ValueError("Historical audit access receipt checksum verification failed.")
	authorization = frappe.get_doc(AUTHORIZATION_DOCTYPE, doc.get("authorization"))
	verify_historical_audit_authorization(authorization)
	if (
		str(authorization.get("indicator_result") or "") != str(doc.get("indicator_result") or "")
		or str(authorization.get("requested_by") or "") != str(doc.get("accessed_by") or "")
		or int(authorization.get("used_reproductions") or 0) < int(doc.get("sequence_no") or 0)
		or int(authorization.get("source_manifest_count") or 0) != int(doc.get("source_manifest_count") or 0)
		or str(authorization.get("result_checksum") or "") != str(doc.get("result_checksum") or "")
		or not hmac.compare_digest(
			_keyed_digest(
				str(authorization.get("purpose") or ""),
				key_id=str(doc.get("hmac_key_id") or ""),
			),
			str(doc.get("purpose_hash") or ""),
		)
	):
		raise ValueError("Historical audit access receipt does not match its authorization.")


def prevent_historical_audit_deletion(doc, method: str | None = None) -> None:
	del doc, method
	raise frappe.ValidationError("Historical indicator audit receipts cannot be deleted.")


def _validated_historical_result(result: str, *, user: str):
	result_doc = frappe.get_doc("IONE Indicator Result", result)
	series_key = _validate_digest(result_doc.get("series_key"), "result series_key")
	with current_indicator_series_lock(series_key) as (pointer, current):
		_verify_series_revision_continuity(series_key, pointer=pointer)
		if str(current.name) == str(result_doc.name):
			raise ValueError("Current indicator revisions use the normal current-only verification path.")
		source_receipt = verify_indicator_result_integrity(result_doc)
		_require_result_scope(result_doc, user=user)
		return result_doc, source_receipt


def _require_result_scope(result_doc, *, user: str) -> None:
	require_scope_read(
		hospital=result_doc.get("hospital"),
		campus=result_doc.get("campus"),
		department=result_doc.get("department"),
		ward=result_doc.get("ward"),
		user=user,
		target_doctype="IONE Indicator Result",
	)


def _assert_authorization_binding(doc, result_doc, source_receipt: dict[str, Any]) -> None:
	expected = {
		"indicator_result": result_doc.name,
		"result_series_key": result_doc.get("series_key"),
		"result_revision": int(result_doc.get("result_revision") or 0),
		"result_checksum": result_doc.get("result_checksum"),
		"input_receipt_hash": result_doc.get("input_receipt_hash"),
		"source_manifest_count": _source_manifest_count(source_receipt),
		"hospital": result_doc.get("hospital"),
		"campus": result_doc.get("campus"),
		"department": result_doc.get("department"),
		"ward": result_doc.get("ward"),
	}
	if any(_canonical(doc.get(fieldname)) != _canonical(value) for fieldname, value in expected.items()):
		raise frappe.PermissionError("Historical audit authorization no longer matches its exact result.")
	if int(doc.get("source_manifest_count") or 0) > int(doc.get("max_source_records") or 0):
		raise frappe.PermissionError("Historical audit source-record resource limit is exceeded.")


def _append_access_receipt(
	authorization,
	result_doc,
	outcome: dict[str, Any],
	*,
	accessed_at,
	sequence: int,
):
	signing_key = active_hmac_key("ione_phi_hmac")
	outcome_hash = _keyed_digest(outcome, key_id=signing_key.key_id)
	values = {
		"access_key": _keyed_digest(
			{
				"accessed_at": accessed_at,
				"authorization": authorization.name,
				"sequence": sequence,
				"user": frappe.session.user,
			},
			key_id=signing_key.key_id,
		),
		"hmac_key_id": signing_key.key_id,
		"authorization": authorization.name,
		"indicator_result": result_doc.name,
		"accessed_by": frappe.session.user,
		"accessed_at": accessed_at,
		"purpose_hash": _keyed_digest(
			str(authorization.get("purpose") or ""),
			key_id=signing_key.key_id,
		),
		"sequence_no": sequence,
		"source_manifest_count": int(authorization.get("source_manifest_count") or 0),
		"result_checksum": result_doc.get("result_checksum"),
		"verified": int(bool(outcome.get("verified"))),
		"reason_code": str(outcome.get("reason_code") or "UNKNOWN")[:140],
		"outcome_hash": outcome_hash,
	}
	values["receipt_checksum"] = _access_receipt_checksum(values)
	doc = frappe.get_doc({"doctype": ACCESS_RECEIPT_DOCTYPE, **values})
	doc.flags.ione_indicator_historical_audit_service = True
	doc.insert(ignore_permissions=True)
	return doc


def _request_checksum(source) -> str:
	return _keyed_digest(
		{fieldname: source.get(fieldname) for fieldname in _REQUEST_FIELDS},
		key_id=str(source.get("request_hmac_key_id") or ""),
	)


def _authorization_checksum(source) -> str:
	return _keyed_digest(
		{
			**{fieldname: source.get(fieldname) for fieldname in _REQUEST_FIELDS},
			**{fieldname: source.get(fieldname) for fieldname in _AUTHORIZATION_FIELDS},
		},
		key_id=str(source.get("authorization_hmac_key_id") or ""),
	)


def _access_receipt_checksum(source) -> str:
	return _keyed_digest(
		{fieldname: source.get(fieldname) for fieldname in _ACCESS_RECEIPT_FIELDS},
		key_id=str(source.get("hmac_key_id") or ""),
	)


def _keyed_digest(value: Any, *, key_id: str | None = None) -> str:
	key = verification_hmac_key("ione_phi_hmac", key_id) if key_id else active_hmac_key("ione_phi_hmac")
	payload = json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	).encode()
	return hmac.new(key.secret, payload, hashlib.sha256).hexdigest()


def _validate_digest(value: Any, label: str) -> str:
	digest = str(value or "")
	if len(digest) != _DIGEST_LENGTH or any(character not in "0123456789abcdef" for character in digest):
		raise ValueError(f"{label} must be an exact lowercase SHA-256 digest.")
	return digest


def _source_manifest_count(receipt: dict[str, Any]) -> int:
	return _exact_int(
		receipt.get("source_manifest_count"),
		"Indicator source manifest count",
		0,
		MAX_HISTORICAL_AUDIT_SOURCE_RECORDS,
	)


def _require_named_historical_auditor(allowed_roles: frozenset[str]) -> str:
	user = str(frappe.session.user or "")
	roles = set(frappe.get_roles(user))
	if (
		user in {"", "Administrator", "Guest"}
		or "IONE Agent Service" in roles
		or not roles.intersection(allowed_roles)
	):
		raise frappe.PermissionError("A named, controlled historical indicator auditor is required.")
	return user


def _exact_int(value: Any, label: str, minimum: int, maximum: int) -> int:
	if type(value) is int:
		number = value
	elif type(value) is str and value == value.strip() and value.isascii() and value.isdigit():
		number = int(value)
	else:
		raise ValueError(f"{label} must be an exact integer.")
	if number < minimum or number > maximum:
		raise ValueError(f"{label} must be between {minimum} and {maximum}.")
	return number


def _bounded_text(value: Any, label: str, *, minimum: int, maximum: int) -> str:
	text = str(value or "").strip()
	if len(text) < minimum or len(text) > maximum:
		raise ValueError(f"{label} must contain between {minimum} and {maximum} characters.")
	return text


def _canonical(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _authorization_outcome(doc) -> dict[str, Any]:
	return {
		"authorization": doc.name,
		"indicator_result": doc.get("indicator_result"),
		"status": doc.get("status"),
		"requested_by": doc.get("requested_by"),
		"reviewed_by": doc.get("reviewed_by"),
		"expires_at": doc.get("expires_at"),
		"max_source_records": int(doc.get("max_source_records") or 0),
		"max_reproductions": int(doc.get("max_reproductions") or 0),
		"used_reproductions": int(doc.get("used_reproductions") or 0),
	}

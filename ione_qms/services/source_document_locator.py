from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlparse

import frappe
from frappe.utils import now_datetime

from ione_qms.permissions import (
	finding_permission,
	qc_finding_evidence_permission,
	require_role,
	require_scope_read,
)
from ione_qms.services.immutability import validate_append_only
from ione_qms.services.integration_scope import authorize_endpoint_scope, scope_policy_checksum

LOCATOR_DOCTYPE = "IONE Source Document Locator"
ACCESS_LOG_DOCTYPE = "IONE Source Document Access Log"
LOCATOR_AUTHOR_ROLES = frozenset({"IONE Integration Administrator"})
LOCATOR_APPROVER_ROLES = frozenset({"IONE QC Administrator", "IONE Medical Affairs"})
LOCATOR_READ_ROLES = LOCATOR_AUTHOR_ROLES | LOCATOR_APPROVER_ROLES | frozenset({"IONE Auditor"})
ACCESS_LOG_READ_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Auditor",
	}
)
LOCATOR_STATUSES = frozenset({"Draft", "Approved", "Suspended", "Retired"})
MAX_SOURCE_RECORD_ID_LENGTH = 200
MAX_SOURCE_RECORD_TYPE_LENGTH = 200
MAX_PATH_COMPONENT_LENGTH = 256
MAX_LOCATORS_PER_SCOPE_FAMILY = 100

_LOCATOR_MANAGED_TOKEN = object()
_ACCESS_LOG_TOKEN = object()
_LOCATOR_SEMANTIC_FIELDS = (
	"locator_code",
	"version",
	"source_system",
	"endpoint",
	"source_record_type",
	"hospital",
	"campus",
	"department",
	"ward",
	"path_prefix",
	"path_suffix",
	"sso_uat_reference",
	"redirect_uat_reference",
)
_LOCATOR_MANAGED_FIELDS = (
	"status",
	"enabled",
	"requested_by",
	"requested_at",
	"approved_by",
	"approved_at",
	"approval_checksum",
	"operated_by",
	"operated_at",
	"operation_comment",
)
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_UAT_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,139}")


class SourceDocumentConfigurationError(ValueError):
	"""A safe, non-sensitive deep-link configuration failure."""


def prepare_source_document_locator(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new():
		return
	if not _managed_locator_mutation():
		_require_named_role(*LOCATOR_AUTHOR_ROLES)
	doc.status = "Draft"
	doc.enabled = 0
	doc.requested_by = frappe.session.user
	doc.requested_at = now_datetime()
	for fieldname in (
		"approved_by",
		"approved_at",
		"approval_checksum",
		"operated_by",
		"operated_at",
		"operation_comment",
	):
		doc.set(fieldname, None)


def validate_source_document_locator(doc, method: str | None = None) -> None:
	del method
	_validate_locator_identifiers(doc)
	_validate_locator_path(doc)
	_validate_locator_scope(doc)
	_validate_locator_endpoint_contract(doc, require_runtime=False)
	_set_locator_keys(doc)
	_validate_locator_lifecycle(doc)
	_lock_and_reject_overlapping_locators(doc)


def approve_source_document_locator(locator: str, review_comment: str) -> dict[str, str]:
	user = _require_named_role(*LOCATOR_APPROVER_ROLES)
	comment = _required_comment(review_comment, "Approval comment")
	with frappe.db.advisory_lock(f"ione-qms:source-document-locator:{locator}", timeout=10):
		doc = frappe.get_doc(LOCATOR_DOCTYPE, locator, for_update=True)
		if doc.get("status") != "Draft":
			frappe.throw("Only a Draft source-document locator can be approved.")
		if str(doc.get("requested_by") or "") == user:
			frappe.throw(
				"The locator author cannot approve the same configuration.",
				frappe.PermissionError,
			)
		_validate_locator_endpoint_contract(doc, require_runtime=True)
		_validate_locator_path(doc)
		_validate_locator_scope(doc)
		_set_locator_keys(doc)
		_lock_and_reject_overlapping_locators(doc)
		doc.status = "Approved"
		doc.enabled = 1
		doc.approved_by = user
		doc.approved_at = now_datetime()
		doc.approval_checksum = _approval_checksum(doc)
		doc.operated_by = user
		doc.operated_at = doc.approved_at
		doc.operation_comment = comment
		with _trusted_locator_mutation():
			doc.save(ignore_permissions=True)
	return {
		"locator": doc.name,
		"status": str(doc.status),
		"approval_checksum": str(doc.approval_checksum),
	}


def operate_source_document_locator(
	locator: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	user = _require_named_role(*LOCATOR_APPROVER_ROLES)
	action_name = str(action or "").strip()
	comment = _required_comment(operation_comment, "Operation comment")
	transitions = {
		("Approved", "Suspend"): ("Suspended", 0),
		("Suspended", "Resume"): ("Approved", 1),
		("Approved", "Retire"): ("Retired", 0),
		("Suspended", "Retire"): ("Retired", 0),
	}
	with frappe.db.advisory_lock(f"ione-qms:source-document-locator:{locator}", timeout=10):
		doc = frappe.get_doc(LOCATOR_DOCTYPE, locator, for_update=True)
		transition = transitions.get((str(doc.get("status") or ""), action_name))
		if not transition:
			frappe.throw("The requested source-document locator transition is not allowed.")
		if action_name == "Resume":
			_validate_locator_endpoint_contract(doc, require_runtime=True)
			if str(doc.get("approval_checksum") or "") != _approval_checksum(doc):
				frappe.throw("The approved locator checksum no longer matches its configuration.")
			_lock_and_reject_overlapping_locators(doc)
		doc.status, doc.enabled = transition
		doc.operated_by = user
		doc.operated_at = now_datetime()
		doc.operation_comment = comment
		with _trusted_locator_mutation():
			doc.save(ignore_permissions=True)
	return {"locator": doc.name, "status": str(doc.status)}


def locate_source_document(finding: str, evidence: str) -> dict[str, Any]:
	"""Resolve an approved HTTPS deep link without reading the source document."""
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Source-document location requires a named clinical user.",
			frappe.PermissionError,
		)
	try:
		finding_doc = frappe.get_doc("IONE QC Finding", finding)
		evidence_doc = frappe.get_doc("IONE QC Finding Evidence", evidence)
	except frappe.DoesNotExistError:
		frappe.throw(
			"Source-document evidence is unavailable or outside the current clinical scope.",
			frappe.PermissionError,
		)
	if (
		str(evidence_doc.get("finding") or "") != str(finding_doc.name)
		or not finding_permission(finding_doc, "read", user=user)
		or not qc_finding_evidence_permission(evidence_doc, "read", user=user)
	):
		frappe.throw(
			"Source-document evidence is unavailable or outside the current clinical scope.",
			frappe.PermissionError,
		)
	scope = _document_scope(finding_doc)
	require_scope_read(
		**scope,
		user=user,
		target_doctype="IONE QC Finding",
		include_personal=True,
	)
	request_id = uuid.uuid4().hex
	raw_record_id = _source_record_id(evidence_doc)
	source_system = str(evidence_doc.get("source_system_link") or "")
	record_type = str(evidence_doc.get("source_record_type") or "")
	record_id_hash = _record_id_hash(source_system, record_type, raw_record_id)
	if not source_system or not record_type or not raw_record_id:
		_write_access_log(
			request_id=request_id,
			finding=finding_doc,
			evidence=evidence_doc,
			locator=None,
			source_system=source_system,
			source_record_type=record_type,
			record_id_hash=record_id_hash,
			result_code="EVIDENCE_SOURCE_IDENTITY_INVALID",
		)
		return _unavailable(request_id, "EVIDENCE_SOURCE_IDENTITY_INVALID")

	locators = _matching_approved_locators(source_system, record_type, scope)
	if len(locators) != 1:
		result_code = "NO_APPROVED_LOCATOR" if not locators else "AMBIGUOUS_LOCATOR"
		_write_access_log(
			request_id=request_id,
			finding=finding_doc,
			evidence=evidence_doc,
			locator=None,
			source_system=source_system,
			source_record_type=record_type,
			record_id_hash=record_id_hash,
			result_code=result_code,
		)
		return _unavailable(request_id, result_code)

	locator_doc = frappe.get_doc(LOCATOR_DOCTYPE, locators[0], for_update=False)
	try:
		url, host = _live_deep_link(locator_doc, scope, raw_record_id)
	except SourceDocumentConfigurationError:
		_write_access_log(
			request_id=request_id,
			finding=finding_doc,
			evidence=evidence_doc,
			locator=locator_doc.name,
			source_system=source_system,
			source_record_type=record_type,
			record_id_hash=record_id_hash,
			result_code="LOCATOR_CONFIGURATION_INVALID",
		)
		return _unavailable(request_id, "LOCATOR_CONFIGURATION_INVALID")

	_write_access_log(
		request_id=request_id,
		finding=finding_doc,
		evidence=evidence_doc,
		locator=locator_doc.name,
		source_system=source_system,
		source_record_type=record_type,
		record_id_hash=record_id_hash,
		result_code="RESOLVED",
	)
	return {
		"available": True,
		"url": url,
		"allowed_host": host,
		"open_in_new_window": True,
		"referrer_policy": "no-referrer",
		"request_id": request_id,
	}


def validate_source_document_access_log(doc, method: str | None = None) -> None:
	del method
	if doc.is_new():
		if getattr(doc.flags, "ione_source_document_access_log", None) is not _ACCESS_LOG_TOKEN:
			frappe.throw("Source-document access logs are system-managed.", frappe.PermissionError)
		if str(doc.get("accessed_by") or "") in {"", "Guest", "Administrator"}:
			frappe.throw("Source-document access logs require a named clinical user.")
		if str(doc.get("accessed_by") or "") != str(frappe.session.user or ""):
			frappe.throw("Source-document access-log actor does not match the request user.")
		request_id = str(doc.get("request_id") or "")
		if not re.fullmatch(r"[0-9a-f]{32}", request_id):
			frappe.throw("Source-document access-log request identity is invalid.")
		finding = frappe.get_doc("IONE QC Finding", doc.get("finding"))
		evidence = frappe.get_doc("IONE QC Finding Evidence", doc.get("evidence"))
		if str(evidence.get("finding") or "") != str(finding.name):
			frappe.throw("Source-document access-log evidence lineage is invalid.")
		scope = _document_scope(finding)
		if any(str(doc.get(fieldname) or "") != value for fieldname, value in scope.items()):
			frappe.throw("Source-document access-log clinical scope is invalid.")
		source_system = str(evidence.get("source_system_link") or "")
		record_type = str(evidence.get("source_record_type") or "")
		raw_record_id = _source_record_id(evidence)
		expected_record_hash = _record_id_hash(source_system, record_type, raw_record_id)
		if (
			str(doc.get("source_system") or "") != source_system
			or str(doc.get("source_record_type") or "") != record_type
		):
			frappe.throw("Source-document access-log source lineage is invalid.")
		if str(doc.get("record_id_hash") or "") != expected_record_hash:
			frappe.throw("Source-document access logs require a record identifier hash.")
		expected_access_key = hashlib.sha256(
			f"{request_id}|{finding.name}|{evidence.name}|{doc.get('result_code') or ''}".encode()
		).hexdigest()
		if str(doc.get("access_key") or "") != expected_access_key:
			frappe.throw("Source-document access-log integrity key is invalid.")
		if doc.get("locator"):
			locator = frappe.get_doc(LOCATOR_DOCTYPE, doc.locator)
			if (
				str(locator.get("source_system") or "") != source_system
				or str(locator.get("source_record_type") or "") != record_type
				or not _scope_matches(locator, scope)
			):
				frappe.throw("Source-document access-log locator lineage is invalid.")
		return
	validate_append_only(doc)


def prevent_source_document_record_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Source-document locator governance and access records cannot be deleted.")


def _live_deep_link(locator, scope: dict[str, str], raw_record_id: str) -> tuple[str, str]:
	if locator.get("status") != "Approved" or int(locator.get("enabled") or 0) != 1:
		raise SourceDocumentConfigurationError("LOCATOR_NOT_ACTIVE")
	if str(locator.get("approval_checksum") or "") != _approval_checksum(locator):
		raise SourceDocumentConfigurationError("LOCATOR_CHECKSUM_MISMATCH")
	if not _scope_matches(locator, scope):
		raise SourceDocumentConfigurationError("LOCATOR_SCOPE_MISMATCH")
	try:
		source, endpoint, origin, host = _validate_locator_endpoint_contract(
			locator,
			require_runtime=True,
		)
		authorize_endpoint_scope(
			endpoint,
			source,
			{**scope, "source_system": source.name},
			require_hospital=True,
		)
	except (frappe.ValidationError, frappe.PermissionError, ValueError, TypeError) as exc:
		raise SourceDocumentConfigurationError("LOCATOR_RUNTIME_CONTRACT_INVALID") from exc
	encoded_identifier = quote(raw_record_id, safe="", encoding="utf-8", errors="strict")
	url = f"{origin}{locator.path_prefix}{encoded_identifier}{locator.get('path_suffix') or ''}"
	parsed = urlparse(url)
	if (
		parsed.scheme != "https"
		or parsed.hostname != host
		or parsed.username
		or parsed.password
		or parsed.query
		or parsed.fragment
		or parsed.netloc != urlparse(origin).netloc
	):
		raise SourceDocumentConfigurationError("GENERATED_URL_INVALID")
	return url, host


def _validate_locator_endpoint_contract(
	doc,
	*,
	require_runtime: bool,
) -> tuple[Any, Any, str, str]:
	source_name = str(doc.get("source_system") or "")
	endpoint_name = str(doc.get("endpoint") or "")
	if not source_name or not endpoint_name:
		frappe.throw("Source-document locators require a source system and endpoint.")
	source = frappe.get_cached_doc("IONE Source System", source_name)
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	if str(endpoint.get("source_system") or "") != source.name:
		frappe.throw("Source-document locator endpoint and source system do not match.")
	if endpoint.get("direction") != "Read Only":
		frappe.throw("Source-document locator endpoints must be Read Only.")
	if int(endpoint.get("tls_verify") or 0) != 1:
		frappe.throw("Source-document locator endpoints must verify TLS.")
	if int(source.get("read_only") or 0) != 1:
		frappe.throw("Source-document locator source systems must be read-only.")
	if require_runtime and (int(source.get("enabled") or 0) != 1 or int(endpoint.get("enabled") or 0) != 1):
		frappe.throw("Source-document locator source and endpoint must both be enabled.")
	origin, host = _https_origin(endpoint.get("base_url"))
	if host not in _exact_allowed_hosts(source.get("allowed_hosts")):
		frappe.throw("Source-document locator host is not allowlisted by the source system.")
	if host not in _exact_allowed_hosts(endpoint.get("allowed_hosts")):
		frappe.throw("Source-document locator host is not allowlisted by the endpoint.")
	return source, endpoint, origin, host


def _validate_locator_identifiers(doc) -> None:
	code = str(doc.get("locator_code") or "")
	version = str(doc.get("version") or "")
	record_type = str(doc.get("source_record_type") or "")
	if not code or code != code.strip() or len(code) > 140:
		frappe.throw("Locator code must contain 1-140 canonical characters.")
	if not version or version != version.strip() or len(version) > 40:
		frappe.throw("Locator version must contain 1-40 canonical characters.")
	if (
		not record_type
		or record_type != record_type.strip()
		or len(record_type) > MAX_SOURCE_RECORD_TYPE_LENGTH
		or any(ord(character) < 32 for character in record_type)
	):
		frappe.throw("Source record type is invalid.")


def _validate_locator_path(doc) -> None:
	prefix = str(doc.get("path_prefix") or "")
	suffix = str(doc.get("path_suffix") or "")
	if len(prefix) > MAX_PATH_COMPONENT_LENGTH or not _valid_prefix(prefix):
		frappe.throw(
			"Path prefix must be an absolute, static HTTPS path ending in '/'; "
			"templates, queries, fragments, percent escapes, and dot segments are forbidden."
		)
	if len(suffix) > MAX_PATH_COMPONENT_LENGTH or not _valid_suffix(suffix):
		frappe.throw(
			"Path suffix must be empty, a static '/segment' path, or a static extension; "
			"templates, queries, fragments, percent escapes, and dot segments are forbidden."
		)
	for fieldname, label in (
		("sso_uat_reference", "SSO UAT reference"),
		("redirect_uat_reference", "no-cross-host-redirect UAT reference"),
	):
		reference = str(doc.get(fieldname) or "")
		if reference and not _UAT_REFERENCE.fullmatch(reference):
			frappe.throw(f"{label} must be a 1-140 character canonical test-evidence identifier, not a URL.")
		if not reference and doc.get("status") != "Draft":
			frappe.throw(f"Approved source-document locators require a {label}.")


def _valid_prefix(value: str) -> bool:
	if not value.startswith("/") or not value.endswith("/") or "\\" in value or "%" in value:
		return False
	if any(character in value for character in ("?", "#", "{", "}")):
		return False
	segments = value.split("/")[1:-1]
	return all(_valid_static_segment(segment) for segment in segments)


def _valid_suffix(value: str) -> bool:
	if value == "":
		return True
	if "\\" in value or "%" in value or any(character in value for character in ("?", "#", "{", "}")):
		return False
	if value.startswith("."):
		return bool(_PATH_SEGMENT.fullmatch(value[1:])) and value[1:] not in {".", ".."}
	if not value.startswith("/") or value.endswith("/"):
		return False
	return all(_valid_static_segment(segment) for segment in value.split("/")[1:])


def _valid_static_segment(segment: str) -> bool:
	return segment not in {"", ".", ".."} and bool(_PATH_SEGMENT.fullmatch(segment))


def _validate_locator_scope(doc) -> None:
	if not str(doc.get("hospital") or ""):
		frappe.throw("Source-document locators require an exact hospital.")
	scope = _document_scope(doc)
	try:
		source = frappe.get_cached_doc("IONE Source System", doc.get("source_system"))
		endpoint = frappe.get_cached_doc("IONE Integration Endpoint", doc.get("endpoint"))
		authorize_endpoint_scope(
			endpoint,
			source,
			{**scope, "source_system": source.name},
			require_hospital=True,
		)
	except (frappe.ValidationError, frappe.PermissionError, ValueError, TypeError) as exc:
		raise frappe.ValidationError(
			"Source-document locator scope exceeds its source or endpoint authority."
		) from exc


def _set_locator_keys(doc) -> None:
	family_payload = _canonical_json(
		{
			"source_system": str(doc.get("source_system") or ""),
			"source_record_type": str(doc.get("source_record_type") or ""),
			"hospital": str(doc.get("hospital") or ""),
		}
	)
	scope_payload = _canonical_json(
		{
			"family": hashlib.sha256(family_payload.encode()).hexdigest(),
			"campus": str(doc.get("campus") or ""),
			"department": str(doc.get("department") or ""),
			"ward": str(doc.get("ward") or ""),
		}
	)
	doc.scope_family_key = hashlib.sha256(family_payload.encode()).hexdigest()
	doc.scope_key = hashlib.sha256(scope_payload.encode()).hexdigest()
	doc.locator_key = hashlib.sha256(
		f"{doc.scope_key}|{doc.get('locator_code') or ''}|{doc.get('version') or ''}".encode()
	).hexdigest()
	doc.active_slot_key = doc.scope_key if doc.get("status") != "Retired" else None


def _validate_locator_lifecycle(doc) -> None:
	status = str(doc.get("status") or "")
	if status not in LOCATOR_STATUSES:
		frappe.throw("Source-document locator status is invalid.")
	if doc.is_new():
		if status != "Draft" or int(doc.get("enabled") or 0) != 0:
			frappe.throw("New source-document locators must start disabled in Draft.")
		return
	previous = frappe.get_doc(LOCATOR_DOCTYPE, doc.name, for_update=True)
	if not previous:
		frappe.throw("Source-document locator disappeared during validation.")
	if not _managed_locator_mutation():
		_require_named_role(*LOCATOR_AUTHOR_ROLES)
		if str(previous.get("status") or "") != "Draft" or status != "Draft":
			frappe.throw(
				"Approved source-document locator lifecycle changes require the governed API.",
				frappe.PermissionError,
			)
		for fieldname in _LOCATOR_MANAGED_FIELDS:
			if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or ""):
				frappe.throw(
					"Source-document locator managed fields cannot be edited directly.",
					frappe.PermissionError,
				)
		return
	if str(previous.get("status") or "") in {"Approved", "Suspended", "Retired"}:
		changed = [
			fieldname
			for fieldname in _LOCATOR_SEMANTIC_FIELDS
			if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
		]
		if changed:
			frappe.throw("Approved source-document locator semantics are immutable; create a new version.")
		if str(previous.get("approval_checksum") or "") != str(doc.get("approval_checksum") or ""):
			frappe.throw("Source-document locator approval checksum is immutable.")


def _lock_and_reject_overlapping_locators(doc) -> None:
	rows = frappe.db.sql(
		"select name, campus, department, ward "  # noqa: S608
		f"from `tab{LOCATOR_DOCTYPE}` "
		"where scope_family_key = %s and name != %s "
		"and status != 'Retired' for update",
		(doc.scope_family_key, str(doc.name or "")),
		as_dict=True,
	)
	candidate = tuple(str(doc.get(fieldname) or "") for fieldname in ("campus", "department", "ward"))
	for row in rows:
		existing = tuple(str(row.get(fieldname) or "") for fieldname in ("campus", "department", "ward"))
		if all(
			not left or not right or left == right for left, right in zip(candidate, existing, strict=True)
		):
			frappe.throw("Source-document locator scope overlaps another non-retired configuration.")


def _matching_approved_locators(
	source_system: str,
	source_record_type: str,
	scope: dict[str, str],
) -> list[str]:
	rows = frappe.get_all(
		LOCATOR_DOCTYPE,
		filters={
			"source_system": source_system,
			"source_record_type": source_record_type,
			"hospital": scope.get("hospital"),
			"status": "Approved",
			"enabled": 1,
		},
		fields=["name", "campus", "department", "ward"],
		limit_page_length=MAX_LOCATORS_PER_SCOPE_FAMILY + 1,
	)
	if len(rows) > MAX_LOCATORS_PER_SCOPE_FAMILY:
		return [str(row.name) for row in rows]
	return [str(row.name) for row in rows if _scope_matches(row, scope)]


def _scope_matches(locator, scope: dict[str, str]) -> bool:
	return all(
		not str(locator.get(fieldname) or "")
		or str(locator.get(fieldname) or "") == str(scope.get(fieldname) or "")
		for fieldname in ("hospital", "campus", "department", "ward")
	)


def _write_access_log(
	*,
	request_id: str,
	finding,
	evidence,
	locator: str | None,
	source_system: str,
	source_record_type: str,
	record_id_hash: str,
	result_code: str,
) -> None:
	scope = _document_scope(finding)
	accessed_at = now_datetime()
	access_key = hashlib.sha256(
		f"{request_id}|{finding.name}|{evidence.name}|{result_code}".encode()
	).hexdigest()
	doc = frappe.get_doc(
		{
			"doctype": ACCESS_LOG_DOCTYPE,
			"access_key": access_key,
			"request_id": request_id,
			"finding": finding.name,
			"evidence": evidence.name,
			"locator": locator,
			"source_system": source_system or None,
			"source_record_type": source_record_type or None,
			"record_id_hash": record_id_hash,
			**scope,
			"accessed_by": frappe.session.user,
			"accessed_at": accessed_at,
			"result_code": result_code,
		}
	)
	doc.flags.ione_source_document_access_log = _ACCESS_LOG_TOKEN
	doc.insert(ignore_permissions=True)


def _approval_checksum(doc) -> str:
	source, endpoint, origin, host = _validate_locator_endpoint_contract(
		doc,
		require_runtime=False,
	)
	contract = {
		"source_system": str(source.name),
		"source_identity_namespace": str(source.get("identity_namespace") or ""),
		"endpoint": str(endpoint.name),
		"origin": origin,
		"host": host,
		"source_allowed_hosts": sorted(_exact_allowed_hosts(source.get("allowed_hosts"))),
		"endpoint_allowed_hosts": sorted(_exact_allowed_hosts(endpoint.get("allowed_hosts"))),
		"direction": str(endpoint.get("direction") or ""),
		"tls_verify": int(endpoint.get("tls_verify") or 0),
		"read_only": int(source.get("read_only") or 0),
		"scope_policy_checksum": scope_policy_checksum(endpoint, source),
	}
	return hashlib.sha256(
		_canonical_json(
			{
				"locator": {
					fieldname: str(doc.get(fieldname) or "") for fieldname in _LOCATOR_SEMANTIC_FIELDS
				},
				"endpoint_contract": contract,
			}
		).encode()
	).hexdigest()


def _source_record_id(evidence) -> str:
	value = evidence.get("source_record_id")
	if not isinstance(value, str):
		return ""
	if (
		not value
		or len(value) > MAX_SOURCE_RECORD_ID_LENGTH
		or any(ord(character) < 32 for character in value)
	):
		return ""
	return value


def _record_id_hash(source_system: str, record_type: str, raw_record_id: str) -> str:
	return hashlib.sha256(f"{source_system}|{record_type}|{raw_record_id}".encode()).hexdigest()


def _https_origin(value: Any) -> tuple[str, str]:
	parsed = urlparse(str(value or ""))
	if (
		parsed.scheme != "https"
		or not parsed.hostname
		or parsed.username
		or parsed.password
		or parsed.query
		or parsed.fragment
		or parsed.path not in {"", "/"}
	):
		frappe.throw(
			"Source-document locator base URL must be an HTTPS origin without path, "
			"credentials, query, or fragment."
		)
	host = parsed.hostname.lower()
	return f"https://{parsed.netloc}", host


def _exact_allowed_hosts(value: Any) -> frozenset[str]:
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Source-document host allowlist must be valid JSON.") from exc
	if not isinstance(parsed, list) or not parsed:
		frappe.throw("Source-document host allowlist must be a non-empty JSON array.")
	hosts: set[str] = set()
	for item in parsed:
		if not isinstance(item, str) or item != item.strip() or not item:
			frappe.throw("Source-document host allowlist entries must be canonical host names.")
		host = item.lower()
		if (
			"*" in host
			or "/" in host
			or "@" in host
			or ":" in host
			or urlparse(f"https://{host}").hostname != host
		):
			frappe.throw("Source-document host allowlist does not permit wildcards, ports, or URLs.")
		hosts.add(host)
	if len(hosts) != len(parsed):
		frappe.throw("Source-document host allowlist contains duplicate hosts.")
	return frozenset(hosts)


def _document_scope(doc) -> dict[str, str]:
	return {
		fieldname: str(doc.get(fieldname) or "") for fieldname in ("hospital", "campus", "department", "ward")
	}


def _canonical_json(value: dict[str, Any]) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_comment(value: str, label: str) -> str:
	comment = str(value or "").strip()
	if not comment or len(comment) > 500:
		frappe.throw(f"{label} must contain 1-500 characters.")
	return comment


def _require_named_role(*roles: str) -> str:
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw("This governed action requires a named accountable user.", frappe.PermissionError)
	require_role(*roles, user=user)
	return user


def _unavailable(request_id: str, error_code: str) -> dict[str, Any]:
	return {
		"available": False,
		"error_code": error_code,
		"request_id": request_id,
	}


@contextmanager
def _trusted_locator_mutation():
	attribute = "ione_source_document_locator_mutation"
	previous_exists = attribute in frappe.flags
	previous = frappe.flags.get(attribute)
	setattr(frappe.flags, attribute, _LOCATOR_MANAGED_TOKEN)
	try:
		yield
	finally:
		if not previous_exists:
			frappe.flags.pop(attribute, None)
		else:
			setattr(frappe.flags, attribute, previous)


def _managed_locator_mutation() -> bool:
	return getattr(frappe.flags, "ione_source_document_locator_mutation", None) is _LOCATOR_MANAGED_TOKEN

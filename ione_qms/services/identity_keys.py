from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import frappe

from ione_qms.services.crypto_keys import (
	HMACKey,
	active_hmac_key,
	retained_hmac_keys,
)

IDENTITY_KEY_CONTRACT_PREFIX = "ione-qms-identity-key-v1"
IDENTITY_KEY_KINDS = frozenset({"encounter", "patient"})
MAX_IDENTITY_KEY_MIGRATION_BATCH = 1_000


def identity_key_contract() -> str:
	return f"{IDENTITY_KEY_CONTRACT_PREFIX}:{active_hmac_key('ione_identity_hmac').key_id}"


def acceptable_identity_key_contracts() -> tuple[str, ...]:
	"""Return active-first contracts that remain cryptographically verifiable."""
	active = active_hmac_key("ione_identity_hmac")
	keys = (active, *retained_hmac_keys("ione_identity_hmac"))
	return tuple(f"{IDENTITY_KEY_CONTRACT_PREFIX}:{key.key_id}" for key in keys)


def identity_key_candidates(
	kind: str,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
) -> tuple[tuple[str, str], ...]:
	"""Return active-first lookup keys for zero-downtime retained-key rotation."""
	active = active_hmac_key("ione_identity_hmac")
	keys = (active, *retained_hmac_keys("ione_identity_hmac"))
	return tuple(
		(
			f"{IDENTITY_KEY_CONTRACT_PREFIX}:{key.key_id}",
			_identity_key_with_key(
				kind,
				source_namespace,
				tenant_hospital,
				source_id,
				key=key,
			),
		)
		for key in keys
	)


def site_identity_key(
	kind: str,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
) -> str:
	"""Return a domain-separated site-keyed identity surrogate."""
	return _identity_key_with_key(
		kind,
		source_namespace,
		tenant_hospital,
		source_id,
		key=active_hmac_key("ione_identity_hmac"),
	)


def stable_source_identity_key(
	kind: str,
	source_system: Any,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
) -> str:
	"""Exact-case, length-delimited tuple fingerprint independent of key rotation."""
	parts = (
		_bounded_identity_part(kind, "kind").lower(),
		_bounded_identity_part(source_system, "source_system"),
		_bounded_identity_part(source_namespace, "source_namespace"),
		_bounded_identity_part(tenant_hospital, "tenant_hospital"),
		_bounded_identity_part(source_id, "source_id"),
	)
	if parts[0] not in IDENTITY_KEY_KINDS:
		raise frappe.ValidationError("Identity-key kind is not governed.")
	framed = b"".join(
		len(encoded).to_bytes(8, "big") + encoded for encoded in (part.encode("utf-8") for part in parts)
	)
	return hashlib.sha256(b"ione-stable-source-identity-v1\0" + framed).hexdigest()


def verify_stored_identity_key(
	kind: str,
	stored_key: Any,
	stored_contract: Any,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
	*,
	allow_legacy: bool = False,
) -> bool:
	"""Verify either the legacy public hash or an active/retained keyed surrogate."""
	active = active_hmac_key("ione_identity_hmac")
	return _verify_stored_identity_key_with_keys(
		kind,
		stored_key,
		stored_contract,
		source_namespace,
		tenant_hospital,
		source_id,
		keys=(active, *retained_hmac_keys("ione_identity_hmac")),
		allow_legacy=allow_legacy,
	)


def _verify_stored_identity_key_with_keys(
	kind: str,
	stored_key: Any,
	stored_contract: Any,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
	*,
	keys: tuple[HMACKey, ...],
	allow_legacy: bool,
) -> bool:
	contract = str(stored_contract or "").strip()
	candidate = str(stored_key or "").strip().lower()
	if not contract:
		if not allow_legacy:
			return False
		expected = legacy_public_identity_key(source_namespace, tenant_hospital, source_id)
		return hmac.compare_digest(candidate, expected)
	prefix = f"{IDENTITY_KEY_CONTRACT_PREFIX}:"
	if not contract.startswith(prefix):
		return False
	key_id = contract.removeprefix(prefix)
	key = next((candidate_key for candidate_key in keys if candidate_key.key_id == key_id), None)
	if key is None:
		return False
	try:
		expected = _identity_key_with_key(
			kind,
			source_namespace,
			tenant_hospital,
			source_id,
			key=key,
		)
	except frappe.ValidationError:
		return False
	return hmac.compare_digest(candidate, expected)


def _identity_key_with_key(
	kind: str,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
	*,
	key: HMACKey,
) -> str:
	normalized_kind = str(kind or "").strip().lower()
	if normalized_kind not in IDENTITY_KEY_KINDS:
		raise frappe.ValidationError("Identity-key kind is not governed.")
	values = {
		"contract": f"{IDENTITY_KEY_CONTRACT_PREFIX}:{key.key_id}",
		"kind": normalized_kind,
		"source_namespace": _bounded_identity_part(source_namespace, "source_namespace"),
		"tenant_hospital": _bounded_identity_part(tenant_hospital, "tenant_hospital"),
		"source_id": _bounded_identity_part(source_id, "source_id"),
	}
	message = json.dumps(
		values,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	).encode()
	return hmac.new(key.secret, message, hashlib.sha256).hexdigest()


def legacy_public_identity_key(
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
) -> str:
	"""Identify the former public SHA key only for bounded upgrade detection."""
	return hashlib.sha256(
		(
			f"{_bounded_identity_part(source_namespace, 'source_namespace')}|"
			f"{_bounded_identity_part(tenant_hospital, 'tenant_hospital')}|"
			f"{_bounded_identity_part(source_id, 'source_id')}"
		).encode()
	).hexdigest()


def backfill_site_identity_keys(batch_size: int = 200) -> dict[str, int | bool]:
	"""Rekey legacy or retained-key surrogates to one captured active key, fail closed."""
	limit = min(max(int(batch_size or 200), 1), MAX_IDENTITY_KEY_MIGRATION_BATCH)
	if not all(
		frappe.db.exists("DocType", doctype) for doctype in ("IONE Patient Index", "IONE Encounter Index")
	):
		return {"updated": 0, "blocked": 0, "has_more": False}
	updated = 0
	blocked = 0
	processed = 0
	has_more = False
	target_key = active_hmac_key("ione_identity_hmac")
	verification_keys = (
		target_key,
		*retained_hmac_keys("ione_identity_hmac"),
	)
	current_contract = f"{IDENTITY_KEY_CONTRACT_PREFIX}:{target_key.key_id}"
	contracts = (
		(
			"IONE Patient Index",
			"patient",
			"patient_key",
			"tenant_hospital",
			"source_patient_id",
		),
		(
			"IONE Encounter Index",
			"encounter",
			"encounter_key",
			"hospital",
			"source_encounter_id",
		),
	)
	for doctype, kind, key_field, hospital_field, source_id_field in contracts:
		remaining = limit - processed
		if remaining <= 0:
			has_more = True
			break
		rows = frappe.db.sql(
			f"select name from `tab{doctype}` "  # noqa: S608
			"where coalesce(identity_key_contract, '') != %s "
			"or coalesce(stable_source_key, '') = '' "
			"order by name asc limit %s",
			(current_contract, remaining + 1),
			as_dict=True,
		)
		if len(rows) > remaining:
			has_more = True
		for candidate in rows[:remaining]:
			processed += 1
			doc = frappe.get_doc(doctype, candidate.name, for_update=True)
			parts = (
				str(doc.get("source_system") or "").strip(),
				str(doc.get("source_namespace") or "").strip(),
				str(doc.get(hospital_field) or "").strip(),
				str(doc.get(source_id_field) or "").strip(),
			)
			if not all(parts):
				blocked += 1
				continue
			if not _verify_stored_identity_key_with_keys(
				kind,
				doc.get(key_field),
				doc.get("identity_key_contract"),
				*parts[1:],
				keys=verification_keys,
				allow_legacy=True,
			):
				blocked += 1
				continue
			expected = _identity_key_with_key(kind, *parts[1:], key=target_key)
			stable_key = stable_source_identity_key(kind, *parts)
			stored_stable_key = str(doc.get("stable_source_key") or "")
			if stored_stable_key and not hmac.compare_digest(stored_stable_key, stable_key):
				blocked += 1
				continue
			conflicts = frappe.db.sql(
				f"select name from `tab{doctype}` where {key_field} = %s "  # noqa: S608
				"order by name asc limit 2 for update",
				(expected,),
				as_dict=True,
			)
			if any(str(conflict.get("name") or "") != str(doc.name) for conflict in conflicts):
				blocked += 1
				continue
			stable_conflicts = frappe.db.sql(
				f"select name from `tab{doctype}` where stable_source_key = %s "  # noqa: S608
				"order by name asc limit 2 for update",
				(stable_key,),
				as_dict=True,
			)
			if any(str(conflict.get("name") or "") != str(doc.name) for conflict in stable_conflicts):
				blocked += 1
				continue
			frappe.db.set_value(
				doctype,
				doc.name,
				{
					key_field: expected,
					"identity_key_contract": current_contract,
					"stable_source_key": stable_key,
				},
				update_modified=False,
			)
			updated += 1
	return {"updated": updated, "blocked": blocked, "has_more": has_more}


def _bounded_identity_part(value: Any, label: str) -> str:
	text = str(value or "").strip()
	if not text or len(text) > 255 or "\x00" in text:
		raise frappe.ValidationError(f"{label} must contain 1-255 safe identity characters.")
	return text

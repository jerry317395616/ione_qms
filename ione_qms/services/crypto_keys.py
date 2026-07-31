from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import frappe

_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MIN_SECRET_LENGTH = 32


@dataclass(frozen=True)
class HMACKey:
	key_id: str
	secret: bytes


def active_hmac_key(config_prefix: str) -> HMACKey:
	"""Load a dedicated active site HMAC key without encryption-key fallback."""
	conf = _site_conf()
	key_id = _validated_key_id(conf.get(f"{config_prefix}_key_id"), config_prefix)
	secret = _validated_secret(conf.get(f"{config_prefix}_key"), config_prefix)
	return HMACKey(key_id=key_id, secret=secret)


def verification_hmac_key(config_prefix: str, key_id: str) -> HMACKey:
	"""Resolve an active or retained historical verification-only key."""
	requested = _validated_key_id(key_id, config_prefix)
	active = active_hmac_key(config_prefix)
	if requested == active.key_id:
		return active
	for candidate in retained_hmac_keys(config_prefix):
		if candidate.key_id == requested:
			return candidate
	raise frappe.ValidationError(
		f"Retained verification key '{requested}' is unavailable for {config_prefix}."
	)


def retained_hmac_keys(config_prefix: str) -> tuple[HMACKey, ...]:
	"""Validate and return the complete historical verification-only key ring."""
	conf = _site_conf()
	raw_ring = conf.get(f"{config_prefix}_verify_keys")
	if isinstance(raw_ring, str):
		try:
			raw_ring = json.loads(raw_ring)
		except (TypeError, ValueError) as exc:
			raise frappe.ValidationError(f"{config_prefix}_verify_keys must be a JSON object.") from exc
	if raw_ring in (None, ""):
		raw_ring = {}
	if not isinstance(raw_ring, Mapping):
		raise frappe.ValidationError(f"{config_prefix}_verify_keys must be a JSON object.")
	if len(raw_ring) > 16:
		raise frappe.ValidationError(f"{config_prefix}_verify_keys exceeds 16 retained keys.")
	active = active_hmac_key(config_prefix)
	keys: list[HMACKey] = []
	for candidate_id, candidate_secret in sorted(raw_ring.items(), key=lambda item: str(item[0])):
		normalized_id = _validated_key_id(candidate_id, config_prefix)
		if normalized_id == active.key_id:
			raise frappe.ValidationError(f"{config_prefix}_verify_keys must not repeat the active key ID.")
		keys.append(
			HMACKey(
				key_id=normalized_id,
				secret=_validated_secret(candidate_secret, config_prefix),
			)
		)
	if len({key.secret for key in keys}) != len(keys):
		raise frappe.ValidationError(
			f"{config_prefix}_verify_keys must use a distinct secret for every key ID."
		)
	if any(key.secret == active.secret for key in keys):
		raise frappe.ValidationError(f"{config_prefix}_verify_keys must not reuse the active key secret.")
	return tuple(keys)


def _site_conf():
	conf = getattr(getattr(frappe, "local", None), "conf", None) or getattr(frappe, "conf", None)
	if conf is None or not hasattr(conf, "get"):
		raise frappe.ValidationError("Site cryptographic configuration is unavailable.")
	return conf


def _validated_key_id(value: Any, label: str) -> str:
	key_id = str(value or "").strip()
	if not _KEY_ID_PATTERN.fullmatch(key_id):
		raise frappe.ValidationError(f"{label}_key_id must contain 1-64 safe identifier characters.")
	return key_id


def _validated_secret(value: Any, label: str) -> bytes:
	secret = str(value or "")
	if len(secret.encode("utf-8")) < _MIN_SECRET_LENGTH or "\x00" in secret:
		raise frappe.ValidationError(f"{label}_key must contain at least {_MIN_SECRET_LENGTH} bytes.")
	return secret.encode("utf-8")

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from typing import Any

import frappe

from ione_qms.services.runtime_settings import production_mode_enabled

MAX_CLOCK_SKEW_SECONDS = 300
NONCE_TTL_SECONDS = 600


def verify_request(
	endpoint: Any,
	raw_body: bytes,
	*,
	force_signature: bool = False,
) -> None:
	_verify_source_ip(endpoint)
	_enforce_rate_limit(endpoint)
	if not int(endpoint.get("require_signature") or 0):
		if force_signature or unsigned_requests_rejected():
			_raise_authentication_error("Unsigned integration requests are disabled")
		return
	timestamp = (frappe.get_request_header("X-IONE-Timestamp") or "").strip()
	nonce = (frappe.get_request_header("X-IONE-Nonce") or "").strip()
	signature = (frappe.get_request_header("X-IONE-Signature") or "").strip().lower()
	if not timestamp or not nonce or not signature:
		_raise_authentication_error("Missing request-signature headers")
	try:
		request_time = int(timestamp)
	except ValueError:
		_raise_authentication_error("Invalid request timestamp")
	if abs(int(time.time()) - request_time) > MAX_CLOCK_SKEW_SECONDS:
		_raise_authentication_error("Request timestamp is outside the accepted window")
	if len(nonce) < 16 or len(nonce) > 128:
		_raise_authentication_error("Invalid request nonce")

	nonce_key = f"ione_qms:integration_nonce:{endpoint.name}:{nonce}"
	cache = frappe.cache

	secret = endpoint.get_password("shared_secret", raise_exception=False)
	if not secret:
		_raise_authentication_error("Integration endpoint has no signing secret")
	message = signature_message(endpoint.name, timestamp, nonce, raw_body)
	expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
	if not hmac.compare_digest(expected, signature):
		_raise_authentication_error("Invalid request signature")
	if not cache.set(
		name=cache.make_key(nonce_key),
		value=b"1",
		ex=NONCE_TTL_SECONDS,
		nx=True,
	):
		_raise_authentication_error("Replay detected")


def signature_message(endpoint: str, timestamp: str, nonce: str, raw_body: bytes) -> bytes:
	"""Bind the credential-selected endpoint and exact body into one unambiguous MAC input."""
	return (
		str(timestamp).encode() + b"." + str(nonce).encode() + b"." + str(endpoint).encode() + b"." + raw_body
	)


def unsigned_requests_rejected() -> bool:
	if production_mode_enabled():
		return True
	return bool(
		frappe.db.exists("DocType", "IONE Integration Settings")
		and frappe.db.get_single_value(
			"IONE Integration Settings",
			"reject_unsigned_requests",
		)
	)


def _verify_source_ip(endpoint: Any) -> None:
	_verify_forwarded_chain()
	allowed = str(endpoint.get("allowed_ip_cidrs") or "").strip()
	if not allowed:
		if production_mode_enabled():
			_raise_authentication_error("Production integration endpoint has no source CIDR allowlist")
		return
	remote = _remote_address()
	try:
		address = ipaddress.ip_address(remote)
	except ValueError:
		_raise_authentication_error("Unable to validate source IP")
	networks = []
	for item in allowed.replace(",", "\n").splitlines():
		if item.strip():
			networks.append(ipaddress.ip_network(item.strip(), strict=False))
	if not any(address in network for network in networks):
		_raise_authentication_error("Source IP is not allowed")


def _verify_forwarded_chain() -> None:
	"""Reject the attacker-controlled leftmost-XFF pattern used by proxy_add_x_forwarded_for."""
	if not production_mode_enabled():
		return
	forwarded = str(frappe.get_request_header("X-Forwarded-For") or "").strip()
	if not forwarded:
		return
	chain = [item.strip() for item in forwarded.split(",") if item.strip()]
	if len(chain) != 1 or chain[0] != _remote_address():
		_raise_authentication_error("Untrusted forwarded source-IP chain")


def _enforce_rate_limit(endpoint: Any) -> None:
	limit = min(max(int(endpoint.get("rate_limit_per_minute") or 600), 1), 100000)
	remote = _remote_address()
	window = int(time.time()) // 60
	cache = frappe.cache
	key = cache.make_key(f"ione_qms:integration_rate:{endpoint.name}:{remote}:{window}")
	count = int(cache.incr(key))
	if count == 1:
		cache.expire(key, 120)
	if count > limit:
		frappe.local.response["http_status_code"] = 429
		frappe.throw("Integration endpoint rate limit exceeded", frappe.RateLimitExceededError)


def _remote_address() -> str:
	remote = getattr(frappe.local, "request_ip", None) or getattr(frappe.local, "request", None)
	if not isinstance(remote, str):
		remote = getattr(remote, "remote_addr", None)
	return str(remote or "unknown")


def _raise_authentication_error(message: str) -> None:
	frappe.local.response["http_status_code"] = 401
	frappe.throw(message, frappe.AuthenticationError)

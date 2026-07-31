from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

import frappe
import requests

from ione_qms.ai.governance import is_private_model_url

MAX_READINESS_RESPONSE_BYTES = 1024 * 1024


def check_qwen_readiness(
	model_name: str = "I-ONE Qwen 35B",
	*,
	require_authentication: bool = True,
) -> dict[str, Any]:
	"""Verify the configured Flow model without sending clinical content."""
	model = frappe.get_cached_doc("Flow Model", model_name)
	provider = frappe.get_cached_doc("Flow Provider", model.provider) if model.get("provider") else None
	base_url = model.get("base_url") or (provider.get("base_url") if provider else None)
	api_key = model.get_password("api_key", raise_exception=False) if model.get("api_key") else None
	if not api_key and provider and provider.get("api_key"):
		api_key = provider.get_password("api_key", raise_exception=False)
	if not base_url or not is_private_model_url(base_url):
		raise frappe.ValidationError("Qwen Flow Model must use an approved internal endpoint")
	if require_authentication and not api_key:
		raise frappe.ValidationError("Qwen endpoint authentication is required for production")

	models_url = urljoin(str(base_url).rstrip("/") + "/", "models")
	if require_authentication:
		anonymous = _get_json(models_url, api_key=None, allow_error=True)
		if anonymous["status_code"] not in {401, 403}:
			raise frappe.ValidationError("Qwen model discovery is accessible without authentication")

	response = _get_json(models_url, api_key=api_key, allow_error=False)
	served_models = {
		str(item.get("id"))
		for item in response["payload"].get("data", [])
		if isinstance(item, dict) and item.get("id")
	}
	expected_model = str(model.model_id).split("/", 1)[-1]
	if expected_model not in served_models:
		raise frappe.ValidationError(
			f"Configured model '{expected_model}' is not served by the Qwen endpoint"
		)
	return {
		"ready": True,
		"flow_model": model.name,
		"model_id": model.model_id,
		"served_models": sorted(served_models),
		"authentication_enforced": bool(require_authentication),
	}


def _get_json(
	url: str,
	*,
	api_key: str | None,
	allow_error: bool,
) -> dict[str, Any]:
	headers = {"Accept": "application/json"}
	if api_key:
		headers["Authorization"] = f"Bearer {api_key}"
	response = requests.get(
		url,
		headers=headers,
		timeout=(3.05, 15),
		allow_redirects=False,
		stream=True,
	)
	if response.is_redirect:
		raise frappe.ValidationError("Qwen readiness endpoint must not redirect")
	body = bytearray()
	for chunk in response.iter_content(chunk_size=65536):
		body.extend(chunk)
		if len(body) > MAX_READINESS_RESPONSE_BYTES:
			raise frappe.ValidationError("Qwen readiness response is too large")
	if not allow_error:
		response.raise_for_status()
	try:
		payload = json.loads(body.decode("utf-8")) if body else {}
	except (UnicodeDecodeError, ValueError) as exc:
		if allow_error and response.status_code >= 400:
			return {"status_code": response.status_code, "payload": {}}
		raise frappe.ValidationError("Qwen readiness response is not valid JSON") from exc
	if not isinstance(payload, dict):
		raise frappe.ValidationError("Qwen readiness response must be a JSON object")
	return {"status_code": response.status_code, "payload": payload}

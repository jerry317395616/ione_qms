from __future__ import annotations


def apply_security_headers(response, request=None):
	"""Apply response-level MIME hardening without changing Frappe core."""
	del request
	response.headers["X-Content-Type-Options"] = "nosniff"
	return response

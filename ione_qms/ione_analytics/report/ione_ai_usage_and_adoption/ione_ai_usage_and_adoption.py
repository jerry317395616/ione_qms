from __future__ import annotations

from typing import Any

from ione_qms.services.analytics_reports import execute_ai_usage_report


def execute(filters=None) -> tuple[Any, ...]:
	return execute_ai_usage_report(filters)

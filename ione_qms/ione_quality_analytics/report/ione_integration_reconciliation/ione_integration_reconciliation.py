from __future__ import annotations

from typing import Any

from ione_qms.services.analytics_reports import execute_integration_reconciliation_report


def execute(filters=None) -> tuple[Any, ...]:
	return execute_integration_reconciliation_report(filters)

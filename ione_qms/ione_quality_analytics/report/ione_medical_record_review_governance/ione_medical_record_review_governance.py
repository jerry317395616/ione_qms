from __future__ import annotations

from typing import Any

from ione_qms.services.analytics_reports import execute_medical_record_review_report


def execute(filters=None) -> tuple[Any, ...]:
	return execute_medical_record_review_report(filters)

from __future__ import annotations

from typing import Any

import frappe

from ione_qms.permissions import require_role
from ione_qms.rule_engine.testing import run_rule_test_case, run_rule_version_tests
from ione_qms.rule_engine.validation import queue_rule_validation
from ione_qms.services.versions import archive_standard_source_url, compare_standard_versions


@frappe.whitelist(methods=["GET"])
def get_standard_version_difference(
	baseline_version: str,
	target_version: str,
	change_start: int = 0,
	change_page_length: int = 25,
	rule_start: int = 0,
	rule_page_length: int = 25,
	relation_start: int = 0,
	relation_page_length: int = 25,
	expected_comparison_checksum: str | None = None,
) -> dict[str, Any]:
	return compare_standard_versions(
		baseline_version,
		target_version,
		change_start=change_start,
		change_page_length=change_page_length,
		rule_start=rule_start,
		rule_page_length=rule_page_length,
		relation_start=relation_start,
		relation_page_length=relation_page_length,
		expected_comparison_checksum=expected_comparison_checksum,
	)


@frappe.whitelist(methods=["POST"])
def archive_standard_source(standard: str) -> dict[str, str]:
	require_role("IONE QC Administrator", "IONE QC Reviewer", "IONE Medical Affairs")
	return archive_standard_source_url(standard)


@frappe.whitelist(methods=["POST"])
def run_test_case(test_case: str) -> dict[str, Any]:
	require_role("IONE QC Administrator", "IONE QC Reviewer", "IONE Medical Affairs")
	return run_rule_test_case(test_case)


@frappe.whitelist(methods=["POST"])
def run_version_tests(rule_version: str) -> dict[str, Any]:
	require_role("IONE QC Administrator", "IONE QC Reviewer", "IONE Medical Affairs")
	return run_rule_version_tests(rule_version)


@frappe.whitelist(methods=["POST"])
def start_validation_run(
	rule_version: str,
	run_type: str,
	window_start: str,
	window_end: str,
	sample_limit: int = 1_000,
) -> dict[str, str]:
	require_role("IONE QC Reviewer", "IONE Medical Affairs")
	return queue_rule_validation(
		rule_version,
		run_type,
		window_start,
		window_end,
		sample_limit,
	)

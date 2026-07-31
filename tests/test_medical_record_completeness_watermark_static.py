from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLINICAL_DOCTYPES = ROOT / "ione_qms" / "ione_clinical_quality" / "doctype"


def _doctype(scrubbed_name: str) -> dict:
	path = CLINICAL_DOCTYPES / scrubbed_name / f"{scrubbed_name}.json"
	return json.loads(path.read_text(encoding="utf-8"))


def _field_map(payload: dict) -> dict[str, dict]:
	return {field["fieldname"]: field for field in payload["fields"]}


def test_completeness_watermark_is_service_managed_and_read_only() -> None:
	payload = _doctype("ione_medical_record_completeness_watermark")
	fields = _field_map(payload)

	assert payload["name"] == "IONE Medical Record Completeness Watermark"
	assert payload["autoname"] == "field:watermark_key"
	assert payload["allow_import"] == 0
	assert set(fields) == {
		"watermark_key",
		"source_watermark_id",
		"endpoint",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"complete_through",
		"period_start",
		"period_end",
		"eligible_record_status",
		"population_date_field",
		"source_sequence",
		"source_record_count",
		"source_manifest_hash",
		"request_hash",
		"watermark_checksum",
		"source_asserted_at",
		"received_at",
	}
	assert all(field.get("read_only") == 1 for field in fields.values())
	assert fields["watermark_key"]["unique"] == 1
	assert fields["watermark_key"]["length"] == 64
	assert fields["hospital"]["reqd"] == 1
	assert fields["source_sequence"]["search_index"] == 1
	assert fields["eligible_record_status"]["options"] == "Final"
	assert fields["population_date_field"]["options"] == "source_finalized_at"
	for digest in ("source_manifest_hash", "request_hash", "watermark_checksum"):
		assert fields[digest]["length"] == 64

	permissions = payload["permissions"]
	assert {permission["role"] for permission in permissions} == {
		"IONE Integration Administrator",
		"IONE Integration Operator",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Auditor",
	}
	for permission in permissions:
		assert permission["read"] == 1
		assert all(permission[action] == 0 for action in ("create", "delete", "write"))


def test_completeness_and_envelope_links_are_frozen_in_governance_records() -> None:
	policy = _field_map(_doctype("ione_medical_record_sampling_policy"))
	batch = _field_map(_doctype("ione_medical_record_review_batch"))
	assignment = _field_map(_doctype("ione_medical_record_review_assignment"))
	acknowledgement = _field_map(_doctype("ione_medical_record_archive_acknowledgement"))
	delivery = _field_map(_doctype("ione_medical_record_archive_delivery"))

	assert policy["source_system"]["reqd"] == 1
	assert policy["source_system"]["options"] == "IONE Source System"
	assert batch["batch_kind"]["options"].splitlines() == ["Initial", "Supplemental"]
	for fieldname in (
		"batch_kind",
		"parent_batch",
		"supplement_sequence",
		"source_system",
		"source_completeness_watermark",
		"source_complete_through",
		"source_completeness_hash",
	):
		assert batch[fieldname]["read_only"] == 1
	assert batch["source_completeness_watermark"]["options"] == ("IONE Medical Record Completeness Watermark")
	assert batch["source_completeness_hash"]["length"] == 64

	for fieldname in ("pending_archive_delivery", "pending_archive_envelope_hash"):
		assert assignment[fieldname]["read_only"] == 1
	assert assignment["pending_archive_envelope_hash"]["length"] == 64

	assert acknowledgement["delivery"]["reqd"] == 1
	assert acknowledgement["delivery"]["options"] == "IONE Medical Record Archive Delivery"
	assert acknowledgement["envelope_hash"]["reqd"] == 1
	assert acknowledgement["envelope_hash"]["length"] == 64
	assert delivery["envelope_count"]["reqd"] == 1
	assert delivery["envelope_manifest_json"]["reqd"] == 1
	assert delivery["envelope_manifest_json"]["options"] == "JSON"


def test_completeness_watermark_governance_hooks_are_registered() -> None:
	hooks = (ROOT / "ione_qms" / "hooks.py").read_text(encoding="utf-8")
	permissions = (ROOT / "ione_qms" / "permissions.py").read_text(encoding="utf-8")
	scope = (ROOT / "ione_qms" / "services" / "scope_hierarchy.py").read_text(encoding="utf-8")
	data_export = (ROOT / "ione_qms" / "services" / "data_export.py").read_text(encoding="utf-8")

	assert "medical_record_completeness_watermark_query" in hooks
	assert "medical_record_completeness_watermark_permission" in hooks
	assert "validate_medical_record_completeness_watermark" in hooks
	assert "def medical_record_completeness_watermark_query" in permissions
	assert "def medical_record_completeness_watermark_permission" in permissions
	assert "IONE Medical Record Completeness Watermark" in scope
	assert data_export.count('"IONE Medical Record Completeness Watermark"') == 3

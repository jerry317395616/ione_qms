"""Release-controlled patient-identity and sensitive-content registry.

This module deliberately has no Frappe imports so build-time generators,
validators, request guards, and runtime disclosure services all consume the
same immutable contract.
"""

from __future__ import annotations

PHI_DISCLOSURE_FIELDS_BY_DOCTYPE: dict[str, frozenset[str]] = {
	"IONE Patient Index": frozenset(
		{
			"date_of_birth",
			"identification_hash",
			"patient_name",
			"phone_hash",
			"source_patient_id",
			"stable_source_key",
		}
	),
	"IONE Encounter Index": frozenset({"encounter_no", "source_encounter_id", "stable_source_key"}),
}

# Operational records may contain source identifiers or unstructured clinical
# payloads. They are never available through generic resource, report, export,
# print, notification, or saved-query surfaces.
OPERATIONAL_SENSITIVE_FIELDS_BY_DOCTYPE: dict[str, frozenset[str]] = {
	"IONE Clinical Quality Event": frozenset(
		{
			"encounter_reference",
			"patient_reference",
			"payload_json",
			"source_record_id",
		}
	),
	"IONE Data Quality Issue": frozenset(
		{
			"actual_value",
			"expected_value",
			"source_record_id",
		}
	),
	"IONE Integration Message": frozenset({"payload_json", "source_record_id"}),
	"IONE Batch Work Item": frozenset({"payload_json"}),
	"IONE AI Data Access Log": frozenset({"snapshot_json"}),
	"IONE AI Analysis Task": frozenset({"input_summary"}),
	"IONE QC Execution": frozenset({"evidence_json"}),
	"IONE QC Finding Evidence": frozenset(
		{
			"actual_json",
			"evidence_json",
			"evidence_text",
			"expected_json",
			"source_record_id",
			"source_reference",
		}
	),
}

PROTECTED_FIELDS_BY_DOCTYPE: dict[str, frozenset[str]] = {
	**PHI_DISCLOSURE_FIELDS_BY_DOCTYPE,
	**OPERATIONAL_SENSITIVE_FIELDS_BY_DOCTYPE,
}
IDENTITY_BEARING_DOCTYPES = frozenset(PROTECTED_FIELDS_BY_DOCTYPE)

DIRECT_IDENTITY_FIELD_NAMES = frozenset(
	{
		"date_of_birth",
		"encounter_no",
		"encounter_reference",
		"identification_hash",
		"patient_name",
		"patient_reference",
		"phone_hash",
		"source_encounter_id",
		"source_patient_id",
		"source_record_id",
	}
)

UNSTRUCTURED_SENSITIVE_FIELD_NAMES = frozenset(
	{
		"actual_json",
		"actual_value",
		"evidence_json",
		"evidence_text",
		"expected_json",
		"expected_value",
		"input_payload",
		"medical_record_text",
		"output_payload",
		"payload_json",
		"source_reference",
	}
)

GENERICALLY_PROTECTED_FIELD_NAMES = DIRECT_IDENTITY_FIELD_NAMES | UNSTRUCTURED_SENSITIVE_FIELD_NAMES

# Administrator bypasses ordinary DocPerm and permission-query conditions in
# Frappe. Generic access to these side-car records can reveal an IONE parent,
# attachment, mail body, assignment description, historical diff, or prepared
# output and is therefore denied at the HTTP request boundary.
PHI_SIDECAR_DOCTYPES = frozenset(
	{
		"Access Log",
		"Activity Log",
		"Assignment Rule",
		"Auto Email Report",
		"Comment",
		"Communication",
		"Communication Link",
		"Error Log",
		"File",
		"Prepared Report",
		"Route History",
		"ToDo",
		"Version",
		"View Log",
		"Webhook Request Log",
	}
)

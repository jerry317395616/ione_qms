"""Dependency-free constants shared by production governance and release tools."""

MANIFEST_FORMAT = "ione-production-evidence-manifest-v1"
RELEASE_MANIFEST_FORMAT = "ione-release-manifest-v1"
EVIDENCE_REGISTER_FORMAT = "ione-production-evidence-register-v1"
MAX_PRODUCTION_APPROVAL_DAYS = 90

REQUIRED_GATE_CODES = (
	"release_integrity",
	"hospital_scope",
	"source_contracts",
	"clinical_content",
	"data_reconciliation",
	"ai_governance",
	"identity_access",
	"security_privacy",
	"performance_capacity",
	"high_availability",
	"backup_restore",
	"clinical_uat",
	"change_approval",
	"oncall_observability",
)

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import frappe
from frappe.utils import getdate, now_datetime
from frappe.utils.file_manager import save_file

from ione_qms.ai.release import (
	build_release_configuration,
	canonical_json,
	current_app_commit_sha,
)
from ione_qms.indicator_engine import (
	calculator_code_hash,
	freeze_indicator_publication_contract,
	get_indicator,
	governed_query_contract_hash,
	normalize_dimension_names,
	physical_query_contract_hash,
	resolved_physical_query_contract,
	validate_formula_schema,
	verify_indicator_publication_contract,
)
from ione_qms.integration.mapping import mapping_definition
from ione_qms.rule_engine.evaluator import validate_rule_definition_schema
from ione_qms.rule_engine.registry import get_rule
from ione_qms.services.indicators import validate_indicator_effective_boundaries
from ione_qms.services.standard_archive import (
	StandardArchiveError,
	StandardArchivePayload,
	download_standard_authority,
	validate_standard_archive_content,
)

DEFINITION_AUTHOR_ROLES = frozenset(
	{
		"IONE QC Administrator",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)
CLINICAL_DEFINITION_REVIEW_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)
AGENT_RELEASE_REVIEW_ROLES = frozenset(
	{
		"IONE Agent Reviewer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)
STANDARD_VERSION_DIFF_ROLES = frozenset(
	{
		"IONE QC Administrator",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE QMS Auditor",
	}
)
STANDARD_VERSION_DIFF_DOCTYPES = (
	"IONE QC Standard Version",
	"IONE QC Standard Clause",
	"IONE QC Rule",
	"IONE QC Rule Version",
)
STANDARD_VERSION_DIFF_FIELDS = (
	"effective_from",
	"effective_to",
	"scope_json",
)
STANDARD_CLAUSE_DIFF_FIELDS = (
	"heading",
	"content",
	"keywords",
)
STANDARD_DIFF_MAX_CLAUSES_PER_VERSION = 5_000
STANDARD_DIFF_MAX_RULE_RELATIONS = 5_000
STANDARD_DIFF_MAX_AFFECTED_RULES = 2_500
STANDARD_DIFF_MAX_PAGE_LENGTH = 25
STANDARD_DIFF_MAX_PAGE_START = 10_000
STANDARD_DIFF_MAX_INPUT_CHARS = 140
STANDARD_DIFF_MAX_CLAUSE_CODE_CHARS = 256
STANDARD_DIFF_MAX_CLAUSE_TEXT_CHARS = {
	"heading": 2_000,
	"content": 200_000,
	"keywords": 4_000,
}
STANDARD_DIFF_MAX_SCOPE_CHARS = 20_000
STANDARD_DIFF_TEXT_PREVIEW_CHARS = 400
STANDARD_DIFF_MAX_RULE_TEXT_CHARS = 512
STANDARD_DIFF_MAX_RULE_EVIDENCE_OUTPUT = 20

RULE_SNAPSHOT_FIELD_MAP = {
	"rule_code_snapshot": "rule_code",
	"rule_name_snapshot": "rule_name",
	"rule_type_snapshot": "rule_type",
	"standard_snapshot": "standard",
	"standard_clause_snapshot": "standard_clause",
	"category_snapshot": "category",
	"risk_level_snapshot": "risk_level",
	"owner_department_snapshot": "owner_department",
	"description_snapshot": "description",
}
RULE_VERSION_SNAPSHOT_FIELDS = tuple(RULE_SNAPSHOT_FIELD_MAP)
RULE_PARENT_GOVERNED_FIELDS = frozenset(RULE_SNAPSHOT_FIELD_MAP.values())
REQUIRED_RULE_SNAPSHOT_FIELDS = frozenset(
	{
		"rule_code_snapshot",
		"rule_name_snapshot",
		"rule_type_snapshot",
		"standard_snapshot",
		"standard_clause_snapshot",
		"risk_level_snapshot",
	}
)
RULE_AUTHORITY_SNAPSHOT_FIELDS = (
	"standard_version_snapshot",
	"standard_version_checksum_snapshot",
	"standard_clause_hash_snapshot",
	"authority_snapshot_json",
	"authority_snapshot_hash",
	"lineage_status",
)
STANDARD_SNAPSHOT_FIELD_MAP = {
	"standard_code_snapshot": "standard_code",
	"standard_name_snapshot": "standard_name",
	"standard_category_snapshot": "standard_category",
	"source_type_snapshot": "source_type",
	"responsible_department_snapshot": "responsible_department",
	"issue_date_snapshot": "issue_date",
	"source_url_snapshot": "source_url",
	"source_file_snapshot": "source_file",
	"description_snapshot": "description",
}
STANDARD_VERSION_SNAPSHOT_FIELDS = (
	*STANDARD_SNAPSHOT_FIELD_MAP,
	"source_file_hash",
	"lineage_status",
)
STANDARD_PARENT_GOVERNED_FIELDS = frozenset(
	{
		*STANDARD_SNAPSHOT_FIELD_MAP.values(),
		"status",
	}
)
INDICATOR_SNAPSHOT_FIELD_MAP = {
	"indicator_code_snapshot": "indicator_code",
	"indicator_name_snapshot": "indicator_name",
	"category_snapshot": "category",
	"definition_snapshot": "definition",
	"parent_calculator_key_snapshot": "calculator_key",
	"parent_formula_json_snapshot": "formula_json",
	"standard_snapshot": "standard",
	"standard_clause_snapshot": "standard_clause",
}
INDICATOR_AUTHORITY_SNAPSHOT_FIELDS = (
	"standard_version_snapshot",
	"standard_version_checksum_snapshot",
	"standard_clause_hash_snapshot",
	"authority_snapshot_json",
	"authority_snapshot_hash",
	"lineage_status",
)
INDICATOR_PARENT_GOVERNED_FIELDS = frozenset(
	{
		*INDICATOR_SNAPSHOT_FIELD_MAP.values(),
		"target_value",
		"target_min",
		"target_max",
		"warning_threshold",
		"critical_threshold",
		"direction",
	}
)
DEFINITION_LINEAGE_VERSION_BINDINGS = {
	"IONE QC Standard Version": "approval_status",
	"IONE QC Rule Version": "status",
	"IONE QC Indicator Version": "status",
}
DEFINITION_LINEAGE_ACTIVE_STATES = {
	"IONE QC Standard Version": frozenset({"Under Review", "Approved"}),
	"IONE QC Rule Version": frozenset(
		{"Expert Review", "Test", "Historical Replay", "Shadow Run", "Approval", "Published"}
	),
	"IONE QC Indicator Version": frozenset({"Under Review", "Published"}),
}
MAX_SOURCE_STANDARD_FILE_BYTES = 50 * 1024 * 1024
MAX_DEFINITION_LINEAGE_BATCH = 1_000
STANDARD_CLAUSE_CONTENT_FIELDS = (
	"clause_key",
	"standard_version",
	"clause_code",
	"heading",
	"content",
	"keywords",
)
STANDARD_CLAUSE_EDITABLE_PARENT_STATES = frozenset({"Draft", "Under Review"})

STANDARD_VERSION_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Under Review"}),
	"Under Review": frozenset({"Draft", "Approved"}),
	"Approved": frozenset({"Retired"}),
	"Retired": frozenset(),
}

RULE_VERSION_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Expert Review"}),
	"Expert Review": frozenset({"Test"}),
	"Test": frozenset({"Historical Replay"}),
	"Historical Replay": frozenset({"Shadow Run"}),
	"Shadow Run": frozenset({"Approval"}),
	"Approval": frozenset({"Published"}),
	"Published": frozenset({"Retired"}),
	"Retired": frozenset(),
}

INDICATOR_VERSION_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Under Review"}),
	"Under Review": frozenset({"Draft", "Published"}),
	"Published": frozenset({"Retired"}),
	"Retired": frozenset(),
}

AGENT_RELEASE_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Approved"}),
	"Approved": frozenset({"Retired"}),
	"Retired": frozenset(),
}

STANDARD_VERSION_TRANSITION_ROLES = {
	("Draft", "Under Review"): DEFINITION_AUTHOR_ROLES,
	("Under Review", "Draft"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Under Review", "Approved"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Approved", "Retired"): CLINICAL_DEFINITION_REVIEW_ROLES,
}

RULE_VERSION_TRANSITION_ROLES = {
	("Draft", "Expert Review"): DEFINITION_AUTHOR_ROLES,
	("Expert Review", "Test"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Test", "Historical Replay"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Historical Replay", "Shadow Run"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Shadow Run", "Approval"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Approval", "Published"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Published", "Retired"): CLINICAL_DEFINITION_REVIEW_ROLES,
}

INDICATOR_VERSION_TRANSITION_ROLES = {
	("Draft", "Under Review"): DEFINITION_AUTHOR_ROLES,
	("Under Review", "Draft"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Under Review", "Published"): CLINICAL_DEFINITION_REVIEW_ROLES,
	("Published", "Retired"): CLINICAL_DEFINITION_REVIEW_ROLES,
}

AGENT_RELEASE_TRANSITION_ROLES = {
	("Draft", "Approved"): AGENT_RELEASE_REVIEW_ROLES,
	("Approved", "Retired"): AGENT_RELEASE_REVIEW_ROLES,
}

STANDARD_VERSION_SELF_APPROVAL_EDGES = frozenset({("Draft", "Under Review")})
RULE_VERSION_SELF_APPROVAL_EDGES = frozenset({("Draft", "Expert Review")})
INDICATOR_VERSION_SELF_APPROVAL_EDGES = frozenset({("Draft", "Under Review")})
AGENT_RELEASE_SELF_APPROVAL_EDGES: frozenset[tuple[str, str]] = frozenset()

AGENT_RELEASE_EDIT_ROLE_ADDITIONS = {
	"Draft": frozenset({"IONE Agent Administrator"}),
}


def _build_state_edit_roles(
	role_map: dict[tuple[str, str], frozenset[str]],
	additions: dict[str, frozenset[str]] | None = None,
) -> dict[str, frozenset[str]]:
	state_roles: dict[str, set[str]] = {}
	for (state, _next_state), roles in role_map.items():
		state_roles.setdefault(state, set()).update(roles)
	for state, roles in (additions or {}).items():
		state_roles.setdefault(state, set()).update(roles)
	return {state: frozenset(roles) for state, roles in state_roles.items()}


STANDARD_VERSION_STATE_EDIT_ROLES = _build_state_edit_roles(STANDARD_VERSION_TRANSITION_ROLES)
RULE_VERSION_STATE_EDIT_ROLES = _build_state_edit_roles(RULE_VERSION_TRANSITION_ROLES)
INDICATOR_VERSION_STATE_EDIT_ROLES = _build_state_edit_roles(INDICATOR_VERSION_TRANSITION_ROLES)
AGENT_RELEASE_STATE_EDIT_ROLES = _build_state_edit_roles(
	AGENT_RELEASE_TRANSITION_ROLES,
	AGENT_RELEASE_EDIT_ROLE_ADDITIONS,
)

GOVERNED_LINEAGE_LINKS: dict[str, dict[str, str]] = {
	"IONE QC Standard Version": {
		"standard": "IONE QC Standard",
	},
	"IONE QC Rule Version": {
		"rule": "IONE QC Rule",
	},
	"IONE QC Indicator Version": {
		"indicator": "IONE QC Indicator",
	},
	"IONE Agent Release": {
		"policy": "IONE Agent Policy",
		"flow_agent": "Flow Agent",
		"flow_model": "Flow Model",
	},
}

_LINEAGE_PARENT_LOCK_SQL = {
	"IONE QC Standard": "select name from `tabIONE QC Standard` where name = %s for update",
	"IONE QC Rule": "select name from `tabIONE QC Rule` where name = %s for update",
	"IONE QC Indicator": "select name from `tabIONE QC Indicator` where name = %s for update",
	"IONE Agent Policy": "select name from `tabIONE Agent Policy` where name = %s for update",
	"Flow Agent": "select name from `tabFlow Agent` where name = %s for update",
	"Flow Model": "select name from `tabFlow Model` where name = %s for update",
}

_ACTIVE_VERSION_BINDINGS = (
	(
		"IONE QC Standard Version",
		"standard",
		"approval_status",
		"Approved",
		"IONE QC Standard",
		"Published",
	),
	(
		"IONE QC Rule Version",
		"rule",
		"status",
		"Published",
		"IONE QC Rule",
		"Active",
	),
	(
		"IONE QC Indicator Version",
		"indicator",
		"status",
		"Published",
		"IONE QC Indicator",
		"Active",
	),
)


def _lock_lineage_parent(parent_doctype: str, name: str) -> None:
	"""Lock an allowlisted lineage parent without interpolating runtime identifiers."""
	query = _LINEAGE_PARENT_LOCK_SQL.get(parent_doctype)
	if query is None:
		frappe.throw(f"Unsupported governed lineage parent type: {parent_doctype}")
	frappe.db.sql(query, (name,))


def _lock_and_validate_lineage_links(doc) -> None:
	"""Lock old/new parents deterministically and reject lineage reassignment."""
	field_targets = GOVERNED_LINEAGE_LINKS.get(doc.doctype)
	if field_targets is None:
		frappe.throw(f"Unsupported governed lineage document type: {doc.doctype}")

	previous = doc.get_doc_before_save()
	lock_targets: set[tuple[str, str]] = set()
	for fieldname, parent_doctype in field_targets.items():
		for source in (previous, doc):
			if source is None:
				continue
			parent_name = str(source.get(fieldname) or "")
			if parent_name:
				lock_targets.add((parent_doctype, parent_name))
	for parent_doctype, parent_name in sorted(lock_targets):
		_lock_lineage_parent(parent_doctype, parent_name)

	if previous is None:
		return
	changed = sorted(
		fieldname
		for fieldname in field_targets
		if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
	)
	if changed:
		frappe.throw(
			"Governed lineage links are immutable after insert; create a new version or release. "
			"Changed: " + ", ".join(changed)
		)


def _lock_rule_parent(name: str) -> None:
	_lock_lineage_parent("IONE QC Rule", name)


def _lock_rule_version(name: str) -> None:
	frappe.db.sql(
		"select name from `tabIONE QC Rule Version` where name = %s for update",
		(name,),
	)


def _lock_standard_version(name: str) -> None:
	frappe.db.sql(
		"select name from `tabIONE QC Standard Version` where name = %s for update",
		(name,),
	)


def _lock_standard_clause(name: str) -> None:
	frappe.db.sql(
		"select name from `tabIONE QC Standard Clause` where name = %s for update",
		(name,),
	)


def _lock_indicator_version(name: str) -> None:
	frappe.db.sql(
		"select name from `tabIONE QC Indicator Version` where name = %s for update",
		(name,),
	)


def validate_standard(doc, method: str | None = None) -> None:
	"""Freeze standard identity and provenance as soon as governed review begins."""
	del method
	current_status = str(doc.get("status") or "Draft")
	previous = doc.get_doc_before_save()
	if previous is None:
		if current_status != "Draft":
			frappe.throw("New standards must start in Draft; status is managed by governed versions")
		doc.set("status", "Draft")
		_require_standard_parent_identity(doc)
		return
	_lock_lineage_parent("IONE QC Standard", doc.name)
	previous_status = str(previous.get("status") or "Draft")
	if current_status != previous_status:
		frappe.throw("Standard status is managed by governed standard-version transitions")
	_require_standard_parent_identity(doc)
	changed = {
		fieldname
		for fieldname in STANDARD_PARENT_GOVERNED_FIELDS - {"status"}
		if previous.get(fieldname) != doc.get(fieldname)
	}
	if changed and frappe.db.exists(
		"IONE QC Standard Version",
		{"standard": doc.name, "approval_status": ["not in", ["Draft"]]},
	):
		frappe.throw(
			"Reviewed standard identity and provenance are immutable; create a new standard "
			"version. Changed: " + ", ".join(sorted(changed))
		)


def validate_rule(doc, method: str | None = None) -> None:
	"""Keep the rule parent as an identity record once reviewed versioning starts."""
	del method
	current_status = str(doc.get("status") or "Draft")
	previous = doc.get_doc_before_save()
	if previous is None:
		if current_status != "Draft":
			frappe.throw("New rules must start in Draft; status is managed by governed versions")
		doc.set("status", "Draft")
		_validate_parent_authority_links(doc)
		return
	_lock_rule_parent(doc.name)

	previous_status = str(previous.get("status") or "Draft")
	if current_status != previous_status:
		frappe.throw("Rule status is managed by governed rule-version transitions")
	_validate_parent_authority_links(doc)
	changed = {
		fieldname
		for fieldname in RULE_PARENT_GOVERNED_FIELDS
		if previous.get(fieldname) != doc.get(fieldname)
	}
	if not changed:
		return
	if frappe.db.exists(
		"IONE QC Rule Version",
		{
			"rule": doc.name,
			"status": ["not in", ["Draft"]],
		},
	):
		frappe.throw(
			"Reviewed rule identity is immutable; create a new rule version. Changed: "
			+ ", ".join(sorted(changed))
		)


def validate_indicator(doc, method: str | None = None) -> None:
	"""Freeze indicator identity, authority, formula, and targets after review starts."""
	del method
	if doc.get("formula_json") not in (None, ""):
		doc.set(
			"formula_json",
			_canonical_json_value(doc.get("formula_json"), "indicator parent formula_json"),
		)
	current_status = str(doc.get("status") or "Draft")
	previous = doc.get_doc_before_save()
	if previous is None:
		if current_status != "Draft":
			frappe.throw("New indicators must start in Draft; status is managed by governed versions")
		doc.set("status", "Draft")
		_validate_parent_authority_links(doc)
		return
	_lock_lineage_parent("IONE QC Indicator", doc.name)
	previous_status = str(previous.get("status") or "Draft")
	if current_status != previous_status:
		frappe.throw("Indicator status is managed by governed indicator-version transitions")
	_validate_parent_authority_links(doc)
	changed = {
		fieldname
		for fieldname in INDICATOR_PARENT_GOVERNED_FIELDS
		if previous.get(fieldname) != doc.get(fieldname)
	}
	if changed and frappe.db.exists(
		"IONE QC Indicator Version",
		{"indicator": doc.name, "status": ["not in", ["Draft"]]},
	):
		frappe.throw(
			"Reviewed indicator identity, authority, formula, and targets are immutable; "
			"create a new indicator version. Changed: " + ", ".join(sorted(changed))
		)


def prevent_definition_parent_deletion(doc, method: str | None = None) -> None:
	del method
	version_binding = {
		"IONE QC Standard": ("IONE QC Standard Version", "standard"),
		"IONE QC Rule": ("IONE QC Rule Version", "rule"),
		"IONE QC Indicator": ("IONE QC Indicator Version", "indicator"),
	}.get(doc.doctype)
	if version_binding is None:
		frappe.throw(f"Unsupported governed definition parent: {doc.doctype}")
	_lock_lineage_parent(doc.doctype, doc.name)
	version_doctype, parent_field = version_binding
	if frappe.db.exists(version_doctype, {parent_field: doc.name}):
		frappe.throw("A governed definition parent with retained versions cannot be deleted")


def validate_standard_clause(doc, method: str | None = None) -> None:
	"""Derive clause publication from its parent version and freeze approved text."""
	del method
	version_name = str(doc.get("standard_version") or "")
	if not version_name:
		return
	previous = doc.get_doc_before_save()
	previous_version_name = str(previous.get("standard_version") or "") if previous else ""
	for locked_version in sorted({version_name, previous_version_name} - {""}):
		_lock_standard_version(locked_version)
	parent_status = str(
		frappe.db.get_value(
			"IONE QC Standard Version",
			version_name,
			"approval_status",
		)
		or "Draft"
	)
	target_status = _standard_clause_status(parent_status)
	if previous is None:
		if parent_status not in STANDARD_CLAUSE_EDITABLE_PARENT_STATES:
			frappe.throw("Clauses can only be created for Draft or Under Review standard versions")
	else:
		changed = {
			fieldname
			for fieldname in STANDARD_CLAUSE_CONTENT_FIELDS
			if previous.get(fieldname) != doc.get(fieldname)
		}
		if changed:
			previous_parent_status = (
				parent_status
				if previous_version_name == version_name
				else str(
					frappe.db.get_value(
						"IONE QC Standard Version",
						previous_version_name,
						"approval_status",
					)
					or "Draft"
				)
			)
			if (
				parent_status not in STANDARD_CLAUSE_EDITABLE_PARENT_STATES
				or previous_parent_status not in STANDARD_CLAUSE_EDITABLE_PARENT_STATES
			):
				frappe.throw(
					"Approved or retired standard clauses are immutable; create a new standard version"
				)

	requested_status = str(doc.get("status") or target_status)
	if requested_status != target_status:
		frappe.throw("Clause status is derived from the governed standard-version lifecycle")
	doc.set("status", target_status)


def prevent_standard_clause_deletion(doc, method: str | None = None) -> None:
	del method
	version_name = str(doc.get("standard_version") or "")
	if not version_name:
		return
	_lock_standard_version(version_name)
	parent_status = str(
		frappe.db.get_value(
			"IONE QC Standard Version",
			version_name,
			"approval_status",
		)
		or "Draft"
	)
	if parent_status not in STANDARD_CLAUSE_EDITABLE_PARENT_STATES:
		frappe.throw("Approved or retired standard clauses cannot be deleted")


def prevent_definition_version_deletion(doc, method: str | None = None) -> None:
	del method
	binding = {
		"IONE QC Standard Version": ("approval_status", _lock_standard_version),
		"IONE QC Rule Version": ("status", _lock_rule_version),
		"IONE QC Indicator Version": ("status", _lock_indicator_version),
	}.get(doc.doctype)
	if binding is None:
		frappe.throw(f"Unsupported governed definition version: {doc.doctype}")
	status_field, lock = binding
	lock(doc.name)
	stored = frappe.db.get_value(
		doc.doctype,
		doc.name,
		[status_field, "lineage_status"],
		as_dict=True,
	)
	if not stored:
		return
	if (
		str(stored.get(status_field) or "Draft") != "Draft"
		or str(stored.get("lineage_status") or "") == "Quarantined"
	):
		frappe.throw(
			"Reviewed or quarantined governed definition versions cannot be deleted; retain and retire them"
		)


def validate_standard_version(doc, method: str | None = None) -> None:
	previous = doc.get_doc_before_save()
	if previous is not None:
		_lock_standard_version(doc.name)
	_lock_and_validate_lineage_links(doc)
	if _retire_quarantined_version(
		doc,
		parent_field="standard",
		status_field="approval_status",
	):
		return
	transition = _validate_governed_transition(
		doc,
		status_field="approval_status",
		transitions=STANDARD_VERSION_TRANSITIONS,
	)
	_require_transition_actor(
		doc,
		transition,
		STANDARD_VERSION_TRANSITION_ROLES,
		STANDARD_VERSION_SELF_APPROVAL_EDGES,
	)
	_require_same_state_editor(
		doc,
		status_field="approval_status",
		transition=transition,
		state_role_map=STANDARD_VERSION_STATE_EDIT_ROLES,
	)
	quarantined_retirement = _require_verified_or_quarantined_retirement(
		doc,
		transition,
		status_field="approval_status",
	)
	if quarantined_retirement:
		_finalize_quarantined_retirement(
			doc,
			parent_field="standard",
			status_field="approval_status",
		)
		return
	standard = frappe.get_doc("IONE QC Standard", doc.standard) if doc.get("standard") else None
	_materialize_standard_snapshot(doc, standard)
	_validate_dates(doc)
	if transition == ("Approved", "Retired") and not doc.get("effective_to"):
		frappe.throw("Retired standard versions require an explicit effective_to")
	_protect_published_content(
		doc,
		status_field="approval_status",
		content_fields=(
			"standard",
			"version",
			"effective_from",
			"effective_to",
			"scope_json",
			*STANDARD_VERSION_SNAPSHOT_FIELDS,
		),
		retirement_mutable_fields=frozenset({"effective_to"}),
	)
	_validate_json_field(doc, "scope_json", expected=(dict, list), required=False)
	_set_active_parent_key(
		doc,
		parent_field="standard",
		status_field="approval_status",
		active_status="Approved",
	)
	_require_single_active_version(
		doc,
		parent_field="standard",
		status_field="approval_status",
		active_status="Approved",
	)
	if doc.get("approval_status") in {"Approved", "Retired"}:
		_require_no_effective_overlap(
			doc,
			parent_field="standard",
			status_field="approval_status",
			governed_statuses=("Approved", "Retired"),
		)
	version_content = _standard_version_content(doc)
	if doc.get("approval_status") == "Approved" and not version_content["standard_clauses"]:
		frappe.throw("Approved standard versions require at least one governed clause")
	if transition == ("Approved", "Retired"):
		_require_no_active_authority_dependents(doc)
		_require_retirement_contains_authority_dependents(doc)
	_set_approval_metadata(doc, transition, approved_state="Approved")
	_set_checksum(doc, version_content)


def validate_rule_version(doc, method: str | None = None) -> None:
	previous = doc.get_doc_before_save()
	if previous is not None:
		_lock_rule_version(doc.name)
	_lock_and_validate_lineage_links(doc)
	if _retire_quarantined_version(
		doc,
		parent_field="rule",
		status_field="status",
	):
		return
	transition = _validate_governed_transition(
		doc,
		status_field="status",
		transitions=RULE_VERSION_TRANSITIONS,
	)
	quarantined_retirement = _require_verified_or_quarantined_retirement(
		doc,
		transition,
		status_field="status",
	)
	rule = frappe.get_doc("IONE QC Rule", doc.rule) if doc.get("rule") else None
	if not quarantined_retirement:
		_materialize_rule_snapshot(doc, rule)
		_materialize_rule_authority_snapshot(doc, rule)
	_require_transition_actor(
		doc,
		transition,
		RULE_VERSION_TRANSITION_ROLES,
		RULE_VERSION_SELF_APPROVAL_EDGES,
	)
	_require_same_state_editor(
		doc,
		status_field="status",
		transition=transition,
		state_role_map=RULE_VERSION_STATE_EDIT_ROLES,
	)
	if quarantined_retirement:
		_finalize_quarantined_retirement(
			doc,
			parent_field="rule",
			status_field="status",
			require_effective_to=True,
		)
		return
	_validate_dates(doc)
	_validate_shadow_version_contract(doc, previous)
	if doc.get("status") == "Published" and not doc.get("effective_from"):
		frappe.throw("Published rule versions require effective_from")
	if transition == ("Published", "Retired") and not doc.get("effective_to"):
		frappe.throw("Retired rule versions require an explicit effective_to")
	condition = _validate_json_field(
		doc,
		"condition_json",
		expected=(dict, list),
		required=True,
	)
	try:
		validate_rule_definition_schema(condition)
	except ValueError as exc:
		raise frappe.ValidationError(f"Invalid condition_json: {exc}") from exc
	exclusions = _validate_json_field(
		doc,
		"exclusion_json",
		expected=(dict, list),
		required=False,
	)
	if exclusions:
		try:
			validate_rule_definition_schema(exclusions)
		except ValueError as exc:
			raise frappe.ValidationError(f"Invalid exclusion_json: {exc}") from exc
	_protect_published_content(
		doc,
		status_field="status",
		content_fields=(
			"rule",
			"version",
			"trigger_event",
			"condition_json",
			"exclusion_json",
			"action",
			"severity",
			"effective_from",
			"effective_to",
			"plugin_key",
			*RULE_VERSION_SNAPSHOT_FIELDS,
			*RULE_AUTHORITY_SNAPSHOT_FIELDS,
			"shadow_observation_start",
			"shadow_observation_end",
			"shadow_min_events",
			"shadow_min_feedback",
			"shadow_max_false_positive_rate",
			"shadow_max_false_negative_rate",
			"shadow_min_feedback_coverage_rate",
			"shadow_min_passed_feedback",
			"shadow_min_excluded_feedback",
		),
		retirement_mutable_fields=frozenset({"effective_to"}),
	)
	if doc.get("rule_type_snapshot") == "Python Plugin":
		plugin_key = str(doc.get("plugin_key") or "")
		if not plugin_key:
			frappe.throw("Python rule versions require a pinned plugin_key")
		rule_class = get_rule(plugin_key)
		if not rule_class:
			frappe.throw(f"Python rule plug-in '{plugin_key}' is not registered")
		if doc.get("status") in {
			"Historical Replay",
			"Shadow Run",
			"Approval",
			"Published",
		}:
			frappe.throw(
				"Historical Replay, Shadow Run, Approval, and Published rules must use "
				"the built-in deterministic expression DSL; Python plug-ins fail closed"
			)
	if not quarantined_retirement:
		_set_checksum(doc, _version_content(doc))
	if not quarantined_retirement and (
		transition == ("Test", "Historical Replay") or doc.get("status") == "Published"
	):
		_require_passing_rule_tests(doc)
	from ione_qms.rule_engine.validation import require_validation_artifact

	if not quarantined_retirement and (
		transition == ("Historical Replay", "Shadow Run") or doc.get("status") == "Published"
	):
		require_validation_artifact(doc, "Historical Replay")
	if not quarantined_retirement and (
		transition == ("Shadow Run", "Approval") or doc.get("status") == "Published"
	):
		require_validation_artifact(doc, "Shadow Run")
	if doc.get("status") == "Published" and int(doc.get("shadow_mode") or 0):
		frappe.throw("Published rule versions must leave shadow mode")
	_set_active_parent_key(
		doc,
		parent_field="rule",
		status_field="status",
		active_status="Published",
	)
	_require_single_active_version(
		doc,
		parent_field="rule",
		status_field="status",
		active_status="Published",
	)
	if doc.get("status") == "Published":
		_require_no_effective_overlap(
			doc,
			parent_field="rule",
			status_field="status",
			governed_statuses=("Published", "Retired"),
		)
	_set_approval_metadata(doc, transition, approved_state="Published")


def validate_indicator_version(doc, method: str | None = None) -> None:
	previous = doc.get_doc_before_save()
	if previous is not None:
		_lock_indicator_version(doc.name)
	_lock_and_validate_lineage_links(doc)
	if _retire_quarantined_version(
		doc,
		parent_field="indicator",
		status_field="status",
	):
		return
	transition = _validate_governed_transition(
		doc,
		status_field="status",
		transitions=INDICATOR_VERSION_TRANSITIONS,
	)
	quarantined_retirement = _require_verified_or_quarantined_retirement(
		doc,
		transition,
		status_field="status",
	)
	indicator = frappe.get_doc("IONE QC Indicator", doc.indicator) if doc.get("indicator") else None
	if not quarantined_retirement:
		_materialize_indicator_snapshot(doc, indicator)
	_require_transition_actor(
		doc,
		transition,
		INDICATOR_VERSION_TRANSITION_ROLES,
		INDICATOR_VERSION_SELF_APPROVAL_EDGES,
	)
	_require_same_state_editor(
		doc,
		status_field="status",
		transition=transition,
		state_role_map=INDICATOR_VERSION_STATE_EDIT_ROLES,
	)
	if quarantined_retirement:
		_finalize_quarantined_retirement(
			doc,
			parent_field="indicator",
			status_field="status",
			require_effective_to=True,
		)
		return
	_validate_dates(doc)
	try:
		validate_indicator_effective_boundaries(doc)
	except ValueError as exc:
		frappe.throw(str(exc))
	formula = _validate_json_field(
		doc,
		"formula_json",
		expected=dict,
		required=False,
	)
	dimensions = _validate_json_field(
		doc,
		"dimensions_json",
		expected=(dict, list),
		required=False,
	)
	try:
		normalized_dimensions = _validate_indicator_dimensions(dimensions)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	if doc.get("multiplier") is not None and float(doc.multiplier or 0) < 0:
		frappe.throw("Indicator multiplier cannot be negative")
	if doc.get("direction") and doc.direction not in {
		"Higher is Better",
		"Lower is Better",
		"Target Range",
	}:
		frappe.throw("Invalid indicator direction")
	_materialize_indicator_defaults(doc)
	_validate_indicator_targets(doc)
	if doc.get("status") == "Published" and not quarantined_retirement:
		_materialize_indicator_execution_contract(
			doc,
			formula=formula,
			dimensions=normalized_dimensions,
		)
		if transition == ("Under Review", "Published"):
			publication_json, publication_checksum = freeze_indicator_publication_contract(doc)
			doc.publication_contract_json = publication_json
			doc.publication_checksum = publication_checksum
			doc.checksum = publication_checksum
		else:
			try:
				verify_indicator_publication_contract(doc)
			except ValueError as exc:
				raise frappe.ValidationError(str(exc)) from exc
	elif doc.get("status") == "Retired" and not quarantined_retirement:
		try:
			verify_indicator_publication_contract(doc)
		except ValueError as exc:
			raise frappe.ValidationError(str(exc)) from exc
	_protect_published_content(
		doc,
		status_field="status",
		content_fields=(
			"indicator",
			"version",
			"calculator_key",
			"numerator_definition",
			"denominator_definition",
			"formula_json",
			"dimensions_json",
			"unit",
			"multiplier",
			"precision",
			"target_value",
			"target_min",
			"target_max",
			"warning_threshold",
			"critical_threshold",
			"direction",
			"calculation_frequency",
			"rolling_window_days",
			"source_mapping",
			"source_system_snapshot",
			"mapping_version_snapshot",
			"mapping_checksum_snapshot",
			"query_contract_hash",
			"physical_query_contract_json",
			"physical_query_contract_hash",
			"schema_signature_hash",
			"calculator_version_snapshot",
			"calculator_code_hash",
			"publication_contract_json",
			"publication_checksum",
			"effective_from",
			"effective_to",
			*INDICATOR_SNAPSHOT_FIELD_MAP,
			*INDICATOR_AUTHORITY_SNAPSHOT_FIELDS,
		),
		retirement_mutable_fields=frozenset({"effective_to"}),
	)
	if not quarantined_retirement and doc.get("status") not in {"Published", "Retired"}:
		_set_checksum(doc, _version_content(doc))
	elif not quarantined_retirement:
		doc.checksum = doc.get("publication_checksum")
	if doc.get("status") == "Published" and not quarantined_retirement:
		calculator_key = str(doc.get("calculator_key") or "")
		if not calculator_key:
			frappe.throw("Published indicator versions require a reviewed calculator_key")
		calculator = get_indicator(calculator_key)
		if not calculator:
			frappe.throw(f"Indicator calculator '{calculator_key}' is not registered")
		if not formula:
			frappe.throw("Published indicator versions require formula_json")
		try:
			validated_formula = validate_formula_schema(
				formula,
				expected_dimensions=list(normalized_dimensions),
			)
			calculator.validate_dimension_capability(validated_formula, normalized_dimensions)
		except ValueError as exc:
			raise frappe.ValidationError(f"Invalid indicator execution contract: {exc}") from exc
	_set_active_parent_key(
		doc,
		parent_field="indicator",
		status_field="status",
		active_status="Published",
	)
	_require_single_active_version(
		doc,
		parent_field="indicator",
		status_field="status",
		active_status="Published",
	)
	if doc.get("status") in {"Published", "Retired"}:
		_require_no_effective_overlap(
			doc,
			parent_field="indicator",
			status_field="status",
			governed_statuses=("Published", "Retired"),
		)
	_set_approval_metadata(doc, transition, approved_state="Published")


def on_standard_version_update(doc, method: str | None = None) -> None:
	del method
	_sync_version_parent_status(
		doc,
		parent_doctype="IONE QC Standard",
		parent_field="standard",
		status_field="approval_status",
		active_version_status="Approved",
		active_parent_status="Published",
	)
	_sync_standard_clause_statuses(doc)


def on_rule_version_update(doc, method: str | None = None) -> None:
	del method
	_sync_version_parent_status(
		doc,
		parent_doctype="IONE QC Rule",
		parent_field="rule",
		status_field="status",
		active_version_status="Published",
		active_parent_status="Active",
	)


def on_indicator_version_update(doc, method: str | None = None) -> None:
	del method
	_sync_version_parent_status(
		doc,
		parent_doctype="IONE QC Indicator",
		parent_field="indicator",
		status_field="status",
		active_version_status="Published",
		active_parent_status="Active",
	)


def audit_indicator_effective_periods(
	batch_size: int = 500,
	cursor: str | None = None,
) -> dict[str, Any]:
	"""Read-only, bounded audit for invalid historical indicator effective periods."""
	limit = min(max(int(batch_size or 500), 1), 1_000)
	filters: dict[str, Any] = {"status": ["in", ["Published", "Retired"]]}
	if cursor:
		filters["name"] = [">", cursor]
	rows = frappe.get_all(
		"IONE QC Indicator Version",
		filters=filters,
		fields=[
			"name",
			"indicator",
			"status",
			"calculation_frequency",
			"rolling_window_days",
			"formula_json",
			"effective_from",
			"effective_to",
		],
		order_by="name asc",
		limit_page_length=limit + 1,
	)
	page = rows[:limit]
	issues: list[dict[str, Any]] = []
	history_by_indicator: dict[str, list[Any]] = {}
	for row in page:
		try:
			validate_indicator_effective_boundaries(row)
		except (TypeError, ValueError) as exc:
			issues.append(
				{
					"version": row.name,
					"indicator": row.indicator,
					"code": "INVALID_EFFECTIVE_BOUNDARY",
					"message": str(exc),
				}
			)
		indicator = str(row.get("indicator") or "")
		if not indicator:
			continue
		if indicator not in history_by_indicator:
			history_by_indicator[indicator] = frappe.get_all(
				"IONE QC Indicator Version",
				filters={
					"indicator": indicator,
					"status": ["in", ["Published", "Retired"]],
				},
				fields=["name", "effective_from", "effective_to"],
				order_by="name asc",
				limit_page_length=1_001,
			)
		history = history_by_indicator[indicator]
		if len(history) > 1_000:
			issues.append(
				{
					"version": row.name,
					"indicator": indicator,
					"code": "HISTORY_AUDIT_LIMIT_EXCEEDED",
					"message": "More than 1,000 governed versions require a dedicated offline audit.",
				}
			)
			continue
		overlaps = [
			other.name
			for other in history
			if other.name != row.name and _effective_intervals_overlap(row, other)
		]
		if overlaps:
			issues.append(
				{
					"version": row.name,
					"indicator": indicator,
					"code": "OVERLAPPING_EFFECTIVE_INTERVAL",
					"related_versions": sorted(overlaps),
					"message": "Governed Published/Retired effective intervals overlap.",
				}
			)
	has_more = len(rows) > limit
	return {
		"scanned": len(page),
		"issue_count": len(issues),
		"issues": issues,
		"has_more": has_more,
		"next_cursor": page[-1].name if has_more and page else None,
	}


def _effective_intervals_overlap(first, second) -> bool:
	first_start = getdate(first.get("effective_from")) if first.get("effective_from") else None
	second_start = getdate(second.get("effective_from")) if second.get("effective_from") else None
	if first_start is None or second_start is None:
		return True
	first_end = getdate(first.get("effective_to")) if first.get("effective_to") else None
	second_end = getdate(second.get("effective_to")) if second.get("effective_to") else None
	first_before_second = first_end is not None and first_end < second_start
	second_before_first = second_end is not None and second_end < first_start
	return not first_before_second and not second_before_first


def compare_standard_versions(
	baseline_version: str,
	target_version: str,
	*,
	change_start: int = 0,
	change_page_length: int = 25,
	rule_start: int = 0,
	rule_page_length: int = 25,
	relation_start: int = 0,
	relation_page_length: int = 25,
	expected_comparison_checksum: str | None = None,
) -> dict[str, Any]:
	"""Compare two governed versions without reading any clinical transaction table.

	The comparison deliberately treats ``clause_code`` as the only cross-version
	clause identity. A code rename is therefore reported as one removal and one
	addition; the service never infers lineage from similar policy text.
	"""
	require_standard_version_diff_access()
	baseline_name = _validated_standard_diff_name(baseline_version, "baseline_version")
	target_name = _validated_standard_diff_name(target_version, "target_version")
	if baseline_name == target_name:
		frappe.throw("Two different standard versions are required")

	change_offset = _bounded_standard_diff_integer(
		change_start,
		"change_start",
		minimum=0,
		maximum=STANDARD_DIFF_MAX_PAGE_START,
	)
	change_limit = _bounded_standard_diff_integer(
		change_page_length,
		"change_page_length",
		minimum=1,
		maximum=STANDARD_DIFF_MAX_PAGE_LENGTH,
	)
	rule_offset = _bounded_standard_diff_integer(
		rule_start,
		"rule_start",
		minimum=0,
		maximum=STANDARD_DIFF_MAX_PAGE_START,
	)
	rule_limit = _bounded_standard_diff_integer(
		rule_page_length,
		"rule_page_length",
		minimum=1,
		maximum=STANDARD_DIFF_MAX_PAGE_LENGTH,
	)
	relation_offset = _bounded_standard_diff_integer(
		relation_start,
		"relation_start",
		minimum=0,
		maximum=STANDARD_DIFF_MAX_PAGE_START,
	)
	relation_limit = _bounded_standard_diff_integer(
		relation_page_length,
		"relation_page_length",
		minimum=1,
		maximum=STANDARD_DIFF_MAX_PAGE_LENGTH,
	)

	versions = _load_standard_diff_versions(baseline_name, target_name)
	baseline = versions[baseline_name]
	target = versions[target_name]
	standard = str(baseline["standard"])
	if not standard or standard != str(target["standard"]):
		frappe.throw("Standard versions must belong to the same governed standard")

	baseline_clauses = _load_standard_diff_clauses(baseline_name)
	target_clauses = _load_standard_diff_clauses(target_name)
	version_field_changes = _build_standard_version_field_changes(baseline, target)
	clause_changes, clause_counts = _build_standard_clause_changes(
		baseline_clauses,
		target_clauses,
	)
	changed_clause_refs = _changed_standard_clause_references(clause_changes)
	rule_relations = _load_affected_standard_rule_relations(
		standard,
		changed_clause_refs,
		tuple(change["fieldname"] for change in version_field_changes),
	)
	affected_rules = _summarize_affected_standard_rules(rule_relations)

	comparison_checksum = _standard_diff_hash(
		{
			"schema": "IONE_STANDARD_VERSION_DIFF_V1",
			"baseline": _standard_diff_version_snapshot(baseline, baseline_clauses),
			"target": _standard_diff_version_snapshot(target, target_clauses),
			"version_field_changes": version_field_changes,
			"clause_changes": clause_changes,
			"affected_rule_relations": rule_relations,
		}
	)
	_verify_expected_standard_diff_checksum(
		expected_comparison_checksum,
		comparison_checksum,
	)

	return {
		"schema": "IONE_STANDARD_VERSION_DIFF_V1",
		"standard": standard,
		"baseline": _public_standard_diff_version(baseline, baseline_clauses),
		"target": _public_standard_diff_version(target, target_clauses),
		"comparison_checksum": comparison_checksum,
		"version_field_changes": version_field_changes,
		"clause_change_counts": clause_counts,
		"clause_changes": _standard_diff_page(
			clause_changes,
			change_offset,
			change_limit,
		),
		"affected_rule_count": len(affected_rules),
		"affected_rules": _standard_diff_page(
			affected_rules,
			rule_offset,
			rule_limit,
		),
		"affected_rule_relation_count": len(rule_relations),
		"affected_rule_relations": _standard_diff_page(
			rule_relations,
			relation_offset,
			relation_limit,
		),
		"limits": {
			"max_clauses_per_version": STANDARD_DIFF_MAX_CLAUSES_PER_VERSION,
			"max_rule_relations": STANDARD_DIFF_MAX_RULE_RELATIONS,
			"max_affected_rules": STANDARD_DIFF_MAX_AFFECTED_RULES,
			"max_page_length": STANDARD_DIFF_MAX_PAGE_LENGTH,
			"text_preview_chars": STANDARD_DIFF_TEXT_PREVIEW_CHARS,
		},
		"clinical_data_read": False,
	}


def require_standard_version_diff_access() -> None:
	"""Fail closed unless a named, accountable definition-governance user reads."""
	user = str(frappe.session.user or "")
	if not user or user in {"Guest", "Administrator"}:
		frappe.throw(
			"Use a named accountable business user for standard-version comparisons.",
			frappe.PermissionError,
		)
	roles = frozenset(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(STANDARD_VERSION_DIFF_ROLES):
		frappe.throw(
			"Current user lacks a standard-version comparison business role.",
			frappe.PermissionError,
		)
	for doctype in STANDARD_VERSION_DIFF_DOCTYPES:
		if not frappe.has_permission(doctype, "read", user=user):
			frappe.throw(
				f"Read permission is required for governed definition type {doctype}.",
				frappe.PermissionError,
			)


def _validated_standard_diff_name(value: Any, fieldname: str) -> str:
	if not isinstance(value, str):
		frappe.throw(f"{fieldname} must be a bounded document name")
	candidate = value.strip()
	if (
		not candidate
		or candidate != value
		or len(candidate) > STANDARD_DIFF_MAX_INPUT_CHARS
		or any(ord(character) < 32 for character in candidate)
	):
		frappe.throw(f"{fieldname} must be a bounded document name")
	return candidate


def _bounded_standard_diff_integer(
	value: Any,
	fieldname: str,
	*,
	minimum: int,
	maximum: int,
) -> int:
	if isinstance(value, bool):
		frappe.throw(f"{fieldname} must be an integer between {minimum} and {maximum}")
	try:
		parsed = int(value)
	except (TypeError, ValueError) as exc:
		frappe.throw(f"{fieldname} must be an integer between {minimum} and {maximum}")
		raise AssertionError("frappe.throw must terminate") from exc  # pragma: no cover
	if str(value).strip() != str(parsed) or not minimum <= parsed <= maximum:
		frappe.throw(f"{fieldname} must be an integer between {minimum} and {maximum}")
	return parsed


def _load_standard_diff_versions(
	baseline_version: str,
	target_version: str,
) -> dict[str, dict[str, Any]]:
	rows = frappe.db.sql(
		"""
		SELECT
			name,
			standard,
			version,
			approval_status,
			effective_from,
			effective_to,
			checksum,
			approved_by,
			approved_at,
			CASE WHEN scope_json IS NULL THEN 1 ELSE 0 END AS scope_json_is_null,
			CHAR_LENGTH(COALESCE(scope_json, '')) AS scope_json_chars,
			LOWER(SHA2(COALESCE(scope_json, ''), 256)) AS scope_json_sha256,
			LEFT(COALESCE(scope_json, ''), %(preview_length)s) AS scope_json_preview
		FROM `tabIONE QC Standard Version`
		WHERE name IN (%(baseline_version)s, %(target_version)s)
		ORDER BY name ASC
		""",
		{
			"baseline_version": baseline_version,
			"target_version": target_version,
			"preview_length": STANDARD_DIFF_TEXT_PREVIEW_CHARS,
		},
		as_dict=True,
	)
	result: dict[str, dict[str, Any]] = {}
	for row in rows:
		name = _bounded_standard_diff_row_text(row, "name", STANDARD_DIFF_MAX_INPUT_CHARS)
		standard = _bounded_standard_diff_row_text(
			row,
			"standard",
			STANDARD_DIFF_MAX_INPUT_CHARS,
		)
		version = _bounded_standard_diff_row_text(row, "version", 32)
		status = _bounded_standard_diff_row_text(row, "approval_status", 32)
		if status not in STANDARD_VERSION_TRANSITIONS:
			frappe.throw(f"Standard version {name} has an invalid lifecycle status")
		checksum = str(_row_value(row, "checksum") or "")
		if not _is_standard_diff_sha256(checksum):
			frappe.throw(f"Standard version {name} has an invalid stored checksum")
		scope_json = _standard_diff_text_state(
			row,
			"scope_json",
			max_source_chars=STANDARD_DIFF_MAX_SCOPE_CHARS,
		)
		result[name] = {
			"name": name,
			"standard": standard,
			"version": version,
			"approval_status": status,
			"effective_from": _standard_diff_scalar(_row_value(row, "effective_from")),
			"effective_to": _standard_diff_scalar(_row_value(row, "effective_to")),
			"scope_json": scope_json,
			"checksum": checksum or None,
			"approved_by": _standard_diff_optional_row_text(
				row,
				"approved_by",
				STANDARD_DIFF_MAX_INPUT_CHARS,
			),
			"approved_at": _standard_diff_scalar(_row_value(row, "approved_at")),
		}
	if set(result) != {baseline_version, target_version}:
		frappe.throw("Both requested standard versions must exist")
	return result


def _load_standard_diff_clauses(standard_version: str) -> dict[str, dict[str, Any]]:
	rows = frappe.db.sql(
		"""
		SELECT
			name,
			clause_key,
			status,
			CASE WHEN clause_code IS NULL THEN 1 ELSE 0 END AS clause_code_is_null,
			CHAR_LENGTH(COALESCE(clause_code, '')) AS clause_code_chars,
			LEFT(
				COALESCE(clause_code, ''),
				%(clause_code_fetch_length)s
			) AS clause_code_preview,
			CASE WHEN heading IS NULL THEN 1 ELSE 0 END AS heading_is_null,
			CHAR_LENGTH(COALESCE(heading, '')) AS heading_chars,
			LOWER(SHA2(COALESCE(heading, ''), 256)) AS heading_sha256,
			LEFT(COALESCE(heading, ''), %(preview_length)s) AS heading_preview,
			CASE WHEN content IS NULL THEN 1 ELSE 0 END AS content_is_null,
			CHAR_LENGTH(COALESCE(content, '')) AS content_chars,
			LOWER(SHA2(COALESCE(content, ''), 256)) AS content_sha256,
			LEFT(COALESCE(content, ''), %(preview_length)s) AS content_preview,
			CASE WHEN keywords IS NULL THEN 1 ELSE 0 END AS keywords_is_null,
			CHAR_LENGTH(COALESCE(keywords, '')) AS keywords_chars,
			LOWER(SHA2(COALESCE(keywords, ''), 256)) AS keywords_sha256,
			LEFT(COALESCE(keywords, ''), %(preview_length)s) AS keywords_preview
		FROM `tabIONE QC Standard Clause`
		WHERE standard_version = %(standard_version)s
		ORDER BY clause_code ASC, name ASC
		LIMIT %(row_limit)s
		""",
		{
			"standard_version": standard_version,
			"clause_code_fetch_length": STANDARD_DIFF_MAX_CLAUSE_CODE_CHARS + 1,
			"preview_length": STANDARD_DIFF_TEXT_PREVIEW_CHARS,
			"row_limit": STANDARD_DIFF_MAX_CLAUSES_PER_VERSION + 1,
		},
		as_dict=True,
	)
	if len(rows) > STANDARD_DIFF_MAX_CLAUSES_PER_VERSION:
		frappe.throw(
			f"Standard version {standard_version} exceeds the bounded "
			f"{STANDARD_DIFF_MAX_CLAUSES_PER_VERSION}-clause comparison limit"
		)
	result: dict[str, dict[str, Any]] = {}
	for row in rows:
		name = _bounded_standard_diff_row_text(row, "name", STANDARD_DIFF_MAX_INPUT_CHARS)
		clause_key = _bounded_standard_diff_row_text(
			row,
			"clause_key",
			STANDARD_DIFF_MAX_INPUT_CHARS,
		)
		if int(_row_value(row, "clause_code_is_null") or 0):
			frappe.throw(f"Standard clause {name} has no governed clause code")
		clause_code_chars = int(_row_value(row, "clause_code_chars") or 0)
		clause_code = str(_row_value(row, "clause_code_preview") or "")
		if (
			not clause_code
			or clause_code_chars != len(clause_code)
			or clause_code_chars > STANDARD_DIFF_MAX_CLAUSE_CODE_CHARS
		):
			frappe.throw(f"Standard clause {name} has an invalid or oversized clause code")
		if clause_code in result:
			frappe.throw(f"Standard version {standard_version} contains duplicate clause code {clause_code}")
		status = _bounded_standard_diff_row_text(row, "status", 32)
		if status not in {"Draft", "Published", "Retired"}:
			frappe.throw(f"Standard clause {name} has an invalid lifecycle status")
		fields = {
			fieldname: _standard_diff_text_state(
				row,
				fieldname,
				max_source_chars=STANDARD_DIFF_MAX_CLAUSE_TEXT_CHARS[fieldname],
			)
			for fieldname in STANDARD_CLAUSE_DIFF_FIELDS
		}
		result[clause_code] = {
			"name": name,
			"clause_key": clause_key,
			"clause_code": clause_code,
			"status": status,
			"fields": fields,
			"record_checksum": _standard_diff_hash(
				{
					"clause_key": clause_key,
					"clause_code": clause_code,
					"status": status,
					"fields": {
						fieldname: _standard_diff_text_digest_state(value)
						for fieldname, value in fields.items()
					},
				}
			),
		}
	return result


def _build_standard_version_field_changes(
	baseline: dict[str, Any],
	target: dict[str, Any],
) -> list[dict[str, Any]]:
	changes: list[dict[str, Any]] = []
	for fieldname in STANDARD_VERSION_DIFF_FIELDS:
		before = baseline[fieldname]
		after = target[fieldname]
		if fieldname == "scope_json":
			equal = _standard_diff_text_digest_state(before) == _standard_diff_text_digest_state(after)
			public_before = _public_standard_diff_text_state(before)
			public_after = _public_standard_diff_text_state(after)
		else:
			equal = before == after
			public_before = before
			public_after = after
		if not equal:
			changes.append(
				{
					"fieldname": fieldname,
					"before": public_before,
					"after": public_after,
				}
			)
	return changes


def _build_standard_clause_changes(
	baseline: dict[str, dict[str, Any]],
	target: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
	changes: list[dict[str, Any]] = []
	counts = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0, "total_changes": 0}
	for clause_code in sorted(set(baseline).union(target)):
		before = baseline.get(clause_code)
		after = target.get(clause_code)
		if before is None:
			change_type = "Added"
			changed_fields = list(STANDARD_CLAUSE_DIFF_FIELDS)
		elif after is None:
			change_type = "Removed"
			changed_fields = list(STANDARD_CLAUSE_DIFF_FIELDS)
		else:
			changed_fields = [
				fieldname
				for fieldname in STANDARD_CLAUSE_DIFF_FIELDS
				if _standard_diff_text_digest_state(before["fields"][fieldname])
				!= _standard_diff_text_digest_state(after["fields"][fieldname])
			]
			if not changed_fields:
				counts["unchanged"] += 1
				continue
			change_type = "Modified"
		counts[change_type.lower()] += 1
		field_changes = [
			{
				"fieldname": fieldname,
				"before": (
					_public_standard_diff_text_state(before["fields"][fieldname])
					if before is not None
					else None
				),
				"after": (
					_public_standard_diff_text_state(after["fields"][fieldname])
					if after is not None
					else None
				),
			}
			for fieldname in changed_fields
		]
		changes.append(
			{
				"clause_code": clause_code,
				"change_type": change_type,
				"baseline_clause": _public_standard_diff_clause_identity(before),
				"target_clause": _public_standard_diff_clause_identity(after),
				"field_changes": field_changes,
				"change_checksum": _standard_diff_hash(
					{
						"clause_code": clause_code,
						"change_type": change_type,
						"before": before["record_checksum"] if before else None,
						"after": after["record_checksum"] if after else None,
					}
				),
			}
		)
	counts["total_changes"] = len(changes)
	return changes, counts


def _changed_standard_clause_references(
	clause_changes: list[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
	references: dict[str, tuple[str, str]] = {}
	for change in clause_changes:
		for side in ("baseline_clause", "target_clause"):
			clause = change.get(side)
			if clause and clause.get("name"):
				references[str(clause["name"])] = (
					str(change["clause_code"]),
					str(change["change_type"]),
				)
	return references


def _load_affected_standard_rule_relations(
	standard: str,
	changed_clause_refs: dict[str, tuple[str, str]],
	changed_standard_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
	if not changed_clause_refs and not changed_standard_fields:
		return []
	version_rows = frappe.db.sql(
		"""
		SELECT
			name AS relation_record,
			rule,
			version AS rule_version,
			status,
			checksum,
			standard_clause_snapshot AS clause_reference,
			CHAR_LENGTH(COALESCE(rule_code_snapshot, '')) AS rule_code_chars,
			LEFT(
				COALESCE(rule_code_snapshot, ''),
				%(rule_text_fetch_length)s
			) AS rule_code_preview,
			CHAR_LENGTH(COALESCE(rule_name_snapshot, '')) AS rule_name_chars,
			LEFT(
				COALESCE(rule_name_snapshot, ''),
				%(rule_text_fetch_length)s
			) AS rule_name_preview
		FROM `tabIONE QC Rule Version`
		WHERE standard_snapshot = %(standard)s
			AND (status IS NULL OR status <> 'Retired')
		ORDER BY rule ASC, name ASC
		LIMIT %(row_limit)s
		""",
		{
			"standard": standard,
			"rule_text_fetch_length": STANDARD_DIFF_MAX_RULE_TEXT_CHARS + 1,
			"row_limit": STANDARD_DIFF_MAX_RULE_RELATIONS + 1,
		},
		as_dict=True,
	)
	parent_rows = frappe.db.sql(
		"""
		SELECT
			name AS relation_record,
			name AS rule,
			NULL AS rule_version,
			status,
			NULL AS checksum,
			standard_clause AS clause_reference,
			CHAR_LENGTH(COALESCE(rule_code, '')) AS rule_code_chars,
			LEFT(COALESCE(rule_code, ''), %(rule_text_fetch_length)s) AS rule_code_preview,
			CHAR_LENGTH(COALESCE(rule_name, '')) AS rule_name_chars,
			LEFT(COALESCE(rule_name, ''), %(rule_text_fetch_length)s) AS rule_name_preview
		FROM `tabIONE QC Rule`
		WHERE standard = %(standard)s
			AND (status IS NULL OR status <> 'Retired')
		ORDER BY name ASC
		LIMIT %(row_limit)s
		""",
		{
			"standard": standard,
			"rule_text_fetch_length": STANDARD_DIFF_MAX_RULE_TEXT_CHARS + 1,
			"row_limit": STANDARD_DIFF_MAX_RULE_RELATIONS + 1,
		},
		as_dict=True,
	)
	if (
		len(version_rows) > STANDARD_DIFF_MAX_RULE_RELATIONS
		or len(parent_rows) > STANDARD_DIFF_MAX_RULE_RELATIONS
	):
		frappe.throw(
			"The standard exceeds the bounded affected-rule relation limit; "
			"run a reviewed offline comparison."
		)

	relations: list[dict[str, Any]] = []
	for source, rows in (("Rule Version Snapshot", version_rows), ("Rule Parent", parent_rows)):
		for row in rows:
			clause_reference = _standard_diff_optional_row_text(
				row,
				"clause_reference",
				STANDARD_DIFF_MAX_INPUT_CHARS,
			)
			clause_change = changed_clause_refs.get(clause_reference or "")
			if not changed_standard_fields and clause_change is None:
				continue
			rule = _bounded_standard_diff_row_text(
				row,
				"rule",
				STANDARD_DIFF_MAX_INPUT_CHARS,
			)
			relation_record = _bounded_standard_diff_row_text(
				row,
				"relation_record",
				STANDARD_DIFF_MAX_INPUT_CHARS,
			)
			rule_code = _bounded_standard_diff_preview_text(row, "rule_code", required=True)
			rule_name = _bounded_standard_diff_preview_text(row, "rule_name", required=True)
			status = _bounded_standard_diff_row_text(row, "status", 32)
			allowed_statuses = (
				frozenset(RULE_VERSION_TRANSITIONS)
				if source == "Rule Version Snapshot"
				else frozenset({"Draft", "Active"})
			)
			if status not in allowed_statuses:
				frappe.throw(f"Rule relation {relation_record} has an invalid lifecycle status")
			checksum = str(_row_value(row, "checksum") or "")
			if source == "Rule Version Snapshot" and not _is_standard_diff_sha256(checksum):
				frappe.throw(f"Rule relation {relation_record} has an invalid checksum")
			rule_version = (
				_bounded_standard_diff_row_text(row, "rule_version", 32)
				if source == "Rule Version Snapshot"
				else None
			)
			relation = {
				"source": source,
				"relation_record": relation_record,
				"rule": rule,
				"rule_version": rule_version,
				"status": status,
				"checksum": checksum or None,
				"rule_code": rule_code or None,
				"rule_name": rule_name or None,
				"clause_reference": clause_reference,
				"impact_basis": {
					"standard_fields": list(changed_standard_fields),
					"clause": (
						{
							"clause_code": clause_change[0],
							"change_type": clause_change[1],
						}
						if clause_change
						else None
					),
				},
			}
			relation["relation_checksum"] = _standard_diff_hash(relation)
			relations.append(relation)
	if len(relations) > STANDARD_DIFF_MAX_RULE_RELATIONS:
		frappe.throw(
			"The comparison exceeds the bounded affected-rule relation limit; "
			"run a reviewed offline comparison."
		)
	return sorted(
		relations,
		key=lambda item: (
			str(item["rule"]),
			str(item["source"]),
			str(item["relation_record"]),
		),
	)


def _summarize_affected_standard_rules(
	relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	grouped: dict[str, list[dict[str, Any]]] = {}
	for relation in relations:
		grouped.setdefault(str(relation["rule"]), []).append(relation)
	if len(grouped) > STANDARD_DIFF_MAX_AFFECTED_RULES:
		frappe.throw(
			"The comparison exceeds the bounded affected-rule count; run a reviewed offline comparison."
		)
	result: list[dict[str, Any]] = []
	for rule, evidence in sorted(grouped.items()):
		codes = sorted({str(item["rule_code"]) for item in evidence if item.get("rule_code")})
		names = sorted({str(item["rule_name"]) for item in evidence if item.get("rule_name")})
		clause_changes = sorted(
			{
				(
					str(item["impact_basis"]["clause"]["clause_code"]),
					str(item["impact_basis"]["clause"]["change_type"]),
				)
				for item in evidence
				if item["impact_basis"].get("clause")
			}
		)
		standard_fields = sorted(
			{str(fieldname) for item in evidence for fieldname in item["impact_basis"]["standard_fields"]}
		)
		result.append(
			{
				"rule": rule,
				"rule_codes": codes[:STANDARD_DIFF_MAX_RULE_EVIDENCE_OUTPUT],
				"rule_code_variant_count": len(codes),
				"rule_names": names[:STANDARD_DIFF_MAX_RULE_EVIDENCE_OUTPUT],
				"rule_name_variant_count": len(names),
				"relation_count": len(evidence),
				"relation_checksum": _standard_diff_hash(evidence),
				"standard_fields": standard_fields,
				"clause_changes": [
					{"clause_code": code, "change_type": change_type}
					for code, change_type in clause_changes[:STANDARD_DIFF_MAX_RULE_EVIDENCE_OUTPUT]
				],
				"clause_change_count": len(clause_changes),
			}
		)
	return result


def _standard_diff_version_snapshot(
	version: dict[str, Any],
	clauses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	return {
		"name": version["name"],
		"standard": version["standard"],
		"version": version["version"],
		"approval_status": version["approval_status"],
		"effective_from": version["effective_from"],
		"effective_to": version["effective_to"],
		"scope_json": _standard_diff_text_digest_state(version["scope_json"]),
		"stored_checksum": version["checksum"],
		"approved_by": version["approved_by"],
		"approved_at": version["approved_at"],
		"clauses": [
			{
				"clause_code": clause_code,
				"name": clause["name"],
				"record_checksum": clause["record_checksum"],
			}
			for clause_code, clause in sorted(clauses.items())
		],
	}


def _public_standard_diff_version(
	version: dict[str, Any],
	clauses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	snapshot = _standard_diff_version_snapshot(version, clauses)
	return {
		"name": snapshot["name"],
		"version": snapshot["version"],
		"approval_status": snapshot["approval_status"],
		"effective_from": snapshot["effective_from"],
		"effective_to": snapshot["effective_to"],
		"scope_json": _public_standard_diff_text_state(version["scope_json"]),
		"stored_checksum": snapshot["stored_checksum"],
		"approved_by": snapshot["approved_by"],
		"approved_at": snapshot["approved_at"],
		"clause_count": len(clauses),
		"observed_snapshot_checksum": _standard_diff_hash(snapshot),
	}


def _public_standard_diff_clause_identity(
	clause: dict[str, Any] | None,
) -> dict[str, Any] | None:
	if clause is None:
		return None
	return {
		"name": clause["name"],
		"clause_key": clause["clause_key"],
		"status": clause["status"],
		"record_checksum": clause["record_checksum"],
	}


def _standard_diff_page(
	rows: list[dict[str, Any]],
	offset: int,
	limit: int,
) -> dict[str, Any]:
	page = rows[offset : offset + limit]
	next_start = offset + len(page)
	has_more = next_start < len(rows)
	return {
		"start": offset,
		"page_length": limit,
		"count": len(page),
		"total": len(rows),
		"has_more": has_more,
		"next_start": next_start if has_more else None,
		"items": page,
	}


def _standard_diff_text_state(
	row: Any,
	prefix: str,
	*,
	max_source_chars: int,
) -> dict[str, Any]:
	is_null = bool(int(_row_value(row, f"{prefix}_is_null") or 0))
	characters = int(_row_value(row, f"{prefix}_chars") or 0)
	if characters < 0 or characters > max_source_chars:
		frappe.throw(f"{prefix} exceeds the bounded comparison text limit")
	digest = str(_row_value(row, f"{prefix}_sha256") or "")
	if not _is_standard_diff_sha256(digest):
		frappe.throw(f"{prefix} has no valid comparison digest")
	preview = str(_row_value(row, f"{prefix}_preview") or "")
	expected_preview_characters = min(characters, STANDARD_DIFF_TEXT_PREVIEW_CHARS)
	if (
		(is_null and characters != 0)
		or len(preview) != expected_preview_characters
		or len(preview) > STANDARD_DIFF_TEXT_PREVIEW_CHARS
	):
		frappe.throw(f"{prefix} preview exceeds the bounded response text limit")
	return {
		"is_null": is_null,
		"characters": characters,
		"sha256": digest,
		"preview": None if is_null else preview,
		"truncated": not is_null and characters > len(preview),
	}


def _public_standard_diff_text_state(state: dict[str, Any]) -> dict[str, Any]:
	return {
		"value": state["preview"],
		"characters": state["characters"],
		"sha256": state["sha256"],
		"truncated": state["truncated"],
		"is_null": state["is_null"],
	}


def _standard_diff_text_digest_state(state: dict[str, Any]) -> dict[str, Any]:
	return {
		"is_null": state["is_null"],
		"characters": state["characters"],
		"sha256": state["sha256"],
	}


def _bounded_standard_diff_row_text(row: Any, fieldname: str, maximum: int) -> str:
	value = _row_value(row, fieldname)
	if not isinstance(value, str) or not value or len(value) > maximum:
		frappe.throw(f"{fieldname} is missing or exceeds the bounded comparison limit")
	return value


def _standard_diff_optional_row_text(
	row: Any,
	fieldname: str,
	maximum: int,
) -> str | None:
	value = _row_value(row, fieldname)
	if value in (None, ""):
		return None
	if not isinstance(value, str) or len(value) > maximum:
		frappe.throw(f"{fieldname} exceeds the bounded comparison limit")
	return value


def _bounded_standard_diff_preview_text(
	row: Any,
	prefix: str,
	*,
	required: bool,
) -> str:
	characters = int(_row_value(row, f"{prefix}_chars") or 0)
	value = str(_row_value(row, f"{prefix}_preview") or "")
	if (
		(required and not value)
		or characters < 0
		or characters > STANDARD_DIFF_MAX_RULE_TEXT_CHARS
		or len(value) != characters
	):
		frappe.throw(f"{prefix} exceeds the bounded affected-rule text limit")
	return value


def _standard_diff_scalar(value: Any) -> str | None:
	if value in (None, ""):
		return None
	if hasattr(value, "isoformat"):
		return str(value.isoformat())
	return str(value)


def _standard_diff_hash(value: Any) -> str:
	encoded = json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	).encode()
	return hashlib.sha256(encoded).hexdigest()


def _is_standard_diff_sha256(value: str) -> bool:
	return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _verify_expected_standard_diff_checksum(
	expected: str | None,
	actual: str,
) -> None:
	if expected in (None, ""):
		return
	if (
		not isinstance(expected, str)
		or not _is_standard_diff_sha256(expected)
		or not hmac.compare_digest(expected, actual)
	):
		frappe.throw(
			"The comparison snapshot changed; restart pagination from the first page.",
			frappe.ValidationError,
		)


def backfill_definition_lineage(batch_size: int = 200) -> dict[str, int | bool]:
	"""Backfill only mutable Drafts; quarantine every ambiguous reviewed legacy version."""
	limit = min(max(int(batch_size or 200), 1), MAX_DEFINITION_LINEAGE_BATCH)
	if not frappe.db.exists("DocType", "IONE Definition Lineage Receipt"):
		return {"backfilled": 0, "quarantined": 0, "blocked": 1, "has_more": False}
	remaining = limit
	backfilled = 0
	quarantined = 0
	url_only_rows = frappe.db.sql(
		"""
		select name
		from `tabIONE QC Standard Version`
		where lineage_status = 'Verified'
			and (
				coalesce(source_file_snapshot, '') = ''
				or coalesce(source_file_hash, '') = ''
			)
		order by name asc
		limit %s
		""",
		(remaining,),
		as_dict=True,
	)
	for row in url_only_rows:
		doc = frappe.get_doc("IONE QC Standard Version", row.name)
		previous_checksum = str(doc.get("checksum") or "")
		doc.set("lineage_status", "Quarantined")
		frappe.db.set_value(
			doc.doctype,
			doc.name,
			"lineage_status",
			"Quarantined",
			update_modified=False,
		)
		_insert_definition_lineage_receipt(
			doc,
			outcome="Quarantined",
			reason_code="URL_ONLY_STANDARD_AUTHORITY",
			previous_checksum=previous_checksum,
		)
		quarantined += 1
	remaining -= len(url_only_rows)
	for doctype, status_field in DEFINITION_LINEAGE_VERSION_BINDINGS.items():
		if remaining <= 0:
			break
		if not frappe.db.exists("DocType", doctype) or not frappe.get_meta(doctype).has_field(
			"lineage_status"
		):
			continue
		rows = frappe.get_all(
			doctype,
			filters={"lineage_status": ["is", "not set"]},
			fields=["name", status_field, "checksum"],
			order_by="name asc",
			limit_page_length=remaining,
		)
		for row in rows:
			doc = frappe.get_doc(doctype, row.name)
			previous_checksum = str(doc.get("checksum") or "")
			state = str(doc.get(status_field) or "Draft")
			outcome = "Quarantined"
			reason_code = "AMBIGUOUS_REVIEWED_LEGACY_LINEAGE"
			if state == "Draft":
				try:
					_rebuild_draft_definition_lineage(doc)
				except frappe.ValidationError, frappe.DoesNotExistError, ValueError:
					doc.set("lineage_status", "Quarantined")
					reason_code = "DRAFT_LINEAGE_REBUILD_FAILED"
				else:
					outcome = "Backfilled"
					reason_code = "DRAFT_LINEAGE_REBUILT"
			else:
				doc.set("lineage_status", "Quarantined")
			values = {"lineage_status": doc.get("lineage_status")}
			if outcome == "Backfilled":
				values.update(_definition_lineage_repair_values(doc))
			frappe.db.set_value(doctype, doc.name, values, update_modified=False)
			_insert_definition_lineage_receipt(
				doc,
				outcome=outcome,
				reason_code=reason_code,
				previous_checksum=previous_checksum,
			)
			if outcome == "Backfilled":
				backfilled += 1
			else:
				quarantined += 1
		remaining -= len(rows)
	active_quarantine = 0
	pending = bool(
		frappe.db.sql(
			"""
			select name
			from `tabIONE QC Standard Version`
			where lineage_status = 'Verified'
				and (
					coalesce(source_file_snapshot, '') = ''
					or coalesce(source_file_hash, '') = ''
				)
			limit 1
			"""
		)
	)
	for doctype, status_field in DEFINITION_LINEAGE_VERSION_BINDINGS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		if frappe.db.exists(doctype, {"lineage_status": ["is", "not set"]}):
			pending = True
		active_quarantine += frappe.db.count(
			doctype,
			{
				"lineage_status": "Quarantined",
				status_field: ["in", sorted(DEFINITION_LINEAGE_ACTIVE_STATES[doctype])],
			},
		)
	return {
		"backfilled": backfilled,
		"quarantined": quarantined,
		"blocked": active_quarantine,
		"has_more": bool(pending or remaining <= 0),
	}


def _rebuild_draft_definition_lineage(doc) -> None:
	if doc.doctype == "IONE QC Standard Version":
		standard = frappe.get_doc("IONE QC Standard", doc.standard)
		_materialize_standard_snapshot(doc, standard)
		_set_checksum(doc, _standard_version_content(doc))
	elif doc.doctype == "IONE QC Rule Version":
		rule = frappe.get_doc("IONE QC Rule", doc.rule)
		_materialize_rule_snapshot(doc, rule)
		_materialize_rule_authority_snapshot(doc, rule)
		_set_checksum(doc, _version_content(doc))
	elif doc.doctype == "IONE QC Indicator Version":
		indicator = frappe.get_doc("IONE QC Indicator", doc.indicator)
		_materialize_indicator_snapshot(doc, indicator)
		_set_checksum(doc, _version_content(doc))
	else:
		frappe.throw("Unsupported definition-lineage repair target")


def _definition_lineage_repair_values(doc) -> dict[str, Any]:
	if doc.doctype == "IONE QC Standard Version":
		fields = (*STANDARD_VERSION_SNAPSHOT_FIELDS, "checksum")
	elif doc.doctype == "IONE QC Rule Version":
		fields = (
			*RULE_VERSION_SNAPSHOT_FIELDS,
			*RULE_AUTHORITY_SNAPSHOT_FIELDS,
			"plugin_key",
			"checksum",
		)
	elif doc.doctype == "IONE QC Indicator Version":
		fields = (
			*INDICATOR_SNAPSHOT_FIELD_MAP,
			*INDICATOR_AUTHORITY_SNAPSHOT_FIELDS,
			"calculator_key",
			"formula_json",
			"direction",
			"target_value",
			"target_min",
			"target_max",
			"warning_threshold",
			"critical_threshold",
			"checksum",
		)
	else:
		frappe.throw("Unsupported definition-lineage repair target")
	return {fieldname: doc.get(fieldname) for fieldname in fields}


def _insert_definition_lineage_receipt(
	doc,
	*,
	outcome: str,
	reason_code: str,
	previous_checksum: str,
) -> None:
	from ione_qms.services.migration_state import SCHEMA_REVISION

	payload = {
		"contract_version": 1,
		"target_doctype": doc.doctype,
		"target_name": doc.name,
		"outcome": outcome,
		"reason_code": reason_code,
		"previous_checksum": previous_checksum,
		"authority_snapshot_hash": str(doc.get("authority_snapshot_hash") or ""),
		"migration_run_id": SCHEMA_REVISION,
	}
	receipt_key = _content_checksum(payload)
	if frappe.db.exists("IONE Definition Lineage Receipt", {"receipt_key": receipt_key}):
		return
	record = frappe.get_doc(
		{
			"doctype": "IONE Definition Lineage Receipt",
			"receipt_key": receipt_key,
			"target_doctype": doc.doctype,
			"target_name": doc.name,
			"outcome": outcome,
			"reason_code": reason_code,
			"previous_checksum": previous_checksum,
			"authority_snapshot_hash": str(doc.get("authority_snapshot_hash") or ""),
			"migration_run_id": SCHEMA_REVISION,
			"recorded_at": now_datetime(),
		}
	)
	try:
		record.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		if not frappe.db.exists("IONE Definition Lineage Receipt", {"receipt_key": receipt_key}):
			raise


def backfill_active_version_keys(batch_size: int = 200) -> dict[str, Any]:
	"""Repair version singleton keys and parent states in bounded, resumable batches.

	Each version DocType consumes at most ``batch_size`` candidate rows per call.
	Queries select only rows whose stored value differs from the required value, so
	already-correct leading rows cannot starve later repairs. A short-lived cursor
	lets a reported, deliberately-unmodified duplicate page yield to later rows on
	the next invocation.
	"""
	limit = min(max(int(batch_size or 200), 1), 1_000)
	summary: dict[str, Any] = {
		"updated": 0,
		"cleared": 0,
		"parent_status_updated": 0,
		"blocked": 0,
		"has_more": False,
		"doctypes": {},
	}
	for binding in _ACTIVE_VERSION_BINDINGS:
		result = _backfill_active_version_binding(binding, batch_size=limit)
		summary["doctypes"][binding[0]] = result
		for fieldname in ("updated", "cleared", "parent_status_updated", "blocked"):
			summary[fieldname] += result[fieldname]
		summary["has_more"] = bool(summary["has_more"] or result["has_more"])
	return summary


def _backfill_active_version_binding(
	binding: tuple[str, str, str, str, str, str],
	*,
	batch_size: int,
) -> dict[str, Any]:
	(
		version_doctype,
		parent_field,
		status_field,
		active_version_status,
		parent_doctype,
		active_parent_status,
	) = binding
	result: dict[str, Any] = {
		"updated": 0,
		"cleared": 0,
		"parent_status_updated": 0,
		"blocked": 0,
		"has_more": False,
		"issues": [],
	}
	if not frappe.db.exists("DocType", version_doctype):
		return result
	frappe.clear_cache(doctype=version_doctype)
	if not frappe.get_meta(version_doctype).has_field("active_parent_key"):
		return result

	remaining = batch_size
	inactive_rows, inactive_pending = _version_backfill_page(
		version_doctype,
		"inactive-key",
		remaining,
		lambda cursor, page_size: _select_inactive_key_rows(
			version_doctype,
			status_field,
			active_version_status,
			cursor,
			page_size,
		),
	)
	for row in inactive_rows:
		frappe.db.set_value(
			version_doctype,
			_row_value(row, "name"),
			"active_parent_key",
			None,
			update_modified=False,
		)
	result["cleared"] += len(inactive_rows)
	result["has_more"] = bool(result["has_more"] or inactive_pending)
	remaining -= len(inactive_rows)
	if remaining <= 0:
		result["has_more"] = True
		return result

	active_rows, active_pending = _version_backfill_page(
		version_doctype,
		"active-key",
		remaining,
		lambda cursor, page_size: _select_active_key_rows(
			version_doctype,
			parent_field,
			status_field,
			active_version_status,
			cursor,
			page_size,
		),
	)
	active_blocked = 0
	result["has_more"] = bool(result["has_more"] or active_pending)
	reported_duplicate_parents: set[str] = set()
	for row in active_rows:
		version_name = str(_row_value(row, "name") or "")
		parent_name = str(_row_value(row, "parent_name") or "")
		if not parent_name:
			active_blocked += 1
			_add_version_backfill_issue(
				result,
				version_doctype,
				version_name,
				"MISSING_ACTIVE_PARENT",
			)
			continue
		if not frappe.db.exists(parent_doctype, parent_name):
			active_blocked += 1
			_add_version_backfill_issue(
				result,
				version_doctype,
				version_name,
				"MISSING_PARENT_RECORD",
				parent_name=parent_name,
			)
			continue
		siblings = _active_version_names(
			version_doctype,
			parent_field,
			parent_name,
			status_field,
			active_version_status,
		)
		if len(siblings) != 1 or siblings[0] != version_name:
			active_blocked += 1
			if parent_name not in reported_duplicate_parents:
				reported_duplicate_parents.add(parent_name)
				_add_version_backfill_issue(
					result,
					version_doctype,
					version_name,
					"DUPLICATE_ACTIVE_VERSION",
					parent_name=parent_name,
					related_names=siblings,
				)
			continue
		try:
			frappe.db.set_value(
				version_doctype,
				version_name,
				"active_parent_key",
				parent_name,
				update_modified=False,
			)
		except frappe.DuplicateEntryError, frappe.UniqueValidationError:
			active_blocked += 1
			_add_version_backfill_issue(
				result,
				version_doctype,
				version_name,
				"ACTIVE_PARENT_KEY_UNIQUE_CONFLICT",
				parent_name=parent_name,
			)
			continue
		result["updated"] += 1
	result["blocked"] += active_blocked
	remaining -= len(active_rows)
	if remaining <= 0:
		result["has_more"] = True
		return result

	active_parent_rows, active_parent_pending = _version_backfill_page(
		version_doctype,
		"active-parent-status",
		remaining,
		lambda cursor, page_size: _select_active_parent_rows(
			version_doctype,
			parent_doctype,
			parent_field,
			status_field,
			active_version_status,
			active_parent_status,
			cursor,
			page_size,
		),
	)
	parent_blocked = 0
	result["has_more"] = bool(result["has_more"] or active_parent_pending)
	for row in active_parent_rows:
		version_name = str(_row_value(row, "version_name") or "")
		parent_name = str(_row_value(row, "parent_name") or "")
		siblings = _active_version_names(
			version_doctype,
			parent_field,
			parent_name,
			status_field,
			active_version_status,
		)
		if not parent_name or len(siblings) != 1 or siblings[0] != version_name:
			parent_blocked += 1
			_add_version_backfill_issue(
				result,
				version_doctype,
				version_name,
				"DUPLICATE_ACTIVE_VERSION",
				parent_name=parent_name,
				related_names=siblings,
			)
			continue
		frappe.db.set_value(
			parent_doctype,
			parent_name,
			"status",
			active_parent_status,
			update_modified=False,
		)
		result["parent_status_updated"] += 1
	result["blocked"] += parent_blocked
	remaining -= len(active_parent_rows)
	if remaining <= 0:
		result["has_more"] = True
		return result

	retired_parent_rows, retired_parent_pending = _version_backfill_page(
		version_doctype,
		"retired-parent-status",
		remaining,
		lambda cursor, page_size: _select_retired_parent_rows(
			version_doctype,
			parent_doctype,
			parent_field,
			status_field,
			active_version_status,
			cursor,
			page_size,
		),
	)
	for row in retired_parent_rows:
		frappe.db.set_value(
			parent_doctype,
			_row_value(row, "parent_name"),
			"status",
			"Retired",
			update_modified=False,
		)
	result["parent_status_updated"] += len(retired_parent_rows)
	result["has_more"] = bool(result["has_more"] or retired_parent_pending)
	remaining -= len(retired_parent_rows)
	result["has_more"] = bool(
		result["has_more"]
		or result["blocked"]
		or remaining <= 0
		or len(inactive_rows) == batch_size
		or len(active_rows) == batch_size
		or len(active_parent_rows) == batch_size
		or len(retired_parent_rows) == batch_size
	)
	return result


def _select_inactive_key_rows(
	version_doctype: str,
	status_field: str,
	active_status: str,
	cursor: str,
	limit: int,
) -> list[Any]:
	table = _sql_table(version_doctype)
	status_column = _sql_column(status_field)
	query = f"""
		SELECT v.name
		FROM {table} v
		WHERE (v.{status_column} IS NULL OR v.{status_column} <> %(active_status)s)
			AND v.active_parent_key IS NOT NULL
			AND v.name > %(cursor)s
		ORDER BY v.name ASC
		LIMIT %(limit)s
	"""  # noqa: S608 -- identifiers are selected from closed allowlists above.
	return frappe.db.sql(
		query,
		{"active_status": active_status, "cursor": cursor, "limit": limit},
		as_dict=True,
	)


def _select_active_key_rows(
	version_doctype: str,
	parent_field: str,
	status_field: str,
	active_status: str,
	cursor: str,
	limit: int,
) -> list[Any]:
	table = _sql_table(version_doctype)
	parent_column = _sql_column(parent_field)
	status_column = _sql_column(status_field)
	query = f"""
		SELECT v.name, v.{parent_column} AS parent_name
		FROM {table} v
		WHERE v.{status_column} = %(active_status)s
			AND (
				v.{parent_column} IS NULL
				OR v.active_parent_key IS NULL
				OR v.active_parent_key <> v.{parent_column}
			)
			AND v.name > %(cursor)s
		ORDER BY v.name ASC
		LIMIT %(limit)s
	"""  # noqa: S608 -- identifiers are selected from closed allowlists above.
	return frappe.db.sql(
		query,
		{"active_status": active_status, "cursor": cursor, "limit": limit},
		as_dict=True,
	)


def _select_active_parent_rows(
	version_doctype: str,
	parent_doctype: str,
	parent_field: str,
	status_field: str,
	active_status: str,
	parent_status: str,
	cursor: str,
	limit: int,
) -> list[Any]:
	version_table = _sql_table(version_doctype)
	parent_table = _sql_table(parent_doctype)
	parent_column = _sql_column(parent_field)
	status_column = _sql_column(status_field)
	query = f"""
		SELECT v.name AS version_name, v.{parent_column} AS parent_name
		FROM {version_table} v
		INNER JOIN {parent_table} p ON p.name = v.{parent_column}
		WHERE v.{status_column} = %(active_status)s
			AND (p.status IS NULL OR p.status <> %(parent_status)s)
			AND v.name > %(cursor)s
		ORDER BY v.name ASC
		LIMIT %(limit)s
	"""  # noqa: S608 -- identifiers are selected from closed allowlists above.
	return frappe.db.sql(
		query,
		{
			"active_status": active_status,
			"parent_status": parent_status,
			"cursor": cursor,
			"limit": limit,
		},
		as_dict=True,
	)


def _select_retired_parent_rows(
	version_doctype: str,
	parent_doctype: str,
	parent_field: str,
	status_field: str,
	active_status: str,
	cursor: str,
	limit: int,
) -> list[Any]:
	version_table = _sql_table(version_doctype)
	parent_table = _sql_table(parent_doctype)
	parent_column = _sql_column(parent_field)
	status_column = _sql_column(status_field)
	query = f"""
		SELECT DISTINCT v.{parent_column} AS parent_name
		FROM {version_table} v
		INNER JOIN {parent_table} p ON p.name = v.{parent_column}
		WHERE v.{status_column} = 'Retired'
			AND (p.status IS NULL OR p.status <> 'Retired')
			AND v.{parent_column} > %(cursor)s
			AND NOT EXISTS (
				SELECT 1
				FROM {version_table} active_v
				WHERE active_v.{parent_column} = v.{parent_column}
					AND active_v.{status_column} = %(active_status)s
			)
		ORDER BY v.{parent_column} ASC
		LIMIT %(limit)s
	"""  # noqa: S608 -- identifiers are selected from closed allowlists above.
	return frappe.db.sql(
		query,
		{"active_status": active_status, "cursor": cursor, "limit": limit},
		as_dict=True,
	)


def _active_version_names(
	version_doctype: str,
	parent_field: str,
	parent_name: str,
	status_field: str,
	active_status: str,
) -> list[str]:
	return [
		str(name)
		for name in frappe.get_all(
			version_doctype,
			filters={
				parent_field: parent_name,
				status_field: active_status,
			},
			pluck="name",
			order_by="name asc",
			limit_page_length=2,
		)
	]


def _version_backfill_page(
	version_doctype: str,
	stage: str,
	limit: int,
	fetch,
) -> tuple[list[Any], bool]:
	if limit <= 0:
		return [], False
	key = _version_backfill_cursor_key(version_doctype, stage)
	cursor = frappe.cache.get_value(key) or ""
	if isinstance(cursor, bytes):
		cursor = cursor.decode()
	rows = list(fetch(str(cursor), limit))
	if not rows:
		if cursor:
			frappe.cache.delete_value(key)
			return [], True
		return [], False
	last_name = str(
		_row_value(rows[-1], "name")
		or _row_value(rows[-1], "version_name")
		or _row_value(rows[-1], "parent_name")
		or ""
	)
	if last_name:
		frappe.cache.set_value(key, last_name, expires_in_sec=604_800)
	return rows, True


def _version_backfill_cursor_key(version_doctype: str, stage: str) -> str:
	return f"ione_qms:version_backfill:{version_doctype}:{stage}"


def _add_version_backfill_issue(
	result: dict[str, Any],
	version_doctype: str,
	version_name: str,
	code: str,
	*,
	parent_name: str = "",
	related_names: list[str] | None = None,
) -> None:
	issue = {
		"code": code,
		"version": version_name,
		"parent": parent_name,
		"related_versions": sorted(related_names or []),
	}
	if issue not in result["issues"]:
		result["issues"].append(issue)
	cache_key = f"ione_qms:version_backfill_issue:{version_doctype}:{version_name}:{code}"
	if frappe.cache.get_value(cache_key):
		return
	frappe.cache.set_value(cache_key, 1, expires_in_sec=86_400)
	frappe.log_error(
		title=f"IONE version backfill requires review: {code}",
		message=(
			f"{version_doctype} {version_name or '[unnamed]'} could not be repaired "
			"automatically. No clinical content was copied into this log."
		),
		reference_doctype=version_doctype,
		reference_name=version_name or None,
	)


def _sql_table(doctype: str) -> str:
	if doctype not in {
		binding_doctype
		for binding in _ACTIVE_VERSION_BINDINGS
		for binding_doctype in (binding[0], binding[4])
	}:
		raise ValueError("Unsupported version backfill DocType")
	return f"`tab{doctype}`"


def _sql_column(fieldname: str) -> str:
	if fieldname not in {"standard", "rule", "indicator", "approval_status", "status"}:
		raise ValueError("Unsupported version backfill field")
	return f"`{fieldname}`"


def _row_value(row: Any, fieldname: str) -> Any:
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _validate_indicator_targets(doc) -> None:
	indicator = (
		frappe.get_cached_doc("IONE QC Indicator", doc.get("indicator")) if doc.get("indicator") else None
	)

	def first(fieldname: str) -> Any:
		value = doc.get(fieldname)
		if value not in (None, ""):
			return value
		return indicator.get(fieldname) if indicator else None

	target_min = first("target_min")
	target_max = first("target_max")
	direction = first("direction")
	if direction == "Target Range" and (target_min in (None, "") or target_max in (None, "")):
		frappe.throw("Target Range indicators require both target_min and target_max")
	if target_min not in (None, "") and target_max not in (None, ""):
		if float(target_min) > float(target_max):
			frappe.throw("Indicator target_min cannot be greater than target_max")


def _materialize_indicator_defaults(doc) -> None:
	if doc.get("status") != "Published" or not doc.get("indicator"):
		return
	previous = doc.get_doc_before_save()
	if previous and previous.get("status") == "Published":
		return
	indicator = frappe.get_cached_doc("IONE QC Indicator", doc.get("indicator"))
	for fieldname in (
		"direction",
		"target_value",
		"target_min",
		"target_max",
		"warning_threshold",
		"critical_threshold",
	):
		if doc.meta.has_field(fieldname) and doc.get(fieldname) in (None, ""):
			value = indicator.get(fieldname)
			if value not in (None, ""):
				doc.set(fieldname, value)


def validate_agent_release(doc, method: str | None = None) -> None:
	"""Protect an Agent release with reviewed approval and retirement transitions."""
	del method
	_lock_and_validate_lineage_links(doc)
	transition = _validate_governed_transition(
		doc,
		status_field="status",
		transitions=AGENT_RELEASE_TRANSITIONS,
	)
	_require_transition_actor(
		doc,
		transition,
		AGENT_RELEASE_TRANSITION_ROLES,
		AGENT_RELEASE_SELF_APPROVAL_EDGES,
	)
	_require_same_state_editor(
		doc,
		status_field="status",
		transition=transition,
		state_role_map=AGENT_RELEASE_STATE_EDIT_ROLES,
	)
	if transition and frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot approve or retire an Agent release; use a named accountable reviewer.",
			frappe.PermissionError,
		)
	policy = frappe.get_doc("IONE Agent Policy", doc.get("policy"))
	agent = frappe.get_doc("Flow Agent", doc.get("flow_agent"))
	model = frappe.get_doc("Flow Model", doc.get("flow_model"))
	if str(policy.get("flow_agent") or "") != str(agent.name):
		frappe.throw("Agent release policy and Flow Agent do not match")
	if str(agent.get("model") or "") != str(model.name):
		frappe.throw("Agent release Flow Model does not match the Flow Agent model")
	if transition == ("Draft", "Approved"):
		from ione_qms.services.agent_evaluations import (
			require_confirmation_lifecycle_evaluation,
		)

		if not int(agent.get("enabled") or 0) or not int(model.get("enabled") or 0):
			frappe.throw("Agent and model must be enabled before an Agent release can be approved")
		if frappe.db.exists(
			"IONE Agent Release",
			{
				"policy": policy.name,
				"status": "Approved",
				"name": ["!=", doc.name],
			},
		):
			frappe.throw("Retire the currently Approved Agent release before approving another")
		configuration = canonical_json(build_release_configuration(policy, agent, model))
		require_confirmation_lifecycle_evaluation(json.loads(configuration))
		doc.configuration_json = configuration
		doc.checksum = hashlib.sha256(configuration.encode()).hexdigest()
		doc.commit_sha = current_app_commit_sha()
	if doc.meta.has_field("active_parent_key"):
		doc.active_parent_key = policy.name if doc.get("status") == "Approved" else None
	previous = doc.get_doc_before_save()
	if previous and previous.get("status") in {"Approved", "Retired"}:
		protected_fields = (
			"release_key",
			"flow_agent",
			"policy",
			"flow_model",
			"version",
			"configuration_json",
			"checksum",
			"commit_sha",
		)
		changed = [
			fieldname for fieldname in protected_fields if previous.get(fieldname) != doc.get(fieldname)
		]
		if changed:
			frappe.throw(
				"Approved Agent release content is immutable; create a new release. "
				+ "Changed: "
				+ ", ".join(changed)
			)
	_set_approval_metadata(doc, transition, approved_state="Approved")


def _validate_governed_transition(
	doc,
	*,
	status_field: str,
	transitions: dict[str, frozenset[str]],
	initial_state: str = "Draft",
) -> tuple[str, str] | None:
	current = str(doc.get(status_field) or initial_state).strip()
	if not doc.get(status_field):
		doc.set(status_field, current)
	previous = doc.get_doc_before_save()
	if previous is None:
		if current != initial_state:
			frappe.throw(f"New {doc.doctype} records must start in {initial_state} status")
		return None
	previous_status = str(previous.get(status_field) or initial_state).strip()
	if previous_status == current:
		return None
	allowed = transitions.get(previous_status)
	if allowed is None or current not in allowed:
		choices = ", ".join(sorted(allowed or ())) or "none"
		frappe.throw(
			f"Invalid {doc.doctype} transition: {previous_status} -> {current}. "
			f"Allowed next states: {choices}."
		)
	return previous_status, current


def _require_single_active_version(
	doc,
	*,
	parent_field: str,
	status_field: str,
	active_status: str,
) -> None:
	if doc.get(status_field) != active_status or not doc.get(parent_field):
		return
	if frappe.db.exists(
		doc.doctype,
		{
			parent_field: doc.get(parent_field),
			status_field: active_status,
			"name": ["!=", doc.name],
		},
	):
		frappe.throw(
			f"{doc.get(parent_field)} already has an active {active_status} version; "
			"retire it before activating another version"
		)


def _set_active_parent_key(
	doc,
	*,
	parent_field: str,
	status_field: str,
	active_status: str,
) -> None:
	if not doc.meta.has_field("active_parent_key"):
		return
	value = doc.get(parent_field) if doc.get(status_field) == active_status else None
	doc.set("active_parent_key", value)


def _sync_version_parent_status(
	doc,
	*,
	parent_doctype: str,
	parent_field: str,
	status_field: str,
	active_version_status: str,
	active_parent_status: str,
) -> None:
	previous = doc.get_doc_before_save()
	if not previous:
		return
	previous_status = str(previous.get(status_field) or "")
	current_status = str(doc.get(status_field) or "")
	if previous_status == current_status or current_status not in {
		active_version_status,
		"Retired",
	}:
		return
	parent_name = doc.get(parent_field)
	if not parent_name:
		return
	if current_status == active_version_status:
		target_status = active_parent_status
	else:
		remaining_active = frappe.db.exists(
			doc.doctype,
			{
				parent_field: parent_name,
				status_field: active_version_status,
				"name": ["!=", doc.name],
			},
		)
		target_status = active_parent_status if remaining_active else "Retired"
	current_parent_status = frappe.db.get_value(parent_doctype, parent_name, "status")
	if current_parent_status != target_status:
		frappe.db.set_value(parent_doctype, parent_name, "status", target_status)


def _sync_standard_clause_statuses(doc) -> None:
	previous = doc.get_doc_before_save()
	if not previous:
		return
	previous_status = str(previous.get("approval_status") or "Draft")
	current_status = str(doc.get("approval_status") or "Draft")
	if previous_status == current_status:
		return
	frappe.db.set_value(
		"IONE QC Standard Clause",
		{"standard_version": doc.name},
		"status",
		_standard_clause_status(current_status),
		update_modified=False,
	)


def _standard_clause_status(parent_status: str) -> str:
	if parent_status == "Approved":
		return "Published"
	if parent_status == "Retired":
		return "Retired"
	return "Draft"


def _require_transition_actor(
	doc,
	transition: tuple[str, str] | None,
	role_map: dict[tuple[str, str], frozenset[str]],
	self_approval_edges: frozenset[tuple[str, str]],
) -> None:
	if transition is None:
		return
	required = role_map.get(transition)
	if not required:
		frappe.throw(f"No reviewed business-role policy exists for {transition[0]} -> {transition[1]}.")
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot perform governed definition transitions; use a named "
			"accountable business user.",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(required):
		frappe.throw(
			f"Current user lacks the reviewed role required for {transition[0]} -> {transition[1]}.",
			frappe.PermissionError,
		)
	if user == doc.get("owner") and transition not in self_approval_edges:
		frappe.throw("Self approval is not allowed", frappe.PermissionError)


def _require_same_state_editor(
	doc,
	*,
	status_field: str,
	transition: tuple[str, str] | None,
	state_role_map: dict[str, frozenset[str]],
) -> None:
	previous = doc.get_doc_before_save()
	if not previous or transition is not None:
		return
	changed = _changed_business_fields(doc, previous)
	if not changed:
		return
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot edit governed definition content; use a named accountable business user.",
			frappe.PermissionError,
		)
	required = state_role_map.get(str(doc.get(status_field) or ""), frozenset())
	roles = set(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(required):
		frappe.throw(
			"Current user cannot edit content in this workflow state. Changed: " + ", ".join(sorted(changed)),
			frappe.PermissionError,
		)


def _changed_business_fields(doc, previous) -> set[str]:
	excluded = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"checksum",
		"active_parent_key",
		"approved_by",
		"approved_at",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
	return {
		field.fieldname
		for field in doc.meta.fields
		if field.fieldname not in excluded
		and not field.fieldtype.endswith("Break")
		and doc.get(field.fieldname) != previous.get(field.fieldname)
	}


def _set_approval_metadata(
	doc,
	transition: tuple[str, str] | None,
	*,
	approved_state: str,
) -> None:
	if not transition or transition[1] != approved_state:
		return
	if doc.meta.has_field("approved_by"):
		doc.set("approved_by", frappe.session.user)
	if doc.meta.has_field("approved_at"):
		doc.set("approved_at", now_datetime())


def _require_verified_or_quarantined_retirement(
	doc,
	transition: tuple[str, str] | None,
	*,
	status_field: str,
) -> bool:
	"""Reject legacy ambiguous lineage, except for a one-way fail-closed retirement."""
	previous = doc.get_doc_before_save()
	if previous is None:
		return False
	previous_state = str(previous.get(status_field) or "Draft")
	previous_lineage = str(previous.get("lineage_status") or "")
	current_lineage = str(doc.get("lineage_status") or "")
	if previous_lineage == "Quarantined":
		if transition == (previous_state, "Retired"):
			doc.set("lineage_status", "Quarantined")
			return True
		frappe.throw(
			"Quarantined legacy definition lineage may only be retired; create a new verified version"
		)
	if previous_state != "Draft" and previous_lineage != "Verified":
		frappe.throw(
			"Legacy governed definition lineage is ambiguous and must be quarantined by the "
			"bounded migration before use"
		)
	if previous_lineage == "Verified" and current_lineage not in {"", "Verified"}:
		frappe.throw("Verified definition lineage cannot be downgraded or replaced")
	return False


def _retire_quarantined_version(
	doc,
	*,
	parent_field: str,
	status_field: str,
) -> bool:
	"""Provide one reviewed, non-semantic exit for any quarantined legacy state."""
	previous = doc.get_doc_before_save()
	if previous is None or str(previous.get("lineage_status") or "") != "Quarantined":
		return False
	previous_state = str(previous.get(status_field) or "Draft")
	current_state = str(doc.get(status_field) or "Draft")
	if current_state != "Retired":
		frappe.throw(
			"Quarantined legacy definition lineage may only be retired; create a new verified replacement"
		)
	user = str(frappe.session.user or "")
	roles = set(frappe.get_roles(user)) if user else set()
	if (
		user in {"", "Guest", "Administrator"}
		or "IONE Agent Service" in roles
		or not roles.intersection(CLINICAL_DEFINITION_REVIEW_ROLES)
	):
		frappe.throw(
			"Retiring quarantined lineage requires a named QC Reviewer or Medical Affairs user",
			frappe.PermissionError,
		)
	changed = _changed_business_fields(doc, previous)
	allowed = {status_field, "effective_to"}
	if changed.difference(allowed):
		frappe.throw(
			"Quarantined lineage cannot be semantically repaired during retirement. Changed: "
			+ ", ".join(sorted(changed.difference(allowed)))
		)
	if str(doc.get("lineage_status") or "") != "Quarantined":
		frappe.throw("Quarantined lineage status is immutable")
	if str(doc.get("checksum") or "") != str(previous.get("checksum") or ""):
		frappe.throw("A quarantined version checksum cannot be changed during retirement")
	previous_end = previous.get("effective_to")
	current_end = doc.get("effective_to")
	if not current_end:
		frappe.throw("Retiring a quarantined definition requires an explicit effective_to")
	if previous_end not in (None, "") and previous_end != current_end:
		frappe.throw("A quarantined definition effective_to cannot be rewritten")
	if previous_state == "Retired":
		frappe.throw("Retired quarantined definitions are immutable")
	_finalize_quarantined_retirement(
		doc,
		parent_field=parent_field,
		status_field=status_field,
		require_effective_to=True,
	)
	return True


def _finalize_quarantined_retirement(
	doc,
	*,
	parent_field: str,
	status_field: str,
	require_effective_to: bool = False,
) -> None:
	"""Allow no semantic repair: only close the interval and remove the active key."""
	_validate_dates(doc)
	if require_effective_to and not doc.get("effective_to"):
		frappe.throw("Retiring a quarantined definition requires an explicit effective_to")
	doc.set("lineage_status", "Quarantined")
	_set_active_parent_key(
		doc,
		parent_field=parent_field,
		status_field=status_field,
		active_status="Published" if status_field == "status" else "Approved",
	)


def _require_standard_parent_identity(standard) -> None:
	missing = [
		fieldname
		for fieldname in ("standard_code", "standard_name", "source_type")
		if not str(standard.get(fieldname) or "").strip()
	]
	if missing:
		frappe.throw("Standard authority identity is incomplete: " + ", ".join(missing))
	if not standard.get("source_url") and not standard.get("source_file"):
		frappe.throw("A governed standard requires either source_url or source_file provenance")


def _validate_parent_authority_links(doc) -> None:
	standard_name = str(doc.get("standard") or "")
	clause_name = str(doc.get("standard_clause") or "")
	if not standard_name or not clause_name:
		frappe.throw("Governed definitions require both standard and standard_clause")
	_load_standard_authority(
		standard_name,
		clause_name,
		require_approved=False,
	)


def _materialize_standard_snapshot(doc, standard) -> None:
	if standard is None:
		frappe.throw("Standard version requires a governed standard parent")
	_require_standard_parent_identity(standard)
	previous = doc.get_doc_before_save()
	previous_state = str(previous.get("approval_status") or "Draft") if previous else "Draft"
	current_state = str(doc.get("approval_status") or "Draft")
	if standard.get("source_file"):
		archived_source_hash = _require_archived_standard_source(standard)
		lineage_status = "Verified"
	elif current_state == "Draft":
		# Installation may seed URL-only templates, but they remain explicitly
		# non-reviewable until a named author archives the real authority bytes.
		archived_source_hash = ""
		lineage_status = "Pending Archive"
	else:
		archived_source_hash = _require_archived_standard_source(standard)
		lineage_status = "Verified"
	if previous is None or previous_state == "Draft":
		for snapshot_field, source_field in STANDARD_SNAPSHOT_FIELD_MAP.items():
			doc.set(snapshot_field, standard.get(source_field))
		doc.set(
			"source_file_hash",
			archived_source_hash,
		)
		doc.set("lineage_status", lineage_status)
	else:
		changed = [
			fieldname
			for fieldname in STANDARD_VERSION_SNAPSHOT_FIELDS
			if previous.get(fieldname) != doc.get(fieldname)
		]
		if changed:
			frappe.throw(
				"Standard identity and provenance snapshots are immutable after review starts. "
				"Changed: " + ", ".join(changed)
			)
		missing = [
			fieldname
			for fieldname in ("standard_code_snapshot", "standard_name_snapshot", "source_type_snapshot")
			if not doc.get(fieldname)
		]
		if missing or str(doc.get("lineage_status") or "") != "Verified":
			frappe.throw("Legacy standard version lacks verified provenance snapshots; create a new version")
	for snapshot_field, source_field in STANDARD_SNAPSHOT_FIELD_MAP.items():
		if str(doc.get(snapshot_field) or "") != str(standard.get(source_field) or ""):
			frappe.throw(
				f"Standard parent provenance drift detected for {snapshot_field}; create a new version"
			)
	expected_file_hash = archived_source_hash
	if not hmac.compare_digest(
		str(doc.get("source_file_hash") or ""),
		str(expected_file_hash or ""),
	):
		frappe.throw("Standard source-file content no longer matches its governed snapshot")


def _materialize_rule_authority_snapshot(doc, rule) -> None:
	if rule is None:
		frappe.throw("Rule version requires a governed rule parent")
	if str(doc.get("standard_snapshot") or "") != str(rule.get("standard") or "") or str(
		doc.get("standard_clause_snapshot") or ""
	) != str(rule.get("standard_clause") or ""):
		frappe.throw("Rule version authority must match its governed rule parent")
	previous = doc.get_doc_before_save()
	previous_state = str(previous.get("status") or "Draft") if previous else "Draft"
	require_approved = str(doc.get("status") or "Draft") != "Draft"
	authority = _build_authority_snapshot(
		str(doc.get("standard_snapshot") or ""),
		str(doc.get("standard_clause_snapshot") or ""),
		require_approved=require_approved,
		effective_from=doc.get("effective_from"),
		effective_to=doc.get("effective_to"),
	)
	if previous is None or previous_state == "Draft":
		_set_authority_snapshot(doc, authority)
	else:
		_require_immutable_authority_snapshot(doc, previous, authority)


def _materialize_indicator_snapshot(doc, indicator) -> None:
	if indicator is None:
		frappe.throw("Indicator version requires a governed indicator parent")
	previous = doc.get_doc_before_save()
	previous_state = str(previous.get("status") or "Draft") if previous else "Draft"
	if previous is None or previous_state == "Draft":
		for snapshot_field, source_field in INDICATOR_SNAPSHOT_FIELD_MAP.items():
			value = indicator.get(source_field)
			if snapshot_field == "parent_formula_json_snapshot" and value not in (None, ""):
				value = _canonical_json_value(value, "indicator parent formula_json")
			doc.set(snapshot_field, value)
		if not doc.get("calculator_key") and indicator.get("calculator_key"):
			doc.set("calculator_key", indicator.get("calculator_key"))
		if not doc.get("formula_json") and indicator.get("formula_json"):
			doc.set(
				"formula_json",
				_canonical_json_value(indicator.get("formula_json"), "indicator formula_json"),
			)
		for fieldname in (
			"direction",
			"target_value",
			"target_min",
			"target_max",
			"warning_threshold",
			"critical_threshold",
		):
			if doc.get(fieldname) in (None, "") and indicator.get(fieldname) not in (None, ""):
				doc.set(fieldname, indicator.get(fieldname))
	else:
		snapshot_fields = (*INDICATOR_SNAPSHOT_FIELD_MAP, *INDICATOR_AUTHORITY_SNAPSHOT_FIELDS)
		changed = [
			fieldname for fieldname in snapshot_fields if previous.get(fieldname) != doc.get(fieldname)
		]
		if changed:
			frappe.throw(
				"Indicator identity and authority snapshots are immutable after review starts. "
				"Changed: " + ", ".join(changed)
			)
		if (
			not doc.get("indicator_code_snapshot")
			or not doc.get("indicator_name_snapshot")
			or str(doc.get("lineage_status") or "") != "Verified"
		):
			frappe.throw("Legacy indicator version lacks verified semantic snapshots; create a new version")
	for snapshot_field, source_field in INDICATOR_SNAPSHOT_FIELD_MAP.items():
		expected = indicator.get(source_field)
		if snapshot_field == "parent_formula_json_snapshot" and expected not in (None, ""):
			expected = _canonical_json_value(expected, "indicator parent formula_json")
		if str(doc.get(snapshot_field) or "") != str(expected or ""):
			frappe.throw(
				f"Indicator parent semantic drift detected for {snapshot_field}; create a new version"
			)
	authority = _build_authority_snapshot(
		str(indicator.get("standard") or ""),
		str(indicator.get("standard_clause") or ""),
		require_approved=str(doc.get("status") or "Draft") != "Draft",
		effective_from=doc.get("effective_from"),
		effective_to=doc.get("effective_to"),
	)
	if previous is None or previous_state == "Draft":
		_set_authority_snapshot(doc, authority)
	else:
		_require_immutable_authority_snapshot(doc, previous, authority)


def _set_authority_snapshot(doc, authority: dict[str, Any]) -> None:
	snapshot_json = _canonical_json_value(authority, "authority snapshot")
	doc.set("standard_snapshot", authority["standard"]["name"])
	doc.set("standard_version_snapshot", authority["standard_version"]["name"])
	doc.set("standard_clause_snapshot", authority["clause"]["name"])
	doc.set(
		"standard_version_checksum_snapshot",
		authority["standard_version"]["checksum"],
	)
	doc.set("standard_clause_hash_snapshot", authority["clause"]["content_hash"])
	doc.set("authority_snapshot_json", snapshot_json)
	doc.set("authority_snapshot_hash", hashlib.sha256(snapshot_json.encode()).hexdigest())
	doc.set("lineage_status", "Verified")


def _require_immutable_authority_snapshot(doc, previous, authority: dict[str, Any]) -> None:
	changed = [
		fieldname
		for fieldname in (
			"standard_snapshot",
			"standard_version_snapshot",
			"standard_clause_snapshot",
			"standard_version_checksum_snapshot",
			"standard_clause_hash_snapshot",
			"authority_snapshot_json",
			"authority_snapshot_hash",
			"lineage_status",
		)
		if previous.get(fieldname) != doc.get(fieldname)
	]
	if changed:
		frappe.throw("Authority snapshots are immutable after review starts. Changed: " + ", ".join(changed))
	expected_json = _canonical_json_value(authority, "authority snapshot")
	expected_hash = hashlib.sha256(expected_json.encode()).hexdigest()
	expected_values = {
		"standard_snapshot": authority["standard"]["name"],
		"standard_version_snapshot": authority["standard_version"]["name"],
		"standard_clause_snapshot": authority["clause"]["name"],
		"standard_version_checksum_snapshot": authority["standard_version"]["checksum"],
		"standard_clause_hash_snapshot": authority["clause"]["content_hash"],
		"authority_snapshot_json": expected_json,
		"authority_snapshot_hash": expected_hash,
		"lineage_status": "Verified",
	}
	if any(str(doc.get(key) or "") != str(value or "") for key, value in expected_values.items()):
		frappe.throw("Definition authority no longer matches its exact governed standard lineage")


def _build_authority_snapshot(
	standard_name: str,
	clause_name: str,
	*,
	require_approved: bool,
	effective_from,
	effective_to,
) -> dict[str, Any]:
	standard, standard_version, clause = _load_standard_authority(
		standard_name,
		clause_name,
		require_approved=require_approved,
	)
	if effective_from:
		_validate_authority_effective_interval(
			standard_version,
			effective_from=effective_from,
			effective_to=effective_to,
		)
	clause_hash = _standard_clause_content_hash(clause)
	return {
		"contract_version": 1,
		"standard": {
			"name": standard.name,
			"code": standard_version.get("standard_code_snapshot"),
			"title": standard_version.get("standard_name_snapshot"),
			"category": standard_version.get("standard_category_snapshot"),
			"source_type": standard_version.get("source_type_snapshot"),
			"source_url": standard_version.get("source_url_snapshot"),
			"source_file": standard_version.get("source_file_snapshot"),
			"source_file_hash": standard_version.get("source_file_hash"),
		},
		"standard_version": {
			"name": standard_version.name,
			"version": standard_version.get("version"),
			"checksum": standard_version.get("checksum"),
			"effective_from": standard_version.get("effective_from"),
			"effective_to": standard_version.get("effective_to"),
		},
		"clause": {
			"name": clause.name,
			"key": clause.get("clause_key"),
			"code": clause.get("clause_code"),
			"content_hash": clause_hash,
		},
	}


def _load_standard_authority(
	standard_name: str,
	clause_name: str,
	*,
	require_approved: bool,
):
	if not standard_name or not clause_name:
		frappe.throw("Exact standard and clause authority are required")
	version_name = str(frappe.db.get_value("IONE QC Standard Clause", clause_name, "standard_version") or "")
	if not version_name:
		frappe.throw("Standard clause has no exact standard-version lineage")
	_lock_standard_version(version_name)
	_lock_lineage_parent("IONE QC Standard", standard_name)
	_lock_standard_clause(clause_name)
	clause = frappe.get_doc("IONE QC Standard Clause", clause_name)
	if str(clause.get("standard_version") or "") != version_name:
		frappe.throw("Standard clause lineage changed during validation; retry the transaction")
	standard_version = frappe.get_doc("IONE QC Standard Version", version_name)
	standard = frappe.get_doc("IONE QC Standard", standard_name)
	if str(standard_version.get("standard") or "") != standard.name:
		frappe.throw("Standard clause does not belong to the selected governed standard")
	if require_approved:
		if str(standard_version.get("approval_status") or "") != "Approved":
			frappe.throw("Definition authority requires an Approved exact standard version")
		if str(standard_version.get("lineage_status") or "") != "Verified":
			frappe.throw("Definition authority standard version is not lineage-verified")
		if str(clause.get("status") or "") != "Published":
			frappe.throw("Definition authority requires a Published exact standard clause")
		if str(standard.get("status") or "") != "Published":
			frappe.throw("Definition authority standard parent is not Published")
		stored_checksum = str(standard_version.get("checksum") or "")
		computed_checksum = _content_checksum(_standard_version_content(standard_version))
		if len(stored_checksum) != 64 or not hmac.compare_digest(
			stored_checksum,
			computed_checksum,
		):
			frappe.throw("Exact standard-version checksum failed integrity verification")
		current_file_hash = _hash_standard_source_file(
			str(standard_version.get("source_file_snapshot") or ""),
			standard_name=standard.name,
		)
		if not current_file_hash or not hmac.compare_digest(
			str(standard_version.get("source_file_hash") or ""),
			str(current_file_hash or ""),
		):
			frappe.throw("Standard source-file content failed governed hash verification")
	return standard, standard_version, clause


def _validate_authority_effective_interval(
	standard_version,
	*,
	effective_from,
	effective_to,
) -> None:
	definition_start = getdate(effective_from)
	definition_end = getdate(effective_to) if effective_to else None
	authority_start = (
		getdate(standard_version.get("effective_from")) if standard_version.get("effective_from") else None
	)
	authority_end = (
		getdate(standard_version.get("effective_to")) if standard_version.get("effective_to") else None
	)
	if authority_start is None or definition_start < authority_start:
		frappe.throw("Definition effective interval starts outside its exact standard version")
	if authority_end and (definition_end is None or definition_end > authority_end):
		frappe.throw("Definition effective interval extends beyond its exact standard version")


def _standard_clause_content_hash(clause) -> str:
	payload = {fieldname: clause.get(fieldname) for fieldname in STANDARD_CLAUSE_CONTENT_FIELDS}
	return _content_checksum(payload)


def _hash_standard_source_file(file_url: str, *, standard_name: str) -> str:
	if not file_url:
		return ""
	files = frappe.get_all(
		"File",
		filters={
			"file_url": file_url,
			"attached_to_doctype": "IONE QC Standard",
			"attached_to_name": standard_name,
		},
		fields=["name", "file_name", "file_size", "file_url", "is_private"],
		order_by="name asc",
		limit_page_length=2,
	)
	if len(files) != 1:
		frappe.throw("Standard source_file must resolve to one file attached to the standard")
	file_row = files[0]
	if not int(file_row.get("is_private") or 0) or not str(file_row.get("file_url") or "").startswith(
		"/private/files/"
	):
		frappe.throw("Standard authority source_file must be a private controlled archive")
	if int(file_row.get("file_size") or 0) > MAX_SOURCE_STANDARD_FILE_BYTES:
		frappe.throw(
			f"Standard source_file exceeds the {MAX_SOURCE_STANDARD_FILE_BYTES}-byte governance limit"
		)
	file_doc = frappe.get_doc("File", file_row.name)
	try:
		content = file_doc.get_content()
	except Exception as exc:
		raise frappe.ValidationError("Standard source_file content could not be read") from exc
	if isinstance(content, str):
		content = content.encode()
	if not isinstance(content, bytes):
		frappe.throw("Standard source_file content has an unsupported representation")
	if not content:
		frappe.throw("Standard source_file must contain real archived bytes")
	if len(content) > MAX_SOURCE_STANDARD_FILE_BYTES:
		frappe.throw(
			f"Standard source_file exceeds the {MAX_SOURCE_STANDARD_FILE_BYTES}-byte governance limit"
		)
	stored_size = int(file_row.get("file_size") or 0)
	if stored_size <= 0 or stored_size != len(content):
		frappe.throw("Standard source_file size metadata does not match its archived bytes")
	file_name = str(file_row.get("file_name") or "")
	declared_media_type = {
		"pdf": "application/pdf",
		"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
		"txt": "text/plain",
	}.get(file_name.rpartition(".")[2].lower())
	if not declared_media_type:
		frappe.throw("Standard source_file must use an approved PDF, DOCX, or TXT extension")
	try:
		validate_standard_archive_content(
			content,
			declared_media_type=declared_media_type,
			final_url=str(file_row.get("file_url") or ""),
			max_bytes=MAX_SOURCE_STANDARD_FILE_BYTES,
		)
	except StandardArchiveError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	return hashlib.sha256(content).hexdigest()


def _require_archived_standard_source(standard) -> str:
	file_url = str(standard.get("source_file") or "")
	if not file_url:
		frappe.throw(
			"URL-only Standards cannot enter verified review. Archive source_url through "
			"archive_standard_source_url (or upload an attached private File) first."
		)
	content_hash = _hash_standard_source_file(file_url, standard_name=standard.name)
	if len(content_hash) != 64:
		frappe.throw("Standard private archive requires a non-empty SHA-256 of real bytes")
	return content_hash


@frappe.whitelist()
def archive_standard_source_url(standard: str) -> dict[str, str]:
	"""Fetch one allowlisted HTTPS authority URL into an attached private File."""
	doc = frappe.get_doc("IONE QC Standard", standard, for_update=True)
	doc.check_permission("write")
	if not set(frappe.get_roles(frappe.session.user)).intersection(DEFINITION_AUTHOR_ROLES):
		frappe.throw("Current user cannot archive Standard authority", frappe.PermissionError)
	if str(doc.get("status") or "Draft") != "Draft":
		frappe.throw("Only a Draft Standard may receive its initial controlled archive")
	source_url = str(doc.get("source_url") or "").strip()
	if not source_url:
		frappe.throw("Standard source_url is required for controlled archival")
	if doc.get("source_file"):
		return {
			"standard": doc.name,
			"source_file": str(doc.source_file),
			"source_file_hash": _require_archived_standard_source(doc),
		}
	payload = _download_standard_authority(source_url)
	content = payload.content
	digest = hashlib.sha256(content).hexdigest()
	file_name = f"{doc.standard_code}-{digest[:16]}.{payload.extension}"
	file_doc = save_file(
		file_name,
		content,
		"IONE QC Standard",
		doc.name,
		is_private=1,
		df="source_file",
	)
	if not int(file_doc.get("is_private") or 0) or not str(file_doc.get("file_url") or "").startswith(
		"/private/files/"
	):
		frappe.throw("Controlled Standard archive was not stored as a private File")
	doc.source_file = file_doc.file_url
	doc.save(ignore_permissions=False)
	current_hash = _require_archived_standard_source(doc)
	if not hmac.compare_digest(digest, current_hash):
		frappe.throw("Controlled Standard archive hash changed while it was attached")
	return {
		"standard": doc.name,
		"source_file": str(file_doc.file_url),
		"source_file_hash": current_hash,
	}


def _download_standard_authority(source_url: str) -> StandardArchivePayload:
	allowed_setting = frappe.conf.get("ione_standard_archive_allowed_hosts") or []
	if isinstance(allowed_setting, str):
		allowed_hosts = {value.strip().lower() for value in allowed_setting.split(",") if value.strip()}
	elif isinstance(allowed_setting, (list, tuple, set)):
		allowed_hosts = {str(value).strip().lower() for value in allowed_setting if str(value).strip()}
	else:
		allowed_hosts = set()
	if not allowed_hosts:
		frappe.throw(
			"Controlled Standard URL archival is disabled until "
			"ione_standard_archive_allowed_hosts is configured"
		)
	try:
		return download_standard_authority(
			source_url,
			allowed_hosts=allowed_hosts,
			max_bytes=MAX_SOURCE_STANDARD_FILE_BYTES,
		)
	except StandardArchiveError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def validate_standard_source_file(doc, method: str | None = None) -> None:
	"""Prevent attachment rebinding or content replacement after standard review starts."""
	del method
	previous = doc.get_doc_before_save()
	standards = _standard_source_file_parents(doc, previous)
	if not standards:
		return
	for standard in standards:
		_lock_lineage_parent("IONE QC Standard", standard)
	reviewed = [
		standard
		for standard in standards
		if frappe.db.exists(
			"IONE QC Standard Version",
			{"standard": standard, "approval_status": ["not in", ["Draft"]]},
		)
	]
	if not reviewed:
		return
	if previous is None:
		frappe.throw("A reviewed standard cannot receive a replacement source File")
	protected = (
		"file_url",
		"content_hash",
		"file_size",
		"is_private",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
	)
	changed = [fieldname for fieldname in protected if previous.get(fieldname) != doc.get(fieldname)]
	if changed:
		frappe.throw("Reviewed standard source Files are immutable. Changed: " + ", ".join(changed))


def prevent_standard_source_file_deletion(doc, method: str | None = None) -> None:
	del method
	standards = _standard_source_file_parents(doc, doc.get_doc_before_save())
	for standard in standards:
		_lock_lineage_parent("IONE QC Standard", standard)
	for standard in standards:
		if frappe.db.exists(
			"IONE QC Standard Version",
			{"standard": standard, "approval_status": ["not in", ["Draft"]]},
		):
			frappe.throw("Reviewed standard source Files cannot be optimized, replaced, or deleted")


def _standard_source_file_parents(doc, previous) -> tuple[str, ...]:
	standards: set[str] = set()
	for source in (previous, doc):
		if source is None:
			continue
		file_url = str(source.get("file_url") or "")
		if file_url:
			standards.update(
				str(name)
				for name in frappe.get_all(
					"IONE QC Standard",
					filters={"source_file": file_url},
					pluck="name",
					order_by="name asc",
					limit_page_length=2,
				)
			)
	return tuple(sorted(standards))


def _canonical_json_value(value: Any, field_label: str) -> str:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise frappe.ValidationError(f"{field_label} must be valid JSON") from exc
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _content_checksum(value: Any) -> str:
	return hashlib.sha256(
		json.dumps(
			value,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
	).hexdigest()


def _require_no_active_authority_dependents(standard_version) -> None:
	dependents = (
		(
			"IONE QC Rule Version",
			"status",
			("Expert Review", "Test", "Historical Replay", "Shadow Run", "Approval", "Published"),
		),
		("IONE QC Indicator Version", "status", ("Under Review", "Published")),
	)
	for doctype, status_field, statuses in dependents:
		if frappe.db.exists(
			doctype,
			{
				"standard_version_snapshot": standard_version.name,
				status_field: ["in", list(statuses)],
			},
		):
			frappe.throw(f"Retire active {doctype} dependents before retiring this exact standard version")


def _require_retirement_contains_authority_dependents(standard_version) -> None:
	authority_end = getdate(standard_version.get("effective_to"))
	for doctype in ("IONE QC Rule Version", "IONE QC Indicator Version"):
		rows = frappe.db.sql(
			f"""
			select
				sum(case when effective_to is null then 1 else 0 end) as open_count,
				max(effective_to) as maximum_end
			from `tab{doctype}`
			where standard_version_snapshot = %s
				and status = 'Retired'
			""",  # noqa: S608 - doctype is a closed internal allowlist
			(standard_version.name,),
			as_dict=True,
		)
		aggregate = rows[0] if rows else {}
		if int(aggregate.get("open_count") or 0):
			frappe.throw(f"Retired {doctype} authority dependents require explicit effective_to")
		maximum_end = aggregate.get("maximum_end")
		if maximum_end and getdate(maximum_end) > authority_end:
			frappe.throw(f"Standard retirement would truncate the effective interval of a retired {doctype}")


def _validate_shadow_version_contract(doc, previous) -> None:
	status = str(doc.get("status") or "Draft")
	if status not in {"Historical Replay", "Shadow Run", "Approval", "Published", "Retired"}:
		return
	from ione_qms.rule_engine.validation import validate_shadow_observation_contract

	validate_shadow_observation_contract(doc)
	if status in {"Historical Replay", "Shadow Run", "Approval"} and not int(doc.get("shadow_mode") or 0):
		frappe.throw("Historical Replay, Shadow Run, and Approval require shadow_mode enabled")
	if previous and str(previous.get("status") or "") in {
		"Historical Replay",
		"Shadow Run",
		"Approval",
		"Published",
		"Retired",
	}:
		fields = (
			"shadow_observation_start",
			"shadow_observation_end",
			"shadow_min_events",
			"shadow_min_feedback",
			"shadow_max_false_positive_rate",
			"shadow_max_false_negative_rate",
			"shadow_min_feedback_coverage_rate",
			"shadow_min_passed_feedback",
			"shadow_min_excluded_feedback",
		)
		changed = [fieldname for fieldname in fields if previous.get(fieldname) != doc.get(fieldname)]
		if changed:
			frappe.throw(
				"Online shadow observation contract is immutable after Shadow Run starts. Changed: "
				+ ", ".join(changed)
			)


def _materialize_rule_snapshot(doc, rule) -> None:
	if rule is None:
		return
	previous = doc.get_doc_before_save()
	previous_status = str(previous.get("status") or "Draft") if previous else "Draft"
	previous_rule_code = previous.get("rule_code_snapshot") if previous else None
	if previous is None:
		for snapshot_field, source_field in RULE_SNAPSHOT_FIELD_MAP.items():
			value = rule.get(source_field)
			if snapshot_field == "risk_level_snapshot" and not value:
				value = "Medium"
			doc.set(snapshot_field, value)
	elif previous_status == "Draft":
		legacy_snapshot = not any(previous.get(fieldname) for fieldname in RULE_VERSION_SNAPSHOT_FIELDS)
		fields_to_materialize = (
			RULE_VERSION_SNAPSHOT_FIELDS if legacy_snapshot else REQUIRED_RULE_SNAPSHOT_FIELDS
		)
		for snapshot_field in fields_to_materialize:
			if legacy_snapshot or not doc.get(snapshot_field):
				source_field = RULE_SNAPSHOT_FIELD_MAP[snapshot_field]
				value = rule.get(source_field)
				if snapshot_field == "risk_level_snapshot" and not value:
					value = "Medium"
				doc.set(snapshot_field, value)
		doc.set("rule_code_snapshot", rule.get("rule_code"))
	else:
		changed = [
			fieldname
			for fieldname in RULE_VERSION_SNAPSHOT_FIELDS
			if previous.get(fieldname) != doc.get(fieldname)
		]
		if changed:
			frappe.throw(
				"Rule semantic snapshots are immutable after expert review starts. Changed: "
				+ ", ".join(changed)
			)
		missing = [fieldname for fieldname in REQUIRED_RULE_SNAPSHOT_FIELDS if not doc.get(fieldname)]
		if missing:
			frappe.throw(
				"Legacy rule version lacks governed semantic snapshots; create and validate a new version. Missing: "
				+ ", ".join(sorted(missing))
			)

	if previous is None or previous_status == "Draft":
		if doc.get("rule_type_snapshot") == "Python Plugin":
			if not doc.get("plugin_key") or (
				previous is not None and doc.get("plugin_key") == previous_rule_code
			):
				doc.set("plugin_key", doc.get("rule_code_snapshot"))
		elif doc.get("rule_type_snapshot") != "Python Plugin":
			doc.set("plugin_key", None)


def _require_passing_rule_tests(doc) -> None:
	from ione_qms.rule_engine.testing import require_current_rule_test_receipts

	require_current_rule_test_receipts(doc)


def _validate_dates(doc) -> None:
	start = doc.get("effective_from")
	end = doc.get("effective_to")
	if start and end and getdate(end) < getdate(start):
		frappe.throw("effective_to cannot be earlier than effective_from")


def _validate_json_field(
	doc,
	fieldname: str,
	*,
	expected: type | tuple[type, ...],
	required: bool,
) -> Any:
	value = doc.get(fieldname)
	if value in (None, ""):
		if required:
			frappe.throw(f"{fieldname} is required")
		return None
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except ValueError as exc:
		raise frappe.ValidationError(f"{fieldname} must be valid JSON") from exc
	if not isinstance(parsed, expected):
		frappe.throw(f"{fieldname} has an invalid JSON shape")
	doc.set(
		fieldname,
		json.dumps(
			parsed,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		),
	)
	return parsed


def _protect_published_content(
	doc,
	*,
	status_field: str,
	content_fields: tuple[str, ...],
	retirement_mutable_fields: frozenset[str] = frozenset(),
) -> None:
	previous = doc.get_doc_before_save()
	if not previous or previous.get(status_field) not in {"Published", "Approved", "Retired"}:
		return
	changed = [fieldname for fieldname in content_fields if previous.get(fieldname) != doc.get(fieldname)]
	retirement_change_allowed = bool(
		changed
		and previous.get(status_field) in {"Published", "Approved"}
		and doc.get(status_field) == "Retired"
		and set(changed).issubset(retirement_mutable_fields)
		and all(
			previous.get(fieldname) in (None, "") and doc.get(fieldname) not in (None, "")
			for fieldname in changed
		)
	)
	if changed and not retirement_change_allowed:
		frappe.throw(
			"Published version content is immutable; create a new version. "
			+ "Changed: "
			+ ", ".join(changed)
		)
	allowed_statuses = {
		"Published": {"Published", "Retired"},
		"Approved": {"Approved", "Retired"},
		"Retired": {"Retired"},
	}
	if doc.get(status_field) not in allowed_statuses[previous.get(status_field)]:
		frappe.throw("A published version may only remain published or be retired")


def _require_no_effective_overlap(
	doc,
	*,
	parent_field: str,
	status_field: str,
	governed_statuses: tuple[str, ...],
) -> None:
	if not doc.get(parent_field) or not doc.get("effective_from"):
		return
	start = getdate(doc.get("effective_from"))
	end = getdate(doc.get("effective_to")) if doc.get("effective_to") else None
	rows = frappe.get_all(
		doc.doctype,
		filters={
			parent_field: doc.get(parent_field),
			status_field: ["in", list(governed_statuses)],
			"name": ["!=", doc.name],
		},
		fields=["name", "effective_from", "effective_to"],
		limit_page_length=1_001,
	)
	if len(rows) > 1_000:
		frappe.throw(
			"More than 1,000 governed versions require the bounded historical "
			"effective-period audit before this transition."
		)
	for row in rows:
		other_start = getdate(row.effective_from) if row.effective_from else None
		other_end = getdate(row.effective_to) if row.effective_to else None
		ends_before_other = end is not None and other_start is not None and end < other_start
		other_ends_before = other_end is not None and other_end < start
		if not ends_before_other and not other_ends_before:
			frappe.throw(
				f"Effective dates overlap governed version {row.name}; "
				"close the prior interval before publication"
			)


def _standard_version_content(doc) -> dict[str, Any]:
	content = _version_content(doc)
	content["standard_clauses"] = _standard_clause_hashes(doc.name)
	return content


def _standard_clause_hashes(standard_version: str) -> list[dict[str, str]]:
	if not standard_version:
		return []
	rows = frappe.get_all(
		"IONE QC Standard Clause",
		filters={"standard_version": standard_version},
		fields=["name", *STANDARD_CLAUSE_CONTENT_FIELDS],
		order_by="clause_key asc, name asc",
		limit_page_length=10_001,
	)
	if len(rows) > 10_000:
		frappe.throw(
			"A standard version with more than 10,000 clauses requires a reviewed bounded checksum process"
		)
	hashes: list[dict[str, str]] = []
	for row in rows:
		clause_content = {
			fieldname: _row_value(row, fieldname) for fieldname in STANDARD_CLAUSE_CONTENT_FIELDS
		}
		encoded = json.dumps(
			clause_content,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
		hashes.append(
			{
				"name": str(_row_value(row, "name") or ""),
				"clause_key": str(_row_value(row, "clause_key") or ""),
				"content_hash": hashlib.sha256(encoded).hexdigest(),
			}
		)
	return hashes


def _version_content(doc) -> dict[str, Any]:
	excluded = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"checksum",
		"status",
		"approval_status",
		"shadow_mode",
		"active_parent_key",
		"approved_by",
		"approved_at",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
	return {
		field.fieldname: doc.get(field.fieldname)
		for field in doc.meta.fields
		if field.fieldname not in excluded and not field.fieldtype.startswith("Section")
	}


def _set_checksum(doc, value: Any) -> None:
	if not doc.meta.has_field("checksum"):
		return
	encoded = json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	).encode()
	doc.checksum = hashlib.sha256(encoded).hexdigest()


def _validate_indicator_dimensions(dimensions: Any) -> tuple[str, ...]:
	"""Use the same governed registry and alias migration as runtime calculation."""
	return normalize_dimension_names(dimensions or [])


def _materialize_indicator_execution_contract(
	doc,
	*,
	formula: dict[str, Any] | None,
	dimensions: tuple[str, ...],
) -> None:
	"""Freeze mapping/query/calculator inputs before a version can be published."""
	calculator_key = str(doc.get("calculator_key") or "").strip().upper()
	calculator = get_indicator(calculator_key)
	if not calculator:
		frappe.throw("Published indicator versions require a registered calculator_key")
	if not formula:
		frappe.throw("Published indicator versions require formula_json")
	execution_formula = dict(formula)
	if doc.get("multiplier") not in (None, ""):
		execution_formula.setdefault("multiplier", doc.get("multiplier"))
	if doc.get("precision") not in (None, ""):
		execution_formula.setdefault("precision", doc.get("precision"))
	try:
		validated_formula = validate_formula_schema(
			execution_formula,
			expected_dimensions=list(dimensions),
		)
		calculator.validate_dimension_capability(validated_formula, dimensions)
	except ValueError as exc:
		raise frappe.ValidationError(f"Invalid indicator execution contract: {exc}") from exc
	mapping_name = str(doc.get("source_mapping") or "").strip()
	if not mapping_name:
		frappe.throw("Published indicator versions require a governed source_mapping")
	mapping = frappe.get_doc("IONE Integration Mapping", mapping_name)
	if str(mapping.get("status") or "") != "Active":
		frappe.throw("Published indicator versions require an Active source mapping")
	try:
		source_mapping_definition = mapping_definition(
			record=mapping.name,
			code=mapping.get("mapping_code"),
			version=mapping.get("version"),
			mapping=mapping.get("mapping_json"),
			master_data=mapping.get("master_data_json"),
			stored_checksum=mapping.get("checksum"),
			stored_snapshot=mapping.get("canonical_snapshot"),
		)
	except ValueError as exc:
		raise frappe.ValidationError(
			f"Published indicator source mapping is not reproducible: {exc}"
		) from exc
	mapped_targets = set(source_mapping_definition.values)
	master_fields = source_mapping_definition.master_data.get("fields")
	if isinstance(master_fields, dict):
		mapped_targets.update(master_fields)
	targets_by_dimension = {
		"hospital": {"hospital"},
		"campus": {"campus"},
		"department": {"department"},
		"ward": {"ward"},
		"medical_group": {"medical_group"},
		"physician": {"medical_staff", "responsible_staff"},
		"disease": {"disease"},
		"surgery": {"surgery"},
		"drg": {"drg"},
	}
	unmapped_dimensions = [
		dimension
		for dimension in dimensions
		if not mapped_targets.intersection(targets_by_dimension[dimension])
	]
	if unmapped_dimensions:
		frappe.throw(
			"Indicator source mapping does not resolve governed dimensions: " + ", ".join(unmapped_dimensions)
		)
	mapping_start = getdate(mapping.get("effective_from")) if mapping.get("effective_from") else None
	mapping_end = getdate(mapping.get("effective_to")) if mapping.get("effective_to") else None
	indicator_start = getdate(doc.get("effective_from")) if doc.get("effective_from") else None
	indicator_end = getdate(doc.get("effective_to")) if doc.get("effective_to") else None
	if not mapping_start or not indicator_start or indicator_start < mapping_start:
		frappe.throw("Indicator effective interval must be covered by its source mapping")
	if mapping_end and (not indicator_end or indicator_end > mapping_end):
		frappe.throw("Indicator effective interval exceeds its source mapping")
	mapping_version = str(mapping.get("version") or "").strip()
	mapping_checksum = str(mapping.get("checksum") or "").strip().lower()
	source_system = str(mapping.get("source_system") or "").strip()
	if (
		not mapping_version
		or not source_system
		or len(mapping_checksum) != 64
		or any(character not in "0123456789abcdef" for character in mapping_checksum)
	):
		frappe.throw("Indicator source mapping lacks immutable version/checksum/source lineage")
	doc.source_system_snapshot = source_system
	doc.mapping_version_snapshot = mapping_version
	doc.mapping_checksum_snapshot = mapping_checksum
	doc.query_contract_hash = governed_query_contract_hash(
		validated_formula,
		list(dimensions),
	)
	try:
		physical_contract = resolved_physical_query_contract(
			validated_formula,
			list(dimensions),
		)
	except ValueError as exc:
		raise frappe.ValidationError(
			f"Indicator physical execution contract is not deployable: {exc}"
		) from exc
	doc.physical_query_contract_json = json.dumps(
		physical_contract,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	)
	doc.physical_query_contract_hash = physical_query_contract_hash(physical_contract)
	doc.schema_signature_hash = str(physical_contract.get("schema_signature_hash") or "")
	doc.calculator_version_snapshot = str(getattr(calculator, "version", "") or "")
	doc.calculator_code_hash = calculator_code_hash(calculator)
	doc.dimensions_json = json.dumps(
		list(dimensions),
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	)
	doc.formula_json = json.dumps(
		validated_formula,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	)

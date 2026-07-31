from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

import frappe


@dataclass(frozen=True)
class IONEFlowToolSpec:
	slug: str
	title: str
	import_path: str
	requires_confirmation: bool
	description: str

	@property
	def document_values(self) -> dict[str, Any]:
		return {
			"title": self.title,
			"slug": self.slug,
			"type": "Imported",
			"enabled": 1,
			"requires_confirmation": int(self.requires_confirmation),
			"is_system_generated": 1,
			"description": self.description,
			"import_path": self.import_path,
			"code": None,
		}


IONE_FLOW_TOOL_SPECS: tuple[IONEFlowToolSpec, ...] = (
	IONEFlowToolSpec(
		"ione_search_quality_standard",
		"检索质量标准",
		"ione_qms.ai.tools.ione_search_quality_standard",
		False,
		"Search published IONE quality standards and clauses. Each result includes a task-bound evidence_reference that must be cited.",
	),
	IONEFlowToolSpec(
		"ione_get_encounter_context",
		"读取就诊质控上下文",
		"ione_qms.ai.tools.ione_get_encounter_context",
		False,
		"Read minimum authorized encounter context and return its immutable task-bound evidence_reference.",
	),
	IONEFlowToolSpec(
		"ione_get_medical_record_sections",
		"读取病历片段",
		"ione_qms.ai.tools.ione_get_medical_record_sections",
		False,
		"Read bounded authorized record sections and return immutable task-bound evidence_references.",
	),
	IONEFlowToolSpec(
		"ione_get_indicator_trend",
		"读取指标趋势",
		"ione_qms.ai.tools.ione_get_indicator_trend",
		False,
		"Read authorized indicator results; each result includes an immutable evidence_reference.",
	),
	IONEFlowToolSpec(
		"ione_get_contributing_cases",
		"读取脱敏贡献病例",
		"ione_qms.ai.tools.ione_get_contributing_cases",
		True,
		"Read bounded de-identified contributing cases, each with an immutable evidence_reference.",
	),
	IONEFlowToolSpec(
		"ione_get_quality_summary",
		"读取定时质量报告汇总",
		"ione_qms.ai.tools.ione_get_quality_summary",
		False,
		"Read the immutable aggregate-only summary bound to one approved scheduled Quality Report task and return its task-bound evidence_reference.",
	),
	IONEFlowToolSpec(
		"ione_get_rectification_context",
		"读取整改上下文",
		"ione_qms.ai.tools.ione_get_rectification_context",
		False,
		"Read one confirmed finding's bounded structured evidence and current rectification, verification, and PDCA state. Patient identifiers and clinical free text are never returned; every returned source has a task-bound evidence_reference.",
	),
	IONEFlowToolSpec(
		"ione_create_analysis_draft",
		"创建分析草稿",
		"ione_qms.ai.tools.ione_create_analysis_draft",
		True,
		"Create an analysis draft using evidence_reference receipts returned by read tools. Never creates a formal finding.",
	),
	IONEFlowToolSpec(
		"ione_create_candidate_finding",
		"创建候选问题",
		"ione_qms.ai.tools.ione_create_candidate_finding",
		True,
		"Create a candidate using task-bound clinical evidence_reference receipts for mandatory human review.",
	),
	IONEFlowToolSpec(
		"ione_create_report_draft",
		"创建质量报告草稿",
		"ione_qms.ai.tools.ione_create_report_draft",
		False,
		"Create exactly one report draft from the scheduled task's immutable aggregate summary. The draft always requires independent human review before formal use.",
	),
)

IONE_FLOW_TOOL_BY_SLUG = {spec.slug: spec for spec in IONE_FLOW_TOOL_SPECS}


def validate_ione_flow_tool(doc, method: str | None = None) -> None:
	"""Reserve every IONE tool slug and reject configuration drift."""
	del method
	spec = IONE_FLOW_TOOL_BY_SLUG.get(str(doc.get("slug") or doc.get("name") or ""))
	if not spec:
		return
	if not getattr(frappe.flags, "ione_qms_tool_maintenance", False):
		_assert_tool_document(doc, spec)


def assert_ione_tool_integrity(slugs: set[str] | frozenset[str] | None = None) -> None:
	"""Verify the live executable definition, not only the LLM-facing slug."""
	targets = sorted(slugs or IONE_FLOW_TOOL_BY_SLUG)
	for slug in targets:
		spec = IONE_FLOW_TOOL_BY_SLUG.get(slug)
		if not spec:
			frappe.throw(f"Unregistered IONE tool slug: {slug}")
		if not frappe.db.exists("Flow Tool", slug):
			frappe.throw(f"Required governed Flow Tool is missing: {slug}")
		_assert_tool_document(frappe.get_doc("Flow Tool", slug), spec)


def tool_registry_fingerprint(slugs: set[str] | frozenset[str] | None = None) -> str:
	targets = sorted(slugs or IONE_FLOW_TOOL_BY_SLUG)
	payload = [asdict(IONE_FLOW_TOOL_BY_SLUG[slug]) for slug in targets]
	return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def live_tool_schema_manifest(
	slugs: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
	"""Return the exact Flow/Pydantic schemas used to expose governed tools to a model."""
	assert_ione_tool_integrity(slugs)
	manifest: list[dict[str, Any]] = []
	for slug in sorted(slugs):
		tool = frappe.get_doc("Flow Tool", slug).to_tool()
		schema = tool.to_dict()
		function = schema.get("function") if isinstance(schema, dict) else None
		if (
			not isinstance(function, dict)
			or str(function.get("name") or "") != slug
			or not isinstance(function.get("parameters"), dict)
		):
			frappe.throw(f"Governed IONE Flow Tool {slug} returned an invalid runtime schema")
		manifest.append(schema)
	return manifest


def live_tool_schema_fingerprint(slugs: set[str] | frozenset[str]) -> str:
	return hashlib.sha256(_canonical_json(live_tool_schema_manifest(slugs)).encode()).hexdigest()


def _assert_tool_document(doc, spec: IONEFlowToolSpec) -> None:
	mismatches = [
		fieldname
		for fieldname, expected in spec.document_values.items()
		if _normalized(doc.get(fieldname)) != _normalized(expected)
	]
	if str(doc.get("name") or spec.slug) != spec.slug:
		mismatches.append("name")
	if mismatches:
		frappe.throw(
			f"Governed IONE Flow Tool {spec.slug} failed its executable integrity check. "
			"Drifted fields: " + ", ".join(sorted(set(mismatches)))
		)


def _normalized(value: Any) -> Any:
	if value in (None, ""):
		return None
	if isinstance(value, bool):
		return int(value)
	return value


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)

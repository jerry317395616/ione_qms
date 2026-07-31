from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

import frappe

from ione_qms.ai.release import (
	assert_approved_agent_release,
	flow_runtime_provider_configuration,
)
from ione_qms.ai.tool_registry import assert_ione_tool_integrity
from ione_qms.constants import (
	AGENT_FORBIDDEN_TOOL_SLUGS,
	AGENT_WRITE_TOOL_SLUGS,
	QWEN_FLOW_MODEL,
	QWEN_MODEL_ID,
)
from ione_qms.services.agent_evaluations import has_passing_agent_evaluation
from ione_qms.services.runtime_settings import (
	production_mode_enabled,
	require_ai_runtime_enabled,
)

ALLOWED_KNOWLEDGE_TOOL_SLUGS = frozenset({"search_knowledge"})
ALLOWED_TRIGGER_DOCTYPES = frozenset(
	{
		"IONE AI Analysis Task",
		"IONE Indicator Alert",
		"IONE QC Finding",
		"IONE Indicator Result",
	}
)


def validate_ione_agent(doc, method: str | None = None) -> None:
	if not _is_ione_agent(doc):
		return
	tools = {row.tool for row in doc.get("tools") or []}
	forbidden = tools.intersection(AGENT_FORBIDDEN_TOOL_SLUGS)
	if forbidden:
		frappe.throw("IONE clinical agents cannot use broad Flow tools: " + ", ".join(sorted(forbidden)))
	invalid = {
		slug for slug in tools if not slug.startswith("ione_") and slug not in ALLOWED_KNOWLEDGE_TOOL_SLUGS
	}
	if invalid:
		frappe.throw("IONE agents may only use approved domain tools: " + ", ".join(sorted(invalid)))
	if int(doc.get("max_iterations") or 0) > 12:
		frappe.throw("IONE agent max_iterations cannot exceed 12")

	policy = _get_policy_for_agent(doc.name)
	if not policy:
		return
	allowed = {row.tool for row in policy.get("allowed_tools") or []}
	if not tools.issubset(allowed.union(ALLOWED_KNOWLEDGE_TOOL_SLUGS)):
		frappe.throw("The Flow Agent tool set exceeds its IONE Agent Policy allowlist")


def validate_ione_trigger(doc, method: str | None = None) -> None:
	if not doc.get("agent") or not _agent_is_governed(doc.agent):
		return
	policy = _get_policy_for_agent(doc.agent)
	if not policy or policy.status != "Active":
		frappe.throw("An active IONE Agent Policy is required before enabling this trigger")
	if not doc.get("run_as") or doc.run_as == "Administrator":
		frappe.throw("IONE triggers require a dedicated non-Administrator service user")
	if str(doc.run_as) != str(policy.service_user or ""):
		frappe.throw("IONE trigger Run As must exactly match its policy service user")
	if "IONE Agent Service" not in frappe.get_roles(doc.run_as):
		frappe.throw("IONE trigger Run As user must have the IONE Agent Service role")
	if doc.event == "Scheduled" and not int(policy.allow_scheduled_run or 0):
		frappe.throw("This IONE Agent Policy does not allow scheduled execution")
	if doc.event == "DocType Event" and doc.target_doctype not in ALLOWED_TRIGGER_DOCTYPES:
		frappe.throw("IONE triggers may run only on approved IONE governance DocTypes")
	if doc.get("condition"):
		frappe.throw("IONE triggers cannot store arbitrary Python conditions")
	if int(doc.get("auto_approve") or 0):
		if not int(policy.allow_auto_approve or 0):
			frappe.throw("This IONE Agent Policy does not allow auto approval")
		if int(policy.contains_patient_data or 0):
			frappe.throw("Patient-data IONE triggers cannot auto-approve tool calls")
		confirmed_tools = frappe.get_all(
			"Flow Tool",
			filters={
				"name": ["in", [row.tool for row in policy.get("allowed_tools") or []]],
				"requires_confirmation": 1,
			},
			pluck="name",
		)
		if confirmed_tools:
			frappe.throw("Auto approval cannot bypass tools that require confirmation")
	if int(doc.get("enabled") or 0):
		frappe.throw(
			"Native Flow Triggers are disabled for governed IONE Agents. "
			"Create a scoped IONE AI Analysis Task through the controlled dispatcher instead."
		)


def validate_policy_runtime(policy, task=None) -> None:
	require_ai_runtime_enabled()
	from ione_qms.ai.evaluation import governed_evaluation_runtime_active

	evaluation_runtime = governed_evaluation_runtime_active()
	if policy.status != "Active" and not evaluation_runtime:
		frappe.throw("IONE Agent Policy is not active")
	if not policy.flow_agent or not frappe.db.exists("Flow Agent", policy.flow_agent):
		frappe.throw("IONE Agent Policy has no valid Flow Agent")
	if not policy.service_user or policy.service_user == "Administrator":
		frappe.throw("IONE Agent Policy requires a dedicated service user")
	if not int(frappe.db.get_value("User", policy.service_user, "enabled") or 0):
		frappe.throw("IONE Agent Policy service user is disabled")
	if "IONE Agent Service" not in frappe.get_roles(policy.service_user):
		frappe.throw("Agent service user is missing the IONE Agent Service role")
	agent = frappe.get_doc("Flow Agent", policy.flow_agent)
	if not int(agent.get("enabled") or 0):
		frappe.throw("IONE Agent Policy Flow Agent is disabled")
	validate_ione_agent(agent)
	if not agent.get("model") or not frappe.db.exists("Flow Model", agent.get("model")):
		frappe.throw("IONE Agent Policy Flow Agent has no valid Flow Model")
	model = frappe.get_cached_doc("Flow Model", agent.get("model"))
	if not int(model.get("enabled") or 0):
		frappe.throw("IONE Agent Policy Flow Model is disabled")
	_validate_qwen_model_configuration(model)
	agent_tools = {row.tool for row in agent.get("tools") or []}
	assert_ione_tool_integrity(agent_tools)
	release = assert_approved_agent_release(policy, agent, model)
	if (
		production_mode_enabled()
		and not evaluation_runtime
		and not has_passing_agent_evaluation(policy, release)
	):
		frappe.throw("Production AI execution requires a current passing Agent Evaluation")
	if int(policy.get("allow_auto_approve") or 0) and agent_tools.intersection(AGENT_WRITE_TOOL_SLUGS):
		frappe.throw("IONE policies cannot auto-approve tools that create governed artifacts")
	if task:
		_validate_policy_scope(policy, task)
		if int(policy.contains_patient_data or 0) or task.get("patient") or task.get("encounter"):
			_validate_local_model(agent.model)
		if int(task.get("record_count") or 0) > int(policy.maximum_records_per_run or 1):
			frappe.throw("AI task exceeds the policy maximum_records_per_run")


def _validate_local_model(flow_model: str) -> None:
	model = frappe.get_cached_doc("Flow Model", flow_model)
	provider_configuration = flow_runtime_provider_configuration(model)
	active_provider = (
		provider_configuration
		if provider_configuration and provider_configuration["exists"] and provider_configuration["enabled"]
		else None
	)
	base_url = model.get("base_url") or (active_provider["base_url"] if active_provider else None)
	if not base_url or not is_private_model_url(base_url):
		frappe.throw("Sensitive medical data may only use an approved internal model endpoint")


def _validate_qwen_model_configuration(model) -> None:
	if str(model.name) != QWEN_FLOW_MODEL:
		frappe.throw(f"IONE Agent Policies must use the reviewed {QWEN_FLOW_MODEL} model")
	if str(model.get("model_id") or "") != QWEN_MODEL_ID:
		frappe.throw(f"{QWEN_FLOW_MODEL} must use the exact reviewed model_id")
	provider_configuration = flow_runtime_provider_configuration(model)
	provider = None
	if provider_configuration and provider_configuration["exists"]:
		provider = frappe.get_cached_doc("Flow Provider", provider_configuration["name"])
	if provider and not provider_configuration["enabled"]:
		frappe.throw(f"{QWEN_FLOW_MODEL} provider is disabled")
	base_url = model.get("base_url") or (provider.get("base_url") if provider else None)
	if not base_url or not is_private_model_url(str(base_url)):
		frappe.throw(f"{QWEN_FLOW_MODEL} must use an approved private allowlisted URL")
	api_key = _document_password(model, "api_key")
	if not api_key and provider:
		api_key = _document_password(provider, "api_key")
	if not api_key:
		frappe.throw(f"{QWEN_FLOW_MODEL} requires an authentication key")


def _validate_policy_scope(policy, task) -> None:
	if str(task.get("task_type") or "") != str(policy.get("agent_category") or ""):
		frappe.throw("AI task type does not match its active policy")
	hospital = str(task.get("hospital") or "")
	campus = str(task.get("campus") or "")
	department = str(task.get("department") or "")
	ward = str(task.get("ward") or "")
	category = str(policy.get("agent_category") or "")
	contains_patient_data = bool(
		int(policy.get("contains_patient_data") or 0)
		or task.get("patient")
		or task.get("encounter")
		or task.get("data_classification") == "Sensitive Medical"
	)
	allowed_hospitals = _policy_scope_values(policy, "allowed_hospitals", "hospital")
	allowed_departments = _policy_scope_values(policy, "allowed_departments", "department")
	requires_hospital = contains_patient_data or bool(hospital) or category != "Policy Governance"
	requires_department = (
		contains_patient_data
		or bool(department)
		or category
		in {
			"Medical Record QC",
			"Indicator Analysis",
			"Rectification",
			"Quality Analysis",
			"Special Topic",
		}
	)
	if requires_hospital:
		if not hospital:
			frappe.throw("This AI task category requires an explicit hospital scope")
		if not allowed_hospitals or hospital not in allowed_hospitals:
			frappe.throw("AI task hospital is outside the policy allowlist")
	if requires_department:
		if not department:
			frappe.throw("This AI task category requires an explicit department scope")
		if not allowed_departments or department not in allowed_departments:
			frappe.throw("AI task department is outside the policy allowlist")
	_validate_scope_hierarchy(
		hospital=hospital,
		campus=campus,
		department=department,
		ward=ward,
	)


def _validate_scope_hierarchy(
	*,
	hospital: str,
	campus: str,
	department: str,
	ward: str,
) -> None:
	hospital_doc = _active_scope_doc("IONE Hospital", hospital) if hospital else None
	campus_doc = _active_scope_doc("IONE Hospital Campus", campus) if campus else None
	department_doc = _active_scope_doc("IONE Medical Department", department) if department else None
	ward_doc = _active_scope_doc("IONE Ward", ward) if ward else None
	if campus_doc:
		if not hospital_doc or campus_doc.get("hospital") != hospital:
			frappe.throw("AI task campus does not belong to its hospital")
	if department_doc:
		if not hospital_doc or department_doc.get("hospital") != hospital:
			frappe.throw("AI task department does not belong to its hospital")
		department_campus = str(department_doc.get("campus") or "")
		if department_campus and department_campus != campus:
			frappe.throw("AI task department does not belong to its campus")
		if campus and not department_campus:
			frappe.throw("AI task department has no reviewed membership in its campus")
	if ward_doc:
		for fieldname, expected in (
			("hospital", hospital),
			("campus", campus),
			("department", department),
		):
			actual = str(ward_doc.get(fieldname) or "")
			if not expected or actual != expected:
				frappe.throw(f"AI task ward does not belong to its {fieldname}")


def _active_scope_doc(doctype: str, name: str):
	if not frappe.db.exists(doctype, name):
		frappe.throw(f"AI task references an unknown {doctype}")
	doc = frappe.get_cached_doc(doctype, name)
	if doc.meta.has_field("status") and doc.get("status") != "Active":
		frappe.throw(f"AI task references an inactive {doctype}")
	return doc


def _policy_scope_values(policy, table_field: str, value_field: str) -> frozenset[str]:
	return frozenset(
		str(row.get(value_field) or "") for row in policy.get(table_field) or [] if row.get(value_field)
	)


def _document_password(doc, fieldname: str) -> str | None:
	if not doc.meta.has_field(fieldname) or not doc.get(fieldname):
		return None
	try:
		return doc.get_password(fieldname, raise_exception=False)
	except Exception:
		return None


def is_private_model_url(url: str) -> bool:
	parsed = urlparse(url)
	if (
		parsed.scheme not in {"http", "https"}
		or not parsed.hostname
		or parsed.username
		or parsed.password
		or parsed.query
		or parsed.fragment
	):
		return False
	host = parsed.hostname.lower()
	allowed = _allowed_model_hosts()
	if host not in allowed:
		return False
	if host in {"localhost", "host.docker.internal"} or host.endswith(".internal"):
		return True
	try:
		return _is_approved_private_address(ipaddress.ip_address(host))
	except ValueError:
		return _hostname_resolves_only_to_private_addresses(
			host,
			parsed.port or (443 if parsed.scheme == "https" else 80),
		)


def _hostname_resolves_only_to_private_addresses(host: str, port: int) -> bool:
	"""Reject public/mixed DNS answers before a governed model endpoint is used."""
	try:
		answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	except OSError:
		return False
	addresses = {str(answer[4][0]).split("%", 1)[0] for answer in answers if answer[4]}
	if not addresses:
		return False
	try:
		return all(_is_approved_private_address(ipaddress.ip_address(value)) for value in addresses)
	except ValueError:
		return False


def _is_approved_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
	if address.is_loopback:
		return True
	return address.is_private and not (
		address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved
	)


def _allowed_model_hosts() -> frozenset[str]:
	if not frappe.db.exists("DocType", "IONE AI Settings"):
		return frozenset()
	settings = frappe.get_single("IONE AI Settings")
	raw = settings.get("allowed_model_hosts") or "[]"
	try:
		values = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		values = []
	return frozenset(str(value).strip().lower() for value in values if str(value).strip())


def _is_ione_agent(doc) -> bool:
	return str(doc.get("title") or doc.get("name") or "").startswith("IONE ")


def _agent_is_governed(agent: str) -> bool:
	return bool(_get_policy_for_agent(agent)) or str(agent).startswith("IONE ")


def _get_policy_for_agent(agent: str):
	if not frappe.db.exists("DocType", "IONE Agent Policy"):
		return None
	name = frappe.db.get_value(
		"IONE Agent Policy",
		{"flow_agent": agent, "status": ["in", ["Active", "Draft"]]},
		"name",
		order_by="modified desc",
	)
	return frappe.get_cached_doc("IONE Agent Policy", name) if name else None

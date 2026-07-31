from __future__ import annotations

import frappe

from ione_qms.ai.governance import validate_ione_agent, validate_policy_runtime
from ione_qms.constants import AGENT_WRITE_TOOL_SLUGS
from ione_qms.services.agent_evaluations import has_passing_agent_evaluation
from ione_qms.services.runtime_settings import production_mode_enabled


def validate_agent_policy(doc, method: str | None = None) -> None:
	if int(doc.get("maximum_records_per_run") or 1) not in range(1, 1001):
		frappe.throw("maximum_records_per_run must be between 1 and 1,000")
	if not doc.get("flow_agent"):
		return
	agent = frappe.get_doc("Flow Agent", doc.flow_agent)
	if not str(agent.get("title") or agent.name).startswith("IONE "):
		frappe.throw("IONE Agent Policy can only govern an IONE Flow Agent")
	validate_ione_agent(agent)
	agent_tools = {row.tool for row in agent.get("tools") or []}
	allowed_tools = {row.tool for row in doc.get("allowed_tools") or []}
	if not agent_tools.issubset(allowed_tools):
		frappe.throw("Agent tools must be a subset of the policy allowlist")
	if int(doc.get("allow_auto_approve") or 0) and allowed_tools.intersection(AGENT_WRITE_TOOL_SLUGS):
		frappe.throw("Policies with artifact-writing tools cannot auto-approve")
	if int(doc.get("contains_patient_data") or 0) and not int(doc.get("requires_human_review") or 0):
		frappe.throw("Patient-data agent policies require human review")
	if doc.get("status") != "Active":
		return
	if not int(agent.get("enabled") or 0):
		frappe.throw("The governed Flow Agent must be enabled before policy activation")
	validate_policy_runtime(doc)
	if production_mode_enabled() and not has_passing_agent_evaluation(doc):
		frappe.throw("Production policy activation requires a passing Agent Evaluation")

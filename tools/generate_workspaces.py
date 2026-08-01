from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ione_qms"
TIMESTAMP = "2026-07-29 00:00:00.000000"

WORKSPACES: tuple[dict[str, Any], ...] = (
	{
		"label": "IONE Foundation",
		"module": "IONE Foundation",
		"icon": "building-2",
		"roles": [
			"IONE QC Administrator",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Physician",
			"IONE Integration Administrator",
			"IONE Integration Operator",
			"IONE QMS Auditor",
			"IONE PHI Identity Reader",
		],
		"sections": {
			"Organization": [
				"IONE Hospital",
				"IONE Hospital Campus",
				"IONE Medical Department",
				"IONE Ward",
			],
			"People and Authorization": [
				"IONE Medical Staff",
				"IONE Staff Qualification",
				"IONE Surgery Authorization",
				"IONE Surgery Procedure Policy",
			],
			"Clinical Index": [
				"IONE Patient Index",
				"IONE Encounter Index",
				"IONE Diagnosis Procedure Mapping",
			],
		},
	},
	{
		"label": "IONE Quality Standards",
		"module": "IONE Quality Standards",
		"icon": "scale",
		"roles": [
			"IONE QC Administrator",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Physician",
			"IONE QMS Auditor",
		],
		"sections": {
			"Standards": [
				"IONE QC Standard",
				"IONE QC Standard Version",
				"IONE QC Standard Clause",
			],
			"Rules": [
				"IONE QC Rule",
				"IONE QC Rule Version",
				"IONE QC Rule Test Case",
				"IONE QC Rule Validation Run",
			],
		},
	},
	{
		"label": "IONE Clinical Quality",
		"module": "IONE Clinical Quality",
		"icon": "shield-check",
		"roles": [
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Physician",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Medical Record Coder",
			"IONE Medical Record Expert Reviewer",
			"IONE Integration Operator",
			"IONE QMS Auditor",
		],
		"sections": {
			"Quality Workflow": [
				"IONE QC Finding",
				"IONE QC Finding Evidence",
				"IONE QC Execution",
			],
			"Clinical Topics": [
				"IONE Medical Record QC",
				"IONE Surgery QC",
				"IONE Medical Safety Event",
			],
			"Surgery Governance": [
				"IONE Surgery MDT Record",
				"IONE Surgery MDT Participant",
				"IONE Surgery Safety Checklist",
				"IONE Surgery Emergency Exception",
			],
			"Medical Record Review Governance": [
				"IONE Medical Record Completeness Watermark",
				"IONE Medical Record Sampling Policy",
				"IONE Medical Record Sampling Policy Operation",
				"IONE Medical Record Review Batch",
			],
			"Medical Record Review": [
				"IONE Medical Record Review Assignment",
				"IONE Medical Record Review Decision",
				"IONE Medical Record Archive Decision",
				"IONE Medical Record Archive Acknowledgement",
				"IONE Medical Record Archive Delivery",
			],
			"Event Intake": [
				{
					"label": "医疗安全事件上报",
					"link_to": "ione-safety-event-report",
					"link_type": "Page",
				},
				"IONE Clinical Quality Event",
			],
		},
	},
	{
		"label": "IONE Indicators",
		"module": "IONE Indicators",
		"icon": "chart-line",
		"roles": [
			"IONE QC Administrator",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Auditor",
		],
		"sections": {
			"Definitions": ["IONE QC Indicator", "IONE QC Indicator Version"],
			"Calculation and Results": [
				"IONE Indicator Calculation",
				"IONE Indicator Result Pointer",
				"IONE Indicator Result",
				"IONE Indicator Result Detail",
				"IONE Indicator Alert",
			],
		},
	},
	{
		"label": "IONE Improvement",
		"module": "IONE Improvement",
		"icon": "rotate-ccw",
		"roles": [
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Physician",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Auditor",
		],
		"sections": {
			"Closed-loop Improvement": [
				"IONE QC Finding Appeal",
				"IONE QC Rectification",
				"IONE QC Verification",
				"IONE PDCA Project",
			],
			"Recurrence Governance": [
				"IONE Finding Recurrence Policy",
				"IONE Finding Recurrence Run",
				"IONE Finding Recurrence Evaluation",
			],
			"Quality Meetings": [
				"IONE Quality Meeting",
				"IONE Meeting Minute",
				"IONE Meeting Decision",
			],
			"Actions and Experience": [
				{
					"label": "Quality Action Workbench",
					"link_to": "ione-quality-action-workbench",
					"link_type": "Page",
				},
				"IONE Quality Action Item",
				"IONE Quality Action Verification Round",
				"IONE Quality Experience Share",
			],
		},
	},
	{
		"label": "IONE Integration",
		"module": "IONE Quality Integration",
		"icon": "plug",
		"roles": [
			"IONE Integration Administrator",
			"IONE Integration Operator",
			"IONE QC Administrator",
			"IONE Medical Affairs",
			"IONE QMS Auditor",
		],
		"sections": {
			"Configuration": [
				"IONE Source System",
				"IONE Integration Endpoint",
				"IONE Integration Mapping",
				"IONE Source Document Locator",
			],
			"Operations": [
				"IONE Integration Job",
				"IONE Integration Message",
				"IONE Data Quality Issue",
				"IONE Data Reconciliation",
				"IONE Source Document Access Log",
			],
		},
	},
	{
		"label": "IONE Flow AI",
		"module": "IONE Flow AI",
		"icon": "bot",
		"roles": [
			"IONE Agent Administrator",
			"IONE Agent Reviewer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE QMS Auditor",
		],
		"sections": {
			"Governance": [
				"IONE Agent Policy",
				"IONE Agent Release",
				"IONE Agent Evaluation Threshold Policy",
				"IONE Agent Test Case",
				"IONE Agent Evaluation",
				"IONE Agent Incident",
				"IONE AI Report Schedule",
			],
			"Tasks and Human Review": [
				"IONE AI Analysis Task",
				"IONE AI Tool Approval",
				"IONE AI Candidate Finding",
				"IONE AI Report Draft",
			],
			"Audit": [
				"IONE Flow Run Link",
				"IONE AI Data Access Log",
				"IONE Quality Report Snapshot",
				"IONE AI Report Recovery Authorization",
			],
			"Flow Runtime": ["Flow Agent", "Flow Model"],
		},
	},
	{
		"label": "IONE Analytics",
		"module": "IONE Quality Analytics",
		"icon": "layout-dashboard",
		"roles": [
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Physician",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Medical Record Coder",
			"IONE Medical Record Expert Reviewer",
			"IONE Agent Reviewer",
			"IONE QMS Auditor",
		],
		"sections": {
			"Command Center": [
				{
					"label": "IONE Quality Command Center",
					"link_to": "ione-quality-command-center",
					"link_type": "Page",
				},
				{
					"label": "医疗质量业务工作台",
					"link_to": "ione-quality-workbench",
					"link_type": "Page",
				},
			],
			"Operational Reports": [
				{
					"label": "IONE Rule Quality",
					"link_to": "IONE Rule Quality",
					"link_type": "Report",
				},
				{
					"label": "IONE Indicator Trend and Quality Profile",
					"link_to": "IONE Indicator Trend and Quality Profile",
					"link_type": "Report",
				},
				{
					"label": "IONE Finding Distribution and Closure",
					"link_to": "IONE Finding Distribution and Closure",
					"link_type": "Report",
				},
				{
					"label": "IONE Integration Reconciliation",
					"link_to": "IONE Integration Reconciliation",
					"link_type": "Report",
				},
				{
					"label": "IONE AI Usage and Adoption",
					"link_to": "IONE AI Usage and Adoption",
					"link_type": "Report",
				},
				{
					"label": "IONE Medical Record Review Governance",
					"link_to": "IONE Medical Record Review Governance",
					"link_type": "Report",
				},
				{
					"label": "IONE Surgery Governance and Outcomes",
					"link_to": "IONE Surgery Governance and Outcomes",
					"link_type": "Report",
				},
				{
					"label": "IONE 2026 National Ten-Goal Progress",
					"link_to": "IONE 2026 National Ten-Goal Progress",
					"link_type": "Report",
				},
				{
					"label": "IONE Improvement Governance Overview",
					"link_to": "IONE Improvement Governance Overview",
					"link_type": "Report",
				},
			],
			"Governed Facts": [
				"IONE Daily Quality Fact",
				"IONE Monthly Quality Fact",
				"IONE Finding Analysis Fact",
				"IONE Surgery Quality Fact",
				"IONE Agent Analysis Fact",
			],
		},
	},
	{
		"label": "IONE Administration",
		"module": "IONE Administration",
		"icon": "settings",
		"roles": [
			"IONE QC Administrator",
			"IONE Agent Administrator",
			"IONE Integration Operator",
			"IONE Medical Affairs",
			"IONE QMS Auditor",
		],
		"sections": {
			"Settings": [
				"IONE System Settings",
				"IONE QC Settings",
				"IONE Integration Settings",
				"IONE AI Settings",
				"IONE Security Policy",
				"IONE Feature Flag",
			],
			"Controlled Operations": [
				"IONE Data Export Request",
				"IONE Release Record",
				"IONE Production Readiness Assessment",
				"IONE Production Activation Event",
			],
			"Privacy Governance": [
				"IONE PHI Disclosure Policy",
				"IONE PHI Access Receipt",
			],
		},
	},
)


def scrub(value: str) -> str:
	return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _sidebar_items(sections: dict[str, list[Any]]) -> list[dict[str, Any]]:
	items: list[dict[str, Any]] = []
	for section, links in sections.items():
		items.append(
			{
				"child": 0,
				"collapsible": 1,
				"indent": 1,
				"keep_closed": 0,
				"label": section,
				"link_type": "DocType",
				"open_in_new_tab": 0,
				"show_arrow": 0,
				"type": "Section Break",
			}
		)
		for entry in links:
			link = (
				{"label": entry, "link_to": entry, "link_type": "DocType"}
				if isinstance(entry, str)
				else entry
			)
			items.append(
				{
					"child": 1,
					"collapsible": 0,
					"indent": 0,
					"keep_closed": 0,
					"label": link["label"],
					"link_to": link["link_to"],
					"link_type": link["link_type"],
					"open_in_new_tab": 0,
					"show_arrow": 0,
					"type": "Link",
				}
			)
	return items


def generate() -> None:
	for index, definition in enumerate(WORKSPACES, start=1):
		module_dir = PACKAGE / scrub(definition["module"])
		workspace_dir = module_dir / "workspace" / scrub(definition["label"])
		workspace_dir.mkdir(parents=True, exist_ok=True)
		(module_dir / "workspace" / "__init__.py").touch(exist_ok=True)
		payload = {
			"app": "ione_qms",
			"charts": [],
			"content": "[]",
			"creation": TIMESTAMP,
			"custom_blocks": [],
			"docstatus": 0,
			"doctype": "Workspace",
			"for_user": "",
			"hide_custom": 0,
			"icon": definition["icon"],
			"idx": 0,
			"indicator_color": "blue",
			"is_hidden": 0,
			"label": definition["label"],
			"link_type": "DocType",
			"links": [],
			"modified": TIMESTAMP,
			"modified_by": "Administrator",
			"module": definition["module"],
			"name": definition["label"],
			"number_cards": [],
			"owner": "Administrator",
			"public": 1,
			"quick_lists": [],
			"roles": [{"role": role} for role in definition["roles"]],
			"sequence_id": float(80 + index),
			"shortcuts": [],
			"sidebar_items": _sidebar_items(definition["sections"]),
			"standard": 1,
			"title": definition["label"],
			"type": "Workspace",
		}
		(workspace_dir / f"{scrub(definition['label'])}.json").write_text(
			json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
			encoding="utf-8",
		)
	print(f"Generated {len(WORKSPACES)} IONE QMS workspaces.")


if __name__ == "__main__":
	generate()

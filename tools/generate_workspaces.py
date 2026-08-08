from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ione_qms"
TIMESTAMP = "2026-07-29 00:00:00.000000"

NAVIGATION_ORDER = {
	"IONE Analytics": 81.0,
	"IONE Clinical Quality": 82.0,
	"IONE Improvement": 83.0,
	"IONE Indicators": 84.0,
	"IONE Quality Standards": 85.0,
	"IONE Flow AI": 86.0,
	"IONE Integration": 87.0,
	"IONE Foundation": 88.0,
	"IONE Administration": 89.0,
}

MENU_LABELS = {
	"IONE Hospital": "医院",
	"IONE Hospital Campus": "院区",
	"IONE Medical Department": "科室",
	"IONE Ward": "病区",
	"IONE Medical Staff": "医务人员",
	"IONE Staff Qualification": "人员资质",
	"IONE Surgery Authorization": "手术授权",
	"IONE Surgery Procedure Policy": "手术项目管理策略",
	"IONE Patient Index": "患者索引",
	"IONE Encounter Index": "就诊索引",
	"IONE Diagnosis Procedure Mapping": "诊断与操作映射",
	"IONE QC Standard": "质量标准",
	"IONE QC Standard Version": "标准版本",
	"IONE QC Standard Clause": "标准条款",
	"IONE QC Rule": "质控规则",
	"IONE QC Rule Version": "规则版本",
	"IONE QC Rule Test Case": "规则测试用例",
	"IONE QC Rule Validation Run": "规则验证任务",
	"IONE QC Finding": "质控问题",
	"IONE QC Finding Evidence": "质控问题证据",
	"IONE QC Execution": "质控执行记录",
	"IONE Medical Record QC": "病历质控",
	"IONE Surgery QC": "手术质控",
	"IONE Medical Safety Event": "医疗安全事件",
	"IONE Surgery MDT Record": "术前多学科讨论记录",
	"IONE Surgery MDT Participant": "多学科讨论参与人员",
	"IONE Surgery Safety Checklist": "手术安全核查",
	"IONE Surgery Emergency Exception": "急诊手术例外",
	"IONE Medical Record Completeness Watermark": "病历数据完整性水位",
	"IONE Medical Record Sampling Policy": "病历抽样策略",
	"IONE Medical Record Sampling Policy Operation": "抽样策略操作记录",
	"IONE Medical Record Review Batch": "病历评审批次",
	"IONE Medical Record Review Assignment": "病历评审任务",
	"IONE Medical Record Review Decision": "病历评审结论",
	"IONE Medical Record Archive Decision": "病历归档决定",
	"IONE Medical Record Archive Acknowledgement": "病历归档确认",
	"IONE Medical Record Archive Delivery": "病历归档交付",
	"IONE Clinical Quality Event": "临床质量事件",
	"IONE QC Indicator": "质量指标",
	"IONE QC Indicator Version": "指标版本",
	"IONE Indicator Calculation": "指标计算任务",
	"IONE Indicator Result Pointer": "指标结果索引",
	"IONE Indicator Result": "指标结果",
	"IONE Indicator Result Detail": "指标结果明细",
	"IONE Indicator Alert": "指标预警",
	"IONE QC Finding Appeal": "质控问题申诉",
	"IONE QC Rectification": "整改任务",
	"IONE QC Verification": "整改复核",
	"IONE PDCA Project": "持续改进项目",
	"IONE Finding Recurrence Policy": "重复问题识别策略",
	"IONE Finding Recurrence Run": "重复问题扫描记录",
	"IONE Finding Recurrence Evaluation": "重复问题评估",
	"IONE Quality Meeting": "质量会议",
	"IONE Meeting Minute": "会议纪要",
	"IONE Meeting Decision": "会议决议",
	"IONE Quality Action Item": "质量行动项",
	"IONE Quality Action Verification Round": "行动验证轮次",
	"IONE Quality Experience Share": "质量经验分享",
	"IONE Source System": "源系统",
	"IONE Integration Endpoint": "接口配置",
	"IONE Integration Mapping": "数据映射",
	"IONE Source Document Locator": "源文档定位",
	"IONE Integration Job": "集成任务",
	"IONE Integration Message": "集成消息",
	"IONE Data Quality Issue": "数据质量问题",
	"IONE Data Reconciliation": "数据对账",
	"IONE Source Document Access Log": "源文档访问日志",
	"IONE Agent Policy": "智能助手策略",
	"IONE Agent Release": "智能助手发布",
	"IONE Agent Evaluation Threshold Policy": "评测阈值策略",
	"IONE Agent Test Case": "评测用例",
	"IONE Agent Evaluation": "评测结果",
	"IONE Agent Incident": "智能助手事件",
	"IONE AI Report Schedule": "智能报告计划",
	"IONE AI Analysis Task": "智能分析任务",
	"IONE AI Tool Approval": "工具调用审批",
	"IONE AI Candidate Finding": "智能候选问题",
	"IONE AI Report Draft": "智能报告草稿",
	"IONE Flow Run Link": "智能运行关联",
	"IONE AI Data Access Log": "智能数据访问日志",
	"IONE Quality Report Snapshot": "质量报告快照",
	"IONE AI Report Recovery Authorization": "报告恢复授权",
	"Flow Agent": "智能助手配置",
	"Flow Model": "大模型配置",
	"IONE Daily Quality Fact": "日质量底账",
	"IONE Monthly Quality Fact": "月质量底账",
	"IONE Finding Analysis Fact": "问题分析底账",
	"IONE Surgery Quality Fact": "手术质量底账",
	"IONE Agent Analysis Fact": "智能分析底账",
	"IONE System Settings": "系统设置",
	"IONE QC Settings": "质控设置",
	"IONE Integration Settings": "集成设置",
	"IONE AI Settings": "智能质控设置",
	"IONE Security Policy": "安全策略",
	"IONE Feature Flag": "功能开关",
	"IONE Data Export Request": "数据导出申请",
	"IONE Release Record": "发布记录",
	"IONE Production Readiness Assessment": "生产就绪评估",
	"IONE Production Activation Event": "生产启用事件",
	"IONE PHI Disclosure Policy": "患者敏感信息披露策略",
	"IONE PHI Access Receipt": "患者敏感信息访问回执",
}

WORKSPACES: tuple[dict[str, Any], ...] = (
	{
		"name": "IONE Foundation",
		"label": "基础资料",
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
			"组织机构": [
				"IONE Hospital",
				"IONE Hospital Campus",
				"IONE Medical Department",
				"IONE Ward",
			],
			"人员与授权": [
				"IONE Medical Staff",
				"IONE Staff Qualification",
				"IONE Surgery Authorization",
				"IONE Surgery Procedure Policy",
			],
			"临床索引": [
				"IONE Patient Index",
				"IONE Encounter Index",
				"IONE Diagnosis Procedure Mapping",
			],
		},
	},
	{
		"name": "IONE Quality Standards",
		"label": "标准规则",
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
			"质量标准": [
				"IONE QC Standard",
				"IONE QC Standard Version",
				"IONE QC Standard Clause",
			],
			"质控规则": [
				"IONE QC Rule",
				"IONE QC Rule Version",
				"IONE QC Rule Test Case",
				"IONE QC Rule Validation Run",
			],
		},
	},
	{
		"name": "IONE Clinical Quality",
		"label": "临床质控",
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
			"质控流程": [
				"IONE QC Finding",
				"IONE QC Finding Evidence",
				"IONE QC Execution",
			],
			"专项质控": [
				"IONE Medical Record QC",
				"IONE Surgery QC",
				"IONE Medical Safety Event",
			],
			"手术管理": [
				"IONE Surgery MDT Record",
				"IONE Surgery MDT Participant",
				"IONE Surgery Safety Checklist",
				"IONE Surgery Emergency Exception",
			],
			"病历评审管理": [
				"IONE Medical Record Completeness Watermark",
				"IONE Medical Record Sampling Policy",
				"IONE Medical Record Sampling Policy Operation",
				"IONE Medical Record Review Batch",
			],
			"病历评审": [
				"IONE Medical Record Review Assignment",
				"IONE Medical Record Review Decision",
				"IONE Medical Record Archive Decision",
				"IONE Medical Record Archive Acknowledgement",
				"IONE Medical Record Archive Delivery",
			],
			"事件上报": [
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
		"name": "IONE Indicators",
		"label": "指标管理",
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
			"指标定义": ["IONE QC Indicator", "IONE QC Indicator Version"],
			"计算与结果": [
				"IONE Indicator Calculation",
				"IONE Indicator Result Pointer",
				"IONE Indicator Result",
				"IONE Indicator Result Detail",
				"IONE Indicator Alert",
			],
		},
	},
	{
		"name": "IONE Improvement",
		"label": "持续改进",
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
			"整改闭环": [
				"IONE QC Finding Appeal",
				"IONE QC Rectification",
				"IONE QC Verification",
				"IONE PDCA Project",
			],
			"重复问题治理": [
				"IONE Finding Recurrence Policy",
				"IONE Finding Recurrence Run",
				"IONE Finding Recurrence Evaluation",
			],
			"质量会议": [
				"IONE Quality Meeting",
				"IONE Meeting Minute",
				"IONE Meeting Decision",
			],
			"行动与经验": [
				{
					"label": "质量行动工作台",
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
		"name": "IONE Integration",
		"label": "数据集成",
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
			"接入配置": [
				"IONE Source System",
				"IONE Integration Endpoint",
				"IONE Integration Mapping",
				"IONE Source Document Locator",
			],
			"运行监控": [
				"IONE Integration Job",
				"IONE Integration Message",
				"IONE Data Quality Issue",
				"IONE Data Reconciliation",
				"IONE Source Document Access Log",
			],
		},
	},
	{
		"name": "IONE Flow AI",
		"label": "智能质控",
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
			"智能治理": [
				"IONE Agent Policy",
				"IONE Agent Release",
				"IONE Agent Evaluation Threshold Policy",
				"IONE Agent Test Case",
				"IONE Agent Evaluation",
				"IONE Agent Incident",
				"IONE AI Report Schedule",
			],
			"任务与人工审核": [
				"IONE AI Analysis Task",
				"IONE AI Tool Approval",
				"IONE AI Candidate Finding",
				"IONE AI Report Draft",
			],
			"审计追踪": [
				"IONE Flow Run Link",
				"IONE AI Data Access Log",
				"IONE Quality Report Snapshot",
				"IONE AI Report Recovery Authorization",
			],
			"运行配置": ["Flow Agent", "Flow Model"],
		},
	},
	{
		"name": "IONE Analytics",
		"label": "质量总览",
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
			"工作台": [
				{
					"label": "医疗质量驾驶舱",
					"link_to": "ione-quality-command-center",
					"link_type": "Page",
				},
				{
					"label": "医疗质量工作台",
					"link_to": "ione-quality-workbench",
					"link_type": "Page",
				},
			],
			"管理报表": [
				{
					"label": "规则运行质量",
					"link_to": "IONE Rule Quality",
					"link_type": "Report",
				},
				{
					"label": "指标趋势与质量画像",
					"link_to": "IONE Indicator Trend and Quality Profile",
					"link_type": "Report",
				},
				{
					"label": "问题分布与闭环",
					"link_to": "IONE Finding Distribution and Closure",
					"link_type": "Report",
				},
				{
					"label": "数据集成对账",
					"link_to": "IONE Integration Reconciliation",
					"link_type": "Report",
				},
				{
					"label": "智能质控使用分析",
					"link_to": "IONE AI Usage and Adoption",
					"link_type": "Report",
				},
				{
					"label": "病历评审治理",
					"link_to": "IONE Medical Record Review Governance",
					"link_type": "Report",
				},
				{
					"label": "手术治理与结局",
					"link_to": "IONE Surgery Governance and Outcomes",
					"link_type": "Report",
				},
				{
					"label": "2026年国家质量目标进展",
					"link_to": "IONE 2026 National Ten-Goal Progress",
					"link_type": "Report",
				},
				{
					"label": "持续改进治理概览",
					"link_to": "IONE Improvement Governance Overview",
					"link_type": "Report",
				},
			],
			"分析底账": [
				"IONE Daily Quality Fact",
				"IONE Monthly Quality Fact",
				"IONE Finding Analysis Fact",
				"IONE Surgery Quality Fact",
				"IONE Agent Analysis Fact",
			],
		},
	},
	{
		"name": "IONE Administration",
		"label": "系统管理",
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
			"系统设置": [
				"IONE System Settings",
				"IONE QC Settings",
				"IONE Integration Settings",
				"IONE AI Settings",
				"IONE Security Policy",
				"IONE Feature Flag",
			],
			"受控操作": [
				"IONE Data Export Request",
				"IONE Release Record",
				"IONE Production Readiness Assessment",
				"IONE Production Activation Event",
			],
			"隐私管理": [
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
				{"label": MENU_LABELS[entry], "link_to": entry, "link_type": "DocType"}
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
	for definition in WORKSPACES:
		module_dir = PACKAGE / scrub(definition["module"])
		workspace_dir = module_dir / "workspace" / scrub(definition["name"])
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
			"name": definition["name"],
			"number_cards": [],
			"owner": "Administrator",
			"public": 1,
			"quick_lists": [],
			"roles": [{"role": role} for role in definition["roles"]],
			"sequence_id": NAVIGATION_ORDER[definition["name"]],
			"shortcuts": [],
			"sidebar_items": _sidebar_items(definition["sections"]),
			"standard": 1,
			"title": definition["label"],
			"type": "Workspace",
		}
		(workspace_dir / f"{scrub(definition['name'])}.json").write_text(
			json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
			encoding="utf-8",
		)
	print(f"Generated {len(WORKSPACES)} medical quality workspaces.")


if __name__ == "__main__":
	generate()

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import frappe

APP_NAME = "ione_qms"
EXPECTED_WORKSPACES = frozenset(
	{
		"IONE Analytics",
		"IONE Clinical Quality",
		"IONE Improvement",
		"IONE Indicators",
		"IONE Quality Standards",
		"IONE Flow AI",
		"IONE Integration",
		"IONE Foundation",
		"IONE Administration",
	}
)
EXPECTED_SECTION_COUNT = 29
EXPECTED_LINK_COUNT = 114
EXPECTED_PAGE_TITLES = {
	"ione-quality-command-center": "医疗质量驾驶舱",
	"ione-quality-action-workbench": "质量行动工作台",
}

_CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]")
_FORBIDDEN_DISPLAY_TEXT = re.compile(r"ione|i-one|qms|ai|flow|mdt|phi", re.IGNORECASE)
_LINK_DOCTYPES = frozenset({"DocType", "Page", "Report"})


def ensure_workspace_navigation() -> dict[str, Any]:
	"""Reconcile Chinese navigation and Page titles without renaming internal objects."""
	payloads = _load_workspace_payloads()
	_validate_navigation(payloads)
	_validate_page_title_contract()
	_validate_runtime_workspace_contract(payloads)

	previous_in_migrate = getattr(frappe.flags, "in_migrate", False)
	frappe.flags.in_migrate = True
	try:
		for payload in payloads:
			name = payload["name"]
			workspace = frappe.get_doc("Workspace", name)
			workspace.update(
				{
					"label": payload["label"],
					"title": payload["title"],
					"icon": payload["icon"],
					"sequence_id": payload["sequence_id"],
				}
			)
			workspace.set("sidebar_items", [])
			for item in payload["sidebar_items"]:
				workspace.append("sidebar_items", item)
			workspace.save(ignore_permissions=True)
			_reconcile_workspace_label(name, payload["label"])
		_reconcile_page_titles()
	except Exception as exc:
		raise RuntimeError(f"Unable to reconcile medical quality navigation: {exc}") from exc
	finally:
		frappe.flags.in_migrate = previous_in_migrate

	frappe.clear_cache()
	return {
		"status": "updated",
		"workspaces": [payload["name"] for payload in payloads],
		"sections": EXPECTED_SECTION_COUNT,
		"links": EXPECTED_LINK_COUNT,
		"page_titles": dict(EXPECTED_PAGE_TITLES),
	}


def _validate_page_title_contract() -> None:
	for page_name, title in EXPECTED_PAGE_TITLES.items():
		_validate_display_text(title, f"Page {page_name} title")
		if not frappe.db.exists("Page", page_name):
			raise RuntimeError(f"Required medical quality Page is missing: {page_name}")


def _validate_runtime_workspace_contract(payloads: list[dict[str, Any]]) -> None:
	"""Fail before the first write if a stable Workspace is absent or no longer app-owned."""
	for payload in payloads:
		name = payload["name"]
		if not frappe.db.exists("Workspace", name):
			raise RuntimeError(f"Required medical quality workspace is missing: {name}")
		workspace = frappe.get_doc("Workspace", name)
		if workspace.app != APP_NAME or workspace.module != payload["module"] or not workspace.standard:
			raise RuntimeError(
				f"Workspace {name} ownership drifted: app={workspace.app}, "
				f"module={workspace.module}, standard={workspace.standard}"
			)


def _reconcile_workspace_label(workspace_name: str, label: str) -> None:
	"""Keep the UI fallback label Chinese while preserving the stable document name."""
	if frappe.db.get_value("Workspace", workspace_name, "label") == label:
		return
	frappe.db.set_value("Workspace", workspace_name, "label", label, update_modified=False)


def _reconcile_page_titles() -> None:
	for page_name, title in EXPECTED_PAGE_TITLES.items():
		if frappe.db.get_value("Page", page_name, "title") == title:
			continue
		# Page.modified is a cache/version signal in Frappe's boot payload. Updating
		# it guarantees clients discard the stale title as soon as migrate completes.
		frappe.db.set_value("Page", page_name, "title", title, update_modified=True)


def _load_workspace_payloads() -> list[dict[str, Any]]:
	app_path = Path(frappe.get_app_path(APP_NAME))
	payloads: list[dict[str, Any]] = []
	for path in sorted(app_path.glob("*/workspace/*/*.json")):
		payload = json.loads(path.read_text(encoding="utf-8"))
		if payload.get("doctype") == "Workspace" and payload.get("app") == APP_NAME:
			payloads.append(payload)
	return sorted(payloads, key=lambda payload: float(payload.get("sequence_id") or 0))


def _validate_navigation(payloads: list[dict[str, Any]]) -> None:
	workspace_names = {str(payload.get("name") or "") for payload in payloads}
	if workspace_names != EXPECTED_WORKSPACES:
		missing = sorted(EXPECTED_WORKSPACES - workspace_names)
		extra = sorted(workspace_names - EXPECTED_WORKSPACES)
		raise RuntimeError(f"Medical quality workspace set drifted; missing={missing}, extra={extra}")

	sections = 0
	links = 0
	missing_targets: list[str] = []
	for payload in payloads:
		_validate_display_text(payload.get("label"), f"Workspace {payload['name']} label")
		_validate_display_text(payload.get("title"), f"Workspace {payload['name']} title")
		for item in payload.get("sidebar_items") or []:
			_validate_display_text(item.get("label"), f"Workspace {payload['name']} sidebar label")
			if item.get("type") == "Section Break":
				sections += 1
				continue
			if item.get("type") != "Link":
				raise RuntimeError(
					f"Workspace {payload['name']} has unsupported sidebar item type: {item.get('type')}"
				)
			links += 1
			link_type = str(item.get("link_type") or "")
			link_to = str(item.get("link_to") or "")
			if link_type not in _LINK_DOCTYPES or not link_to:
				raise RuntimeError(
					f"Workspace {payload['name']} has an invalid sidebar target: {link_type}:{link_to}"
				)
			if not frappe.db.exists(link_type, link_to):
				missing_targets.append(f"{link_type}:{link_to}")

	if sections != EXPECTED_SECTION_COUNT or links != EXPECTED_LINK_COUNT:
		raise RuntimeError(
			"Medical quality navigation count drifted; "
			f"expected {EXPECTED_SECTION_COUNT} sections/{EXPECTED_LINK_COUNT} links, "
			f"found {sections} sections/{links} links"
		)
	if missing_targets:
		raise RuntimeError(
			"Medical quality navigation has unavailable targets: " + ", ".join(sorted(missing_targets))
		)


def _validate_display_text(value: Any, context: str) -> None:
	label = str(value or "").strip()
	if not label or not _CHINESE_TEXT.search(label):
		raise RuntimeError(f"{context} must contain a Chinese display name")
	if _FORBIDDEN_DISPLAY_TEXT.search(label):
		raise RuntimeError(f"{context} contains a forbidden English name or abbreviation: {label}")

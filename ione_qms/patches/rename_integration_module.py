from __future__ import annotations

import frappe
from frappe.utils import scrub

OLD_MODULE = "IONE Integration"
NEW_MODULE = "IONE Quality Integration"
QMS_APP = "ione_qms"
COLLISION_APP = "ione_medical_insurance"
QMS_DOCTYPES = (
	"IONE Data Quality Issue",
	"IONE Data Reconciliation",
	"IONE Integration Allowed Scope",
	"IONE Integration Endpoint",
	"IONE Integration Job",
	"IONE Integration Mapping",
	"IONE Integration Message",
	"IONE Source Document Access Log",
	"IONE Source Document Locator",
	"IONE Source System",
)
ANALYTICS_OLD_MODULE = "IONE Analytics"
ANALYTICS_NEW_MODULE = "IONE Quality Analytics"
ANALYTICS_QMS_DOCTYPES = (
	"IONE Agent Analysis Fact",
	"IONE Daily Quality Fact",
	"IONE Finding Analysis Fact",
	"IONE Monthly Quality Fact",
	"IONE Surgery Quality Fact",
)
MODULE_MIGRATIONS = (
	(OLD_MODULE, NEW_MODULE, QMS_DOCTYPES),
	(ANALYTICS_OLD_MODULE, ANALYTICS_NEW_MODULE, ANALYTICS_QMS_DOCTYPES),
)


def _set_module_owner(module: str, app_name: str) -> None:
	owner = frappe.db.get_value("Module Def", module, "app_name")
	if owner and owner != app_name:
		frappe.throw(
			f"Cannot assign module {module} to {app_name}; it is owned by {owner}",
			frappe.ValidationError,
		)
	frappe.db.set_value("Module Def", module, "app_name", app_name, update_modified=False)


def _release_old_module(old_module: str, installed_apps: set[str]) -> None:
	old_exists = bool(frappe.db.exists("Module Def", old_module))
	old_owner = frappe.db.get_value("Module Def", old_module, "app_name") if old_exists else None
	if COLLISION_APP in installed_apps:
		if not old_exists:
			frappe.get_doc(
				{
					"doctype": "Module Def",
					"module_name": old_module,
					"app_name": COLLISION_APP,
				}
			).insert(ignore_permissions=True)
		elif old_owner in {None, "", QMS_APP, COLLISION_APP}:
			frappe.db.set_value(
				"Module Def",
				old_module,
				"app_name",
				COLLISION_APP,
				update_modified=False,
			)
		else:
			frappe.throw(
				f"Cannot release module {old_module}; it is owned by {old_owner}",
				frappe.ValidationError,
			)


def _migrate_qms_module(
	old_module: str,
	new_module: str,
	qms_doctypes: tuple[str, ...],
	installed_apps: set[str],
) -> None:
	_release_old_module(old_module, installed_apps)
	if not frappe.db.exists("Module Def", new_module):
		frappe.get_doc(
			{
				"doctype": "Module Def",
				"module_name": new_module,
				"app_name": QMS_APP,
			}
		).insert(ignore_permissions=True)
	else:
		_set_module_owner(new_module, QMS_APP)

	for doctype in qms_doctypes:
		if frappe.db.exists("DocType", doctype):
			frappe.db.set_value(
				"DocType",
				doctype,
				"module",
				new_module,
				update_modified=False,
			)

	frappe.local.module_app[scrub(new_module)] = QMS_APP
	if COLLISION_APP in installed_apps:
		frappe.local.module_app[scrub(old_module)] = COLLISION_APP


def execute() -> None:
	installed_apps = set(frappe.get_installed_apps())
	for old_module, new_module, qms_doctypes in MODULE_MIGRATIONS:
		_migrate_qms_module(old_module, new_module, qms_doctypes, installed_apps)

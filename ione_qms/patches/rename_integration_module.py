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


def _set_module_owner(module: str, app_name: str) -> None:
	owner = frappe.db.get_value("Module Def", module, "app_name")
	if owner and owner != app_name:
		frappe.throw(
			f"Cannot assign module {module} to {app_name}; it is owned by {owner}",
			frappe.ValidationError,
		)
	frappe.db.set_value("Module Def", module, "app_name", app_name, update_modified=False)


def execute() -> None:
	installed_apps = set(frappe.get_installed_apps())
	old_exists = bool(frappe.db.exists("Module Def", OLD_MODULE))
	old_owner = frappe.db.get_value("Module Def", OLD_MODULE, "app_name") if old_exists else None

	if COLLISION_APP in installed_apps:
		if not old_exists:
			frappe.get_doc(
				{
					"doctype": "Module Def",
					"module_name": OLD_MODULE,
					"app_name": COLLISION_APP,
				}
			).insert(ignore_permissions=True)
		elif old_owner in {None, "", QMS_APP, COLLISION_APP}:
			frappe.db.set_value(
				"Module Def",
				OLD_MODULE,
				"app_name",
				COLLISION_APP,
				update_modified=False,
			)
		else:
			frappe.throw(
				f"Cannot release module {OLD_MODULE}; it is owned by {old_owner}",
				frappe.ValidationError,
			)

	if not frappe.db.exists("Module Def", NEW_MODULE):
		frappe.get_doc(
			{
				"doctype": "Module Def",
				"module_name": NEW_MODULE,
				"app_name": QMS_APP,
			}
		).insert(ignore_permissions=True)
	else:
		_set_module_owner(NEW_MODULE, QMS_APP)

	for doctype in QMS_DOCTYPES:
		if frappe.db.exists("DocType", doctype):
			frappe.db.set_value(
				"DocType",
				doctype,
				"module",
				NEW_MODULE,
				update_modified=False,
			)

	frappe.local.module_app[scrub(NEW_MODULE)] = QMS_APP
	if COLLISION_APP in installed_apps:
		frappe.local.module_app[scrub(OLD_MODULE)] = COLLISION_APP

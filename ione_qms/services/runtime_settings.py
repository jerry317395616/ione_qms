from __future__ import annotations

import frappe

_TEST_MIGRATION_RECEIPT_VERIFIED = False


def ai_runtime_enabled() -> bool:
	"""Return true only when both independent AI kill switches are enabled."""
	if not _doctype_exists("IONE System Settings") or not _doctype_exists("IONE AI Settings"):
		return False
	if not _post_migrate_ready():
		return False
	return bool(
		int(frappe.db.get_single_value("IONE System Settings", "enable_ai") or 0)
		and int(frappe.db.get_single_value("IONE AI Settings", "enabled") or 0)
	)


def require_ai_runtime_enabled() -> None:
	"""Fail closed for every path that can reach a model or queue an AI task."""
	if ai_runtime_enabled():
		return
	frappe.throw(
		"IONE AI runtime is disabled. Both System Settings.enable_ai and "
		"AI Settings.enabled must be explicitly enabled after production approval."
	)


def realtime_rules_enabled() -> bool:
	if not _doctype_exists("IONE System Settings"):
		return False
	if not _post_migrate_ready():
		return False
	return bool(int(frappe.db.get_single_value("IONE System Settings", "enable_realtime_rules") or 0))


def production_mode_enabled() -> bool:
	if not _doctype_exists("IONE System Settings"):
		return False
	return bool(int(frappe.db.get_single_value("IONE System Settings", "production_mode") or 0))


def require_realtime_rules_enabled() -> None:
	if realtime_rules_enabled():
		return
	frappe.throw("IONE real-time rule execution is disabled by System Settings.")


def require_post_migrate_runtime_ready(operation: str) -> None:
	if getattr(frappe.flags, "in_test", False) and _TEST_MIGRATION_RECEIPT_VERIFIED:
		return
	from ione_qms.services.migration_state import assert_migration_complete

	assert_migration_complete(operation)


def verify_test_migration_runtime() -> None:
	"""Verify the real receipt once before isolation tests mutate governed rows."""
	global _TEST_MIGRATION_RECEIPT_VERIFIED
	if not getattr(frappe.flags, "in_test", False):
		frappe.throw("IONE test migration runtime can only be verified by the Frappe test runner.")
	if _TEST_MIGRATION_RECEIPT_VERIFIED:
		return
	from ione_qms.services.migration_state import assert_migration_complete

	assert_migration_complete("begin IONE QMS test runtime")
	_TEST_MIGRATION_RECEIPT_VERIFIED = True


def validate_runtime_enablement(doc, method: str | None = None) -> None:
	"""Block every production workload switch until the current receipt verifies."""
	del method
	enabled_fields = {
		"IONE System Settings": ("enable_ai", "enable_realtime_rules", "production_mode"),
		"IONE AI Settings": ("enabled",),
	}
	requested = [
		fieldname for fieldname in enabled_fields.get(str(doc.doctype), ()) if int(doc.get(fieldname) or 0)
	]
	if not requested:
		return
	from ione_qms.services.migration_state import assert_migration_complete

	assert_migration_complete(f"enable {doc.doctype} fields " + ", ".join(sorted(requested)))


def default_finding_due_days() -> int:
	if not _doctype_exists("IONE QC Settings"):
		return 7
	value = int(frappe.db.get_single_value("IONE QC Settings", "default_finding_due_days") or 7)
	return min(max(value, 1), 365)


def _doctype_exists(doctype: str) -> bool:
	try:
		return bool(frappe.db.exists("DocType", doctype))
	except Exception:
		return False


def _post_migrate_ready() -> bool:
	try:
		from ione_qms.services.migration_state import migration_is_complete

		return migration_is_complete()
	except Exception:
		return False

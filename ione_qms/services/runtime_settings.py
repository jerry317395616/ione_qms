from __future__ import annotations

import frappe

_TEST_MIGRATION_RECEIPT_VERIFIED = False


def ai_runtime_enabled() -> bool:
	"""Return true only when both independent AI kill switches are enabled."""
	if not _doctype_exists("IONE System Settings") or not _doctype_exists("IONE AI Settings"):
		return False
	if not _post_migrate_ready():
		return False
	if _production_mode_configured() and not production_mode_enabled():
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
	if _production_mode_configured() and not production_mode_enabled():
		return False
	return bool(int(frappe.db.get_single_value("IONE System Settings", "enable_realtime_rules") or 0))


def production_mode_enabled() -> bool:
	if not _doctype_exists("IONE System Settings"):
		return False
	if not _production_mode_configured():
		return False
	try:
		from ione_qms.services.production_readiness import production_activation_is_current

		return production_activation_is_current()
	except Exception:
		return False


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
	"""Block workload enablement unless migration and the production gate verify."""
	del method
	previous = doc.get_doc_before_save()
	if (
		str(doc.doctype) == "IONE System Settings"
		and previous
		and int(previous.get("production_mode") or 0)
		and not int(doc.get("production_mode") or 0)
	):
		# A fail-closed production kill switch always disables its dependent
		# workloads in the same transaction, including direct emergency saves.
		doc.enable_ai = 0
		doc.enable_realtime_rules = 0
	enabled_fields = {
		"IONE System Settings": ("enable_ai", "enable_realtime_rules", "production_mode"),
		"IONE AI Settings": ("enabled",),
	}
	requested = [
		fieldname for fieldname in enabled_fields.get(str(doc.doctype), ()) if int(doc.get(fieldname) or 0)
	]
	if not requested:
		if str(doc.doctype) != "IONE System Settings":
			return
	if str(doc.doctype) == "IONE System Settings":
		_event_field_guard(doc, previous)
	from ione_qms.services.migration_state import assert_migration_complete

	if requested:
		assert_migration_complete(f"enable {doc.doctype} fields " + ", ".join(sorted(requested)))
	_validate_production_activation_for_settings(doc, previous)


def _event_field_guard(doc, previous) -> None:
	current_event = str(doc.get("production_activation_event") or "")
	previous_event = str(previous.get("production_activation_event") or "") if previous else ""
	if current_event == previous_event:
		return
	if getattr(frappe.flags, "ione_production_activation", None) != current_event:
		frappe.throw(
			"Production activation events may only be bound through the governed production API."
		)


def _validate_production_activation_for_settings(doc, previous) -> None:
	if str(doc.doctype) == "IONE System Settings":
		production_requested = bool(int(doc.get("production_mode") or 0))
		governed_fields = ("production_mode", "enable_realtime_rules", "enable_ai")
		previous_values = {
			fieldname: int(previous.get(fieldname) or 0) if previous else 0
			for fieldname in governed_fields
		}
		raised = any(
			int(doc.get(fieldname) or 0) > previous_values[fieldname] for fieldname in governed_fields
		)
		current_event = str(doc.get("production_activation_event") or "")
		previous_event = str(previous.get("production_activation_event") or "") if previous else ""
		if not production_requested:
			if not current_event and not previous_event:
				return
			if raised:
				frappe.throw(
					"A site with production activation history may only re-enable workloads "
					"through the governed production API."
				)
			return
		event_name = str(doc.get("production_activation_event") or "")
		if getattr(frappe.flags, "ione_production_activation", None) != event_name:
			frappe.throw("Production workloads may only be enabled through the governed production API.")
		from ione_qms.services.production_readiness import assert_activation_event_authorizes

		assert_activation_event_authorizes(event_name)
		event = frappe.get_doc("IONE Production Activation Event", event_name)
		if int(doc.get("enable_realtime_rules") or 0) > int(event.get("realtime_rules_enabled") or 0):
			frappe.throw("The production activation event does not authorize real-time rules.")
		if int(doc.get("enable_ai") or 0) > int(event.get("ai_enabled") or 0):
			frappe.throw("The production activation event does not authorize AI.")
		return

	if str(doc.doctype) == "IONE AI Settings" and int(doc.get("enabled") or 0):
		if not _production_mode_configured():
			return
		previous_enabled = int(previous.get("enabled") or 0) if previous else 0
		if previous_enabled:
			return
		event_name = str(
			frappe.db.get_single_value("IONE System Settings", "production_activation_event") or ""
		)
		if getattr(frappe.flags, "ione_production_activation", None) != event_name:
			frappe.throw("Production AI may only be enabled through the governed production API.")
		from ione_qms.services.production_readiness import assert_activation_event_authorizes

		assert_activation_event_authorizes(event_name)
		event = frappe.get_doc("IONE Production Activation Event", event_name)
		if not int(event.get("ai_enabled") or 0):
			frappe.throw("The production activation event does not authorize AI.")


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


def _production_mode_configured() -> bool:
	try:
		return bool(int(frappe.db.get_single_value("IONE System Settings", "production_mode") or 0))
	except Exception:
		return False

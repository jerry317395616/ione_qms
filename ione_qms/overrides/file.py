from __future__ import annotations

import frappe

from ione_qms.overrides.phi_query_guard import PHI_METADATA_DOCTYPES
from ione_qms.permissions import finding_appeal_evidence_file_permission
from ione_qms.services.finding_appeals import (
	prevent_finding_appeal_evidence_deletion,
	validate_finding_appeal_evidence_file,
)
from ione_qms.services.versions import (
	prevent_standard_source_file_deletion,
	validate_standard_source_file,
)


class IONEQMSFileMixin:
	"""Apply QMS evidence guards cooperatively to the effective File controller.

	Frappe composes ``extend_doctype_class`` mixins in front of the controller
	selected by ``override_doctype_class``. Therefore ``super()`` resolves to
	Drive's File controller when Drive is installed, and to Frappe's File
	controller otherwise.
	"""

	def validate(self):
		validate_protected_identity_file(self)
		validate_finding_appeal_evidence_file(self)
		validate_standard_source_file(self)
		return super().validate()

	@frappe.whitelist()
	def optimize_file(self):
		validate_protected_identity_file(self)
		prevent_finding_appeal_evidence_deletion(self)
		prevent_standard_source_file_deletion(self)
		return super().optimize_file()

	def on_trash(self):
		validate_protected_identity_file(self)
		prevent_finding_appeal_evidence_deletion(self)
		prevent_standard_source_file_deletion(self)
		return super().on_trash()

	def is_downloadable(self) -> bool:
		return (
			qms_file_permission(
				self,
				"read",
				user=frappe.session.user,
			)
			and super().is_downloadable()
		)


def validate_protected_identity_file(doc) -> None:
	"""Forbid direct File attachments and duplicate byte paths for identity-bearing records."""
	if _migration_file_bypass_active():
		return
	if str(doc.get("attached_to_doctype") or "") in PHI_METADATA_DOCTYPES:
		frappe.throw(
			"Direct File attachments are not permitted on identity-bearing QMS records. "
			"Use the purpose-built governed evidence workflow.",
			frappe.PermissionError,
		)
	if _file_group_has_protected_parent(doc, exclude_name=str(doc.get("name") or "")):
		frappe.throw(
			"A File URL or content hash cannot be duplicated from an identity-bearing QMS attachment.",
			frappe.PermissionError,
		)


def qms_file_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
	debug: bool = False,
) -> bool:
	"""Compose the existing evidence permission with the protected-attachment deny boundary."""
	if _migration_file_bypass_active():
		return True
	if _file_group_has_protected_parent(doc):
		return False
	return finding_appeal_evidence_file_permission(
		doc,
		ptype,
		user=user,
		debug=debug,
	)


def qms_file_query(user: str | None = None) -> str:
	"""Hide every File row whose URL or bytes alias a protected QMS attachment."""
	del user
	quoted_doctypes = ", ".join(frappe.db.escape(doctype) for doctype in sorted(PHI_METADATA_DOCTYPES))
	file_table = "`tabFile`"
	protected_table = "`tabFile` protected_qms_file"
	return (
		f"coalesce({file_table}.attached_to_doctype, '') not in ({quoted_doctypes})"  # noqa: S608
		f" and not exists (select 1 from {protected_table}"
		f" where protected_qms_file.attached_to_doctype in ({quoted_doctypes})"
		f" and ((coalesce({file_table}.file_url, '') != ''"
		f" and protected_qms_file.file_url = {file_table}.file_url)"
		f" or (coalesce({file_table}.content_hash, '') != ''"
		f" and protected_qms_file.content_hash = {file_table}.content_hash)))"
	)


def _file_group_has_protected_parent(doc, *, exclude_name: str = "") -> bool:
	if str(doc.get("attached_to_doctype") or "") in PHI_METADATA_DOCTYPES:
		return True
	filters: list[dict[str, object]] = []
	file_url = str(doc.get("file_url") or "").strip()
	content_hash = str(doc.get("content_hash") or "").strip()
	if file_url:
		filters.append({"file_url": file_url})
	if content_hash:
		filters.append({"content_hash": content_hash})
	for candidate_filters in filters:
		rows = frappe.get_all(
			"File",
			filters=candidate_filters,
			fields=["name", "attached_to_doctype"],
			order_by="name asc",
			limit_page_length=1_001,
		)
		if len(rows) > 1_000:
			frappe.throw("File alias validation exceeded its safe bounded cardinality.")
		for row in rows:
			if exclude_name and str(row.get("name") or "") == exclude_name:
				continue
			if str(row.get("attached_to_doctype") or "") in PHI_METADATA_DOCTYPES:
				return True
	return False


def _migration_file_bypass_active() -> bool:
	return any(
		getattr(frappe.flags, flag_name, False) for flag_name in ("in_install", "in_migrate", "in_uninstall")
	)

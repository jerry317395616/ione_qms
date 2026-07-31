from __future__ import annotations

from typing import Any, Never

import frappe
from frappe.core.doctype.data_export.exporter import export_data as frappe_export_data

from ione_qms.overrides.native_export_guard import (
	ExportTargetInspectionError,
	collect_export_target_strings,
)
from ione_qms.services.data_export import GOVERNED_NATIVE_EXPORT_DOCTYPES


@frappe.whitelist()
def export_data(
	doctype: Any = None,
	parent_doctype: Any = None,
	all_doctypes: bool | int | str = True,
	with_data: bool | int | str = False,
	select_columns: str | dict[str, list[str]] | None = None,
	file_type: str = "CSV",
	template: bool | str = False,
	filters: str | dict[str, Any] | list | None = None,
	export_without_column_meta: bool | str = False,
):
	"""Route governed QMS data through the independently approved export workflow."""
	try:
		requested_doctypes = collect_export_target_strings(doctype, parent_doctype)
	except ExportTargetInspectionError:
		_deny_native_qms_export()
	if requested_doctypes.intersection(GOVERNED_NATIVE_EXPORT_DOCTYPES):
		_deny_native_qms_export()
	return frappe_export_data(
		doctype=doctype,
		parent_doctype=parent_doctype,
		all_doctypes=all_doctypes,
		with_data=with_data,
		select_columns=select_columns,
		file_type=file_type,
		template=template,
		filters=filters,
		export_without_column_meta=export_without_column_meta,
	)


def _deny_native_qms_export() -> Never:
	message = "This QMS DocType can only be exported through an approved IONE Data Export Request."
	frappe.throw(message, frappe.PermissionError)
	raise frappe.PermissionError(message)

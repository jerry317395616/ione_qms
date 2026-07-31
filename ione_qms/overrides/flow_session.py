from __future__ import annotations

import frappe

from ione_qms.ai.flow_gateway import _session_is_governed
from ione_qms.tasks.security import governed_flow_session_retention_verified


class IONEGovernedFlowSessionMixin:
	"""Keep governed Flow evidence on hold until IONE retention verification."""

	@staticmethod
	def clear_old_logs(days: int = 30) -> None:
		cutoff = frappe.utils.add_days(frappe.utils.now(), -int(days or 30))
		sessions = frappe.get_all(
			"Flow Session",
			filters={"modified": ["<", cutoff]},
			pluck="name",
			order_by="modified asc, name asc",
		)
		eligible: list[str] = []
		for session in sessions:
			session_name = str(session or "")
			if not session_name:
				continue
			retention_failure_type = None
			try:
				governed = _session_is_governed(session_name) or _session_has_governed_run_lineage(
					session_name
				)
				verified = governed_flow_session_retention_verified(session_name) if governed else True
			except Exception as exc:
				retention_failure_type = type(exc).__name__
			if retention_failure_type:
				frappe.log_error(
					title="IONE governed Flow cleanup held",
					message=(
						f"{retention_failure_type}: Flow Session {session_name} was preserved "
						"because its retention state could not be verified."
					),
					reference_doctype="Flow Session",
					reference_name=session_name,
				)
				continue
			if governed and not verified:
				continue
			eligible.append(session_name)

		from flow.flow.doctype.flow_session.flow_session import (
			_delete_attachment_files,
			_purge_attachment_chunks,
		)

		for batch in frappe.utils.create_batch(eligible, 100):
			frappe.db.delete("Flow Run", {"session": ["in", batch]})
			frappe.db.delete("Flow Session Message", {"parent": ["in", batch]})
			_delete_attachment_files(batch)
			frappe.db.delete("Flow Session Attachment", {"parent": ["in", batch]})
			frappe.db.delete("Flow Session", {"name": ["in", batch]})
			_purge_attachment_chunks(batch)


def _session_has_governed_run_lineage(session_name: str) -> bool:
	"""Use immutable run/link lineage when mutable Agent configuration is gone."""
	return bool(
		frappe.db.sql(
			(
				"select 1 "
				"from `tabFlow Run` run "
				"left join `tabIONE Flow Run Link` link on link.flow_run = run.name "
				"where run.session = %s "
				"and (run.reference_doctype = 'IONE AI Analysis Task' or link.name is not null) "
				"limit 1"
			),
			(session_name,),
		)
	)

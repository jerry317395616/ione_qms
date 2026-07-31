from frappe.model.document import Document

from ione_qms.services.indicator_audit import (
	prevent_historical_audit_deletion,
	validate_historical_audit_access_receipt,
)


class IONEIndicatorHistoricalAuditAccessReceipt(Document):
	def validate(self) -> None:
		validate_historical_audit_access_receipt(self)

	def on_trash(self) -> None:
		prevent_historical_audit_deletion(self)

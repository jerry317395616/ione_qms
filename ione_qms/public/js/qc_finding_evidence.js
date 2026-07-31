frappe.ui.form.on("IONE QC Finding Evidence", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.finding) {
			return;
		}
		frm.add_custom_button(__("Locate Source Document"), async () => {
			const response = await frappe.call({
				method: "ione_qms.api.source_document.locate_finding_evidence",
				type: "POST",
				args: {
					finding: frm.doc.finding,
					evidence: frm.doc.name,
				},
				freeze: true,
				freeze_message: __("Resolving approved source-document link..."),
			});
			const result = response.message || {};
			if (!result.available) {
				frappe.msgprint({
					title: __("Source Document Unavailable"),
					message: __("No approved source-document link is available ({0}).", [
						result.error_code || "FAIL_CLOSED",
					]),
					indicator: "orange",
				});
				return;
			}
			let parsed;
			try {
				parsed = new URL(result.url);
			} catch {
				frappe.throw(__("The approved source-document URL is invalid."));
			}
			if (
				parsed.protocol !== "https:" ||
				parsed.hostname !== result.allowed_host ||
				parsed.username ||
				parsed.password ||
				parsed.search ||
				parsed.hash
			) {
				frappe.throw(__("The approved source-document URL failed the browser safety check."));
			}
			window.open(result.url, "_blank", "noopener,noreferrer");
		});
	},
});

frappe.ui.form.on("IONE PDCA Project", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		const unverified = (frm.doc.effect_measurements || [])
			.filter((row) => row.measurement_code && !row.verified_at)
			.map((row) => row.measurement_code);
		if (frm.doc.status === "Active" && unverified.length) {
			frm.add_custom_button(__("Verify Effect Measurements"), () => {
				const dialog = new frappe.ui.Dialog({
					title: __("Verify Effect Measurements"),
					fields: [
						{
							fieldname: "verification_comment",
							fieldtype: "Small Text",
							label: __("Verification Comment"),
							reqd: 1,
						},
					],
					primary_action_label: __("Verify"),
					primary_action(values) {
						dialog.hide();
						frappe.call({
							method: "ione_qms.api.pdca.verify_pdca_measurements",
							type: "POST",
							args: {
								project: frm.doc.name,
								measurement_codes: unverified,
								verification_comment: values.verification_comment,
							},
							freeze: true,
							callback() {
								frm.reload_doc();
							},
						});
					},
				});
				dialog.show();
			});
		}

		if (frm.doc.status === "Measuring" && !frm.doc.standardized_at) {
			frm.add_custom_button(__("Record Standardization"), () => {
				const dialog = new frappe.ui.Dialog({
					title: __("Record Standardization Decision"),
					fields: [
						{
							fieldname: "decision",
							fieldtype: "Select",
							label: __("Decision"),
							options: ["Adopted", "Not Adopted"],
							reqd: 1,
						},
						{
							fieldname: "conclusion",
							fieldtype: "Small Text",
							label: __("Conclusion"),
							reqd: 1,
						},
						{
							fieldname: "evidence_reference",
							fieldtype: "Data",
							label: __("Evidence Reference"),
							reqd: 1,
						},
						{
							fieldname: "evidence_hash",
							fieldtype: "Data",
							label: __("Evidence SHA-256"),
							reqd: 1,
						},
					],
					primary_action_label: __("Record"),
					primary_action(values) {
						dialog.hide();
						frappe.call({
							method: "ione_qms.api.pdca.record_pdca_standardization_decision",
							type: "POST",
							args: {
								project: frm.doc.name,
								...values,
							},
							freeze: true,
							callback() {
								frm.reload_doc();
							},
						});
					},
				});
				dialog.show();
			});
		}
	},
});

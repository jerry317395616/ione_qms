const IONE_LOCATOR_APPROVER_ROLES = ["IONE QC Administrator", "IONE Medical Affairs"];

function ione_can_review_locator(frm) {
	return (
		IONE_LOCATOR_APPROVER_ROLES.some((role) => frappe.user_roles.includes(role)) &&
		frm.doc.requested_by !== frappe.session.user
	);
}

async function ione_locator_call(frm, method, args, success_message) {
	await frappe.call({
		method,
		type: "POST",
		args,
		freeze: true,
		freeze_message: __("Applying governed source-document locator transition..."),
	});
	frappe.show_alert({ message: success_message, indicator: "green" });
	await frm.reload_doc();
}

frappe.ui.form.on("IONE Source Document Locator", {
	refresh(frm) {
		if (frm.is_new() || !ione_can_review_locator(frm)) {
			return;
		}
		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Approve Locator"), () => {
				frappe.prompt(
					[
						{
							fieldname: "review_comment",
							fieldtype: "Small Text",
							label: __("Approval Comment"),
							reqd: 1,
						},
					],
					(values) =>
						ione_locator_call(
							frm,
							"ione_qms.api.source_document.approve_locator",
							{ locator: frm.doc.name, review_comment: values.review_comment },
							__("Source-document locator approved."),
						),
					__("Approve Source-Document Locator"),
				);
			});
			return;
		}
		const actions =
			frm.doc.status === "Approved"
				? [
						["Suspend", __("Suspend Locator")],
						["Retire", __("Retire Locator")],
					]
				: frm.doc.status === "Suspended"
					? [
							["Resume", __("Resume Locator")],
							["Retire", __("Retire Locator")],
						]
					: [];
		actions.forEach(([action, label]) => {
			frm.add_custom_button(
				label,
				() => {
					frappe.prompt(
						[
							{
								fieldname: "operation_comment",
								fieldtype: "Small Text",
								label: __("Operation Comment"),
								reqd: 1,
							},
						],
						(values) =>
							ione_locator_call(
								frm,
								"ione_qms.api.source_document.operate_locator",
								{
									locator: frm.doc.name,
									action,
									operation_comment: values.operation_comment,
								},
								__("Source-document locator updated."),
							),
						label,
					);
				},
				__("Governance"),
			);
		});
	},
});

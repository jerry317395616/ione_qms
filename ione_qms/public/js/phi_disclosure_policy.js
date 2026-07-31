const IONE_PHI_POLICY_AUTHOR_ROLES = ["IONE QC Administrator", "IONE Medical Affairs"];
const IONE_PHI_POLICY_REVIEWER_ROLES = ["IONE Medical Affairs", "IONE Auditor"];

function ione_has_any_phi_policy_role(roles) {
	return roles.some((role) => frappe.user_roles.includes(role));
}

async function ione_phi_policy_call(frm, method, args, success_message) {
	await frappe.call({
		method,
		type: "POST",
		args,
		freeze: true,
		freeze_message: __("Applying governed PHI disclosure policy transition..."),
	});
	frappe.show_alert({ message: success_message, indicator: "green" });
	await frm.reload_doc();
}

frappe.ui.form.on("IONE PHI Disclosure Policy", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const current_user = frappe.session.user;
		if (
			frm.doc.status === "Draft" &&
			frm.doc.requested_by === current_user &&
			ione_has_any_phi_policy_role(IONE_PHI_POLICY_AUTHOR_ROLES)
		) {
			frm.add_custom_button(
				__("Submit for Independent Review"),
				() =>
					ione_phi_policy_call(
						frm,
						"ione_qms.api.privacy.submit_phi_disclosure_policy",
						{ policy: frm.doc.name },
						__("PHI disclosure policy submitted for independent review."),
					),
				__("Governance"),
			);
		}
		if (
			frm.doc.status === "Under Review" &&
			frm.doc.requested_by !== current_user &&
			frm.doc.submitted_by !== current_user &&
			ione_has_any_phi_policy_role(IONE_PHI_POLICY_REVIEWER_ROLES)
		) {
			frm.add_custom_button(
				__("Approve Effective Schedule"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "review_comment",
								fieldtype: "Small Text",
								label: __("Independent Review Comment"),
								reqd: 1,
							},
						],
						(values) =>
							ione_phi_policy_call(
								frm,
								"ione_qms.api.privacy.approve_phi_disclosure_policy",
								{
									policy: frm.doc.name,
									review_comment: values.review_comment,
								},
								__("PHI disclosure policy independently approved."),
							),
						__("Approve PHI Disclosure Policy"),
					);
				},
				__("Governance"),
			);
		}
		if (
			["Scheduled", "Active"].includes(frm.doc.status) &&
			ione_has_any_phi_policy_role(IONE_PHI_POLICY_REVIEWER_ROLES)
		) {
			frm.add_custom_button(
				__("Retire Policy"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "retirement_reason",
								fieldtype: "Small Text",
								label: __("Retirement Reason"),
								reqd: 1,
							},
						],
						(values) =>
							ione_phi_policy_call(
								frm,
								"ione_qms.api.privacy.retire_phi_disclosure_policy",
								{
									policy: frm.doc.name,
									retirement_reason: values.retirement_reason,
								},
								__("PHI disclosure policy retired."),
							),
						__("Retire PHI Disclosure Policy"),
					);
				},
				__("Governance"),
			);
		}
	},
});

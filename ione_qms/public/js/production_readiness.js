const IONE_PRODUCTION_ASSESSOR_ROLES = ["IONE QC Administrator", "IONE Medical Affairs"];
const IONE_PRODUCTION_REVIEWER_ROLES = ["IONE QMS Auditor"];
const IONE_PRODUCTION_REVOCATION_ROLES = ["IONE QMS Auditor", "IONE Medical Affairs"];

function ione_has_production_role(roles) {
	return roles.some((role) => frappe.user_roles.includes(role));
}

async function ione_production_call(frm, method, args, message) {
	await frappe.call({
		method: `ione_qms.api.production.${method}`,
		type: "POST",
		args,
		freeze: true,
		freeze_message: __("Applying governed production transition..."),
	});
	frappe.show_alert({message, indicator: "green"});
	await frm.reload_doc();
}

function ione_reason_dialog(title, field_label, primary_label, callback) {
	const dialog = new frappe.ui.Dialog({
		title,
		fields: [
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: field_label,
				reqd: 1,
			},
		],
		primary_action_label: primary_label,
		primary_action(values) {
			dialog.hide();
			callback(values.reason);
		},
	});
	dialog.show();
}

function ione_activation_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Activate Production Mode"),
		fields: [
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Approved Change / Activation Reason"),
				reqd: 1,
			},
			{
				fieldname: "enable_realtime_rules",
				fieldtype: "Check",
				label: __("Enable Real-time Rules"),
				default: 0,
			},
			{
				fieldname: "enable_ai",
				fieldtype: "Check",
				label: __("Enable Governed AI"),
				default: 0,
			},
		],
		primary_action_label: __("Activate"),
		primary_action(values) {
			dialog.hide();
			ione_production_call(
				frm,
				"activate_production_mode",
				{
					assessment: frm.doc.name,
					reason: values.reason,
					enable_realtime_rules: values.enable_realtime_rules,
					enable_ai: values.enable_ai,
				},
				__("Production mode activated with an immutable audit event."),
			);
		},
	});
	dialog.show();
}

frappe.ui.form.on("IONE Production Readiness Assessment", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const actor = frappe.session.user;
		if (
			frm.doc.status === "Draft" &&
			frm.doc.requested_by === actor &&
			ione_has_production_role(IONE_PRODUCTION_ASSESSOR_ROLES)
		) {
			frm.add_custom_button(
				__("Submit Complete Evidence"),
				() =>
					ione_production_call(
						frm,
						"submit_assessment",
						{assessment: frm.doc.name},
						__("Production readiness evidence submitted for independent review."),
					),
				__("Production Governance"),
			);
		}
		if (
			frm.doc.status === "Under Review" &&
			frm.doc.requested_by !== actor &&
			frm.doc.submitted_by !== actor &&
			ione_has_production_role(IONE_PRODUCTION_REVIEWER_ROLES)
		) {
			frm.add_custom_button(
				__("Approve All P0 Gates"),
				() =>
					ione_reason_dialog(
						__("Approve Production Readiness"),
						__("Independent Review Comment"),
						__("Approve"),
						(reason) =>
							ione_production_call(
								frm,
								"approve_assessment",
								{assessment: frm.doc.name, review_comment: reason},
								__("Production readiness independently approved."),
							),
					),
				__("Production Governance"),
			);
			frm.add_custom_button(
				__("Reject"),
				() =>
					ione_reason_dialog(
						__("Reject Production Readiness"),
						__("Rejection Reason"),
						__("Reject"),
						(reason) =>
							ione_production_call(
								frm,
								"reject_assessment",
								{assessment: frm.doc.name, review_comment: reason},
								__("Production readiness rejected."),
							),
					),
				__("Production Governance"),
			);
		}
		if (
			frm.doc.status === "Approved" &&
			![frm.doc.requested_by, frm.doc.submitted_by, frm.doc.reviewed_by].includes(actor) &&
			ione_has_production_role(IONE_PRODUCTION_ASSESSOR_ROLES)
		) {
			frm.add_custom_button(
				__("Activate Production Mode"),
				() => ione_activation_dialog(frm),
				__("Production Governance"),
			);
		}
		if (
			frm.doc.status === "Approved" &&
			ione_has_production_role(IONE_PRODUCTION_REVOCATION_ROLES)
		) {
			frm.add_custom_button(
				__("Revoke Approval"),
				() =>
					ione_reason_dialog(
						__("Revoke Production Approval"),
						__("Revocation Reason"),
						__("Revoke"),
						(reason) =>
							ione_production_call(
								frm,
								"revoke_assessment",
								{assessment: frm.doc.name, reason},
								__("Production readiness approval revoked and workloads disabled."),
							),
					),
				__("Production Governance"),
			);
		}
	},
});

frappe.ui.form.on("IONE System Settings", {
	refresh(frm) {
		if (
			frm.doc.production_mode &&
			ione_has_production_role([...IONE_PRODUCTION_ASSESSOR_ROLES, ...IONE_PRODUCTION_REVIEWER_ROLES])
		) {
			frm.add_custom_button(
				__("Emergency Deactivate Production"),
				() =>
					ione_reason_dialog(
						__("Deactivate Production Mode"),
						__("Deactivation Reason"),
						__("Deactivate"),
						(reason) =>
							ione_production_call(
								frm,
								"deactivate_production_mode",
								{reason},
								__("Production workloads disabled."),
							),
					),
				__("Production Governance"),
			);
		}
	},
});

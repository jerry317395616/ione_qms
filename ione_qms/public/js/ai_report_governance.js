(() => {
	"use strict";

	const SCHEDULE_REVIEW_ROLES = ["IONE Agent Reviewer", "IONE Medical Affairs"];
	const SCHEDULE_OPERATION_ROLES = ["IONE Agent Administrator", "IONE Medical Affairs"];
	const REPORT_REVIEW_ROLES = [
		"IONE Agent Reviewer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	];

	const has_role = (roles) =>
		roles.some((role) => (frappe.user_roles || []).includes(role));

	const governed_call = async (frm, method, args, message) => {
		await frappe.call({
			method,
			type: "POST",
			args,
			freeze: true,
			freeze_message: __("Applying the governed report transition…"),
		});
		frappe.show_alert({ message, indicator: "green" });
		await frm.reload_doc();
	};

	const comment_dialog = (title, label, callback) => {
		frappe.prompt(
			[
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label,
					reqd: 1,
				},
			],
			(values) => callback(values.comment),
			title,
		);
	};

	const recovery_dialog = (frm) => {
		frappe.prompt(
			[
				{
					fieldname: "reason_code",
					fieldtype: "Data",
					label: __("Stable Reason Code"),
					reqd: 1,
					description: __(
						"Use 2-64 uppercase letters, digits, dot, colon, dash, or underscore.",
					),
				},
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Recovery Authorization Reason"),
					reqd: 1,
				},
			],
			(values) =>
				governed_call(
					frm,
					"ione_qms.api.ai.authorize_quality_report_recovery",
					{
						schedule: frm.doc.name,
						prior_task: frm.doc.last_task,
						reason_code: values.reason_code,
						reason: values.reason,
					},
					__("Quality-report recovery request processed."),
				),
			__("Authorize Monthly Report Recovery"),
		);
	};

	frappe.ui.form.on("IONE AI Report Schedule", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (
				frm.doc.status === "Approved" &&
				frm.doc.enabled &&
				frm.doc.last_result === "Failed" &&
				frm.doc.last_task &&
				frm.doc.report_reviewer === frappe.session.user
			) {
				frm.add_custom_button(
					__("Authorize Monthly Recovery"),
					() => recovery_dialog(frm),
					__("Governance"),
				);
			}
			if (!has_role(SCHEDULE_REVIEW_ROLES)) {
				return;
			}
			if (
				frm.doc.status === "Draft" &&
				frm.doc.requested_by !== frappe.session.user
			) {
				[
					["Approve", __("Approve Schedule")],
					["Reject", __("Reject Schedule")],
				].forEach(([decision, label]) => {
					frm.add_custom_button(
						label,
						() =>
							comment_dialog(
								label,
								__("Review Comment"),
								(comment) =>
									governed_call(
										frm,
										"ione_qms.api.ai.review_quality_report_schedule",
										{
											schedule: frm.doc.name,
											decision,
											review_comment: comment,
										},
										__("Quality-report schedule reviewed."),
									),
							),
						__("Governance"),
					);
				});
				return;
			}
			if (!has_role(SCHEDULE_OPERATION_ROLES)) {
				return;
			}
			const actions =
				frm.doc.status === "Approved"
					? [
							["Suspend", __("Suspend Schedule")],
							["Retire", __("Retire Schedule")],
						]
					: frm.doc.status === "Suspended"
						? [
								["Resume", __("Resume Schedule")],
								["Retire", __("Retire Schedule")],
							]
						: [];
			actions.forEach(([action, label]) => {
				frm.add_custom_button(
					label,
					() =>
						comment_dialog(
							label,
							__("Operation Comment"),
							(comment) =>
								governed_call(
									frm,
									"ione_qms.api.ai.operate_quality_report_schedule",
									{
										schedule: frm.doc.name,
										action,
										operation_comment: comment,
									},
									__("Quality-report schedule updated."),
								),
						),
					__("Governance"),
				);
			});
		},
	});

	frappe.ui.form.on("IONE AI Report Draft", {
		refresh(frm) {
			if (
				frm.is_new() ||
				!has_role(REPORT_REVIEW_ROLES) ||
				!["Draft", "Pending Review"].includes(frm.doc.status)
			) {
				return;
			}
			[
				["Approve", __("Approve Draft")],
				["Reject", __("Reject Draft")],
				["Request Revision", __("Request Revision")],
			].forEach(([decision, label]) => {
				frm.add_custom_button(
					label,
					() =>
						comment_dialog(
							label,
							__("Review Comment"),
							(comment) =>
								governed_call(
									frm,
									"ione_qms.api.ai.review_report_draft",
									{
										draft: frm.doc.name,
										decision,
										review_comment: comment,
									},
									__("AI report draft reviewed."),
								),
						),
					__("Governance"),
				);
			});
		},
	});
})();

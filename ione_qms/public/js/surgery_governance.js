(() => {
	"use strict";

	const REVIEW_ROLES = ["IONE QC Reviewer", "IONE Medical Affairs"];
	const RECORD_ROLES = [
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	];
	const GROUP = __("Surgery Governance");
	const API = {
		qualification: "ione_qms.api.surgery.review_staff_qualification_record",
		authorization: "ione_qms.api.surgery.review_surgery_authorization_record",
		policy: "ione_qms.api.surgery.review_surgery_procedure_policy_record",
		retire: "ione_qms.api.surgery.retire_surgery_governance_definition",
		mdt: "ione_qms.api.surgery.record_surgery_mdt",
		checklist: "ione_qms.api.surgery.record_surgery_safety_checklist",
		requestException: "ione_qms.api.surgery.request_surgery_emergency_exception",
		reviewException: "ione_qms.api.surgery.review_surgery_emergency_exception",
	};

	const hasRole = (roles) =>
		roles.some((role) => (frappe.user_roles || []).includes(role));

	const post = async (method, args, message) => {
		const response = await frappe.call({
			method,
			args,
			type: "POST",
			freeze: true,
			freeze_message: message,
		});
		return response.message;
	};

	const commentDialog = (frm, { title, action, method, argument, value }) => {
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label: __("Accountable Comment"),
					reqd: 1,
				},
			],
			primary_action_label: title,
			primary_action: async ({ comment }) => {
				await post(
					method,
					{ [argument]: value, decision: action, review_comment: comment },
					__("Recording governed review…")
				);
				dialog.hide();
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	const retireDialog = (frm) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Retire Definition"),
			fields: [
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Retirement Reason"),
					reqd: 1,
				},
			],
			primary_action_label: __("Retire"),
			primary_action: async ({ reason }) => {
				await post(
					API.retire,
					{
						doctype: frm.doctype,
						name: frm.doc.name,
						retirement_reason: reason,
					},
					__("Retiring governed definition…")
				);
				dialog.hide();
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	const bindDefinition = (doctype, method, argument) => {
		frappe.ui.form.on(doctype, {
			refresh: (frm) => {
				if (
					frm.is_new() ||
					frappe.session.user === "Administrator" ||
					!hasRole(REVIEW_ROLES)
				) {
					return;
				}
				if (frm.doc.status === "Draft" && frm.doc.owner !== frappe.session.user) {
					frm.add_custom_button(
						__("Approve"),
						() =>
							commentDialog(frm, {
								title: __("Approve Definition"),
								action: "Approve",
								method,
								argument,
								value: frm.doc.name,
							}),
						GROUP
					);
					frm.add_custom_button(
						__("Reject"),
						() =>
							commentDialog(frm, {
								title: __("Reject Definition"),
								action: "Reject",
								method,
								argument,
								value: frm.doc.name,
							}),
						GROUP
					);
				}
				if (frm.doc.status === "Approved") {
					frm.add_custom_button(
						__("Retire"),
						() => retireDialog(frm),
						GROUP
					);
				}
			},
		});
	};

	bindDefinition("IONE Staff Qualification", API.qualification, "qualification");
	bindDefinition("IONE Surgery Authorization", API.authorization, "authorization");
	bindDefinition("IONE Surgery Procedure Policy", API.policy, "policy");

	const mdtDialog = (frm) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Record Completed Pre-operative MDT"),
			fields: [
				{
					fieldname: "meeting_at",
					fieldtype: "Datetime",
					label: __("Meeting Started At"),
					reqd: 1,
				},
				{
					fieldname: "completed_at",
					fieldtype: "Datetime",
					label: __("Completed At"),
					reqd: 1,
				},
				{
					fieldname: "chair",
					fieldtype: "Link",
					options: "IONE Medical Staff",
					label: __("Chair"),
					reqd: 1,
				},
				{
					fieldname: "conclusion",
					fieldtype: "Select",
					options: ["Proceed", "Proceed with Conditions", "Do Not Proceed"].join("\n"),
					label: __("Conclusion"),
					reqd: 1,
				},
				{
					fieldname: "conclusion_summary",
					fieldtype: "Long Text",
					label: __("Conclusion Summary"),
					reqd: 1,
				},
				{
					fieldname: "participants",
					fieldtype: "Code",
					options: "JSON",
					label: __("Participants JSON"),
					description: __(
						'Array of {"medical_staff","participant_role","confirmed_at","evidence_reference"}.'
					),
					reqd: 1,
				},
				{
					fieldname: "evidence_reference",
					fieldtype: "Small Text",
					label: __("Evidence Reference"),
					reqd: 1,
				},
			],
			primary_action_label: __("Record MDT"),
			primary_action: async (values) => {
				await post(
					API.mdt,
					{ surgery_qc: frm.doc.name, ...values },
					__("Recording immutable MDT evidence…")
				);
				dialog.hide();
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	const checklistDialog = (frm) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Complete Pre-operative Safety Checklist"),
			fields: [
				{
					fieldname: "completed_at",
					fieldtype: "Datetime",
					label: __("Completed At"),
					reqd: 1,
				},
				{
					fieldname: "items",
					fieldtype: "Code",
					options: "JSON",
					label: __("Checklist Results JSON"),
					description: __('Array of {"item_code","result"}; every result must be "Pass".'),
					reqd: 1,
				},
				{
					fieldname: "evidence_reference",
					fieldtype: "Small Text",
					label: __("Evidence Reference"),
					reqd: 1,
				},
			],
			primary_action_label: __("Complete Checklist"),
			primary_action: async (values) => {
				await post(
					API.checklist,
					{ surgery_qc: frm.doc.name, ...values },
					__("Recording immutable safety checklist…")
				);
				dialog.hide();
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	const exceptionDialog = (frm) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Request Emergency Exception"),
			fields: [
				{
					fieldname: "waive_authorization",
					fieldtype: "Check",
					label: __("Authorization"),
				},
				{
					fieldname: "waive_mdt",
					fieldtype: "Check",
					label: __("MDT"),
				},
				{
					fieldname: "waive_safety_checklist",
					fieldtype: "Check",
					label: __("Safety Checklist"),
				},
				{
					fieldname: "reason_code",
					fieldtype: "Data",
					label: __("Reason Code"),
					reqd: 1,
				},
				{
					fieldname: "reason",
					fieldtype: "Long Text",
					label: __("Reason"),
					reqd: 1,
				},
				{
					fieldname: "valid_from",
					fieldtype: "Datetime",
					label: __("Valid From"),
					reqd: 1,
				},
				{
					fieldname: "valid_to",
					fieldtype: "Datetime",
					label: __("Valid To"),
					reqd: 1,
				},
			],
			primary_action_label: __("Submit Request"),
			primary_action: async (values) => {
				const requestedScopes = [];
				if (values.waive_authorization) requestedScopes.push("Authorization");
				if (values.waive_mdt) requestedScopes.push("MDT");
				if (values.waive_safety_checklist) requestedScopes.push("Safety Checklist");
				if (!requestedScopes.length) {
					frappe.throw(__("Select at least one explicit exception scope."));
				}
				await post(
					API.requestException,
					{
						surgery_qc: frm.doc.name,
						reason_code: values.reason_code,
						reason: values.reason,
						valid_from: values.valid_from,
						valid_to: values.valid_to,
						requested_scopes: JSON.stringify(requestedScopes),
					},
					__("Submitting governed emergency exception…")
				);
				dialog.hide();
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	frappe.ui.form.on("IONE Surgery QC", {
		refresh: (frm) => {
			if (
				frm.is_new() ||
				frappe.session.user === "Administrator" ||
				!hasRole(RECORD_ROLES) ||
				frm.doc.surgery_phase !== "Scheduled"
			) {
				return;
			}
			frm.add_custom_button(__("Record MDT"), () => mdtDialog(frm), GROUP);
			frm.add_custom_button(
				__("Complete Safety Checklist"),
				() => checklistDialog(frm),
				GROUP
			);
			frm.add_custom_button(
				__("Request Emergency Exception"),
				() => exceptionDialog(frm),
				GROUP
			);
		},
	});

	frappe.ui.form.on("IONE Surgery Emergency Exception", {
		refresh: (frm) => {
			if (
				frm.is_new() ||
				frm.doc.status !== "Pending" ||
				frm.doc.requested_by === frappe.session.user ||
				frappe.session.user === "Administrator" ||
				!hasRole(REVIEW_ROLES)
			) {
				return;
			}
			for (const decision of ["Approve", "Reject"]) {
				frm.add_custom_button(
					__(decision),
					() =>
						commentDialog(frm, {
							title: __(`${decision} Emergency Exception`),
							action: decision,
							method: API.reviewException,
							argument: "exception",
							value: frm.doc.name,
						}),
					GROUP
				);
			}
		},
	});
})();

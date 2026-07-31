(() => {
	"use strict";

	const POLICY_REVIEW_ROLES = ["IONE QC Reviewer", "IONE Medical Affairs"];
	const CODER_ROLE = "IONE Medical Record Coder";
	const EXPERT_ROLE = "IONE Medical Record Expert Reviewer";
	const MEDICAL_AFFAIRS_ROLE = "IONE Medical Affairs";
	const OUTCOMES = ["Pass", "Defect", "Needs Correction", "Unable to Determine"];

	const has_role = (roles) =>
		roles.some((role) => (frappe.user_roles || []).includes(role));

	const governed_call = async (frm, method, args, message) => {
		await frappe.call({
			method,
			type: "POST",
			args,
			freeze: true,
			freeze_message: __("Applying the governed medical-record transition…"),
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

	const review_policy = (frm, decision, label) =>
		comment_dialog(label, __("Review Comment"), (comment) =>
			governed_call(
				frm,
				"ione_qms.api.medical_record.review_medical_record_sampling_policy",
				{
					policy: frm.doc.name,
					decision,
					review_comment: comment,
				},
				__("Medical-record sampling policy reviewed."),
			),
		);

	const operate_policy = (frm, action, label) =>
		comment_dialog(label, __("Operation Comment"), (comment) =>
			governed_call(
				frm,
				"ione_qms.api.medical_record.operate_medical_record_sampling_policy",
				{
					policy: frm.doc.name,
					action,
					operation_comment: comment,
				},
				__("Medical-record sampling policy updated."),
			),
		);

	const create_batch = (frm) => {
		frappe.prompt(
			[
				{
					fieldname: "period_start",
					fieldtype: "Date",
					label: __("Period Start"),
					reqd: 1,
				},
				{
					fieldname: "period_end",
					fieldtype: "Date",
					label: __("Period End"),
					reqd: 1,
				},
				{
					fieldname: "completeness_watermark",
					fieldtype: "Link",
					options: "IONE Medical Record Completeness Watermark",
					label: __("Signed Source Completeness Watermark"),
					reqd: 1,
				},
				{
					fieldname: "parent_batch",
					fieldtype: "Link",
					options: "IONE Medical Record Review Batch",
					label: __("Parent Batch (required only for late-record supplement)"),
				},
			],
			(values) =>
				governed_call(
					frm,
					"ione_qms.api.medical_record.create_medical_record_review_batch",
					{
						policy: frm.doc.name,
						period_start: values.period_start,
						period_end: values.period_end,
						completeness_watermark: values.completeness_watermark,
						parent_batch: values.parent_batch || null,
					},
					__("Medical-record review batch created."),
				),
			__("Create Review Batch"),
		);
	};

	const evidence_references = (value) =>
		(value || "")
			.split(/[\n,]/)
			.map((item) => item.trim())
			.filter(Boolean);

	const submit_review = (frm, stage) => {
		const method =
			stage === "Coder"
				? "ione_qms.api.medical_record.submit_medical_record_coder_review"
				: "ione_qms.api.medical_record.submit_medical_record_expert_review";
		const title =
			stage === "Coder" ? __("Submit Coder Review") : __("Submit Expert Review");
		frappe.prompt(
			[
				{
					fieldname: "outcome",
					fieldtype: "Select",
					label: __("Outcome"),
					options: OUTCOMES.join("\n"),
					reqd: 1,
				},
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label: __("Review Comment"),
					reqd: 1,
				},
				{
					fieldname: "finding",
					fieldtype: "Link",
					options: "IONE QC Finding",
					label: __("Confirmed Finding"),
				},
				{
					fieldname: "evidence_references",
					fieldtype: "Small Text",
					label: __("Evidence References (one per line)"),
					description: __(
						"Use governed references in the form IONE QC Finding Evidence:&lt;name&gt;.",
					),
				},
			],
			(values) =>
				governed_call(
					frm,
					method,
					{
						assignment: frm.doc.name,
						outcome: values.outcome,
						comment: values.comment,
						finding: values.finding || null,
						evidence_references: evidence_references(values.evidence_references),
					},
					__("Medical-record review submitted."),
				),
			title,
		);
	};

	frappe.ui.form.on("IONE Medical Record Sampling Policy", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (
				frm.doc.status === "Draft" &&
				has_role(POLICY_REVIEW_ROLES) &&
				frm.doc.requested_by !== frappe.session.user
			) {
				frm.add_custom_button(
					__("Approve Policy"),
					() => review_policy(frm, "Approve", __("Approve Policy")),
					__("Governance"),
				);
				frm.add_custom_button(
					__("Reject Policy"),
					() => review_policy(frm, "Reject", __("Reject Policy")),
					__("Governance"),
				);
			}
			if (frm.doc.status === "Approved" && has_role(POLICY_REVIEW_ROLES)) {
				frm.add_custom_button(
					__("Create Review Batch"),
					() => create_batch(frm),
					__("Review"),
				);
			}
			if (!has_role([MEDICAL_AFFAIRS_ROLE])) {
				return;
			}
			const actions =
				frm.doc.status === "Approved"
					? [
							["Suspend", __("Suspend Policy")],
							["Retire", __("Retire Policy")],
						]
					: frm.doc.status === "Suspended"
						? [
								["Activate", __("Resume Policy")],
								["Retire", __("Retire Policy")],
							]
						: [];
			actions.forEach(([action, label]) => {
				frm.add_custom_button(
					label,
					() => operate_policy(frm, action, label),
					__("Governance"),
				);
			});
		},
	});

	frappe.ui.form.on("IONE Medical Record Review Assignment", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (frm.doc.status === "Coder Review" && has_role([CODER_ROLE])) {
				if (!frm.doc.assigned_coder) {
					frm.add_custom_button(__("Claim Coder Review"), () =>
						governed_call(
							frm,
							"ione_qms.api.medical_record.claim_medical_record_coder_assignment",
							{ assignment: frm.doc.name },
							__("Coder review claimed."),
						),
					);
				} else if (frm.doc.assigned_coder === frappe.session.user) {
					frm.add_custom_button(__("Submit Coder Review"), () =>
						submit_review(frm, "Coder"),
					);
				}
			}
			if (frm.doc.status === "Expert Review" && has_role([EXPERT_ROLE])) {
				if (!frm.doc.assigned_expert) {
					frm.add_custom_button(__("Claim Expert Review"), () =>
						governed_call(
							frm,
							"ione_qms.api.medical_record.claim_medical_record_expert_assignment",
							{ assignment: frm.doc.name },
							__("Expert review claimed."),
						),
					);
				} else if (frm.doc.assigned_expert === frappe.session.user) {
					frm.add_custom_button(__("Submit Expert Review"), () =>
						submit_review(frm, "Expert"),
					);
				}
			}
		},
	});

	frappe.ui.form.on("IONE Medical Record Archive Decision", {
		refresh(frm) {
			if (frm.is_new() || !has_role([MEDICAL_AFFAIRS_ROLE])) {
				return;
			}
			frm.add_custom_button(
				__("Create Override Decision"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "target_decision",
								fieldtype: "Select",
								label: __("Decision"),
								options: ["Allow", "Hold"].join("\n"),
								reqd: 1,
							},
							{
								fieldname: "reason_code",
								fieldtype: "Data",
								label: __("Approved Reason Code"),
								reqd: 1,
							},
							{
								fieldname: "comment",
								fieldtype: "Small Text",
								label: __("Override Comment"),
								reqd: 1,
							},
						],
						(values) =>
							governed_call(
								frm,
								"ione_qms.api.medical_record.override_medical_record_archive_decision",
								{
									archive_decision: frm.doc.name,
									target_decision: values.target_decision,
									reason_code: values.reason_code,
									comment: values.comment,
								},
								__("Archive override decision created."),
							),
						__("Create Override Decision"),
					);
				},
				__("Governance"),
			);
		},
	});
})();

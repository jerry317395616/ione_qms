(() => {
	"use strict";

	const SUBMITTER_ROLES = [
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
	];
	const REVIEWER_ROLES = ["IONE QC Reviewer", "IONE Medical Affairs"];
	const APPEAL_GROUP = __("Finding Appeal");
	const API = {
		submit: "ione_qms.api.improvement.submit_finding_appeal",
		discard: "ione_qms.api.improvement.discard_unbound_finding_appeal_evidence",
		review: "ione_qms.api.improvement.review_finding_appeal",
		withdraw: "ione_qms.api.improvement.withdraw_finding_appeal",
	};
	const GOVERNED_WORKFLOW_ACTIONS = [
		"Appeal Finding",
		"Withdraw Finding Appeal",
		"Approve Finding Appeal",
		"Reject Finding Appeal",
	];

	const has_role = (allowed) =>
		allowed.some((role) => (frappe.user_roles || []).includes(role));

	const call_governed_api = async (method, args) => {
		const response = await frappe.call({
			method,
			args,
			type: "POST",
			freeze: true,
			freeze_message: __("Updating the finding appeal…"),
		});
		return response.message;
	};

	const discard_unbound_evidence = async (upload) => {
		if (!upload?.name || !upload?.content_hash) {
			return;
		}
		try {
			await call_governed_api(API.discard, {
				evidence_file: upload.name,
				evidence_content_hash: upload.content_hash,
			});
		} catch {
			// The server intentionally refuses cleanup after an upload has been bound.
		}
	};

	const suppress_governed_workflow_actions = (frm) => {
		const remove_items = () => {
			const labels = new Set(GOVERNED_WORKFLOW_ACTIONS.map((action) => __(action)));
			frm.page.actions.find(".menu-item-label").each((_index, element) => {
				if (labels.has($(element).text().trim())) {
					$(element).closest("li").remove();
				}
			});
			if (!frm.page.actions.find(".dropdown-item").length) {
				frm.page.actions.parent().addClass("hide");
			}
		};
		if (!frm.__ione_appeal_action_observer) {
			frm.__ione_appeal_action_observer = new MutationObserver(remove_items);
			frm.__ione_appeal_action_observer.observe(frm.page.actions.get(0), {
				childList: true,
				subtree: true,
			});
		}
		remove_items();
	};

	const submit_dialog = (frm) => {
		let secure_upload = null;
		const dialog = new frappe.ui.Dialog({
			title: __("Submit Finding Appeal"),
			fields: [
				{
					fieldname: "appeal_type",
					fieldtype: "Select",
					label: __("Appeal Type"),
					options: [
						"Factual Dispute",
						"Rule Applicability",
						"Clinical Exception",
						"Evidence Dispute",
						"Other",
					].join("\n"),
					reqd: 1,
				},
				{
					fieldname: "reason",
					fieldtype: "Long Text",
					label: __("Reason"),
					reqd: 1,
				},
				{
					fieldname: "evidence_summary",
					fieldtype: "Long Text",
					label: __("Evidence Summary"),
				},
				{
					fieldname: "evidence_upload",
					fieldtype: "Attach",
					label: __("Private Evidence Attachment"),
					options: {
						make_attachments_public: false,
						allow_toggle_private: false,
						allow_web_link: false,
						allow_google_drive: false,
						allow_toggle_optimize: false,
						allow_take_photo: false,
						disable_file_browser: true,
						on_success: async (file) => {
							if (
								!file?.name ||
								!file?.content_hash ||
								!file?.file_url?.startsWith("/private/files/") ||
								Number(file?.is_private) !== 1
							) {
								await discard_unbound_evidence(file);
								frappe.throw(
									__("Finding appeal evidence must be a private local upload.")
								);
							}
							if (secure_upload && secure_upload.name !== file.name) {
								await discard_unbound_evidence(secure_upload);
							}
							secure_upload = {
								name: file.name,
								content_hash: file.content_hash,
								file_url: file.file_url,
							};
							await dialog.set_value("evidence_upload", file.file_url);
						},
						restrictions: {
							max_file_size: 20 * 1024 * 1024,
						},
					},
					description: __("Only new private local files up to 20 MB are accepted."),
				},
			],
			primary_action_label: __("Submit Appeal"),
			primary_action: async (values) => {
				const attachment = values.evidence_upload || "";
				if (!attachment && secure_upload) {
					await discard_unbound_evidence(secure_upload);
					secure_upload = null;
				}
				if (
					attachment &&
					(!secure_upload ||
						secure_upload.file_url !== attachment ||
						!attachment.startsWith("/private/files/"))
				) {
					frappe.throw(__("Finding appeal evidence upload identity is inconsistent."));
				}
				try {
					await call_governed_api(API.submit, {
						finding: frm.doc.name,
						appeal_type: values.appeal_type,
						reason: values.reason,
						evidence_summary: values.evidence_summary,
						evidence_file: secure_upload?.name || "",
						evidence_content_hash: secure_upload?.content_hash || "",
					});
				} catch (error) {
					await discard_unbound_evidence(secure_upload);
					secure_upload = null;
					throw error;
				}
				secure_upload = null;
				dialog.hide();
				frappe.show_alert({ message: __("Finding appeal submitted."), indicator: "green" });
				await frm.reload_doc();
			},
			onhide: () => {
				void discard_unbound_evidence(secure_upload);
				secure_upload = null;
			},
		});
		dialog.show();
	};

	const review_dialog = (frm, appeal, decision) => {
		const approving = decision === "Approve";
		const dialog = new frappe.ui.Dialog({
			title: approving ? __("Approve Finding Appeal") : __("Reject Finding Appeal"),
			fields: [
				{
					fieldname: "review_comment",
					fieldtype: "Small Text",
					label: __("Review Comment"),
					reqd: 1,
				},
			],
			primary_action_label: approving ? __("Approve") : __("Reject"),
			primary_action: async (values) => {
				await call_governed_api(API.review, {
					appeal: appeal.name,
					decision,
					review_comment: values.review_comment,
				});
				dialog.hide();
				frappe.show_alert({ message: __("Finding appeal reviewed."), indicator: "green" });
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	const withdraw_dialog = (frm, appeal) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Withdraw Finding Appeal"),
			fields: [
				{
					fieldname: "withdraw_comment",
					fieldtype: "Small Text",
					label: __("Withdrawal Comment"),
					reqd: 1,
				},
			],
			primary_action_label: __("Withdraw Appeal"),
			primary_action: async (values) => {
				await call_governed_api(API.withdraw, {
					appeal: appeal.name,
					withdraw_comment: values.withdraw_comment,
				});
				dialog.hide();
				frappe.show_alert({ message: __("Finding appeal withdrawn."), indicator: "green" });
				await frm.reload_doc();
			},
		});
		dialog.show();
	};

	const active_appeal = async (finding) => {
		const response = await frappe.db.get_value(
			"IONE QC Finding Appeal",
			{ active_key: finding },
			["name", "submitted_by"]
		);
		if (!response.message?.name) {
			return null;
		}
		return response.message;
	};

	frappe.ui.form.on("IONE QC Finding", {
		refresh: async (frm) => {
			suppress_governed_workflow_actions(frm);
			if (frm.is_new() || frappe.session.user === "Administrator") {
				return;
			}
			if (frm.doc.status === "Confirmed" && has_role(SUBMITTER_ROLES)) {
				frm.add_custom_button(
					__("Submit Appeal"),
					() => submit_dialog(frm),
					APPEAL_GROUP
				);
				return;
			}
			if (frm.doc.status !== "Appealed") {
				return;
			}
			const appeal = await active_appeal(frm.doc.name);
			if (!appeal || frm.doc.status !== "Appealed") {
				return;
			}
			const current_user = frappe.session.user;
			if (has_role(SUBMITTER_ROLES) && appeal.submitted_by === current_user) {
				frm.add_custom_button(
					__("Withdraw Appeal"),
					() => withdraw_dialog(frm, appeal),
					APPEAL_GROUP
				);
			}
			if (has_role(REVIEWER_ROLES) && appeal.submitted_by !== current_user) {
				frm.add_custom_button(
					__("Approve Appeal"),
					() => review_dialog(frm, appeal, "Approve"),
					APPEAL_GROUP
				);
				frm.add_custom_button(
					__("Reject Appeal"),
					() => review_dialog(frm, appeal, "Reject"),
					APPEAL_GROUP
				);
			}
		},
		before_workflow_action: (frm) => {
			if (GOVERNED_WORKFLOW_ACTIONS.includes(frm.selected_workflow_action)) {
				frappe.throw(__("Finding appeal transitions must use the governed appeal actions."));
			}
		},
	});
})();

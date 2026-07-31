frappe.ui.form.on("IONE Finding Recurrence Policy", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		if (frm.doc.status === "Draft" && !frm.doc.requested_at) {
			frm.add_custom_button(__("Request Approval"), () => {
				comment_dialog(__("Approval Request Comment"), (comment) => {
					call_policy_api(
						frm,
						"ione_qms.api.pdca.request_finding_recurrence_policy_approval",
						{ policy: frm.doc.name, comment },
					);
				});
			});
		}

		if (frm.doc.status === "Draft" && frm.doc.requested_at) {
			for (const decision of ["Approve", "Reject"]) {
				frm.add_custom_button(
					__(decision),
					() => {
						comment_dialog(__("Review Comment"), (review_comment) => {
							call_policy_api(
								frm,
								"ione_qms.api.pdca.review_finding_recurrence_policy",
								{
									policy: frm.doc.name,
									decision,
									review_comment,
								},
							);
						});
					},
					__("Review"),
				);
			}
		}

		const operations = {
			Approved: ["Suspend", "Retire"],
			Suspended: ["Resume", "Retire"],
		};
		for (const action of operations[frm.doc.status] || []) {
			frm.add_custom_button(
				__(action),
				() => {
					comment_dialog(__("Operation Comment"), (operation_comment) => {
						call_policy_api(
							frm,
							"ione_qms.api.pdca.operate_finding_recurrence_policy",
							{
								policy: frm.doc.name,
								action,
								operation_comment,
							},
						);
					});
				},
				__("Governance"),
			);
		}

		if (frm.doc.status === "Approved" && frm.doc.enabled) {
			frm.add_custom_button(__("Run Recurrence Scan"), () => {
				const dialog = new frappe.ui.Dialog({
					title: __("Run Finding Recurrence Scan"),
					fields: [
						{
							fieldname: "as_of",
							fieldtype: "Date",
							label: __("As-of Date"),
							reqd: 1,
							default: frappe.datetime.get_today(),
						},
					],
					primary_action_label: __("Run"),
					primary_action(values) {
						dialog.hide();
						frappe.call({
							method: "ione_qms.api.pdca.run_finding_recurrence_policy_scan",
							type: "POST",
							args: { policy: frm.doc.name, as_of: values.as_of },
							freeze: true,
							callback(response) {
								const result = response.message || {};
								frappe.msgprint(
									__("Recurrence run {0} evaluated {1} finding(s).", [
										result.run || "",
										result.finding_count || 0,
									]),
								);
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

function comment_dialog(label, callback) {
	const dialog = new frappe.ui.Dialog({
		title: label,
		fields: [
			{
				fieldname: "comment",
				fieldtype: "Small Text",
				label,
				reqd: 1,
			},
		],
		primary_action_label: __("Submit"),
		primary_action(values) {
			dialog.hide();
			callback(values.comment);
		},
	});
	dialog.show();
}

function call_policy_api(frm, method, args) {
	frappe.call({
		method,
		type: "POST",
		args,
		freeze: true,
		callback() {
			frm.reload_doc();
		},
	});
}

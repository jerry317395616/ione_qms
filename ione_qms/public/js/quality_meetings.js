(() => {
	const API = "ione_qms.api.quality_meetings.";

	function post(method, args, onSuccess) {
		return frappe.call({
			method: API + method,
			type: "POST",
			args,
			freeze: true,
			freeze_message: __("Processing governed workflow action..."),
			callback(response) {
				if (!response.exc) {
					if (onSuccess) {
						onSuccess(response.message || {});
					}
					frappe.show_alert({message: __("Workflow action completed"), indicator: "green"});
				}
			},
		});
	}

	function requestKey() {
		if (window.crypto && typeof window.crypto.randomUUID === "function") {
			return window.crypto.randomUUID();
		}
		return `${Date.now()}-${frappe.utils.get_random(32)}`;
	}

	function commentDialog(title, primaryLabel, callback) {
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label: __("Review comment"),
					reqd: 1,
				},
			],
			primary_action_label: primaryLabel,
			primary_action(values) {
				dialog.hide();
				callback(values.comment);
			},
		});
		dialog.show();
	}

	function reload(frm) {
		return frm.reload_doc();
	}

	frappe.ui.form.on("IONE Quality Meeting", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (frm.doc.status === "Draft") {
				frm.add_custom_button(__("Submit"), () => {
					post("submit_quality_meeting", {meeting: frm.doc.name}, () => reload(frm));
				});
				frm.add_custom_button(__("Cancel"), () => {
					commentDialog(__("Cancel meeting"), __("Cancel"), (reason) => {
						post("cancel_quality_meeting", {meeting: frm.doc.name, reason}, () => reload(frm));
					});
				});
			}
			if (frm.doc.status === "Submitted") {
				frm.add_custom_button(__("Approve"), () => {
					commentDialog(__("Approve meeting"), __("Approve"), (review_comment) => {
						post(
							"review_quality_meeting",
							{meeting: frm.doc.name, decision: "Approve", review_comment},
							() => reload(frm),
						);
					});
				});
				frm.add_custom_button(__("Cancel"), () => {
					commentDialog(__("Cancel meeting"), __("Cancel"), (review_comment) => {
						post(
							"review_quality_meeting",
							{meeting: frm.doc.name, decision: "Cancel", review_comment},
							() => reload(frm),
						);
					});
				});
			}
			if (frm.doc.status === "Approved") {
				frm.add_custom_button(__("Record held meeting"), () => heldMeetingDialog(frm));
				frm.add_custom_button(__("Cancel"), () => {
					commentDialog(__("Cancel meeting"), __("Cancel"), (reason) => {
						post("cancel_quality_meeting", {meeting: frm.doc.name, reason}, () => reload(frm));
					});
				});
			}
			if (frm.doc.status === "Held") {
				frm.add_custom_button(__("Create action item"), () => {
					actionItemDialog({meeting: frm.doc.name});
				});
				frm.add_custom_button(__("Open minute preparation"), () => {
					post("mark_meeting_minutes_pending", {meeting: frm.doc.name}, () => reload(frm));
				});
			}
			if (frm.doc.status === "Minutes Pending") {
				frm.add_custom_button(__("Create action item"), () => {
					actionItemDialog({meeting: frm.doc.name});
				});
				frm.add_custom_button(__("Close with approved minute"), () => {
					frappe.confirm(
						__("Close this meeting using its approved, checksummed minute?"),
						() => post("close_quality_meeting", {meeting: frm.doc.name}, () => reload(frm)),
					);
				});
			}
			if (frm.doc.status === "Closed") {
				frm.add_custom_button(__("Create action item"), () => {
					actionItemDialog({meeting: frm.doc.name});
				});
			}
		},
	});

	function heldMeetingDialog(frm) {
		const attendanceTemplate = (frm.doc.attendees || []).map((row) => ({
			user: row.user,
			attendance_status: "Attended",
			joined_at: frm.doc.scheduled_start,
			left_at: frm.doc.scheduled_end,
			attendance_evidence: "",
			absence_reason: "",
		}));
		const dialog = new frappe.ui.Dialog({
			title: __("Record held meeting"),
			fields: [
				{
					fieldname: "actual_start",
					fieldtype: "Datetime",
					label: __("Actual start"),
					reqd: 1,
				},
				{
					fieldname: "actual_end",
					fieldtype: "Datetime",
					label: __("Actual end"),
					reqd: 1,
				},
				{
					fieldname: "attendance",
					fieldtype: "Code",
					options: "JSON",
					label: __("Attendance JSON"),
					description: __(
						"Record every approved attendee as Attended or Absent and include evidence.",
					),
					default: JSON.stringify(attendanceTemplate, null, 2),
					reqd: 1,
				},
				{
					fieldname: "evidence_reference",
					fieldtype: "Small Text",
					label: __("Meeting evidence reference"),
					reqd: 1,
				},
			],
			primary_action_label: __("Record held"),
			primary_action(values) {
				let attendance;
				try {
					attendance = JSON.parse(values.attendance);
				} catch (_error) {
					frappe.msgprint(__("Attendance must be valid JSON."));
					return;
				}
				dialog.hide();
				post(
					"record_quality_meeting_held",
					{
						meeting: frm.doc.name,
						actual_start: values.actual_start,
						actual_end: values.actual_end,
						attendance,
						evidence_reference: values.evidence_reference,
					},
					() => reload(frm),
				);
			},
		});
		dialog.show();
	}

	frappe.ui.form.on("IONE Meeting Minute", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (frm.doc.status === "Draft") {
				frm.add_custom_button(__("Submit"), () => {
					post("submit_meeting_minute", {minute: frm.doc.name}, () => reload(frm));
				});
			}
			if (frm.doc.status === "Submitted") {
				frm.add_custom_button(__("Approve"), () => {
					commentDialog(__("Approve minute"), __("Approve"), (review_comment) => {
						post(
							"review_meeting_minute",
							{minute: frm.doc.name, decision: "Approve", review_comment},
							() => reload(frm),
						);
					});
				});
				frm.add_custom_button(__("Reject"), () => {
					commentDialog(__("Reject minute"), __("Reject"), (review_comment) => {
						post(
							"review_meeting_minute",
							{minute: frm.doc.name, decision: "Reject", review_comment},
							() => reload(frm),
						);
					});
				});
			}
		},
	});

	frappe.ui.form.on("IONE Meeting Decision", {
		refresh(frm) {
			if (!frm.is_new()) {
				frm.add_custom_button(__("Create action item"), () => {
					actionItemDialog({meeting_decision: frm.doc.name});
				});
			}
		},
	});

	function actionItemDialog(sourceLinks) {
		const dialog = new frappe.ui.Dialog({
			title: __("Create governed quality action item"),
			fields: [
				{fieldname: "title", fieldtype: "Data", label: __("Title"), reqd: 1},
				{
					fieldname: "description",
					fieldtype: "Text",
					label: __("Action description"),
					reqd: 1,
				},
				{
					fieldname: "assigned_to",
					fieldtype: "Link",
					options: "User",
					label: __("Action owner"),
					reqd: 1,
				},
				{
					fieldname: "verifier",
					fieldtype: "Link",
					options: "User",
					label: __("Independent verifier"),
					reqd: 1,
				},
				{fieldname: "due_date", fieldtype: "Date", label: __("Due date"), reqd: 1},
			],
			primary_action_label: __("Create"),
			primary_action(values) {
				dialog.hide();
				post(
					"create_quality_action_item",
					{...values, ...sourceLinks},
					(message) => {
						if (message.action_item) {
							frappe.set_route("Form", "IONE Quality Action Item", message.action_item);
						}
					},
				);
			},
		});
		dialog.show();
	}

	frappe.ui.form.on("IONE Quality Action Item", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (frm.doc.status === "Open") {
				frm.add_custom_button(__("Start"), () => {
					post("start_quality_action_item", {action_item: frm.doc.name}, () => reload(frm));
				});
			}
			if (frm.doc.status === "In Progress") {
				frm.add_custom_button(__("Submit for verification"), () => {
					const dialog = new frappe.ui.Dialog({
						title: __("Submit action evidence"),
						fields: [
							{
								fieldname: "completion_evidence",
								fieldtype: "Text",
								label: __("Completion evidence"),
								reqd: 1,
							},
							{
								fieldname: "effect_result",
								fieldtype: "Text",
								label: __("Effect result"),
								reqd: 1,
							},
						],
						primary_action_label: __("Submit"),
						primary_action(values) {
							dialog.hide();
							post(
								"submit_quality_action_verification",
								{action_item: frm.doc.name, request_key: requestKey(), ...values},
								() => reload(frm),
							);
						},
					});
					dialog.show();
				});
			}
			if (frm.doc.status === "Pending Verification") {
				frm.add_custom_button(__("Close"), () => {
					commentDialog(__("Verify action effect"), __("Close"), (verification_comment) => {
						post(
							"review_quality_action_item",
							{
								action_item: frm.doc.name,
								decision: "Close",
								verification_comment,
								request_key: requestKey(),
							},
							() => reload(frm),
						);
					});
				});
				frm.add_custom_button(__("Return for rework"), () => {
					commentDialog(__("Return action item"), __("Return"), (verification_comment) => {
						post(
							"review_quality_action_item",
							{
								action_item: frm.doc.name,
								decision: "Rework",
								verification_comment,
								request_key: requestKey(),
							},
							() => reload(frm),
						);
					});
				});
			}
			if (["Open", "In Progress", "Pending Verification"].includes(frm.doc.status)) {
				frm.add_custom_button(__("Cancel"), () => {
					commentDialog(__("Cancel action item"), __("Cancel"), (reason) => {
						post(
							"cancel_quality_action_item",
							{action_item: frm.doc.name, reason, request_key: requestKey()},
							() => reload(frm),
						);
					});
				});
			}
		},
	});

	frappe.ui.form.on("IONE Quality Experience Share", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (frm.doc.status === "Draft") {
				frm.add_custom_button(__("Submit"), () => {
					post("submit_quality_experience_share", {experience: frm.doc.name}, () => reload(frm));
				});
			}
			if (frm.doc.status === "Submitted") {
				frm.add_custom_button(__("Publish"), () => {
					commentDialog(__("Publish deidentified experience"), __("Publish"), (review_comment) => {
						post(
							"review_quality_experience_share",
							{experience: frm.doc.name, decision: "Publish", review_comment},
							() => reload(frm),
						);
					});
				});
				frm.add_custom_button(__("Reject"), () => {
					commentDialog(__("Reject experience"), __("Reject"), (review_comment) => {
						post(
							"review_quality_experience_share",
							{experience: frm.doc.name, decision: "Reject", review_comment},
							() => reload(frm),
						);
					});
				});
			}
			if (frm.doc.status === "Published") {
				frm.add_custom_button(__("Retire"), () => {
					commentDialog(__("Retire experience"), __("Retire"), (reason) => {
						post(
							"retire_quality_experience_share",
							{experience: frm.doc.name, reason},
							() => reload(frm),
						);
					});
				});
			}
		},
	});
})();

const IONE_PHI_IDENTITY_FIELDS = {
	"IONE Patient Index": [
		["source_patient_id", __("Source Patient ID")],
		["patient_name", __("Patient Name")],
		["date_of_birth", __("Date of Birth")],
		["identification_hash", __("Identification Hash")],
		["phone_hash", __("Phone Hash")],
	],
	"IONE Encounter Index": [
		["source_encounter_id", __("Source Encounter ID")],
		["encounter_no", __("Encounter No")],
	],
};

function ione_phi_request_id() {
	const bytes = new Uint8Array(16);
	window.crypto.getRandomValues(bytes);
	return `phi-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function ione_render_phi_result(message) {
	const rows = Object.entries(message.data || {})
		.map(
			([fieldname, value]) =>
				`<tr><th>${frappe.utils.escape_html(fieldname)}</th><td>${frappe.utils.escape_html(
					value === null || value === undefined ? "" : String(value),
				)}</td></tr>`,
		)
		.join("");
	const receipt = frappe.utils.escape_html(String(message.receipt || ""));
	frappe.msgprint({
		title: __("Controlled PHI Disclosure"),
		indicator: "orange",
		message:
			`<p>${__(
				"Identity data is displayed for the approved purpose only. Do not copy it into uncontrolled channels.",
			)}</p>` +
			`<table class="table table-bordered">${rows}</table>` +
			`<p><strong>${__("Audit Receipt")}:</strong> ${receipt}</p>`,
	});
}

function ione_open_phi_dialog(frm) {
	const field_specs = IONE_PHI_IDENTITY_FIELDS[frm.doctype] || [];
	const dialog_fields = [
		{
			fieldname: "purpose_code",
			fieldtype: "Data",
			label: __("Approved Purpose Code"),
			reqd: 1,
		},
		{
			fieldname: "field_section",
			fieldtype: "Section Break",
			label: __("Minimum Necessary Fields"),
		},
		...field_specs.map(([fieldname, label]) => ({
			fieldname: `include_${fieldname}`,
			fieldtype: "Check",
			label,
			default: 0,
		})),
	];
	const dialog = new frappe.ui.Dialog({
		title: __("Controlled Patient Identity Access"),
		fields: dialog_fields,
		primary_action_label: __("Disclose and Audit"),
		primary_action: async (values) => {
			const fields = field_specs
				.filter(([fieldname]) => values[`include_${fieldname}`])
				.map(([fieldname]) => fieldname);
			if (!fields.length) {
				frappe.msgprint(__("Select at least one minimum-necessary identity field."));
				return;
			}
			dialog.disable_primary_action();
			try {
				const result = await frappe.call({
					method: "ione_qms.api.privacy.read_phi_identity",
					type: "POST",
					args: {
						reference_doctype: frm.doctype,
						reference_name: frm.doc.name,
						fields,
						purpose_code: values.purpose_code,
						request_id: ione_phi_request_id(),
					},
					freeze: true,
					freeze_message: __("Authorizing and recording PHI disclosure..."),
				});
				dialog.hide();
				ione_render_phi_result(result.message || {});
			} finally {
				dialog.enable_primary_action();
			}
		},
	});
	dialog.show();
}

for (const doctype of Object.keys(IONE_PHI_IDENTITY_FIELDS)) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (
				frm.is_new() ||
				frappe.session.user === "Administrator" ||
				!frappe.user_roles.includes("IONE PHI Identity Reader")
			) {
				return;
			}
			frm.add_custom_button(
				__("Read Patient Identity"),
				() => ione_open_phi_dialog(frm),
				__("Privacy"),
			);
		},
	});
}

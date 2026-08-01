frappe.pages["ione-quality-action-workbench"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Quality Action Workbench"),
		single_column: true,
	});
	const workbench = new IONEQualityActionWorkbench(page, wrapper);
	$(wrapper).on("show", () => workbench.refresh());
};

class IONEQualityActionWorkbench {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.loading = false;
		this.filters = {status: "", role: ""};
		this.body = $('<div class="container-fluid py-3"></div>').appendTo(
			this.wrapper.find(".page-content").empty(),
		);
		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.page.add_field({
			fieldname: "action_role",
			label: __("View"),
			fieldtype: "Select",
			options: [
				{label: __("Role default"), value: ""},
				{label: __("My owned actions"), value: "Owner"},
				{label: __("My verification queue"), value: "Verifier"},
				{label: __("Authorized oversight scope"), value: "All"},
			],
			change: () => {
				this.filters.role = this.page.fields_dict.action_role.get_value() || "";
				this.refresh();
			},
		});
		this.page.add_field({
			fieldname: "action_status",
			label: __("Status"),
			fieldtype: "Select",
			options: [
				{label: __("All statuses"), value: ""},
				{label: __("Open"), value: "Open"},
				{label: __("In Progress"), value: "In Progress"},
				{label: __("Pending Verification"), value: "Pending Verification"},
				{label: __("Closed"), value: "Closed"},
				{label: __("Cancelled"), value: "Cancelled"},
			],
			change: () => {
				this.filters.status = this.page.fields_dict.action_status.get_value() || "";
				this.refresh();
			},
		});
	}

	async refresh() {
		if (this.loading) {
			return;
		}
		this.loading = true;
		this.page.set_indicator(__("Loading"), "orange");
		try {
			const response = await frappe.call({
				method: "ione_qms.api.quality_meetings.quality_action_workbench",
				type: "POST",
				args: {
					status: this.filters.status || null,
					role: this.filters.role || null,
					limit: 200,
				},
			});
			this.render(response.message || {items: [], counts: {}});
			this.page.set_indicator(__("Updated"), "green");
		} catch (error) {
			this.renderError(error);
			this.page.set_indicator(__("Load failed"), "red");
		} finally {
			this.loading = false;
		}
	}

	render(data) {
		this.body.empty();
		const counts = data.counts || {};
		const summary = $('<div class="row mb-3"></div>').appendTo(this.body);
		this.summaryCard(summary, __("Visible actions"), counts.total || 0, "blue");
		this.summaryCard(summary, __("Overdue"), counts.overdue || 0, "red");
		this.summaryCard(
			summary,
			__("Pending verification"),
			counts.pending_verification || 0,
			"orange",
		);

		const card = $('<section class="card"></section>').appendTo(this.body);
		const tableWrapper = $('<div class="table-responsive"></div>').appendTo(card);
		const table = $('<table class="table table-sm table-hover mb-0"></table>').appendTo(
			tableWrapper,
		);
		const header = $("<tr></tr>").appendTo($("<thead></thead>").appendTo(table));
		[
			__("Action"),
			__("Status"),
			__("Department"),
			__("Owner"),
			__("Verifier"),
			__("Due date"),
		].forEach((label) => $("<th></th>").text(label).appendTo(header));
		const tbody = $("<tbody></tbody>").appendTo(table);
		const items = data.items || [];
		if (!items.length) {
			$("<td></td>")
				.attr("colspan", 6)
				.addClass("text-muted py-3")
				.text(__("No quality action items are visible in the selected authorized scope."))
				.appendTo($("<tr></tr>").appendTo(tbody));
			return;
		}
		items.forEach((item) => {
			const row = $("<tr></tr>").appendTo(tbody);
			if (item.overdue) {
				row.addClass("table-danger");
			}
			const actionCell = $("<td></td>").appendTo(row);
			$('<button type="button" class="btn btn-link btn-sm p-0"></button>')
				.text(item.title || item.name)
				.on("click", () => frappe.set_route("Form", "IONE Quality Action Item", item.name))
				.appendTo(actionCell);
			$("<td></td>").text(item.status || "—").appendTo(row);
			$("<td></td>").text(item.department || "—").appendTo(row);
			$("<td></td>").text(item.assigned_to || "—").appendTo(row);
			$("<td></td>").text(item.verifier || "—").appendTo(row);
			$("<td></td>")
				.text(item.due_date || "—")
				.append(item.overdue ? $('<span class="badge badge-danger ml-2"></span>').text(__("Overdue")) : "")
				.appendTo(row);
		});
	}

	summaryCard(parent, label, value, indicator) {
		const column = $('<div class="col-12 col-md-4 mb-2"></div>').appendTo(parent);
		const card = $('<section class="card h-100"></section>').appendTo(column);
		const body = $('<div class="card-body py-3"></div>').appendTo(card);
		$("<div></div>").addClass("text-muted").text(label).appendTo(body);
		$("<h3></h3>")
			.addClass(`text-${indicator} mb-0`)
			.text(String(value))
			.appendTo(body);
	}

	renderError(error) {
		this.body.empty();
		const type = String(error?.exc_type || "");
		const status = Number(error?.httpStatus || error?.status || 0);
		const permissionDenied =
			status === 401 || status === 403 || type.includes("PermissionError");
		$('<div class="alert alert-danger" role="alert"></div>')
			.text(
				permissionDenied
					? __("Your account is not authorized for this action workbench view.")
					: __("The action workbench could not be loaded. Please retry or contact Quality Management."),
			)
			.appendTo(this.body);
	}
}

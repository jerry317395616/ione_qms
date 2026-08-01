frappe.pages["ione-quality-workbench"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("医疗质量业务工作台"),
		single_column: true,
	});
	const workbench = new IONEQualityWorkbench(page, wrapper);
	$(wrapper).on("show", () => workbench.refresh());
};

class IONEQualityWorkbench {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.loading = false;
		this.body = $('<div class="container-fluid py-3"></div>').appendTo(
			this.wrapper.find(".page-content").empty()
		);
		this.page.set_primary_action(__("刷新"), () => this.refresh(), "refresh");
	}

	async refresh() {
		if (this.loading) {
			return;
		}
		this.loading = true;
		this.page.set_indicator(__("正在加载"), "orange");
		try {
			const data = await frappe.xcall("ione_qms.api.dashboard.get_role_workbench");
			this.render(data);
			this.page.set_indicator(__("已更新"), "green");
		} catch (error) {
			this.render_error(error);
			this.page.set_indicator(__("加载失败"), "red");
		} finally {
			this.loading = false;
		}
	}

	render(data) {
		this.body.empty();
		this.render_scope(data);
		const grid = $('<div class="row"></div>').appendTo(this.body);
		this.section_definitions().forEach((definition) => {
			const column = $('<div class="col-12 col-xl-6 mb-3"></div>').appendTo(grid);
			this.render_section(
				column,
				definition,
				(data.sections && data.sections[definition.key]) || []
			);
		});
	}

	render_scope(data) {
		const card = $('<section class="card mb-3"></section>').appendTo(this.body);
		const body = $('<div class="card-body"></div>').appendTo(card);
		const persona_labels = {
			Physician: __("医生个人"),
			Department: __("科室管理"),
			Functional: __("职能管理"),
			Auditor: __("审计只读"),
		};
		$("<h4></h4>")
			.text(persona_labels[data.persona] || __("业务工作台"))
			.appendTo(body);
		$('<p class="text-muted mb-1"></p>')
			.text(__("数据范围：{0}", [this.scope_label(data.scope || {})]))
			.appendTo(body);
		if (data.read_only) {
			$('<span class="badge badge-secondary"></span>')
				.text(__("只读"))
				.appendTo(body);
		}
		if (
			(data.persona === "Physician" || data.persona === "Department") &&
			data.scope &&
			data.scope.staff_mapped === false
		) {
			$('<div class="alert alert-warning mt-3 mb-0" role="status"></div>')
				.text(
					__(
						"当前账号没有有效的医疗人员映射，系统已按最小权限返回空工作台。请联系医疗质量管理员维护人员映射。"
					)
				)
				.appendTo(body);
		}
	}

	scope_label(scope) {
		const labels = {
			Personal: __("仅本人负责记录"),
			"Authorized Departments": __("已授权科室"),
			"Permission-filtered Organization": __("权限允许的全院范围"),
			"Read-only Permission-filtered Organization": __("权限允许的全院只读范围"),
		};
		const base = labels[scope.mode] || __("按当前账号权限");
		if (scope.mode === "Authorized Departments" && scope.department_count !== null) {
			return __("{0}（{1} 个科室）", [base, Number(scope.department_count || 0)]);
		}
		return base;
	}

	render_section(parent, definition, rows) {
		const card = $('<section class="card h-100"></section>').appendTo(parent);
		const header = $('<div class="card-header d-flex justify-content-between"></div>').appendTo(card);
		$("<strong></strong>").text(definition.title).appendTo(header);
		$('<span class="badge badge-light"></span>').text(String(rows.length)).appendTo(header);
		const responsive = $('<div class="table-responsive"></div>').appendTo(card);
		const table = $('<table class="table table-sm table-hover mb-0"></table>').appendTo(responsive);
		const head_row = $("<tr></tr>").appendTo($("<thead></thead>").appendTo(table));
		definition.columns.forEach((column) => {
			$("<th></th>").text(column.label).appendTo(head_row);
		});
		const tbody = $("<tbody></tbody>").appendTo(table);
		if (!rows.length) {
			$("<td></td>")
				.attr("colspan", definition.columns.length)
				.addClass("text-muted py-3")
				.text(__("暂无待办或当前账号无权读取"))
				.appendTo($("<tr></tr>").appendTo(tbody));
			return;
		}
		rows.forEach((row) => {
			const table_row = $("<tr></tr>").appendTo(tbody);
			definition.columns.forEach((column, index) => {
				const cell = $("<td></td>").appendTo(table_row);
				const value = this.display_value(row[column.fieldname], column.fieldname);
				if (index === 0 && row.name) {
					$('<button type="button" class="btn btn-link btn-sm p-0"></button>')
						.text(value)
						.attr("aria-label", __("打开授权记录 {0}", [value]))
						.on("click", () =>
							frappe.set_route("Form", definition.doctype, row.name)
						)
						.appendTo(cell);
				} else {
					cell.text(value);
				}
			});
		});
	}

	display_value(value, fieldname) {
		if (fieldname === "near_miss" || fieldname === "anonymous_report") {
			return value ? __("是") : __("否");
		}
		if (value === null || value === undefined || value === "") {
			return "—";
		}
		return String(value);
	}

	render_error(error) {
		this.body.empty();
		const status = Number(error?.httpStatus || error?.status || 0);
		const type = String(error?.exc_type || "");
		const permission_denied =
			status === 401 || status === 403 || type.includes("PermissionError");
		$('<div class="alert alert-danger" role="alert"></div>')
			.text(
				permission_denied
					? __("当前账号没有角色化业务工作台访问权限。")
					: __("业务工作台暂时无法加载，请稍后重试或联系医疗质量管理员。")
			)
			.appendTo(this.body);
	}

	section_definitions() {
		return [
			{
				key: "findings",
				title: __("质量问题"),
				doctype: "IONE QC Finding",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "severity", label: __("等级")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "due_date", label: __("期限")},
				],
			},
			{
				key: "appeals",
				title: __("待处理申诉"),
				doctype: "IONE QC Finding Appeal",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "appeal_type", label: __("类型")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "submitted_at", label: __("提交时间")},
				],
			},
			{
				key: "rectifications",
				title: __("整改任务"),
				doctype: "IONE QC Rectification",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "due_date", label: __("期限")},
				],
			},
			{
				key: "verifications",
				title: __("复核任务"),
				doctype: "IONE QC Verification",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "modified", label: __("更新时间")},
				],
			},
			{
				key: "indicator_alerts",
				title: __("指标预警"),
				doctype: "IONE Indicator Alert",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "alert_level", label: __("预警级别")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "triggered_at", label: __("触发时间")},
				],
			},
			{
				key: "safety_events",
				title: __("医疗安全事件"),
				doctype: "IONE Medical Safety Event",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "event_type", label: __("分类")},
					{fieldname: "severity", label: __("等级")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "event_time", label: __("发生时间")},
				],
			},
			{
				key: "pdca_projects",
				title: __("PDCA 项目"),
				doctype: "IONE PDCA Project",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "project_code", label: __("项目编码")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "end_date", label: __("结束日期")},
				],
			},
			{
				key: "ai_candidates",
				title: __("AI 候选问题"),
				doctype: "IONE AI Candidate Finding",
				columns: [
					{fieldname: "name", label: __("编号")},
					{fieldname: "severity", label: __("等级")},
					{fieldname: "status", label: __("状态")},
					{fieldname: "department", label: __("科室")},
					{fieldname: "creation", label: __("生成时间")},
				],
			},
		];
	}
}

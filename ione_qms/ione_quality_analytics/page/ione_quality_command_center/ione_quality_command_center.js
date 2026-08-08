frappe.pages["ione-quality-command-center"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("医疗质量驾驶舱"),
		single_column: true,
	});
	const dashboard = new IONEQualityCommandCenter(page, wrapper);
	$(wrapper).on("show", () => dashboard.refresh());
};

class IONEQualityCommandCenter {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.department = null;
		this.days = 30;
		this.body = $('<div class="ione-command-center"></div>').appendTo(
			this.wrapper.find(".page-content").empty()
		);
		this.department_field = this.page.add_field({
			label: __("科室"),
			fieldname: "department",
			fieldtype: "Link",
			options: "IONE Medical Department",
			change: () => {
				this.department = this.department_field.get_value() || null;
				this.refresh();
			},
		});
		this.days_field = this.page.add_field({
			label: __("周期"),
			fieldname: "days",
			fieldtype: "Select",
			options: ["7", "30", "60", "90"],
			default: "30",
			change: () => {
				this.days = Number(this.days_field.get_value() || 30);
				this.refresh();
			},
		});
		this.page.set_primary_action(__("刷新"), () => this.refresh(), "refresh");
	}

	async refresh() {
		if (this.loading) return;
		this.loading = true;
		this.page.set_indicator(__("正在加载"), "orange");
		try {
			const data = await frappe.xcall("ione_qms.api.dashboard.get_command_center", {
				department: this.department,
				days: this.days,
			});
			this.render(data);
			this.page.set_indicator(__("已更新"), "green");
		} catch (error) {
			this.page.set_indicator(__("加载失败"), "red");
			frappe.msgprint({
				title: __("无法加载驾驶舱"),
				message: error.message || __("请联系 IONE QMS 管理员。"),
				indicator: "red",
			});
		} finally {
			this.loading = false;
		}
	}

	render(data) {
		this.body.empty();
		this.render_cards(data.cards || {});
		const chart_grid = $('<div class="ione-chart-grid"></div>').appendTo(this.body);
		this.render_trend(chart_grid, data.finding_trend || []);
		this.render_status(chart_grid, data.status_distribution || []);
		this.render_tables(data.indicator_alerts || [], data.priority_findings || []);
	}

	render_cards(cards) {
		const labels = {
			open_findings: __("开放问题"),
			high_risk: __("高风险问题"),
			overdue: __("逾期问题"),
			pending_rectification: __("待完成整改"),
			pending_ai_review: __("待审核 AI 候选"),
			integration_errors_24h: __("24 小时集成错误"),
		};
		const grid = $('<div class="ione-metric-grid"></div>').appendTo(this.body);
		Object.entries(labels).forEach(([key, label]) => {
			const value = frappe.utils.escape_html(String(cards[key] || 0));
			$(
				`<div class="ione-metric-card">
					<div class="ione-metric-label">${frappe.utils.escape_html(label)}</div>
					<div class="ione-metric-value">${value}</div>
				</div>`
			).appendTo(grid);
		});
	}

	render_trend(container, rows) {
		const card = $(
			'<section class="ione-panel"><h3>问题发现与关闭趋势</h3><div class="ione-trend-chart"></div></section>'
		).appendTo(container);
		new frappe.Chart(card.find(".ione-trend-chart")[0], {
			data: {
				labels: rows.map((row) => row.date),
				datasets: [
					{name: __("发现"), values: rows.map((row) => row.detected)},
					{name: __("关闭"), values: rows.map((row) => row.closed)},
				],
			},
			type: "line",
			height: 280,
			colors: ["#dc2626", "#059669"],
			lineOptions: {regionFill: 1, hideDots: 0},
			axisOptions: {xIsSeries: true},
		});
	}

	render_status(container, rows) {
		const card = $(
			'<section class="ione-panel"><h3>开放问题状态分布</h3><div class="ione-status-chart"></div></section>'
		).appendTo(container);
		if (!rows.length) {
			card.find(".ione-status-chart").html(`<p class="text-muted">${__("暂无数据")}</p>`);
			return;
		}
		new frappe.Chart(card.find(".ione-status-chart")[0], {
			data: {
				labels: rows.map((row) => __(row.status)),
				datasets: [{values: rows.map((row) => row.count)}],
			},
			type: "donut",
			height: 280,
		});
	}

	render_tables(alerts, findings) {
		const grid = $('<div class="ione-table-grid"></div>').appendTo(this.body);
		this.render_table(
			grid,
			__("指标预警"),
			alerts,
			[
				["alert_level", __("级别")],
				["department", __("科室")],
				["status", __("状态")],
				["creation", __("时间")],
			],
			"IONE Indicator Alert"
		);
		this.render_table(
			grid,
			__("优先处置问题"),
			findings,
			[
				["title", __("问题")],
				["severity", __("等级")],
				["department", __("科室")],
				["status", __("状态")],
				["due_date", __("期限")],
			],
			"IONE QC Finding"
		);
	}

	render_table(container, title, rows, columns, doctype) {
		const card = $('<section class="ione-panel ione-table-panel"></section>').appendTo(container);
		$("<h3></h3>").text(title).appendTo(card);
		const table = $('<table class="table table-hover"><thead><tr></tr></thead><tbody></tbody></table>');
		columns.forEach(([, label]) => $("<th></th>").text(label).appendTo(table.find("thead tr")));
		if (!rows.length) {
			$(`<tr><td colspan="${columns.length}" class="text-muted">${__("暂无数据")}</td></tr>`).appendTo(
				table.find("tbody")
			);
		}
		rows.forEach((row) => {
			const tr = $("<tr></tr>").appendTo(table.find("tbody"));
			columns.forEach(([fieldname], index) => {
				const cell = $("<td></td>").text(row[fieldname] || "");
				if (index === 0 && row.name) {
					cell.addClass("ione-link").on("click", () => frappe.set_route("Form", doctype, row.name));
				}
				cell.appendTo(tr);
			});
		});
		table.appendTo(card);
	}
}

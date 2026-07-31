frappe.pages["ione-safety-event-report"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("医疗安全事件上报"),
		single_column: true,
	});
	new IONESafetyEventReport(page, wrapper);
};

class IONESafetyEventReport {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.controls = {};
		this.submitting = false;
		this.submission_request_id = null;
		this.allowed_severities = new Set(["Low", "Medium", "High", "Critical"]);
		this.render();
		this.page.set_primary_action(__("确认并提交"), () => this.submit(), "check");
	}

	render() {
		const page_content = this.wrapper.find(".page-content").empty();
		const container = $('<div class="container-fluid py-3"></div>').appendTo(page_content);

		$(
			'<div class="alert alert-info mb-3" role="status"></div>'
		)
			.text(
				__(
					"请如实报告事件。匿名上报不会在事件记录中保存报告人身份或精确上报时间；调查人员仍可能依据事件事实开展核查。"
				)
			)
			.appendTo(container);

		const card = $('<section class="card"></section>').appendTo(container);
		const card_body = $('<div class="card-body"></div>').appendTo(card);
		const fields = $('<div class="row"></div>').appendTo(card_body);

		this.add_control(fields, {
			fieldname: "reporting_mode",
			fieldtype: "Select",
			label: __("报告方式"),
			options: ["实名上报", "匿名上报"],
			default: "实名上报",
			reqd: 1,
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "near_miss",
			fieldtype: "Check",
			label: __("近乎差错（尚未造成伤害）"),
			default: 0,
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "event_type",
			fieldtype: "Select",
			label: __("事件分类"),
			options: [
				"",
				"诊疗与处置",
				"用药安全",
				"手术与麻醉",
				"输血安全",
				"院感防控",
				"护理安全",
				"跌倒或坠床",
				"医疗器械与设备",
				"标本与检验",
				"沟通与交接",
				"信息与隐私",
				"其他",
			],
			reqd: 1,
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "severity",
			fieldtype: "Select",
			label: __("事件等级"),
			options: ["", "Low", "Medium", "High", "Critical"],
			description: __("Low 低、Medium 中、High 高、Critical 严重"),
			reqd: 1,
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "event_time",
			fieldtype: "Datetime",
			label: __("发生时间"),
			default: frappe.datetime.now_datetime(),
			reqd: 1,
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "department",
			fieldtype: "Link",
			label: __("发生科室"),
			options: "IONE Medical Department",
			reqd: 1,
			change: () => {
				if (this.controls.ward) {
					this.controls.ward.set_value("");
				}
			},
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "ward",
			fieldtype: "Link",
			label: __("发生病区/地点"),
			options: "IONE Ward",
			get_query: () => {
				const department = this.controls.department.get_value();
				return department ? {filters: {department}} : {};
			},
		}, "col-lg-6");
		this.add_control(fields, {
			fieldname: "narrative",
			fieldtype: "Text",
			label: __("事件描述"),
			description: __("请描述客观事实、经过及已知影响，10 至 20,000 字。"),
			reqd: 1,
		}, "col-12");
		this.add_control(fields, {
			fieldname: "immediate_action",
			fieldtype: "Text",
			label: __("即时处置措施"),
			description: __("如尚未采取措施，请说明当前状态。"),
		}, "col-12");

		$('<p class="text-muted small mb-0"></p>')
			.text(
				__(
					"本页面不会把表单内容写入网址、本地存储或浏览器日志。请勿在共享终端离开未提交的表单。"
				)
			)
			.appendTo(card_body);
	}

	add_control(parent, definition, column_class) {
		const column = $(`<div class="${column_class} mb-2"></div>`).appendTo(parent);
		const original_change = definition.change;
		const control = frappe.ui.form.make_control({
			parent: column,
			df: {
				...definition,
				change: () => {
					this.submission_request_id = null;
					if (original_change) {
						original_change();
					}
				},
			},
			render_input: true,
		});
		control.refresh();
		this.controls[definition.fieldname] = control;
		return control;
	}

	get_values() {
		return Object.fromEntries(
			Object.entries(this.controls).map(([fieldname, control]) => [
				fieldname,
				control.get_value(),
			])
		);
	}

	validate(values) {
		const missing = [
			["reporting_mode", __("报告方式")],
			["event_type", __("事件分类")],
			["severity", __("事件等级")],
			["event_time", __("发生时间")],
			["department", __("发生科室")],
			["narrative", __("事件描述")],
		].filter(([fieldname]) => !String(values[fieldname] || "").trim());
		if (missing.length) {
			frappe.msgprint({
				title: __("请补全必填项"),
				message: missing.map(([, label]) => label).join("、"),
				indicator: "orange",
			});
			return false;
		}

		const narrative_length = String(values.narrative).trim().length;
		const action_length = String(values.immediate_action || "").trim().length;
		if (narrative_length < 10 || narrative_length > 20000) {
			frappe.msgprint({
				title: __("事件描述长度不符合要求"),
				message: __("事件描述应为 10 至 20,000 字。"),
				indicator: "orange",
			});
			return false;
		}
		if (action_length > 20000) {
			frappe.msgprint({
				title: __("即时处置措施过长"),
				message: __("即时处置措施不能超过 20,000 字。"),
				indicator: "orange",
			});
			return false;
		}
		if (!this.allowed_severities.has(values.severity)) {
			frappe.msgprint({
				title: __("事件等级无效"),
				message: __("请重新选择事件等级。"),
				indicator: "orange",
			});
			return false;
		}
		return true;
	}

	build_payload(values) {
		return {
			request_id: this.submission_request_id,
			event_time: values.event_time,
			event_type: String(values.event_type).trim(),
			severity: values.severity,
			narrative: String(values.narrative).trim(),
			department: values.department,
			hospital: null,
			campus: null,
			ward: values.ward || null,
			patient: null,
			encounter: null,
			immediate_action: String(values.immediate_action || "").trim() || null,
			anonymous: values.reporting_mode === "匿名上报" ? 1 : 0,
			near_miss: values.near_miss ? 1 : 0,
		};
	}

	async submit() {
		if (this.submitting) {
			return;
		}
		const values = this.get_values();
		if (!this.validate(values)) {
			return;
		}
		const confirmed = await this.confirm_submission(values);
		if (!confirmed) {
			return;
		}

		this.submission_request_id = this.submission_request_id || this.make_request_id();
		this.set_submitting(true);
		try {
			const result = await frappe.xcall(
				"ione_qms.api.quality.report_safety_event",
				this.build_payload(values)
			);
			const reference = result.safety_event || result.event;
			await this.clear_form();
			const success_message = $("<div></div>")
				.append($("<p></p>").text(__("事件已安全提交。")))
				.append(
					$("<p></p>").text(
						__("受理编号：{0}", [reference || __("已受理")])
					)
				);
			if (result.duplicate) {
				success_message.append(
					$("<p></p>").text(__("该次请求此前已受理，未重复创建事件。"))
				);
			}
			frappe.msgprint({
				title: __("上报成功"),
				message: success_message.prop("outerHTML"),
				indicator: "green",
			});
		} catch (error) {
			this.show_submit_error(error);
		} finally {
			this.set_submitting(false);
		}
	}

	confirm_submission(values) {
		const anonymous = values.reporting_mode === "匿名上报";
		const mode_message = anonymous
			? __("将以匿名方式提交，事件记录不会保存您的身份。")
			: __("将以实名方式提交，您的登录账号会写入受限审计字段。");
		const message = $("<div></div>")
			.append($("<p></p>").text(mode_message))
			.append(
				$("<p></p>").text(
					__("分类：{0}；等级：{1}；科室：{2}", [
						values.event_type,
						values.severity,
						values.department,
					])
				)
			)
			.append($("<p></p>").text(__("确认提交后，表单将在受理成功时立即清空。")));
		return new Promise((resolve) => {
			frappe.confirm(message.prop("outerHTML"), () => resolve(true), () => resolve(false));
		});
	}

	async clear_form() {
		this.submission_request_id = null;
		const defaults = {
			reporting_mode: "实名上报",
			near_miss: 0,
			event_type: "",
			severity: "",
			event_time: frappe.datetime.now_datetime(),
			department: "",
			ward: "",
			narrative: "",
			immediate_action: "",
		};
		await Promise.all(
			Object.entries(defaults).map(([fieldname, value]) =>
				Promise.resolve(this.controls[fieldname].set_value(value))
			)
		);
	}

	make_request_id() {
		if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
			return globalThis.crypto.randomUUID();
		}
		if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === "function") {
			const bytes = new Uint8Array(16);
			globalThis.crypto.getRandomValues(bytes);
			return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
		}
		return `safety-${Date.now()}-${frappe.utils.get_random(24)}`;
	}

	set_submitting(submitting) {
		this.submitting = submitting;
		this.page.set_indicator(
			submitting ? __("正在安全提交") : __("可提交"),
			submitting ? "orange" : "blue"
		);
		if (this.page.btn_primary) {
			this.page.btn_primary.prop("disabled", submitting);
		}
	}

	show_submit_error(error) {
		const status = Number(error?.httpStatus || error?.status || 0);
		const type = String(error?.exc_type || "");
		const permission_denied =
			status === 401 || status === 403 || type.includes("PermissionError");
		frappe.msgprint({
			title: permission_denied ? __("无权上报") : __("上报未完成"),
			message: permission_denied
				? __("当前账号没有医疗安全事件上报权限，请联系医务或质控部门。")
				: __(
						"请检查必填项和事件范围后重试。若问题持续，请联系医务或质控部门；系统不会在此处回显事件描述。"
					),
			indicator: "red",
		});
	}
}

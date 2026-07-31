frappe.query_reports["IONE Improvement Governance Overview"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -29),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{ fieldname: "hospital", label: __("Hospital"), fieldtype: "Link", options: "IONE Hospital" },
		{
			fieldname: "campus",
			label: __("Campus"),
			fieldtype: "Link",
			options: "IONE Hospital Campus",
		},
		{
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "IONE Medical Department",
		},
		{ fieldname: "ward", label: __("Ward"), fieldtype: "Link", options: "IONE Ward" },
	],
};

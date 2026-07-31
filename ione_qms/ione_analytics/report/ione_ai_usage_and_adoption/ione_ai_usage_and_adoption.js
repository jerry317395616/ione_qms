frappe.query_reports["IONE AI Usage and Adoption"] = {
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
		{
			fieldname: "task_type",
			label: __("Task Type"),
			fieldtype: "Select",
			options:
				"\nPolicy Governance\nMedical Record QC\nIndicator Analysis\nRectification\nQuality Report\nQuality Analysis\nReport Draft\nSpecial Topic",
		},
		{
			fieldname: "policy",
			label: __("Policy"),
			fieldtype: "Link",
			options: "IONE Agent Policy",
		},
		{
			fieldname: "flow_agent",
			label: __("Flow Agent"),
			fieldtype: "Link",
			options: "Flow Agent",
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

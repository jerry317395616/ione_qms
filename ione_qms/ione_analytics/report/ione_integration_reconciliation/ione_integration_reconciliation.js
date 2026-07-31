frappe.query_reports["IONE Integration Reconciliation"] = {
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
			fieldname: "source_system",
			label: __("Source System"),
			fieldtype: "Link",
			options: "IONE Source System",
		},
		{
			fieldname: "endpoint",
			label: __("Endpoint"),
			fieldtype: "Link",
			options: "IONE Integration Endpoint",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nPending\nMatched\nMismatch\nFailed\nReviewed",
		},
	],
};

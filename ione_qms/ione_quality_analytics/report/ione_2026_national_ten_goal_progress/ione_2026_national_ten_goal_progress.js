frappe.query_reports["IONE 2026 National Ten-Goal Progress"] = {
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
			fieldname: "standard",
			label: __("Published National Policy Standard"),
			fieldtype: "Link",
			options: "IONE QC Standard",
			reqd: 1,
			get_query: () => ({ filters: { source_type: "National Policy", status: "Published" } }),
		},
		{
			fieldname: "indicator",
			label: __("Indicator"),
			fieldtype: "Link",
			options: "IONE QC Indicator",
			get_query: () => ({
				filters: {
					standard: frappe.query_report.get_filter_value("standard"),
					status: "Active",
				},
			}),
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
		{ fieldname: "medical_group", label: __("Medical Group"), fieldtype: "Data" },
		{
			fieldname: "physician",
			label: __("Physician"),
			fieldtype: "Link",
			options: "IONE Medical Staff",
		},
		{ fieldname: "disease", label: __("Disease"), fieldtype: "Data" },
		{ fieldname: "surgery", label: __("Surgery"), fieldtype: "Data" },
		{ fieldname: "drg", label: __("DRG"), fieldtype: "Data" },
		{ fieldname: "dip", label: __("DIP"), fieldtype: "Data" },
	],
};

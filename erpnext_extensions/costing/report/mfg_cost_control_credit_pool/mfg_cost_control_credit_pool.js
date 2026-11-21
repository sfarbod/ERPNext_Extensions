// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.query_reports["MFG Cost Control Credit Pool"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "persian_period",
			label: __("Persian Period"),
			fieldtype: "Data",
			description: __("Format YYYY-MM, e.g. 1405-03"),
		},
		{
			fieldname: "persian_year",
			label: __("Persian Year"),
			fieldtype: "Int",
		},
		{
			fieldname: "persian_month",
			label: __("Persian Month"),
			fieldtype: "Int",
		},
		{
			fieldname: "control_type",
			label: __("Control Type"),
			fieldtype: "Select",
			options: "\nLabor\nOverhead",
		},
		{
			fieldname: "control_account",
			label: __("Control Account"),
			fieldtype: "Link",
			options: "Account",
		},
		{
			fieldname: "allocation_status",
			label: __("Document Status"),
			fieldtype: "Select",
			options: "\nDraft\nCalculated\nSubmitted\nCancelled",
		},
		{
			fieldname: "pool_status",
			label: __("Pool Status"),
			fieldtype: "Select",
			options:
				"\nDraft\nCalculated\nSubmitted\nCancelled\nNo Eligible Credit\nPartially Allocated\nFully Allocated",
		},
		{
			fieldname: "credit_allocation",
			label: __("Allocation Reference"),
			fieldtype: "Link",
			options: "MFG Cost Control Credit Allocation",
		},
	],
};

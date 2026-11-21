// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.query_reports["MFG Cost Control Credit Batch Product Allocation"] = {
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
			fieldname: "credit_allocation",
			label: __("Source Credit Pool"),
			fieldtype: "Link",
			options: "MFG Cost Control Credit Allocation",
		},
		{
			fieldname: "batch_no",
			label: __("Production Batch"),
			fieldtype: "Data",
		},
		{
			fieldname: "item_code",
			label: __("Product"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "product_allocation",
			label: __("Product Allocation"),
			fieldtype: "Link",
			options: "MFG Cost Product Allocation",
		},
		{
			fieldname: "include_zero_amounts",
			label: __("Include Zero Amounts"),
			fieldtype: "Check",
			default: 0,
		},
	],
};

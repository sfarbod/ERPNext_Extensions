frappe.query_reports["Facility Balance"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "bank",
			label: __("Bank"),
			fieldtype: "Link",
			options: "Bank",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nDraft\nActive\nClosed",
		},
		{
			fieldname: "facility",
			label: __("Facility"),
			fieldtype: "Link",
			options: "Facility",
		},
		{
			fieldname: "facility_name",
			label: __("Facility Name"),
			fieldtype: "Data",
		},
		{
			fieldname: "facility_type",
			label: __("Facility Type"),
			fieldtype: "Link",
			options: "Facility Type",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			// Kept for backward compatibility with bookmarks / E2E; aliased to to_date in Python.
			fieldname: "as_on_date",
			label: __("As On Date"),
			fieldtype: "Date",
		},
	],
};

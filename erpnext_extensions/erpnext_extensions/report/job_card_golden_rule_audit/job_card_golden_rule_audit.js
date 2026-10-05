// Copyright (c) 2026, ERPNext Extensions contributors
// Date basis: Job Card.posting_date

frappe.query_reports["Job Card Golden Rule Audit"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "job_card",
			label: __("Job Card"),
			fieldtype: "Link",
			options: "Job Card",
		},
		{
			fieldname: "work_order",
			label: __("Work Order"),
			fieldtype: "Link",
			options: "Work Order",
		},
		{
			fieldname: "item",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "batch",
			label: __("Batch"),
			fieldtype: "Data",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: [
				"",
				"MISSING_CONSUMPTION",
				"UNEXPLAINED_WIP",
				"MISSING_RETURN",
				"OVER_CONSUMED",
				"OVER_RETURNED",
				"SCRAP_PAIR_MISMATCH",
				"MULTIPLE_MANUFACTURE",
				"MERGE_REVIEW",
				"MATERIAL_ISSUE_REVIEW",
				"BATCH_MISMATCH",
				"AMBIGUOUS_OWNERSHIP",
				"BLOCKED",
				"BALANCED",
			].join("\n"),
		},
		{
			fieldname: "show_balanced",
			label: __("Show Balanced"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		if (column.fieldname === "golden_status") {
			const st = data.golden_status || "";
			if (st === "BALANCED") {
				value = `<span style="color:green;font-weight:600">${value}</span>`;
			} else if (st === "BLOCKED" || st === "OVER_CONSUMED" || st === "OVER_RETURNED") {
				value = `<span style="color:#b00;font-weight:600">${value}</span>`;
			} else if (st) {
				value = `<span style="color:#c60;font-weight:600">${value}</span>`;
			}
		}
		if (column.fieldname === "open_rebuild" && data.job_card) {
			const jc = encodeURIComponent(data.job_card);
			value = `<a href="/app/job-card-stock-rebuild?job_card=${jc}">${__(
				"Open Rebuild"
			)}</a>`;
		}
		return value;
	},
};

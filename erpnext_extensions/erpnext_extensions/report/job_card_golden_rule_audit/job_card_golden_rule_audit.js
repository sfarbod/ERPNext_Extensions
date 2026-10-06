// Copyright (c) 2026, ERPNext Extensions contributors
// Job Card Golden Rule Audit — Job Card × Item (batch ignored). v5.5.10

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
			fieldname: "status",
			label: __("Status / Reason"),
			fieldtype: "Select",
			options: [
				"",
				"BALANCED",
				"REVIEW",
				"MISSING_CONSUMPTION",
				"UNEXPLAINED_WIP",
				"OVER_CONSUMED",
				"OVER_RETURNED",
				"MULTIPLE_MANUFACTURE",
				"MATERIAL_ISSUE_REVIEW",
				"AMBIGUOUS_OWNERSHIP",
				"BLOCKED",
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
		if (column.fieldname === "golden_status" || column.fieldname === "job_card_summary") {
			const st = data[column.fieldname] || "";
			if (st === "BALANCED") {
				value = `<span style="color:#1e7e34;font-weight:600">${value}</span>`;
			} else if (st === "REVIEW") {
				value = `<span style="color:#c0392b;font-weight:600">${value}</span>`;
			}
		}
		if (column.fieldname === "remaining_wip" && Math.abs(flt(data.remaining_wip)) > 0.0001) {
			value = `<span style="color:#c0392b;font-weight:600">${value}</span>`;
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

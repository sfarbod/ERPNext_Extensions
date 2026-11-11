// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("Finished Goods Cost Allocation Settings", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Validate Item Primary UOMs"), () => {
			frappe.confirm(
				__("Replace existing Item Primary UOM rows with Default Primary UOM?"),
				() => run_uom(frm, 1),
				() => run_uom(frm, 0)
			);
		}, __("UOM"));

		frm.add_custom_button(__("Preview UOM Exceptions"), () => {
			frappe.call({
				method: "erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.preview_uom_exceptions",
				args: { settings_name: frm.doc.name },
				callback(r) {
					const msg = r.message || {};
					frappe.msgprint({
						title: __("UOM Exceptions ({0})", [msg.count || 0]),
						message: `<pre style="max-height:360px;overflow:auto">${frappe.utils.escape_html(
							JSON.stringify(msg.rows || [], null, 2)
						)}</pre>`,
					});
				},
			});
		}, __("UOM"));

		frm.add_custom_button(__("Preview Generate Department Rules"), () => {
			frappe.prompt(
				[
					{ fieldname: "from_date", fieldtype: "Date", label: __("From Date") },
					{ fieldname: "to_date", fieldtype: "Date", label: __("To Date") },
				],
				(values) => {
					frappe.call({
						method:
							"erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules.preview_generate_department_rules",
						args: {
							settings_name: frm.doc.name,
							from_date: values.from_date,
							to_date: values.to_date,
						},
						freeze: true,
						callback(r) {
							frappe.msgprint({
								title: __("Rule Generation Preview"),
								message: `<pre style="max-height:420px;overflow:auto">${frappe.utils.escape_html(
									JSON.stringify(r.message || {}, null, 2)
								)}</pre>`,
							});
						},
					});
				},
				__("Preview Generate Department Rules"),
				__("Preview")
			);
		}, __("Expense Rules"));

		frm.add_custom_button(__("Generate Department Rules"), () => {
			frappe.prompt(
				[
					{ fieldname: "from_date", fieldtype: "Date", label: __("From Date") },
					{ fieldname: "to_date", fieldtype: "Date", label: __("To Date") },
					{
						fieldname: "replace_all",
						fieldtype: "Check",
						label: __("Replace all existing rules"),
						default: 0,
					},
				],
				(values) => {
					frappe.confirm(
						values.replace_all
							? __("This will REPLACE all existing expense rules. Continue?")
							: __("Append new non-duplicate rules from GL discovery?"),
						() => {
							frappe.call({
								method:
									"erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules.generate_department_rules",
								args: {
									settings_name: frm.doc.name,
									from_date: values.from_date,
									to_date: values.to_date,
									replace_all: values.replace_all ? 1 : 0,
								},
								freeze: true,
								callback() {
									frm.reload_doc();
								},
							});
						}
					);
				},
				__("Generate Department Rules"),
				__("Generate")
			);
		}, __("Expense Rules"));
	},
});

function run_uom(frm, replace_existing) {
	frappe.call({
		method: "erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.validate_and_refresh_item_uoms",
		args: { settings_name: frm.doc.name, replace_existing },
		freeze: true,
		callback() {
			frm.reload_doc();
		},
	});
}

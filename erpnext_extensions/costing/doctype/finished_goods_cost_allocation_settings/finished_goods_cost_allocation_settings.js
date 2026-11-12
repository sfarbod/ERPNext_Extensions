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

		frm.add_custom_button(__("Apply BOX(1pfs) Primary UOM"), () => {
			frappe.confirm(
				__(
					"Set Primary UOM to BOX(1pfs) for all configured Items and apply verified conversion factors (1/2/4 from packaging or ERPNext). Continue?"
				),
				() => {
					frappe.call({
						method:
							"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_box1.apply_box1_primary_uoms",
						args: { settings_name: frm.doc.name },
						freeze: true,
						callback(r) {
							frappe.msgprint({
								title: __("BOX(1pfs) Applied"),
								message: `<pre style="max-height:360px;overflow:auto">${frappe.utils.escape_html(
									JSON.stringify(r.message || {}, null, 2)
								)}</pre>`,
							});
							frm.reload_doc();
						},
					});
				}
			);
		}, __("UOM"));

		frm.add_custom_button(__("Add Items to Costing Scope"), () => {
			frappe.prompt(
				[
					{
						fieldname: "item_list",
						fieldtype: "Small Text",
						label: __("Item Codes (comma / newline separated)"),
					},
					{
						fieldname: "primary_uom",
						fieldtype: "Link",
						label: __("Primary UOM"),
						options: "UOM",
						default: frm.doc.default_primary_uom,
					},
				],
				(values) => {
					const codes = [];
					if (values.item_list) {
						values.item_list.split(/[\n,]+/).forEach((x) => {
							const v = (x || "").trim();
							if (v) codes.push(v);
						});
					}
					frappe.call({
						method:
							"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.add_items_to_primary_uoms",
						args: {
							settings_name: frm.doc.name,
							items: codes,
							primary_uom: values.primary_uom,
						},
						freeze: true,
						callback() {
							frm.reload_doc();
						},
					});
				},
				__("Add Items (any Item Group)"),
				__("Add")
			);
		}, __("UOM"));

		frm.add_custom_button(__("Preview Excel Rule Generation"), () => {
			frappe.call({
				method:
					"erpnext_extensions.iran_accounting.fg_cost_allocation.excel_rule_import.preview_excel_rule_generation",
				args: { settings_name: frm.doc.name },
				freeze: true,
				callback(r) {
					const m = r.message || {};
					frappe.msgprint({
						title: __("Excel Rule Generation Preview"),
						message: `<pre style="max-height:420px;overflow:auto">${frappe.utils.escape_html(
							JSON.stringify(
								{
									mapping_mode: m.mapping_mode,
									accounts: m.resolved_account_count,
									departments: m.resolved_department_count,
									unresolved_departments: m.unresolved_departments,
									blank_department_excluded: m.excluded_blank_dept,
									expected_combinations: m.expected_combinations_after_resolve,
									to_create: m.to_create,
									to_reuse: m.to_reuse,
									cost_center_policy: m.cost_center_policy,
									existing_rules: m.existing_rule_count,
								},
								null,
								2
							)
						)}</pre>`,
					});
				},
			});
		}, __("Expense Rules"));

		frm.add_custom_button(__("Generate Rules from Excel"), () => {
			frappe.confirm(
				__(
					"Rebuild Expense Account Dimension Rules from private Excel sources (Account × Department, blank Cost Center). Manual rows are preserved. Continue?"
				),
				() => {
					frappe.call({
						method:
							"erpnext_extensions.iran_accounting.fg_cost_allocation.excel_rule_import.generate_excel_rules",
						args: {
							settings_name: frm.doc.name,
							replace_generated: 1,
						},
						freeze: true,
						freeze_message: __("Generating Account × Department rules (Cost Center blank)..."),
						callback(r) {
							frappe.msgprint({
								title: __("Rule Generation Result"),
								message: `<pre style="max-height:420px;overflow:auto">${frappe.utils.escape_html(
									JSON.stringify(r.message || {}, null, 2)
								)}</pre>`,
							});
							frm.reload_doc();
						},
					});
				}
			);
		}, __("Expense Rules"));

		make_summary_grids_readonly(frm);
		if (erpnext_extensions.costing && erpnext_extensions.costing.enable_child_table_downloads) {
			erpnext_extensions.costing.enable_child_table_downloads(frm);
		}
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

function make_summary_grids_readonly(frm) {
	["rule_generation_summary", "uom_validation_summary"].forEach((fieldname) => {
		const field = frm.get_field(fieldname);
		if (!field || !field.grid) return;
		field.grid.cannot_add_rows = true;
		field.grid.cannot_delete_rows = true;
		field.grid.wrapper &&
			field.grid.wrapper.find(".grid-add-row, .grid-add-multiple-rows, .grid-remove-rows, .grid-remove-all-rows, .grid-upload").addClass("hidden");
	});
}

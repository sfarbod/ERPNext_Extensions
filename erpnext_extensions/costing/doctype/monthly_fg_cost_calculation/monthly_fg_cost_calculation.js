// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("Monthly FG Cost Calculation", {
	refresh(frm) {
		frm.disable_save_menu_for_standard = false;
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Calculate / Recalculate"), () => {
			frappe.confirm(
				__("Retain manual batch share overrides?"),
				() => run_calc(frm, 1),
				() => run_calc(frm, 0)
			);
		});

		if (frm.doc.status !== "Locked") {
			frm.add_custom_button(__("Lock"), () => {
				frappe.call({
					method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.lock_monthly",
					args: { name: frm.doc.name, lock: 1 },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		} else {
			frm.add_custom_button(__("Unlock"), () => {
				frappe.call({
					method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.lock_monthly",
					args: { name: frm.doc.name, lock: 0 },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		}

		add_list_button(frm, __("Expense Pools"), "MFG Cost Expense Pool");
		add_list_button(frm, __("GL Lines"), "MFG Cost GL Line");
		add_list_button(frm, __("Batches"), "MFG Cost Batch Allocation");
		add_list_button(frm, __("Batch × Expense"), "MFG Cost Batch Expense Allocation");
		add_list_button(frm, __("Products"), "MFG Cost Product Allocation");
		add_list_button(frm, __("Product × Expense"), "MFG Cost Product Expense Allocation");
		add_list_button(frm, __("Revisions"), "MFG Cost Calculation Revision");
	},
});

function run_calc(frm, retain_overrides) {
	frappe.call({
		method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.calculate_monthly",
		args: { name: frm.doc.name, retain_overrides },
		freeze: true,
		freeze_message: __("Calculating monthly FG cost allocation..."),
		callback: (r) => {
			if (r.message) {
				frappe.show_alert({
					message: __("Status: {0}, revision {1}", [
						r.message.status,
						r.message.revision,
					]),
					indicator: r.message.is_consistent ? "green" : "orange",
				});
			}
			frm.reload_doc();
		},
	});
}

function add_list_button(frm, label, doctype) {
	frm.add_custom_button(
		label,
		() => {
			frappe.set_route("List", doctype, {
				monthly_calculation: frm.doc.name,
				revision: frm.doc.revision,
			});
		},
		__("Drill-down")
	);
}

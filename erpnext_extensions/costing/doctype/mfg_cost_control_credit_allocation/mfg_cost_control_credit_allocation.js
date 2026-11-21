// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("MFG Cost Control Credit Allocation", {
	refresh(frm) {
		frm.trigger("setup_calculate_button");
		if (frm.doc.persian_year && frm.doc.persian_month && !frm.doc.persian_period) {
			frm.set_value(
				"persian_period",
				`${cint(frm.doc.persian_year)}-${String(cint(frm.doc.persian_month)).padStart(2, "0")}`
			);
		}
	},

	persian_year(frm) {
		frm.trigger("set_persian_period");
	},
	persian_month(frm) {
		frm.trigger("set_persian_period");
	},

	set_persian_period(frm) {
		if (frm.doc.persian_year == null || frm.doc.persian_month == null) {
			return;
		}
		frm.set_value(
			"persian_period",
			`${cint(frm.doc.persian_year)}-${String(cint(frm.doc.persian_month)).padStart(2, "0")}`
		);
	},

	setup_calculate_button(frm) {
		frm.clear_custom_buttons();
		if (frm.is_new() || frm.doc.docstatus === 1 || frm.doc.status === "Cancelled") {
			return;
		}
		frm.add_custom_button(__("Calculate Credit Allocation"), () => {
			frappe.call({
				method:
					"erpnext_extensions.costing.doctype.mfg_cost_control_credit_allocation.mfg_cost_control_credit_allocation.calculate",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __("Identifying credits and allocating with Batch Expense shares…"),
				callback(r) {
					frm.reload_doc();
					if (r.message) {
						frappe.show_alert({
							message: __(
								"Labor eligible {0} / Overhead eligible {1}",
								[
									format_currency(r.message.labor_eligible_credit || 0, frm.doc.currency),
									format_currency(r.message.overhead_eligible_credit || 0, frm.doc.currency),
								]
							),
							indicator: r.message.is_consistent ? "green" : "orange",
						});
					}
				},
			});
		}).addClass("btn-primary");
	},
});

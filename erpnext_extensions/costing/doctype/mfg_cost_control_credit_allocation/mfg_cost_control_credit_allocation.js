// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("MFG Cost Control Credit Allocation", {
	refresh(frm) {
		frm.trigger("setup_calculate_button");
		frm.trigger("setup_trace_buttons");
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
		if (frm.is_new() || frm.doc.docstatus === 1 || frm.doc.status === "Cancelled") {
			return;
		}
		frm.add_custom_button(__("Calculate Credit Allocation"), () => {
			frappe.call({
				method:
					"erpnext_extensions.costing.doctype.mfg_cost_control_credit_allocation.mfg_cost_control_credit_allocation.calculate",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __(
					"Allocating full Monthly Credit Amount with Batch Expense shares…"
				),
				callback(r) {
					frm.reload_doc();
					if (r.message) {
						frappe.show_alert({
							message: __(
								"Allocated Labor {0} / Overhead {1} from Monthly Credit base",
								[
									format_currency(r.message.labor_allocated || 0, frm.doc.currency),
									format_currency(r.message.overhead_allocated || 0, frm.doc.currency),
								]
							),
							indicator: r.message.is_consistent ? "green" : "orange",
						});
					}
				},
			});
		}).addClass("btn-primary");
	},

	setup_trace_buttons(frm) {
		if (frm.is_new()) {
			return;
		}
		const nav = __("Traceability");
		frm.add_custom_button(__("Control Credit Pools"), () => {
			frappe.set_route("query-report", "MFG Cost Control Credit Pool", {
				credit_allocation: frm.doc.name,
				persian_period: frm.doc.persian_period,
				company: frm.doc.company,
			});
		}, nav);
		frm.add_custom_button(__("Batch × Product Allocation"), () => {
			frappe.set_route("query-report", "MFG Cost Control Credit Batch Product Allocation", {
				credit_allocation: frm.doc.name,
				persian_period: frm.doc.persian_period,
				company: frm.doc.company,
			});
		}, nav);
		if (frm.doc.monthly_calculation) {
			frm.add_custom_button(__("Monthly FG Cost Calculation"), () => {
				frappe.set_route("Form", "Monthly FG Cost Calculation", frm.doc.monthly_calculation);
			}, nav);
		}
		frm.add_custom_button(__("Product Allocations"), () => {
			frappe.route_options = {
				monthly_calculation: frm.doc.monthly_calculation,
			};
			frappe.set_route("List", "MFG Cost Product Allocation");
		}, nav);
	},
});

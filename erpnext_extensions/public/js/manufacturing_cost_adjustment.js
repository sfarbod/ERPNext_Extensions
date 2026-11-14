// Copyright (c) 2026, ERPNext Extensions contributors

frappe.ui.form.on("Manufacturing Cost Adjustment", {
	refresh(frm) {
		frm.trigger("mca_hide_cancel");
		frm.trigger("mca_status_indicator");
		frm.trigger("mca_retry_button");
		frm.trigger("mca_set_queries");
	},

	mca_hide_cancel(frm) {
		// Defense in depth: no independent cancel
		if (frm.doc.docstatus === 1) {
			frm.page.clear_secondary_action();
			$('.inner-group-button:contains("Cancel"), button:contains("Cancel")', frm.page.wrapper)
				.filter((_, el) => $(el).text().trim() === __("Cancel"))
				.hide();
			frm.page.hide_menu_item(__("Cancel"));
		}
	},

	mca_status_indicator(frm) {
		if (!frm.doc.processing_status) return;
		const map = {
			Draft: "blue",
			Queued: "orange",
			"Appending Costs": "orange",
			Allocating: "orange",
			"Rebuilding SLE": "orange",
			"Rebuilding GL": "orange",
			"Validating Contract": "orange",
			"Pending RIV": "orange",
			Reconciling: "orange",
			Completed: "green",
			Failed: "red",
			VoidedByStockEntryCancel: "darkgrey",
		};
		frm.dashboard.set_headline_alert(
			__("Processing: {0} | Repost: {1}", [
				frm.doc.processing_status,
				frm.doc.repost_status || "-",
			]),
			map[frm.doc.processing_status] || "blue"
		);
		if (frm.doc.processing_status !== "Completed" && frm.doc.docstatus === 1) {
			frm.dashboard.add_comment(
				__("Not Completed until SLE/GL rebuild and RIV finish successfully."),
				"orange",
				true
			);
		}
	},

	mca_retry_button(frm) {
		if (frm.doc.docstatus !== 1) return;
		if (!["Failed", "Pending RIV"].includes(frm.doc.processing_status)) return;
		if (frm.doc.processing_status === "Completed") return;
		frm.add_custom_button(__("Retry Processing"), () => {
			frappe.call({
				method:
					"erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment.manufacturing_cost_adjustment.retry_mca_processing",
				args: { name: frm.doc.name },
				freeze: true,
				callback() {
					frm.reload_doc();
				},
			});
		});
	},

	mca_set_queries(frm) {
		frm.set_query("stock_entry", () => ({
			filters: {
				docstatus: 1,
				purpose: ["in", ["Manufacture", "Repack"]],
				company: frm.doc.company || undefined,
			},
		}));
		frm.set_query("expense_account", "costs", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
			},
		}));
	},

	costs_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.exchange_rate) frappe.model.set_value(cdt, cdn, "exchange_rate", 1);
	},
});

frappe.ui.form.on("Manufacturing Cost Adjustment Cost", {
	expense_account(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.expense_account) return;
		frappe.db.get_value("Account", row.expense_account, "account_currency", (r) => {
			if (r && r.account_currency) {
				frappe.model.set_value(cdt, cdn, "account_currency", r.account_currency);
			}
		});
	},
	amount(frm, cdt, cdn) {
		frm.trigger("recalc_row_base", cdt, cdn);
	},
	exchange_rate(frm, cdt, cdn) {
		frm.trigger("recalc_row_base", cdt, cdn);
	},
});

frappe.ui.form.on("Manufacturing Cost Adjustment", {
	recalc_row_base(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const base = flt(row.amount) * flt(row.exchange_rate || 1);
		frappe.model.set_value(cdt, cdn, "base_amount", base);
		let total = 0;
		(frm.doc.costs || []).forEach((r) => {
			total += flt(r.base_amount);
		});
		frm.set_value("total_additional_costs", total);
	},
});

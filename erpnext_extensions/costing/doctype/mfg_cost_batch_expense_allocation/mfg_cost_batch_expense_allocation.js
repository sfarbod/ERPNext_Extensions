// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("MFG Cost Batch Expense Allocation", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.set_df_property("allocated_amount", "read_only", 1);
		frm.set_df_property("calculated_batch_share", "read_only", 1);
		frm.set_df_property("pool_monthly_net_expense", "read_only", 1);

		frm.add_custom_button(__("Apply Share Override"), () => open_share_dialog(frm));
		frm.dashboard.set_headline_alert(
			__(
				"Allocated Amount = Pool Monthly Net Expense × Applied Batch Share. Use Apply Share Override or change Applied Batch Share and Save."
			),
			"blue"
		);
	},

	applied_batch_share(frm) {
		recalc_client(frm);
	},

	pool_monthly_net_expense(frm) {
		recalc_client(frm);
	},

	after_save(frm) {
		// Engine may update Allocated Amount / siblings via set_value — reload visible values.
		frm.reload_doc();
	},
});

function recalc_client(frm) {
	const pool = flt(frm.doc.pool_monthly_net_expense);
	const share = flt(frm.doc.applied_batch_share);
	const amt = flt(pool * share);
	frm.set_value("allocated_amount", amt);
	frm.dashboard.set_headline(
		__("Preview Allocated Amount: {0} (= {1} × {2})", [
			format_number(amt),
			format_number(pool),
			format_number(share),
		])
	);
}

function open_share_dialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "applied_share",
				fieldtype: "Float",
				label: __("Applied Batch Share"),
				reqd: 1,
				default: frm.doc.applied_batch_share,
				description: __("Allocated Amount will be Pool Monthly Net × this share."),
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason"),
			},
		],
		(values) => {
			frappe.call({
				method:
					"erpnext_extensions.costing.doctype.mfg_cost_batch_expense_allocation.mfg_cost_batch_expense_allocation.update_applied_batch_share",
				args: {
					name: frm.doc.name,
					applied_share: values.applied_share,
					reason: values.reason,
				},
				freeze: true,
				freeze_message: __("Recalculating allocated amounts and parent totals..."),
				callback(r) {
					const msg = r.message || {};
					frappe.show_alert({
						message: __(
							"Applied share {0}. Allocated total {1}. Share variance {2}.",
							[
								msg.applied_share,
								format_number(msg.total_allocated_expenses),
								format_number(msg.share_variance),
							]
						),
						indicator: msg.over_allocated ? "red" : "orange",
					});
					frm.reload_doc();
				},
			});
		},
		__("Edit Applied Batch Share"),
		__("Update")
	);
}

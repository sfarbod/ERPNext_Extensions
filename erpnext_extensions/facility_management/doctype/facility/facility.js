// Copyright (c) 2026, ERPNext Extensions contributors

frappe.provide("erpnext_extensions.facility_management.dimension_queries");

frappe.ui.form.on("Facility", {
	onload(frm) {
		setup_pdc_allocation_query(frm);
	},
	refresh(frm) {
		erpnext_extensions.facility_management.dimension_queries.setup?.(frm);
		setup_pdc_allocation_query(frm);
		sync_debt_purchase_flag(frm).then(() => {
			toggle_pdc_grid_editable(frm);
			refresh_facility_footers(frm);
			// Re-apply after grid finish rendering (survives grid.make rebuild).
			setTimeout(() => refresh_facility_footers(frm), 0);
			setTimeout(() => refresh_facility_footers(frm), 200);
		});
		if (frm.is_new() || frm.doc.status === "Closed") {
			return;
		}
		if (can_preview_or_create_receipt(frm)) {
			frm.add_custom_button(__("Preview Receipt Journal Entry"), () =>
				preview_receipt_je(frm)
			);
			frm.add_custom_button(__("Create Receipt Journal Entry"), () => {
				frappe.call({
					method: "erpnext_extensions.facility_management.doctype.facility.facility.create_receipt_journal_entry",
					args: { name: frm.doc.name },
					freeze: true,
					callback() {
						frm.reload_doc();
					},
				});
			});
		}
		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Close Facility"), () => {
				frappe.call({
					method: "erpnext_extensions.facility_management.doctype.facility.facility.close_facility",
					args: { name: frm.doc.name },
					freeze: true,
					callback() {
						frm.reload_doc();
					},
				});
			});
		}
	},
	facility_type(frm) {
		sync_debt_purchase_flag(frm).then(() => refresh_facility_footers(frm));
	},
	is_debt_purchase(frm) {
		toggle_pdc_grid_editable(frm);
		refresh_facility_footers(frm);
	},
	principal_amount(frm) {
		refresh_facility_footers(frm);
	},
	profit_amount(frm) {
		refresh_facility_footers(frm);
	},
	status(frm) {
		toggle_pdc_grid_editable(frm);
	},
	// Child-table row add/remove events belong on the parent controller.
	repayment_schedule_add(frm) {
		refresh_facility_footers(frm);
	},
	repayment_schedule_remove(frm) {
		setTimeout(() => refresh_facility_footers(frm), 0);
	},
	debt_purchase_pdcs_add(frm) {
		refresh_facility_footers(frm);
	},
	debt_purchase_pdcs_remove(frm) {
		setTimeout(() => refresh_facility_footers(frm), 0);
	},
});

frappe.ui.form.on("Facility Repayment Schedule", {
	principal_amount(frm) {
		refresh_facility_footers(frm);
	},
	profit_amount(frm) {
		refresh_facility_footers(frm);
	},
});

frappe.ui.form.on("Facility PDC Allocation", {
	post_dated_cheque(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.post_dated_cheque) {
			frappe.model.set_value(cdt, cdn, {
				cheque_no: null,
				cheque_due_date: null,
				drawer_bank_name: null,
				cheque_amount: null,
				cheque_status: null,
			});
			refresh_facility_footers(frm);
			return;
		}
		frappe.db.get_value(
			"Post Dated Cheque",
			row.post_dated_cheque,
			["cheque_no", "cheque_due_date", "drawer_bank_name", "cheque_amount", "cheque_status"],
			(r) => {
				if (!r) {
					return;
				}
				frappe.model.set_value(cdt, cdn, {
					cheque_no: r.cheque_no,
					cheque_due_date: r.cheque_due_date,
					drawer_bank_name: r.drawer_bank_name,
					cheque_amount: r.cheque_amount,
					cheque_status: r.cheque_status,
				});
				refresh_facility_footers(frm);
			}
		);
	},
	cheque_amount(frm) {
		refresh_facility_footers(frm);
	},
});

function refresh_facility_footers(frm) {
	const footer = erpnext_extensions.facility_management.grid_footer;
	if (!footer || !footer.refresh_all) {
		return;
	}
	footer.refresh_all(frm);
}

function setup_pdc_allocation_query(frm) {
	frm.set_query("post_dated_cheque", "debt_purchase_pdcs", () => ({
		query:
			"erpnext_extensions.facility_management.facility_debt_purchase.facility_allocation_pdc_query",
		filters: {
			company: frm.doc.company || undefined,
			facility: frm.doc.name || "",
		},
	}));
}

function toggle_pdc_grid_editable(frm) {
	if (!frm.fields_dict.debt_purchase_pdcs) {
		return;
	}
	const closed = frm.doc.status === "Closed";
	frm.set_df_property("debt_purchase_pdcs", "read_only", closed ? 1 : 0);
}

/**
 * Keep hidden Check `is_debt_purchase` in sync with Facility Type.
 * drives depends_on for the PDC section without fragile display toggles.
 */
function sync_debt_purchase_flag(frm) {
	if (!frm.doc.facility_type) {
		if (cint(frm.doc.is_debt_purchase)) {
			frm.set_value("is_debt_purchase", 0);
		}
		return Promise.resolve(0);
	}
	return frappe.db
		.get_value("Facility Type", frm.doc.facility_type, "is_debt_purchase")
		.then((r) => {
			const val = cint(r && r.message && r.message.is_debt_purchase);
			if (cint(frm.doc.is_debt_purchase) !== val) {
				frm.set_value("is_debt_purchase", val);
			}
			return val;
		})
		.catch(() => 0);
}

function can_preview_or_create_receipt(frm) {
	return (
		!cint(frm.doc.is_opening_facility) &&
		!frm.doc.receipt_journal_entry &&
		flt(frm.doc.principal_amount) > 0 &&
		!!frm.doc.company
	);
}

function preview_receipt_je(frm) {
	frappe.call({
		method: "erpnext_extensions.facility_management.doctype.facility.facility.preview_receipt_journal_entry",
		args: { name: frm.doc.name },
		freeze: true,
		callback(r) {
			erpnext_extensions.facility_management.je_preview.show_facility_je_preview_dialog(
				r.message,
				__("Receipt Journal Entry Preview")
			);
		},
	});
}

function cint(v) {
	return parseInt(v, 10) || 0;
}

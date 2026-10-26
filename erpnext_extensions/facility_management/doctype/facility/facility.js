// Copyright (c) 2026, ERPNext Extensions contributors

frappe.provide("erpnext_extensions.facility_management.dimension_queries");

frappe.ui.form.on("Facility", {
	refresh(frm) {
		erpnext_extensions.facility_management.dimension_queries.setup?.(frm);
		setup_debt_purchase_section(frm);
		setup_pdc_allocation_query(frm);
		refresh_facility_footers(frm);
		toggle_pdc_grid_editable(frm);
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
		setup_debt_purchase_section(frm);
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
});

frappe.ui.form.on("Facility Repayment Schedule", {
	principal_amount(frm) {
		refresh_facility_footers(frm);
	},
	profit_amount(frm) {
		refresh_facility_footers(frm);
	},
	repayment_schedule_add(frm) {
		refresh_facility_footers(frm);
	},
	repayment_schedule_remove(frm) {
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
	debt_purchase_pdcs_add(frm) {
		refresh_facility_footers(frm);
	},
	debt_purchase_pdcs_remove(frm) {
		refresh_facility_footers(frm);
	},
});

function refresh_facility_footers(frm) {
	const footer = erpnext_extensions.facility_management.grid_footer;
	if (!footer) {
		return;
	}
	footer.refresh_schedule_footer(frm);
	footer.refresh_pdc_allocation_footer(frm);
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
	const grid = frm.fields_dict.debt_purchase_pdcs && frm.fields_dict.debt_purchase_pdcs.grid;
	if (!grid) {
		return;
	}
	const closed = frm.doc.status === "Closed";
	frm.set_df_property("debt_purchase_pdcs", "read_only", closed ? 1 : 0);
}

function setup_debt_purchase_section(frm) {
	if (!frm.doc.facility_type) {
		frm.toggle_display("section_debt_purchase_pdcs", false);
		frm.toggle_display("debt_purchase_pdcs", false);
		return;
	}
	frappe.db.get_value("Facility Type", frm.doc.facility_type, "is_debt_purchase", (r) => {
		const show = cint(r && r.is_debt_purchase);
		frm.toggle_display("section_debt_purchase_pdcs", !!show);
		frm.toggle_display("debt_purchase_pdcs", !!show);
		if (show) {
			refresh_facility_footers(frm);
		}
	});
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

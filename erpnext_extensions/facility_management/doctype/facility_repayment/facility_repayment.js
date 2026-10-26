// Copyright (c) 2026, ERPNext Extensions contributors

const REPAYMENT_ACCOUNTS_AND_DIMENSIONS = [
	"bank_account",
	"loan_payable_account",
	"deferred_loan_interest_account",
	"interest_expense_account",
	"penalty_expense_account",
	"cost_center",
	"department",
	"bank_dimension",
	"bank_account_dimension",
];

const METHOD_BANK = "Bank Account";
const METHOD_DP = "Debt Purchase Cheque";

frappe.ui.form.on("Facility Repayment", {
	onload(frm) {
		if (frm.doc.docstatus === 0) {
			erpnext_extensions.facility_management.defaults.init_form(frm);
		}
		setup_facility_link_query(frm);
		setup_debt_purchase_cheque_query(frm);
	},
	refresh(frm) {
		recalculate_total_payment(frm);
		setup_facility_link_query(frm);
		sync_repayment_accounts_dimensions_read_only(frm);
		toggle_repayment_method_fields(frm);
		setup_debt_purchase_cheque_query(frm);
		if (frm.doc.docstatus === 0) {
			erpnext_extensions.facility_management.defaults.init_form(frm);
		}
		if (can_preview_repayment(frm)) {
			frm.add_custom_button(
				__("Preview Journal Entry"),
				() => preview_repayment_je(frm),
				__("Actions")
			);
		}
	},
	repayment_method(frm) {
		const method = (frm.doc.repayment_method || METHOD_BANK).trim() || METHOD_BANK;
		if (method === METHOD_BANK) {
			frm.set_value("post_dated_cheque", null);
		} else if (method === METHOD_DP) {
			frm.set_value("bank_account", null);
		}
		toggle_repayment_method_fields(frm);
		setup_debt_purchase_cheque_query(frm);
	},
	facility(frm) {
		recalculate_total_payment(frm);
		setup_debt_purchase_cheque_query(frm);
		clear_incompatible_pdc(frm);
	},
	principal_amount(frm) {
		recalculate_total_payment(frm);
	},
	profit_amount(frm) {
		recalculate_total_payment(frm);
	},
	penalty_amount(frm) {
		recalculate_total_payment(frm);
	},
});

function setup_facility_link_query(frm) {
	// Eligible repayments require Active Facility (server also validates).
	// Search matches Facility ID, facility_name / loan number, bank, remarks.
	frm.set_query("facility", () => {
		const filters = { status: "Active" };
		if (frm.doc.company) {
			filters.company = frm.doc.company;
		}
		return {
			query:
				"erpnext_extensions.facility_management.facility_queries.facility_link_query",
			filters,
		};
	});
}

function setup_debt_purchase_cheque_query(frm) {
	frm.set_query("post_dated_cheque", () => ({
		query:
			"erpnext_extensions.facility_management.facility_debt_purchase.debt_purchase_cheque_query",
		filters: {
			company: frm.doc.company || undefined,
			facility: frm.doc.facility || undefined,
		},
	}));
}

function clear_incompatible_pdc(frm) {
	if (frm.doc.docstatus !== 0 || !frm.doc.post_dated_cheque || !frm.doc.facility) {
		return;
	}
	const method = (frm.doc.repayment_method || METHOD_BANK).trim() || METHOD_BANK;
	if (method !== METHOD_DP) {
		return;
	}
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "Facility PDC Allocation",
			filters: {
				parent: frm.doc.facility,
				parenttype: "Facility",
				post_dated_cheque: frm.doc.post_dated_cheque,
			},
			fields: ["name"],
			limit_page_length: 1,
		},
		callback(r) {
			if (!(r.message && r.message.length)) {
				frm.set_value("post_dated_cheque", null);
			}
		},
	});
}

function toggle_repayment_method_fields(frm) {
	const method = (frm.doc.repayment_method || METHOD_BANK).trim() || METHOD_BANK;
	const is_bank = method === METHOD_BANK;
	const is_dp = method === METHOD_DP;

	frm.toggle_display("bank_account", is_bank);
	frm.toggle_reqd("bank_account", is_bank && frm.doc.docstatus === 0);
	frm.toggle_display("post_dated_cheque", is_dp);
	frm.toggle_reqd("post_dated_cheque", is_dp && frm.doc.docstatus === 0);
}

function sync_repayment_accounts_dimensions_read_only(frm) {
	const read_only = frm.doc.docstatus !== 0 ? 1 : 0;
	REPAYMENT_ACCOUNTS_AND_DIMENSIONS.forEach((fieldname) => {
		if (frm.fields_dict[fieldname]) {
			frm.set_df_property(fieldname, "read_only", read_only);
		}
	});
}

function can_preview_repayment(frm) {
	if (frm.doc.docstatus !== 0 || !frm.doc.facility) {
		return false;
	}
	const total =
		flt(frm.doc.principal_amount) + flt(frm.doc.profit_amount) + flt(frm.doc.penalty_amount);
	return total > 0;
}

function recalculate_total_payment(frm) {
	const total =
		flt(frm.doc.principal_amount) + flt(frm.doc.profit_amount) + flt(frm.doc.penalty_amount);
	frm.doc.total_payment_amount = total;
	frm.refresh_field("total_payment_amount");
}

function preview_repayment_je(frm) {
	frappe.call({
		method: "erpnext_extensions.facility_management.doctype.facility_repayment.facility_repayment.preview_repayment_journal_entry",
		args: { doc: frm.doc },
		freeze: true,
		callback(r) {
			erpnext_extensions.facility_management.je_preview.show_facility_je_preview_dialog(
				r.message,
				__("Repayment Journal Entry Preview")
			);
		},
	});
}

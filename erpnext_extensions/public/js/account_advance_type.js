// Copyright (c) 2026, ERPNext Extensions contributors
// Account.advance_account_type UX hints (v5.7.42)

frappe.ui.form.on("Account", {
	refresh(frm) {
		frm.trigger("advance_account_type");
	},

	advance_account_type(frm) {
		const t = frm.doc.advance_account_type;
		if (!t) {
			frm.set_df_property("advance_account_type", "description", __(
				"Optional. Mark separate-party Advance Received (Liability/Receivable) or Advance Paid (Asset/Payable)."
			));
			return;
		}
		if (t === "Advance Received") {
			frm.set_df_property(
				"advance_account_type",
				"description",
				__("Requires Root Type = Liability and Account Type = Receivable (company currency ledger).")
			);
		} else if (t === "Advance Paid") {
			frm.set_df_property(
				"advance_account_type",
				"description",
				__("Requires Root Type = Asset and Account Type = Payable (company currency ledger).")
			);
		}
	},
});

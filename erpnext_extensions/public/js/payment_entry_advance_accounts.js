// Copyright (c) 2026, ERPNext Extensions contributors
// Payment Entry: keep book_advance checkbox read-only; refresh after party account change (v5.7.42)

frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		if (frm.fields_dict.book_advance_payments_in_separate_party_account) {
			frm.set_df_property("book_advance_payments_in_separate_party_account", "read_only", 1);
		}
	},

	paid_from(frm) {
		if (frm.doc.payment_type === "Receive") {
			frm.trigger("preview_advance_flag");
		}
	},

	paid_to(frm) {
		if (frm.doc.payment_type === "Pay") {
			frm.trigger("preview_advance_flag");
		}
	},

	preview_advance_flag(frm) {
		// Client hint only — authoritative flag is set on server validate.
		const company_on = cint(
			frappe.get_doc(":Company", frm.doc.company)?.book_advance_payments_in_separate_party_account
		);
		if (!company_on || !frm.doc.company) {
			return;
		}
		const account =
			frm.doc.payment_type === "Receive"
				? frm.doc.paid_from
				: frm.doc.payment_type === "Pay"
					? frm.doc.paid_to
					: null;
		if (!account) {
			return;
		}
		frappe.db.get_value("Account", account, "advance_account_type", (r) => {
			const t = (r && r.advance_account_type) || "";
			let expected_on = 0;
			if (frm.doc.payment_type === "Receive" && t === "Advance Received") {
				expected_on = 1;
			} else if (frm.doc.payment_type === "Pay" && t === "Advance Paid") {
				expected_on = 1;
			}
			// Do not write the read-only field from client when it would fight fetch_from;
			// server validate is source of truth. Optional status indicator:
			if (frm.dashboard && expected_on) {
				frm.dashboard.set_headline_alert(
					__("Separate-party advance account selected — flag will be ON after Save."),
					"blue"
				);
			}
		});
	},
});

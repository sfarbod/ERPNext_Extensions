// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
// v5.7.26 — Offer Accrual JE regeneration when no Draft/Submitted JE is linked.
//
// IMPORTANT: call fully-qualified erpnext_extensions whitelist methods.
// frm.call("generate_accrual_journal_entry") incorrectly resolves to
// hrms.payroll.doctype.payroll_entry.payroll_entry.generate_accrual_journal_entry.

const ACCRUAL_JE_API = {
	has_active:
		"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.has_active_accrual_journal_entry",
	generate:
		"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.generate_accrual_journal_entry",
};

frappe.ui.form.on("Payroll Entry", {
	refresh(frm) {
		frm.trigger("toggle_generate_accrual_je_button");
	},

	toggle_generate_accrual_je_button(frm) {
		frm.remove_custom_button(__("Create Accrual Journal Entry"));
		if (frm.doc.docstatus !== 1 || frm.is_new()) {
			return;
		}
		frappe
			.call({
				method: ACCRUAL_JE_API.has_active,
				args: { payroll_entry: frm.doc.name },
			})
			.then((r) => {
				const msg = r.message || {};
				if (msg.has_active) {
					return;
				}
				frm
					.add_custom_button(__("Create Accrual Journal Entry"), () => {
						frappe.confirm(
							__(
								"Create a new Draft Accrual Journal Entry for this Payroll Entry? Cancelled Journal Entries will not block this action."
							),
							() => {
								frappe
									.call({
										method: ACCRUAL_JE_API.generate,
										args: { payroll_entry: frm.doc.name },
										freeze: true,
										freeze_message: __("Creating Accrual Journal Entry..."),
									})
									.then((res) => {
										if (res.message) {
											frappe.set_route("Form", "Journal Entry", res.message);
										} else {
											frm.reload_doc();
										}
									});
							}
						);
					})
					.addClass("btn-primary");
			});
	},
});

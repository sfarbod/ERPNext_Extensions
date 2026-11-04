// Copyright (c) 2026, ERPNext Extensions contributors
// Check Submission — dry-run Delivery Note submit validations (Write, no Submit required).

frappe.ui.form.on("Delivery Note", {
	refresh(frm) {
		erpnext_extensions.dn_check_submission.toggle_button(frm);
	},
});

frappe.provide("erpnext_extensions.dn_check_submission");

erpnext_extensions.dn_check_submission.toggle_button = function (frm) {
	frm.remove_custom_button(__("Check Submission"));

	if (frm.is_new() || frm.doc.docstatus !== 0) {
		return;
	}
	// Write is enough; Submit must not be required.
	if (!(frm.perm && frm.perm[0] && frm.perm[0].write)) {
		return;
	}

	frm.add_custom_button(__("Check Submission"), () => {
		erpnext_extensions.dn_check_submission.run(frm);
	});
};

erpnext_extensions.dn_check_submission.run = function (frm) {
	if (frm._dn_check_submission_busy) {
		return;
	}
	if (frm.is_dirty && frm.is_dirty()) {
		frappe.msgprint({
			title: __("Unsaved changes"),
			indicator: "orange",
			message: __(
				"Please save the Delivery Note before running Check Submission so the server validates the stored document."
			),
		});
		return;
	}

	frm._dn_check_submission_busy = true;
	frm.dashboard.set_headline_alert(__("Running submission validations…"), "blue");

	frappe.call({
		method:
			"erpnext_extensions.stock_extensions.delivery_note_check_submission.api.validate_delivery_note_before_submit",
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: __("Checking submission validations…"),
		callback(r) {
			const result = (r && r.message) || {};
			erpnext_extensions.dn_check_submission.show_result(result);
		},
		error() {
			// frappe.call already surfaces server exceptions; keep UI unlock only.
		},
		always() {
			frm._dn_check_submission_busy = false;
			frm.dashboard.clear_headline();
		},
	});
};

erpnext_extensions.dn_check_submission.show_result = function (result) {
	if (result.ok) {
		const caveat = result.caveat
			? `<p class="text-muted small mt-2">${frappe.utils.escape_html(result.caveat)}</p>`
			: "";
		frappe.msgprint({
			title: __("Check Submission"),
			indicator: "green",
			message:
				`<p>${frappe.utils.escape_html(
					result.message ||
						__(
							"All submission validations passed. This Delivery Note is ready for submission."
						)
				)}</p>${caveat}`,
		});
		return;
	}

	const errors = result.errors || [];
	const items = errors
		.map((err) => {
			const msg = frappe.utils.escape_html((err && err.message) || "");
			const typ = frappe.utils.escape_html((err && err.exc_type) || "");
			return `<li><strong>${typ}</strong>: ${msg}</li>`;
		})
		.join("");

	frappe.msgprint({
		title: __("Submission validation failed"),
		indicator: "red",
		message:
			items ||
			`<p>${frappe.utils.escape_html(
				result.message || __("Submission validation failed.")
			)}</p>`,
	});
};

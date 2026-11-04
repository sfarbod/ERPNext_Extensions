// Copyright (c) 2026, ERPNext Extensions contributors
// Check Submission — strict read-only Delivery Note validations (Write, no Submit required).

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
	frm.dashboard.set_headline_alert(__("Running read-only submission validations…"), "blue");

	frappe.call({
		method:
			"erpnext_extensions.stock_extensions.delivery_note_check_submission.api.validate_delivery_note_before_submit",
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: __("Checking submission validations (read-only)…"),
		callback(r) {
			const result = (r && r.message) || {};
			erpnext_extensions.dn_check_submission.show_result(result);
		},
		error() {},
		always() {
			frm._dn_check_submission_busy = false;
			frm.dashboard.clear_headline();
		},
	});
};

erpnext_extensions.dn_check_submission._limitations_html = function (limitations) {
	if (!limitations || !limitations.length) {
		return "";
	}
	const items = limitations
		.map((lim) => {
			const sev = frappe.utils.escape_html((lim && lim.severity) || "advisory");
			const msg = frappe.utils.escape_html((lim && lim.message) || "");
			const code = frappe.utils.escape_html((lim && lim.code) || "");
			return `<li><strong>[${sev}]</strong> ${code ? `<code>${code}</code>: ` : ""}${msg}</li>`;
		})
		.join("");
	return `<hr/><p><strong>${__("Limitations")}</strong></p><ul>${items}</ul>`;
};

erpnext_extensions.dn_check_submission.show_result = function (result) {
	const limitations_html = erpnext_extensions.dn_check_submission._limitations_html(
		result.limitations
	);

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
							"All supported read-only submission validations passed."
						)
				)}</p>${caveat}${limitations_html}`,
		});
		return;
	}

	const errors = result.errors || [];
	const error_items = errors
		.map((err) => {
			const msg = frappe.utils.escape_html((err && err.message) || "");
			const typ = frappe.utils.escape_html((err && err.exc_type) || "");
			return `<li><strong>${typ}</strong>: ${msg}</li>`;
		})
		.join("");

	const incomplete = !errors.length && (result.limitations || []).some((l) => l.severity === "blocking");
	frappe.msgprint({
		title: incomplete ? __("Check Submission incomplete") : __("Submission validation failed"),
		indicator: incomplete ? "orange" : "red",
		message:
			(error_items ? `<ul>${error_items}</ul>` : "") +
			`<p>${frappe.utils.escape_html(
				result.message || __("Submission validation failed.")
			)}</p>${limitations_html}`,
	});
};

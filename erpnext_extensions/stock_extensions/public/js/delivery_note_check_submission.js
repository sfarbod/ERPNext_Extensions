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

erpnext_extensions.dn_check_submission._category_label = function (category) {
	const map = {
		stock_availability: __("Stock Availability"),
		batch_expiration: __("Batch Expiration"),
		batch_eligibility: __("Batch Eligibility"),
		serial_number: __("Serial Number"),
		serial_batch_bundle: __("Serial and Batch Bundle"),
		warehouse: __("Warehouse"),
		over_delivery: __("Over Delivery"),
		accounting: __("Accounting"),
		mandatory_fields: __("Mandatory Fields"),
		sales_order_hold: __("Sales Order Hold"),
		credit_limit: __("Credit Limit"),
		approving_authority: __("Approving Authority"),
		links: __("Links"),
		taxes_enabled: __("Taxes"),
		tax_account_company: __("Taxes"),
	};
	return map[category] || frappe.utils.escape_html(category || __("Validation"));
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

erpnext_extensions.dn_check_submission._errors_html = function (errors) {
	if (!errors || !errors.length) {
		return "";
	}

	// Group by category while preserving order of first appearance.
	const order = [];
	const groups = {};
	errors.forEach((err) => {
		const cat = (err && err.category) || "validation";
		if (!groups[cat]) {
			groups[cat] = [];
			order.push(cat);
		}
		groups[cat].push(err);
	});

	const sections = order
		.map((cat, idx) => {
			const label = erpnext_extensions.dn_check_submission._category_label(cat);
			const items = groups[cat]
				.map((err) => {
					const parts = [];
					if (err.item_code) {
						parts.push(
							`${__("Item")}: <code>${frappe.utils.escape_html(err.item_code)}</code>`
						);
					}
					if (err.row != null) {
						parts.push(`${__("Row")}: ${frappe.utils.escape_html(String(err.row))}`);
					}
					if (err.warehouse) {
						parts.push(
							`${__("Warehouse")}: ${frappe.utils.escape_html(err.warehouse)}`
						);
					}
					if (err.batch_no) {
						parts.push(
							`${__("Batch")}: <code>${frappe.utils.escape_html(err.batch_no)}</code>`
						);
					}
					if (err.serial_no) {
						parts.push(
							`${__("Serial")}: <code>${frappe.utils.escape_html(err.serial_no)}</code>`
						);
					}
					if (err.needed != null && err.available != null) {
						parts.push(
							`${__("Required")}: ${frappe.utils.escape_html(String(err.needed))}, ${__(
								"Available"
							)}: ${frappe.utils.escape_html(String(err.available))}`
						);
					}
					if (err.expiry_date && err.posting_date) {
						parts.push(
							`${__("Expiration date")}: ${frappe.utils.escape_html(
								String(err.expiry_date)
							)}, ${__("Delivery Note date")}: ${frappe.utils.escape_html(
								String(err.posting_date)
							)}`
						);
					}
					const meta = parts.length
						? `<div class="text-muted small">${parts.join(" · ")}</div>`
						: "";
					const msg = frappe.utils.escape_html((err && err.message) || "");
					return `<li style="margin-bottom:0.5rem;">${msg}${meta}</li>`;
				})
				.join("");
			return `<div style="margin-bottom:0.75rem;"><strong>${idx + 1}. ${label}</strong><ul>${items}</ul></div>`;
		})
		.join("");

	return sections;
};

erpnext_extensions.dn_check_submission._warnings_html = function (warnings) {
	if (!warnings || !warnings.length) {
		return "";
	}
	const items = warnings
		.map((w) => `<li>${frappe.utils.escape_html((w && w.message) || "")}</li>`)
		.join("");
	return `<hr/><p><strong>${__("Warnings")}</strong></p><ul>${items}</ul>`;
};

erpnext_extensions.dn_check_submission.show_result = function (result) {
	const limitations_html = erpnext_extensions.dn_check_submission._limitations_html(
		result.limitations
	);
	const warnings_html = erpnext_extensions.dn_check_submission._warnings_html(result.warnings);
	const summary = result.summary || {};
	const error_count = summary.errors != null ? summary.errors : (result.errors || []).length;
	const status = result.status || (result.ok ? "ready" : error_count ? "blocked" : "incomplete");

	if (result.ok || status === "ready" || status === "pass") {
		const caveat = result.caveat
			? `<p class="text-muted small mt-2">${frappe.utils.escape_html(result.caveat)}</p>`
			: "";
		frappe.msgprint({
			title: __("Check Submission — READY"),
			indicator: "green",
			message:
				`<p>${frappe.utils.escape_html(
					result.message || __("READY — All applicable critical validations passed.")
				)}</p>${caveat}${warnings_html}${limitations_html}`,
		});
		return;
	}

	const incomplete = status === "incomplete";
	const title = incomplete
		? __("Check Submission — INCOMPLETE")
		: __("Check Submission — BLOCKED ({0} error(s))", [error_count]);

	const errors_html = erpnext_extensions.dn_check_submission._errors_html(result.errors || []);

	frappe.msgprint({
		title: title,
		indicator: incomplete ? "orange" : "red",
		message:
			(errors_html || "") +
			`<p>${frappe.utils.escape_html(
				result.message ||
					(incomplete
						? __("INCOMPLETE — Critical validations could not be completed.")
						: __("BLOCKED — Submission validation failed."))
			)}</p>${warnings_html}${limitations_html}`,
	});
};

// Copyright (c) 2026, ERPNext Extensions contributors
// Compact summary bars for Facility child tables (presentation only).
// Labels use __() with English source strings (translated via fa.csv).
//
// Attach to the field $wrapper (outside grid.wrapper) so grid.make()/refresh
// rebuilds cannot wipe the summary.

frappe.provide("erpnext_extensions.facility_management.grid_footer");

(function () {
	const ns = erpnext_extensions.facility_management.grid_footer;

	function parseAmount(val) {
		if (val === null || val === undefined || val === "") {
			return 0;
		}
		if (typeof val === "number") {
			return Math.round(val);
		}
		const cleaned = String(val).replace(/,/g, "").trim();
		if (!cleaned) {
			return 0;
		}
		const n = Number(cleaned);
		if (!Number.isFinite(n)) {
			return 0;
		}
		return Math.round(n);
	}

	function formatIrr(n) {
		const abs = Math.abs(Math.trunc(n));
		const formatted = abs.toLocaleString("en-US");
		return (n < 0 ? "-" : "") + formatted;
	}

	function ensureFooterEl(frm, fieldname, key) {
		const field = frm.fields_dict[fieldname];
		if (!field || !field.$wrapper || !field.$wrapper.length) {
			return null;
		}
		const cls = `ee-facility-summary ee-facility-grid-footer ee-facility-grid-footer-${key}`;
		let $el = field.$wrapper.find(`.ee-facility-grid-footer-${key}`);
		if (!$el.length) {
			$el = $(`<div class="${cls}" role="group"></div>`);
			field.$wrapper.append($el);
		} else {
			$el.attr("class", cls);
			$el.removeAttr("style");
		}
		return $el;
	}

	function itemHtml(label, value, opts) {
		opts = opts || {};
		const extra = opts.extraClass ? ` ${opts.extraClass}` : "";
		const hint = opts.hint
			? `<span class="ee-facility-summary__hint">· ${frappe.utils.escape_html(opts.hint)}</span>`
			: "";
		const title = opts.title ? ` title="${frappe.utils.escape_html(opts.title)}"` : "";
		return `
			<div class="ee-facility-summary__item${extra}"${title}>
				<span class="ee-facility-summary__label">${frappe.utils.escape_html(label)}</span>
				<span class="ee-facility-summary__value">${frappe.utils.escape_html(String(value))} ${hint}</span>
			</div>
		`;
	}

	ns.render_html = function (frm, fieldname, key, html) {
		const $el = ensureFooterEl(frm, fieldname, key);
		if ($el) {
			$el.html(html || "");
			$el.toggle(true);
		}
	};

	ns.sum_field = function (rows, fieldname) {
		let total = 0;
		(rows || []).forEach((row) => {
			total += parseAmount(row[fieldname]);
		});
		return total;
	};

	ns.format_irr = formatIrr;
	ns.parse_amount = parseAmount;
	ns.item_html = itemHtml;

	ns.refresh_schedule_footer = function (frm) {
		if (!frm.fields_dict.repayment_schedule) {
			return;
		}
		const rows = frm.doc.repayment_schedule || [];
		const count = rows.length;
		const principal = ns.sum_field(rows, "principal_amount");
		const profit = ns.sum_field(rows, "profit_amount");
		const total = principal + profit;
		const html =
			itemHtml(__("Installment Count"), count) +
			itemHtml(__("Total Principal"), formatIrr(principal)) +
			itemHtml(__("Total Profit"), formatIrr(profit)) +
			itemHtml(__("Total Installments"), formatIrr(total));
		ns.render_html(frm, "repayment_schedule", "schedule", html);
	};

	ns.refresh_pdc_allocation_footer = function (frm) {
		if (!frm.fields_dict.debt_purchase_pdcs) {
			return;
		}
		if (!cint(frm.doc.is_debt_purchase)) {
			const $el = frm.fields_dict.debt_purchase_pdcs.$wrapper.find(
				".ee-facility-grid-footer-pdc"
			);
			if ($el.length) {
				$el.empty().hide();
			}
			return;
		}
		const rows = frm.doc.debt_purchase_pdcs || [];
		const count = rows.length;
		const allocated = ns.sum_field(rows, "cheque_amount");
		const principal = parseAmount(frm.doc.principal_amount);
		const profit = parseAmount(frm.doc.profit_amount);
		const facilityTotal = principal + profit;
		const diff = allocated - facilityTotal;
		const matched = diff === 0;
		const diffOpts = matched
			? {
					extraClass: "ee-facility-summary__item--ok",
					hint: __("matched"),
					title: __("Difference is zero"),
			  }
			: {
					extraClass: "ee-facility-summary__item--warn",
					hint: __("mismatch"),
					title: __("Cheque total does not equal Facility total"),
			  };
		const html =
			itemHtml(__("Cheque Count"), count) +
			itemHtml(__("Cheque Total"), formatIrr(allocated)) +
			itemHtml(__("Principal"), formatIrr(principal)) +
			itemHtml(__("Profit"), formatIrr(profit)) +
			itemHtml(__("Facility Total"), formatIrr(facilityTotal)) +
			itemHtml(__("Difference"), formatIrr(diff), diffOpts);
		ns.render_html(frm, "debt_purchase_pdcs", "pdc", html);
	};

	ns.refresh_all = function (frm) {
		ns.refresh_schedule_footer(frm);
		ns.refresh_pdc_allocation_footer(frm);
	};
})();

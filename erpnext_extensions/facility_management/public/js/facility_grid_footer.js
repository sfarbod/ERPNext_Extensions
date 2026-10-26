// Copyright (c) 2026, ERPNext Extensions contributors
// Shared read-only footers for Facility child tables (presentation only).
//
// IMPORTANT: Attach footers to the *field* $wrapper (outside grid.wrapper) so
// Frappe grid.make()/refresh rebuilds cannot wipe them.

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
		const cls = `ee-facility-grid-footer ee-facility-grid-footer-${key}`;
		let $el = field.$wrapper.find(`.ee-facility-grid-footer-${key}`);
		if (!$el.length) {
			$el = $(`<div class="${cls}" style="
				padding:10px 12px;
				margin:8px 0 4px 0;
				border:1px solid var(--border-color);
				border-radius:var(--border-radius);
				background:var(--control-bg);
				font-size:var(--text-md);
				line-height:1.7;
			"></div>`);
			// Place after the grid control, still inside the field wrapper.
			field.$wrapper.append($el);
		}
		return $el;
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

	ns.refresh_schedule_footer = function (frm) {
		if (!frm.fields_dict.repayment_schedule) {
			return;
		}
		const rows = frm.doc.repayment_schedule || [];
		const count = rows.length;
		const principal = ns.sum_field(rows, "principal_amount");
		const profit = ns.sum_field(rows, "profit_amount");
		const total = principal + profit;
		const html = `
			<div><strong>${__("تعداد اقساط")}:</strong> ${count}</div>
			<div><strong>${__("جمع اصل")}:</strong> ${formatIrr(principal)} ${__("ریال")}</div>
			<div><strong>${__("جمع سود")}:</strong> ${formatIrr(profit)} ${__("ریال")}</div>
			<div><strong>${__("جمع کل اقساط")}:</strong> ${formatIrr(total)} ${__("ریال")}</div>
		`;
		ns.render_html(frm, "repayment_schedule", "schedule", html);
	};

	ns.refresh_pdc_allocation_footer = function (frm) {
		if (!frm.fields_dict.debt_purchase_pdcs) {
			return;
		}
		// Keep footer in DOM even when section is depends_on-hidden; visibility follows section.
		const rows = frm.doc.debt_purchase_pdcs || [];
		const count = rows.length;
		const allocated = ns.sum_field(rows, "cheque_amount");
		const principal = parseAmount(frm.doc.principal_amount);
		const profit = parseAmount(frm.doc.profit_amount);
		const facilityTotal = principal + profit;
		const diff = allocated - facilityTotal;
		const html = `
			<div><strong>${__("تعداد چک‌ها")}:</strong> ${count}</div>
			<div><strong>${__("جمع مبلغ چک‌ها")}:</strong> ${formatIrr(allocated)} ${__("ریال")}</div>
			<div><strong>${__("اصل تسهیلات")}:</strong> ${formatIrr(principal)} ${__("ریال")}</div>
			<div><strong>${__("فرع تسهیلات")}:</strong> ${formatIrr(profit)} ${__("ریال")}</div>
			<div><strong>${__("جمع تسهیلات")}:</strong> ${formatIrr(facilityTotal)} ${__("ریال")}</div>
			<div><strong>${__("اختلاف (چک − تسهیلات)")}:</strong> ${formatIrr(diff)} ${__("ریال")}</div>
		`;
		ns.render_html(frm, "debt_purchase_pdcs", "pdc", html);
	};

	ns.refresh_all = function (frm) {
		ns.refresh_schedule_footer(frm);
		ns.refresh_pdc_allocation_footer(frm);
	};
})();

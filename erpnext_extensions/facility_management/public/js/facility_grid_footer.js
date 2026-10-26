// Copyright (c) 2026, ERPNext Extensions contributors
// Shared read-only footers for Facility child tables (presentation only).

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
		// Integer-safe IRR: round half-up to whole rial for display/totals.
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

	function ensureFooterEl(grid, key) {
		if (!grid || !grid.wrapper) {
			return null;
		}
		const cls = `ee-facility-grid-footer ee-facility-grid-footer-${key}`;
		let $el = grid.wrapper.find(`.ee-facility-grid-footer-${key}`);
		if (!$el.length) {
			$el = $(`<div class="${cls}" style="padding:8px 12px;margin-top:4px;border-top:1px solid var(--border-color);font-size:var(--text-md);line-height:1.7;"></div>`);
			grid.wrapper.find(".grid-footer").before($el);
			if (!$el.prev().length && !grid.wrapper.find(".grid-footer").length) {
				grid.wrapper.append($el);
			}
		}
		return $el;
	}

	ns.render_html = function (grid, key, html) {
		const $el = ensureFooterEl(grid, key);
		if ($el) {
			$el.html(html || "");
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
		const grid = frm.fields_dict.repayment_schedule && frm.fields_dict.repayment_schedule.grid;
		if (!grid) {
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
		ns.render_html(grid, "schedule", html);
	};

	ns.refresh_pdc_allocation_footer = function (frm) {
		const grid = frm.fields_dict.debt_purchase_pdcs && frm.fields_dict.debt_purchase_pdcs.grid;
		if (!grid) {
			return;
		}
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
		ns.render_html(grid, "pdc", html);
	};
})();

// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
// Shared Costing list footer totals (filtered dataset via listview.get_data / API).

(function () {
	const TOTAL_FIELDS = {
		"Monthly FG Cost Calculation": [
			"total_identified_expenses",
			"total_allocated_expenses",
			"unallocated_amount",
			"gl_entry_count",
			"batch_count",
			"product_count",
			"pool_count",
			"missing_uom_count",
		],
		"MFG Cost Expense Pool": [
			"monthly_debit",
			"monthly_credit",
			"monthly_net_expense",
			"gl_entry_count",
			"allocated_amount",
		],
		"MFG Cost GL Line": ["debit", "credit", "net_amount"],
		"MFG Cost Batch Allocation": [
			"actual_qty",
			"normalized_qty",
			"total_allocated_expense",
			"applied_share",
		],
		"MFG Cost Batch Expense Allocation": [
			"pool_monthly_net_expense",
			"allocated_amount",
		],
		"MFG Cost Product Allocation": [
			"actual_qty",
			"normalized_qty",
			"product_allocated_expense",
		],
		"MFG Cost Account Dimension Summary": [
			"pool_monthly_net_expense",
			"total_allocated_amount",
			"allocated_batch_count",
		],
		"MFG Cost Calculation Revision": [],
	};

	// Applied share / percentages: only sum when a single expense pool filter is active.
	const CONDITIONAL_SHARE_FIELDS = {
		"MFG Cost Batch Expense Allocation": ["applied_batch_share"],
	};

	function money(n) {
		return format_number(n || 0);
	}

	function inject_footer(listview, totals, fields) {
		const $area = listview.$result;
		if (!$area || !$area.length) return;
		$area.find(".costing-list-totals").remove();
		if (!fields.length) return;
		const cells = fields
			.map((f) => {
				const label = frappe.meta.get_docfield(listview.doctype, f)?.label || f;
				return `<span style="margin-right:18px"><strong>${frappe.utils.escape_html(
					__(label)
				)}:</strong> ${money(totals[f] || 0)}</span>`;
			})
			.join("");
		$area.append(
			`<div class="costing-list-totals" style="padding:12px 15px;margin-top:8px;border:1px solid var(--border-color);border-radius:6px;background:var(--subtle-fg);font-size:13px;font-weight:500">${cells}</div>`
		);
	}

	function compute_from_data(data, fields) {
		const totals = {};
		fields.forEach((f) => (totals[f] = 0));
		(data || []).forEach((row) => {
			fields.forEach((f) => {
				totals[f] += flt(row[f]);
			});
		});
		return totals;
	}

	function has_expense_pool_filter(listview) {
		const filters = listview.filter_area?.get?.() || [];
		return filters.some((f) => f[1] === "expense_pool" || f[1] === "Expense Pool");
	}

	Object.keys(TOTAL_FIELDS).forEach((doctype) => {
		const existing = frappe.listview_settings[doctype] || {};
		const prev_onload = existing.onload;
		const prev_refresh = existing.refresh;
		frappe.listview_settings[doctype] = Object.assign({}, existing, {
			onload(listview) {
				if (typeof prev_onload === "function") prev_onload(listview);
			},
			refresh(listview) {
				if (typeof prev_refresh === "function") prev_refresh(listview);
				let fields = (TOTAL_FIELDS[doctype] || []).slice();
				const conditional = CONDITIONAL_SHARE_FIELDS[doctype] || [];
				if (conditional.length && has_expense_pool_filter(listview)) {
					fields = fields.concat(conditional);
				}
				const data = listview.data || [];
				inject_footer(listview, compute_from_data(data, fields), fields);
			},
		});
	});
})();

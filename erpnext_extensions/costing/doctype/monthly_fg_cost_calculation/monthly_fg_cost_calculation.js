// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("Monthly FG Cost Calculation", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Calculate / Recalculate"), () => {
			frappe.confirm(
				__("Retain manual batch share overrides?"),
				() => run_calc(frm, 1),
				() => run_calc(frm, 0)
			);
		});

		if (frm.doc.status !== "Locked") {
			frm.add_custom_button(__("Lock"), () => {
				frappe.call({
					method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.lock_monthly",
					args: { name: frm.doc.name, lock: 1 },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		} else {
			frm.add_custom_button(__("Unlock"), () => {
				frappe.call({
					method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.lock_monthly",
					args: { name: frm.doc.name, lock: 0 },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		}

		add_list_button(frm, __("Expense Pools"), "MFG Cost Expense Pool");
		add_list_button(frm, __("GL Entry Details"), "MFG Cost GL Line");
		add_list_button(frm, __("Production Batches"), "MFG Cost Batch Allocation");
		add_list_button(frm, __("Batch × Expense Pool Allocations"), "MFG Cost Batch Expense Allocation");
		add_list_button(frm, __("Product Details"), "MFG Cost Product Allocation");
		add_list_button(frm, __("Account / Dimension Summary"), "MFG Cost Account Dimension Summary");
		add_list_button(frm, __("Calculation Revisions"), "MFG Cost Calculation Revision");

		style_over_allocation(frm);
		make_summary_clickable(frm);
		render_account_dimension_summary(frm);
		render_reconciliation_panel(frm);
		render_exceptions_panel(frm);

		add_list_button(frm, __("Reconciliation Checks"), "MFG Cost Reconciliation Check");
		add_list_button(frm, __("Exceptions"), "MFG Cost Exception");
	},
});

function run_calc(frm, retain_overrides) {
	frappe.call({
		method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.calculate_monthly",
		args: { name: frm.doc.name, retain_overrides },
		freeze: true,
		freeze_message: __("Calculating monthly FG cost allocation..."),
		callback: (r) => {
			if (r.message) {
				frappe.show_alert({
					message: __("Status: {0}, revision {1}", [
						r.message.status,
						r.message.revision,
					]),
					indicator: r.message.is_consistent ? "green" : "orange",
				});
			}
			frm.reload_doc();
		},
	});
}

function add_list_button(frm, label, doctype) {
	frm.add_custom_button(label, () => open_filtered_list(frm, doctype), __("Drill-down"));
}

function open_filtered_list(frm, doctype, extra_filters) {
	frappe.route_options = Object.assign(
		{
			monthly_calculation: frm.doc.name,
			revision: frm.doc.revision,
		},
		extra_filters || {}
	);
	frappe.set_route("List", doctype);
}

function style_over_allocation(frm) {
	const $field = frm.get_field("total_allocated_expenses");
	if (!$field || !$field.$wrapper) return;
	const identified = flt(frm.doc.total_identified_expenses);
	const allocated = flt(frm.doc.total_allocated_expenses);
	const excess = allocated - identified;
	const $control = $field.$wrapper.find(".control-value, .like-disabled-input, input");
	if (allocated > identified + 0.0001) {
		$control.css({ color: "#d1242f", "font-weight": "600" });
		frm.dashboard.clear_headline();
		frm.dashboard.set_headline_alert(
			__(
				"Total Allocated Expenses exceed Total Identified Monthly Expenses by {0}. Calculation is Incomplete.",
				[format_currency(excess, frm.doc.currency)]
			),
			"red"
		);
	} else {
		$control.css({ color: "", "font-weight": "" });
	}
}

function make_summary_clickable(frm) {
	const map = [
		{ field: "pool_count", doctype: "MFG Cost Expense Pool" },
		{ field: "gl_entry_count", doctype: "MFG Cost GL Line" },
		{ field: "batch_count", doctype: "MFG Cost Batch Allocation" },
		{ field: "product_count", doctype: "MFG Cost Product Allocation" },
		{ field: "total_allocated_expenses", doctype: "MFG Cost Batch Expense Allocation" },
		{
			field: "missing_uom_count",
			doctype: "MFG Cost Product Allocation",
			extra: { missing_uom_factor: 1 },
		},
		{ field: "total_identified_expenses", doctype: "MFG Cost Expense Pool" },
	];

	map.forEach((entry) => {
		const field = frm.get_field(entry.field);
		if (!field || !field.$wrapper) return;
		// v16 may keep .control-value hidden while showing an input; bind both + label.
		const $targets = field.$wrapper.find(
			".control-value, .like-disabled-input, input, .control-input-wrapper, .form-control"
		);
		const $label = field.$wrapper.find(".control-label, .form-group label").first();
		const handler = () => open_filtered_list(frm, entry.doctype, entry.extra || {});
		$targets.css({ cursor: "pointer", "text-decoration": "underline" });
		$targets.off("click.costing_summary").on("click.costing_summary", handler);
		if ($label.length) {
			$label.css({ cursor: "pointer", "text-decoration": "underline" });
			$label.off("click.costing_summary").on("click.costing_summary", handler);
		}
		field.$wrapper.attr("title", __("Open filtered list"));
		field.$wrapper.css({ cursor: "pointer" });
		field.$wrapper.off("click.costing_summary").on("click.costing_summary", handler);
	});
}

function render_reconciliation_panel(frm) {
	const field = frm.get_field("reconciliation_html");
	if (!field) return;
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "MFG Cost Reconciliation Check",
			filters: { monthly_calculation: frm.doc.name, revision: frm.doc.revision },
			fields: ["name", "check_name", "status", "expected_value", "actual_value", "difference", "details", "batch_no", "account"],
			limit_page_length: 30,
			order_by: "status asc, modified desc",
		},
		callback(r) {
			const rows = r.message || [];
			const counts = { Passed: 0, Failed: 0, Warning: 0, Incomplete: 0 };
			rows.forEach((x) => {
				counts[x.status] = (counts[x.status] || 0) + 1;
			});
			let body = rows
				.map(
					(x) => `<tr style="cursor:pointer" data-status="${frappe.utils.escape_html(x.status || "")}">
					<td>${frappe.utils.escape_html(x.check_name || "")}</td>
					<td>${frappe.utils.escape_html(x.status || "")}</td>
					<td class="text-right">${format_number(x.expected_value)}</td>
					<td class="text-right">${format_number(x.actual_value)}</td>
					<td class="text-right">${format_number(x.difference)}</td>
					<td>${frappe.utils.escape_html(x.batch_no || x.account || "")}</td>
					<td>${frappe.utils.escape_html(x.details || "")}</td>
				</tr>`
				)
				.join("");
			field.$wrapper.html(`
				<p><a href="#" class="costing-open-recon">${__("Open full Reconciliation list")}</a>
				&nbsp;|&nbsp; Passed: ${counts.Passed || 0}, Failed: ${counts.Failed || 0}, Warning: ${counts.Warning || 0}</p>
				<div class="table-responsive" style="max-height:280px;overflow:auto">
				<table class="table table-bordered table-condensed">
				<thead><tr><th>${__("Check")}</th><th>${__("Status")}</th><th>${__("Expected")}</th><th>${__("Actual")}</th><th>${__("Diff")}</th><th>${__("Ref")}</th><th>${__("Details")}</th></tr></thead>
				<tbody>${body || `<tr><td colspan="7" class="text-muted">${__("No structured reconciliation rows yet. Recalculate to generate.")}</td></tr>`}</tbody>
				</table></div>`);
			field.$wrapper.find(".costing-open-recon").on("click", (e) => {
				e.preventDefault();
				open_filtered_list(frm, "MFG Cost Reconciliation Check");
			});
		},
	});
}

function render_exceptions_panel(frm) {
	const field = frm.get_field("exceptions_html");
	if (!field) return;
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "MFG Cost Exception",
			filters: { monthly_calculation: frm.doc.name, revision: frm.doc.revision },
			fields: ["exception_type", "severity", "item_code", "item_name", "batch_no", "description", "resolution_status"],
			limit_page_length: 30,
			order_by: "severity asc, modified desc",
		},
		callback(r) {
			const rows = r.message || [];
			let body = rows
				.map(
					(x) => `<tr>
					<td>${frappe.utils.escape_html(x.exception_type || "")}</td>
					<td>${frappe.utils.escape_html(x.severity || "")}</td>
					<td>${frappe.utils.escape_html(x.item_code || "")}</td>
					<td>${frappe.utils.escape_html(x.item_name || "")}</td>
					<td>${frappe.utils.escape_html(x.batch_no || "")}</td>
					<td>${frappe.utils.escape_html(x.description || "")}</td>
				</tr>`
				)
				.join("");
			field.$wrapper.html(`
				<p><a href="#" class="costing-open-exc">${__("Open full Exceptions list")}</a></p>
				<div class="table-responsive" style="max-height:280px;overflow:auto">
				<table class="table table-bordered table-condensed">
				<thead><tr><th>${__("Type")}</th><th>${__("Severity")}</th><th>${__("Item")}</th><th>${__("Name")}</th><th>${__("Batch")}</th><th>${__("Description")}</th></tr></thead>
				<tbody>${body || `<tr><td colspan="6" class="text-muted">${__("No structured exceptions. Recalculate to generate.")}</td></tr>`}</tbody>
				</table></div>`);
			field.$wrapper.find(".costing-open-exc").on("click", (e) => {
				e.preventDefault();
				open_filtered_list(frm, "MFG Cost Exception");
			});
		},
	});
}

function render_account_dimension_summary(frm) {
	const field = frm.get_field("account_dimension_summary_html");
	if (!field) return;
	field.$wrapper.html(`<div class="text-muted">${__("Loading...")}</div>`);
	frappe.call({
		method:
			"erpnext_extensions.iran_accounting.fg_cost_allocation.contract.get_account_dimension_summary",
		args: { monthly_name: frm.doc.name, revision: frm.doc.revision },
		callback(r) {
			const rows = (r.message && r.message.rows) || [];
			if (!rows.length) {
				field.$wrapper.html(
					`<div class="text-muted">${__("No account / dimension allocations for this revision.")}</div>`
				);
				return;
			}
			const head = [
				__("Account"),
				__("Account Name"),
				__("Cost Center"),
				__("Department"),
				__("Expense Pool"),
				__("Pool Monthly Net"),
				__("Batches"),
				__("Total Applied Share"),
				__("Total Allocated"),
				__("Status"),
			];
			let body = "";
			rows.forEach((row) => {
				const pool = row.expense_pool || "";
				body += `<tr data-pool="${frappe.utils.escape_html(pool)}" style="cursor:pointer">
					<td>${frappe.utils.escape_html(row.account || "")}</td>
					<td>${frappe.utils.escape_html(row.account_name || "")}</td>
					<td>${frappe.utils.escape_html(row.cost_center || "")}</td>
					<td>${frappe.utils.escape_html(row.department || "")}</td>
					<td>${frappe.utils.escape_html(pool)}</td>
					<td class="text-right">${format_number(row.pool_monthly_net_expense)}</td>
					<td class="text-right">${cint(row.allocated_batch_count)}</td>
					<td class="text-right">${format_number(row.total_applied_batch_share)}</td>
					<td class="text-right">${format_number(row.total_allocated_amount)}</td>
					<td>${frappe.utils.escape_html(row.allocation_status || "")}</td>
				</tr>`;
			});
			field.$wrapper.html(`
				<div class="table-responsive" style="max-height:420px;overflow:auto">
				<table class="table table-bordered table-condensed">
					<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
					<tbody>${body}</tbody>
				</table>
				<p class="text-muted small">${__("Click a row to open Batch × Expense Pool Allocations for that Expense Pool.")}</p>
				</div>`);
			field.$wrapper.find("tbody tr").on("click", function () {
				const pool = $(this).attr("data-pool");
				open_filtered_list(
					frm,
					"MFG Cost Batch Expense Allocation",
					pool ? { expense_pool: pool } : {}
				);
			});
		},
	});
}

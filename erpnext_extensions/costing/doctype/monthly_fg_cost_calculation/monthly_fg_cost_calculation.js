// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
// Monthly FG Cost Calculation — native List drill-downs + export (no custom HTML grids).

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

		const drill = __("Details");
		add_list_button(frm, __("Expense Pools"), "MFG Cost Expense Pool", drill);
		add_list_button(frm, __("GL Entry Details"), "MFG Cost GL Line", drill);
		add_list_button(frm, __("Production Batches"), "MFG Cost Batch Allocation", drill);
		add_list_button(frm, __("Batch × Expense Pool"), "MFG Cost Batch Expense Allocation", drill);
		add_list_button(frm, __("Product Allocations"), "MFG Cost Product Allocation", drill);
		add_list_button(frm, __("Account / Dimension Summary"), "MFG Cost Account Dimension Summary", drill);
		add_list_button(frm, __("Reconciliation Checks"), "MFG Cost Reconciliation Check", drill);
		add_list_button(frm, __("Exceptions"), "MFG Cost Exception", drill);
		add_list_button(frm, __("Calculation Revisions"), "MFG Cost Calculation Revision", drill);

		const exp = __("Export");
		COSTING_EXPORT_TARGETS.forEach((t) => {
			frm.add_custom_button(__(t.label) + " (CSV)", () => export_detail(frm, t.doctype, "csv"), exp);
			frm.add_custom_button(__(t.label) + " (XLSX)", () => export_detail(frm, t.doctype, "xlsx"), exp);
		});

		style_over_allocation(frm);
		make_summary_clickable(frm);
		show_consumable_status(frm);
		render_native_summary_panels(frm);
	},
});

const COSTING_EXPORT_TARGETS = [
	{ label: "Expense Pools", doctype: "MFG Cost Expense Pool" },
	{ label: "GL Lines", doctype: "MFG Cost GL Line" },
	{ label: "Batches", doctype: "MFG Cost Batch Allocation" },
	{ label: "Batch Expense", doctype: "MFG Cost Batch Expense Allocation" },
	{ label: "Product Allocation", doctype: "MFG Cost Product Allocation" },
	{ label: "Account Dimension", doctype: "MFG Cost Account Dimension Summary" },
	{ label: "Reconciliation", doctype: "MFG Cost Reconciliation Check" },
	{ label: "Exceptions", doctype: "MFG Cost Exception" },
	{ label: "Revisions", doctype: "MFG Cost Calculation Revision" },
];

function run_calc(frm, retain_overrides) {
	frappe.call({
		method: "erpnext_extensions.iran_accounting.fg_cost_allocation.engine.calculate_monthly",
		args: { name: frm.doc.name, retain_overrides },
		freeze: true,
		freeze_message: __("Calculating monthly FG cost allocation..."),
		callback: (r) => {
			if (r.message) {
				frappe.show_alert({
					message: __("Status: {0}, revision {1}", [r.message.status, r.message.revision]),
					indicator: r.message.is_consistent ? "green" : "orange",
				});
			}
			frm.reload_doc();
		},
	});
}

function add_list_button(frm, label, doctype, group) {
	frm.add_custom_button(label, () => open_filtered_list(frm, doctype), group);
}

function open_filtered_list(frm, doctype, extra_filters) {
	const opts = { monthly_calculation: frm.doc.name };
	if (doctype !== "MFG Cost Calculation Revision") {
		opts.revision = frm.doc.revision;
	}
	frappe.route_options = Object.assign(opts, extra_filters || {});
	frappe.set_route("List", doctype);
}

function export_detail(frm, doctype, file_format) {
	const args = {
		doctype,
		file_format,
		monthly_calculation: frm.doc.name,
	};
	if (doctype !== "MFG Cost Calculation Revision") {
		args.revision = frm.doc.revision;
	}
	open_export_url(
		"erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing.export_costing_doctype",
		args
	);
}

function open_export_url(method, args) {
	const params = new URLSearchParams();
	Object.entries(args || {}).forEach(([k, v]) => {
		if (v !== undefined && v !== null && v !== "") params.set(k, v);
	});
	window.open(`/api/method/${method}?${params.toString()}`, "_blank");
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
		{ field: "exception_count", doctype: "MFG Cost Exception" },
	];

	map.forEach((entry) => {
		const field = frm.get_field(entry.field);
		if (!field || !field.$wrapper) return;
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

function show_consumable_status(frm) {
	frappe.call({
		method: "erpnext_extensions.iran_accounting.fg_cost_allocation.contract.is_consumable",
		args: { monthly_name: frm.doc.name },
		callback(r) {
			const msg = r.message || {};
			const ok = !!msg.consumable;
			frm.dashboard.add_indicator(
				ok ? __("Consumable: Yes") : __("Consumable: No — {0}", [msg.reason || frm.doc.status]),
				ok ? "green" : "orange"
			);
		},
	});
}

function render_native_summary_panels(frm) {
	frappe.call({
		method: "erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing.get_detail_counts",
		args: { monthly_name: frm.doc.name, revision: frm.doc.revision },
		callback(r) {
			const c = r.message || {};
			render_panel(
				frm,
				"account_dimension_summary_html",
				__("Account / Dimension Allocation Summary"),
				__("Native list of {0} Account × Dimension rows for revision {1}. Use Details or Export — no embedded custom grid.", [
					cint(c.account_dimension),
					cint(c.revision),
				]),
				"MFG Cost Account Dimension Summary",
				c.account_dimension
			);
			const recon = c.reconciliation || {};
			const recon_line = __(
				"Passed {0} · Failed {1} · Warning {2} · Total {3} (revision {4})",
				[
					cint(recon.Passed),
					cint(recon.Failed),
					cint(recon.Warning),
					cint(c.reconciliation_total),
					cint(c.revision),
				]
			);
			render_panel(
				frm,
				"reconciliation_html",
				__("Reconciliation"),
				recon_line +
					"<br>" +
					__(
						"Full checks open in the standard List View (filterable, sortable, paginated). Preview tables were removed to match Frappe Desk conventions."
					),
				"MFG Cost Reconciliation Check",
				c.reconciliation_total
			);

			const exc = c.exceptions || {};
			let exc_msg;
			if (!cint(c.exceptions_total)) {
				exc_msg = __(
					"No structured exceptions for revision {0}. This means exceptions were generated and none remain open — not that they were skipped.",
					[cint(c.revision)]
				);
			} else {
				exc_msg = __(
					"Blocking {0} · Warning {1} · Info {2} · Total {3} (revision {4})",
					[
						cint(exc.Blocking),
						cint(exc.Warning),
						cint(exc.Info) + cint(exc.Informational),
						cint(c.exceptions_total),
						cint(c.revision),
					]
				);
			}
			render_panel(
				frm,
				"exceptions_html",
				__("Exceptions"),
				exc_msg +
					"<br>" +
					__("Open the standard Exceptions list for Type, Severity, Item, Batch, Description, and export."),
				"MFG Cost Exception",
				c.exceptions_total
			);
		},
	});
}

function render_panel(frm, fieldname, title, body_html, doctype, count) {
	const field = frm.get_field(fieldname);
	if (!field) return;
	const id = frappe.utils.get_random(8);
	field.$wrapper.html(`
		<div class="costing-summary-panel" style="border:1px solid var(--border-color);border-radius:8px;padding:12px 14px;background:var(--fg-color)">
			<div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center">
				<div>
					<div style="font-weight:600;margin-bottom:4px">${frappe.utils.escape_html(title)}</div>
					<div class="text-muted" style="font-size:13px;line-height:1.45">${body_html}</div>
				</div>
				<div style="display:flex;gap:8px;flex-wrap:wrap">
					<button class="btn btn-sm btn-default" data-action="open" data-id="${id}">${__("Open List")} (${cint(count)})</button>
					<button class="btn btn-sm btn-default" data-action="csv" data-id="${id}">${__("Export CSV")}</button>
					<button class="btn btn-sm btn-default" data-action="xlsx" data-id="${id}">${__("Export XLSX")}</button>
				</div>
			</div>
		</div>`);
	field.$wrapper.find(`[data-id="${id}"]`).on("click", function () {
		const action = $(this).attr("data-action");
		if (action === "open") {
			open_filtered_list(frm, doctype);
		} else {
			export_detail(frm, doctype, action);
		}
	});
}

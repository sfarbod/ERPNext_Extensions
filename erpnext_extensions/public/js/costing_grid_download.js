// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
// Enable Download (CSV + XLSX) on Costing child tables and list views.
// Settings child tables export ALL rows via server when possible.

frappe.provide("erpnext_extensions.costing");

erpnext_extensions.costing.DOWNLOADABLE_TABLE_FIELDS = [
	"item_primary_uoms",
	"expense_account_dimension_rules",
	"rule_generation_summary",
	"uom_validation_summary",
	"batch_allocations",
	"product_allocations",
	"expense_pools",
	"gl_lines",
	"account_dimension_summaries",
	"batch_expense_allocations",
	"product_expense_allocations",
	"reconciliation_checks",
	"exceptions",
	"revisions",
];

erpnext_extensions.costing.READONLY_SUMMARY_FIELDS = [
	"rule_generation_summary",
	"uom_validation_summary",
];

erpnext_extensions.costing.SETTINGS_CHILD_EXPORT_FIELDS = [
	"item_primary_uoms",
	"expense_account_dimension_rules",
	"rule_generation_summary",
	"uom_validation_summary",
];

erpnext_extensions.costing.LIST_EXPORT_DOCTYPES = [
	"MFG Cost Expense Pool",
	"MFG Cost GL Line",
	"MFG Cost Batch Allocation",
	"MFG Cost Batch Expense Allocation",
	"MFG Cost Product Allocation",
	"MFG Cost Product Expense Allocation",
	"MFG Cost Account Dimension Summary",
	"MFG Cost Reconciliation Check",
	"MFG Cost Exception",
	"MFG Cost Calculation Revision",
	"Monthly FG Cost Calculation",
];

erpnext_extensions.costing.enable_child_table_downloads = function (frm) {
	if (!frm || !frm.fields_dict) return;

	(erpnext_extensions.costing.DOWNLOADABLE_TABLE_FIELDS || []).forEach((fieldname) => {
		const field = frm.get_field(fieldname);
		if (!field || !field.grid) return;

		const grid = field.grid;
		const is_readonly_summary = (erpnext_extensions.costing.READONLY_SUMMARY_FIELDS || []).includes(
			fieldname
		);

		if (is_readonly_summary) {
			grid.cannot_add_rows = true;
			grid.cannot_delete_rows = true;
			if (typeof grid.toggle_add_row === "function") {
				try {
					grid.toggle_add_row(false);
				} catch (e) {
					/* ignore */
				}
			}
			grid.wrapper && grid.wrapper.find(".grid-add-row, .grid-add-multiple-rows").addClass("hidden");
			grid.wrapper && grid.wrapper.find(".grid-remove-rows, .grid-remove-all-rows").addClass("hidden");
			grid.wrapper && grid.wrapper.find(".grid-upload").addClass("hidden").off("click");
		}

		if (!grid.__costing_download_bound) {
			const $btn = grid.wrapper.find(".grid-download");
			$btn.removeClass("hidden");
			if (!$btn.data("costing-download-bound")) {
				$btn.data("costing-download-bound", 1);
				$btn.off("click.costing_download").on("click.costing_download", () => {
					erpnext_extensions.costing.download_grid_csv(frm, fieldname);
					return false;
				});
			}
			// Add XLSX button next to Download when missing
			if (!grid.wrapper.find(".grid-download-xlsx").length) {
				const $xlsx = $(
					`<button type="button" class="btn btn-xs btn-secondary grid-download-xlsx" style="margin-left:4px">${__(
						"XLSX"
					)}</button>`
				);
				$btn.after($xlsx);
				$xlsx.on("click", () => {
					erpnext_extensions.costing.download_grid_xlsx(frm, fieldname);
					return false;
				});
			}
			grid.__costing_download_bound = true;
		} else {
			grid.wrapper.find(".grid-download").removeClass("hidden");
			if (is_readonly_summary) {
				grid.wrapper.find(".grid-upload").addClass("hidden");
			}
		}
	});
};

erpnext_extensions.costing.download_grid_csv = function (frm, fieldname) {
	if (
		frm.doctype === "Finished Goods Cost Allocation Settings" &&
		(erpnext_extensions.costing.SETTINGS_CHILD_EXPORT_FIELDS || []).includes(fieldname)
	) {
		erpnext_extensions.costing.export_settings_child(frm, fieldname, "csv");
		return;
	}
	erpnext_extensions.costing._client_grid_download(frm, fieldname, "csv");
};

erpnext_extensions.costing.download_grid_xlsx = function (frm, fieldname) {
	if (
		frm.doctype === "Finished Goods Cost Allocation Settings" &&
		(erpnext_extensions.costing.SETTINGS_CHILD_EXPORT_FIELDS || []).includes(fieldname)
	) {
		erpnext_extensions.costing.export_settings_child(frm, fieldname, "xlsx");
		return;
	}
	// Client grids without server child export: CSV via downloadify; prompt XLSX via server if monthly
	erpnext_extensions.costing._client_grid_download(frm, fieldname, "csv");
	frappe.show_alert({
		message: __("XLSX for in-form grids uses CSV when rows are already loaded on the form."),
		indicator: "blue",
	});
};

erpnext_extensions.costing.export_settings_child = function (frm, fieldname, file_format) {
	const url =
		"/api/method/erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing.export_settings_child_table";
	const args = {
		settings_name: frm.doc.name,
		child_field: fieldname,
		file_format: file_format || "csv",
	};
	erpnext_extensions.costing._download_via_get(url, args);
};

erpnext_extensions.costing._client_grid_download = function (frm, fieldname) {
	const field = frm.get_field(fieldname);
	if (!field || !field.grid) return;

	const title = field.df.label || frappe.model.unscrub(fieldname);
	const meta = frappe.get_meta(field.df.options);
	if (!meta) return;

	const value_fields = (meta.fields || []).filter((df) => frappe.model.is_value_type(df.fieldtype));
	const headers = value_fields.map((df) => df.label || df.fieldname);
	const fieldnames = value_fields.map((df) => df.fieldname);
	const rows = frm.doc[fieldname] || [];
	if (!rows.length) {
		frappe.msgprint(__("No records to export."));
		return;
	}

	const data = [[title], [], headers, fieldnames];
	rows.forEach((row) => {
		data.push(
			fieldnames.map((fn, i) => {
				let value = row[fn];
				const df = value_fields[i];
				if (df.fieldtype === "Date" && value) {
					value = frappe.datetime.str_to_user(value);
				}
				if (df.fieldtype === "Check") {
					value = cint(value);
				}
				return value == null ? "" : value;
			})
		);
	});

	frappe.tools.downloadify(data, null, title);
};

erpnext_extensions.costing._download_via_get = function (url, args) {
	const params = new URLSearchParams();
	Object.entries(args || {}).forEach(([k, v]) => {
		if (v !== undefined && v !== null && v !== "") params.set(k, v);
	});
	window.open(`${url}?${params.toString()}`, "_blank");
};

erpnext_extensions.costing.export_listview = function (listview, file_format) {
	const doctype = listview.doctype;
	if (!(erpnext_extensions.costing.LIST_EXPORT_DOCTYPES || []).includes(doctype)) {
		frappe.msgprint(__("Export is not configured for {0}.", [doctype]));
		return;
	}
	const filters = listview.filter_area ? listview.filter_area.get() : [];
	const args = {
		doctype,
		file_format: file_format || "csv",
		filters: JSON.stringify(filters || []),
	};
	const search = listview.search_input && listview.search_input.get_value
		? listview.search_input.get_value()
		: "";
	if (search) args.search = search;
	erpnext_extensions.costing._download_via_get(
		"/api/method/erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing.export_costing_doctype",
		args
	);
};

// Hook ListView export buttons for Costing doctypes
$(document).on("frappe.ui.form.on-list", () => {});

frappe.listview_settings = frappe.listview_settings || {};

(erpnext_extensions.costing.LIST_EXPORT_DOCTYPES || []).forEach((dt) => {
	const existing = frappe.listview_settings[dt] || {};
	const prev_onload = existing.onload;
	frappe.listview_settings[dt] = Object.assign({}, existing, {
		onload(listview) {
			if (typeof prev_onload === "function") {
				prev_onload(listview);
			}
			if (listview._costing_export_bound) return;
			listview._costing_export_bound = true;
			listview.page.add_inner_button(__("Export CSV"), () => {
				erpnext_extensions.costing.export_listview(listview, "csv");
			});
			listview.page.add_inner_button(__("Export XLSX"), () => {
				erpnext_extensions.costing.export_listview(listview, "xlsx");
			});
		},
	});
});

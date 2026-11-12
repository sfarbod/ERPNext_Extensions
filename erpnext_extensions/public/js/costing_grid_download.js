// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
// Enable standard Frappe grid Download (CSV) on Costing child tables.
// Hides Upload on read-only summary grids.

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

		// Ensure Download is visible and wired (standard Frappe CSV export of all rows).
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
	const field = frm.get_field(fieldname);
	if (!field || !field.grid) return;

	const title = field.df.label || frappe.model.unscrub(fieldname);
	const meta = frappe.get_meta(field.df.options);
	if (!meta) return;

	const value_fields = (meta.fields || []).filter((df) => frappe.model.is_value_type(df.fieldtype));
	const headers = value_fields.map((df) => df.label || df.fieldname);
	const fieldnames = value_fields.map((df) => df.fieldname);

	const data = [[title], [], headers, fieldnames];
	(frm.doc[fieldname] || []).forEach((row) => {
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

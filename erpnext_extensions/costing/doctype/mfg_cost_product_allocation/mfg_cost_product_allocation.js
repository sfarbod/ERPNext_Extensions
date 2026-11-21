// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
// UI: Document Account Control Breakdown with an integrated grid total row.

frappe.ui.form.on("MFG Cost Product Allocation", {
	refresh(frm) {
		frm.trigger("style_document_breakdown");
		frm.trigger("render_breakdown_grid_footer");
		frm.trigger("setup_breakdown_excel_button");
		frm.trigger("setup_credit_allocation_nav");
	},

	setup_credit_allocation_nav(frm) {
		if (frm.is_new()) {
			return;
		}
		const nav = __("Credit Control");
		frm.add_custom_button(__("Control Credit Pools"), () => {
			const args = {};
			if (frm.doc.persian_period) {
				args.persian_period = frm.doc.persian_period;
			}
			frappe.set_route("query-report", "MFG Cost Control Credit Pool", args);
		}, nav);
		frm.add_custom_button(__("This Product Credit Shares"), () => {
			frappe.set_route("query-report", "MFG Cost Control Credit Batch Product Allocation", {
				product_allocation: frm.doc.name,
				batch_no: frm.doc.batch_no,
				item_code: frm.doc.item_code,
			});
		}, nav);
		if (!frm.doc.monthly_calculation) {
			return;
		}
		frappe.db.get_value(
			"MFG Cost Control Credit Allocation",
			{
				monthly_calculation: frm.doc.monthly_calculation,
				status: ["in", ["Calculated", "Submitted"]],
				docstatus: ["<", 2],
			},
			"name"
		).then((r) => {
			const name = r && r.message && r.message.name;
			if (!name) {
				return;
			}
			frm.add_custom_button(__("Open Credit Allocation {0}", [name]), () => {
				frappe.set_route("Form", "MFG Cost Control Credit Allocation", name);
			}, nav);
		});
	},

	breakdown_total_qty(frm) {
		frm.trigger("render_breakdown_grid_footer");
	},
	breakdown_total_overhead(frm) {
		frm.trigger("render_breakdown_grid_footer");
	},
	breakdown_total_labor(frm) {
		frm.trigger("render_breakdown_grid_footer");
	},
	breakdown_total_amount(frm) {
		frm.trigger("render_breakdown_grid_footer");
	},

	style_document_breakdown(frm) {
		_ensure_breakdown_styles();

		const grid_field = frm.get_field("document_account_control_breakdown");
		if (!grid_field || !grid_field.grid) {
			return;
		}

		const $grid_wrap = $(grid_field.$wrapper);
		const $section = $grid_wrap.closest(".form-section");
		if ($section && $section.length) {
			$section.addClass("mfg-pa-doc-breakdown-section");
		}

		// Hide legacy HTML summary field if present.
		if (frm.fields_dict.breakdown_totals_html) {
			frm.set_df_property("breakdown_totals_html", "hidden", 1);
			frm.fields_dict.breakdown_totals_html.$wrapper.empty().hide();
		}

		const grid = grid_field.grid;
		const widths = {
			source_document_type: 1,
			source_document: 2,
			quantity: 1,
			overhead_amount: 1,
			labor_amount: 1,
			allocated_overhead_share: 1,
			allocated_labor_share: 1,
			unallocated_overhead: 1,
			unallocated_labor: 1,
			total_allocated_amount: 1,
		};
		Object.entries(widths).forEach(([fieldname, columns]) => {
			if (grid.update_docfield_property) {
				grid.update_docfield_property(fieldname, "columns", columns);
				grid.update_docfield_property(fieldname, "in_list_view", 1);
			}
		});
		if (grid.update_docfield_property) {
			grid.update_docfield_property("currency", "in_list_view", 0);
		}
		if (grid.setup_visible_columns) {
			grid.setup_visible_columns();
		}

		_hook_grid_refresh(frm, grid);
		_hook_footer_resize(frm);

		if (typeof grid.refresh === "function") {
			grid.refresh();
		}

		frappe.after_ajax(() => {
			$(grid_field.$wrapper).css({ width: "100%", "max-width": "100%" });
			$(grid_field.$wrapper).find(".form-grid").css({ width: "100%", "max-width": "100%" });
			frm.trigger("render_breakdown_grid_footer");
		});
	},

	render_breakdown_grid_footer(frm) {
		_ensure_breakdown_styles();
		const grid_field = frm.get_field("document_account_control_breakdown");
		if (!grid_field || !grid_field.grid) {
			return;
		}
		_render_grid_total_row(frm, grid_field.grid);
	},

	setup_breakdown_excel_button(frm) {
		_ensure_breakdown_styles();

		// Resolve section via the child-table field (Section Break wrappers vary by Desk layout).
		const grid_field = frm.get_field("document_account_control_breakdown");
		if (!grid_field || !grid_field.$wrapper) {
			return;
		}
		let $section = $(grid_field.$wrapper).closest(".form-section");
		if (!$section.length && frm.fields_dict.section_document_breakdown) {
			const sw = frm.fields_dict.section_document_breakdown.$wrapper;
			$section = sw
				? $(sw).closest(".form-section").addBack(".form-section").filter(".form-section").first()
				: $();
			if (!$section.length && sw) {
				$section = $(sw).hasClass("form-section") ? $(sw) : $(sw).find(".form-section").first();
			}
		}
		if (!$section.length) {
			return;
		}
		$section.addClass("mfg-pa-doc-breakdown-section");

		let $head = $section.children(".section-head").first();
		if (!$head.length) {
			$head = $section.find("> .section-head, .section-head").first();
		}
		if (!$head.length) {
			$head = $section.find(".section-header, .row.form-section-heading").first();
		}
		if (!$head.length) {
			return;
		}

		$head.addClass("mfg-pa-breakdown-section-head");
		$head.find(".mfg-pa-breakdown-excel-btn").remove();

		const $btn = $(`
			<button type="button" class="btn btn-xs btn-default mfg-pa-breakdown-excel-btn" title="${frappe.utils.escape_html(
				__("دانلود اکسل")
			)}">
				<svg class="icon icon-sm" aria-hidden="true" style="margin-inline-end:4px">
					<use href="#icon-download"></use>
				</svg>
				<span>${__("دانلود اکسل")}</span>
			</button>
		`);
		$head.append($btn);
		$btn.on("click", (e) => {
			e.preventDefault();
			e.stopPropagation();
			if (frm.is_new()) {
				frappe.msgprint(__("Save the document before downloading."));
				return;
			}
			const url =
				"/api/method/erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing.export_product_document_breakdown";
			const params = new URLSearchParams({ product_allocation: frm.doc.name });
			window.open(`${url}?${params.toString()}`, "_blank");
		});
	},
});

function _hook_grid_refresh(frm, grid) {
	if (grid.__mfg_pa_footer_hooked) {
		return;
	}
	grid.__mfg_pa_footer_hooked = true;
	const original_refresh = grid.refresh.bind(grid);
	grid.refresh = function () {
		const result = original_refresh();
		frappe.after_ajax(() => {
			_render_grid_total_row(frm, grid);
		});
		setTimeout(() => _render_grid_total_row(frm, grid), 0);
		return result;
	};
}

function _hook_footer_resize(frm) {
	if (window.__mfg_pa_footer_resize_bound) {
		return;
	}
	window.__mfg_pa_footer_resize_bound = true;
	$(window).on(
		"resize.mfg_pa_breakdown_footer",
		frappe.utils.debounce(() => {
			if (cur_frm && cur_frm.doctype === "MFG Cost Product Allocation") {
				cur_frm.trigger("render_breakdown_grid_footer");
			}
		}, 150)
	);
}

function _format_qty(qty) {
	const n = flt(qty);
	return format_number(n, null, n % 1 ? 3 : 0);
}

function _render_grid_total_row(frm, grid) {
	const $form_grid = $(grid.wrapper).find(".form-grid").first();
	if (!$form_grid.length) {
		return;
	}

	$form_grid.find(".mfg-pa-breakdown-grid-total-row").remove();

	const rows = frm.doc.document_account_control_breakdown || [];
	const currency = frm.doc.currency || "IRR";
	const qty = flt(frm.doc.breakdown_total_qty);
	const overhead = flt(frm.doc.breakdown_total_overhead);
	const labor = flt(frm.doc.breakdown_total_labor);
	const total = flt(frm.doc.breakdown_total_amount);

	// Mirror the live heading columns so widths/order stay in sync (including scroll).
	const $heading_cols = $form_grid.find(".grid-heading-row .data-row > .col, .grid-heading-row .data-row > .row-check, .grid-heading-row .data-row > .row-index");
	let col_defs = [];
	if ($heading_cols.length) {
		$heading_cols.each(function () {
			const $col = $(this);
			col_defs.push({
				fieldname: $col.attr("data-fieldname") || "",
				classes: $col.attr("class") || "col grid-static-col",
				width: $col[0].getBoundingClientRect().width,
				is_check: $col.hasClass("row-check"),
				is_index: $col.hasClass("row-index"),
			});
		});
	} else if (grid.visible_columns && grid.visible_columns.length) {
		col_defs.push({ fieldname: "", classes: "row-check col", width: 40, is_check: true });
		col_defs.push({ fieldname: "", classes: "row-index grid-static-col col", width: 40, is_index: true });
		grid.visible_columns.forEach(([df, columns]) => {
			col_defs.push({
				fieldname: df.fieldname,
				classes: `col grid-static-col col-xs-${columns || 2}`,
				width: null,
				is_check: false,
				is_index: false,
			});
		});
	} else {
		return;
	}

	const values = {
		source_document_type: __("جمع کل"),
		source_document: "",
		quantity: _format_qty(qty),
		overhead_amount: format_currency(overhead, currency),
		labor_amount: format_currency(labor, currency),
		allocated_overhead_share: format_currency(flt(frm.doc.breakdown_total_allocated_overhead_share), currency),
		allocated_labor_share: format_currency(flt(frm.doc.breakdown_total_allocated_labor_share), currency),
		unallocated_overhead: format_currency(flt(frm.doc.breakdown_total_unallocated_overhead), currency),
		unallocated_labor: format_currency(flt(frm.doc.breakdown_total_unallocated_labor), currency),
		total_allocated_amount: format_currency(total, currency),
	};
	const numeric_fields = new Set([
		"quantity",
		"overhead_amount",
		"labor_amount",
		"allocated_overhead_share",
		"allocated_labor_share",
		"unallocated_overhead",
		"unallocated_labor",
		"total_allocated_amount",
	]);

	let cells_html = "";
	col_defs.forEach((col) => {
		const style = col.width ? `style="width:${Math.round(col.width)}px;min-width:${Math.round(col.width)}px;max-width:${Math.round(col.width)}px;"` : "";
		if (col.is_check) {
			cells_html += `<div class="${frappe.utils.escape_html(col.classes)}" ${style}></div>`;
			return;
		}
		if (col.is_index) {
			cells_html += `<div class="${frappe.utils.escape_html(col.classes)}" ${style}><span>Σ</span></div>`;
			return;
		}
		const raw = values.hasOwnProperty(col.fieldname) ? values[col.fieldname] : "";
		const align = numeric_fields.has(col.fieldname) ? " text-right" : "";
		const bold = col.fieldname === "source_document_type" || numeric_fields.has(col.fieldname) ? " mfg-pa-total-cell" : "";
		cells_html += `
			<div class="${frappe.utils.escape_html(col.classes)}${align}${bold}"
				data-fieldname="${frappe.utils.escape_html(col.fieldname || "")}" ${style}>
				<div class="static-area ellipsis">${frappe.utils.escape_html(String(raw))}</div>
			</div>
		`;
	});

	const $footer = $(`
		<div class="grid-row mfg-pa-breakdown-grid-total-row" data-total-row="1">
			<div class="data-row row">
				${cells_html}
			</div>
		</div>
	`);

	// Place inside form-grid so horizontal scroll keeps footer aligned with columns.
	$form_grid.append($footer);

	// Re-sync widths after layout; keep footer in the same scroll container.
	requestAnimationFrame(() => {
		_sync_footer_column_widths($form_grid);
	});

	// Empty-grid: still show footer (zeros from parent totals) for consistent UX.
	$footer.toggle(true);
	$footer.attr("data-row-count", String(rows.length));
}

function _sync_footer_column_widths($form_grid) {
	const $footer_cols = $form_grid.find(".mfg-pa-breakdown-grid-total-row .data-row > .col, .mfg-pa-breakdown-grid-total-row .data-row > .row-check, .mfg-pa-breakdown-grid-total-row .data-row > .row-index");
	const $heading_cols = $form_grid.find(".grid-heading-row .data-row > .col, .grid-heading-row .data-row > .row-check, .grid-heading-row .data-row > .row-index");
	if (!$footer_cols.length || $footer_cols.length !== $heading_cols.length) {
		return;
	}
	$heading_cols.each(function (idx) {
		const w = Math.round(this.getBoundingClientRect().width);
		const $fc = $footer_cols.eq(idx);
		$fc.css({
			width: `${w}px`,
			"min-width": `${w}px`,
			"max-width": `${w}px`,
			flex: `0 0 ${w}px`,
		});
	});
}

function _ensure_breakdown_styles() {
	const style_id = "mfg-pa-doc-breakdown-style";
	if (document.getElementById(style_id)) {
		return;
	}
	const css = `
	.form-section.mfg-pa-doc-breakdown-section .section-body,
	.form-section.mfg-pa-doc-breakdown-section .form-column,
	.form-section.mfg-pa-doc-breakdown-section [data-fieldname="document_account_control_breakdown"],
	.form-section.mfg-pa-doc-breakdown-section [data-fieldname="document_account_control_breakdown"] .control-input-wrapper,
	.form-section.mfg-pa-doc-breakdown-section [data-fieldname="document_account_control_breakdown"] .form-grid-container,
	.form-section.mfg-pa-doc-breakdown-section [data-fieldname="document_account_control_breakdown"] .form-grid {
		width: 100% !important;
		max-width: 100% !important;
	}
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="source_document"] .static-area {
		white-space: nowrap !important;
		overflow: hidden;
		text-overflow: ellipsis;
		font-weight: 600;
	}
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="quantity"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="overhead_amount"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="labor_amount"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="allocated_overhead_share"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="allocated_labor_share"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="unallocated_overhead"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="unallocated_labor"],
	.form-section.mfg-pa-doc-breakdown-section .grid-static-col[data-fieldname="total_allocated_amount"] {
		text-align: right !important;
		direction: ltr;
	}
	.form-section.mfg-pa-doc-breakdown-section .form-grid-container {
		overflow-x: auto;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-grid-total-row {
		border-top: 1px solid var(--border-color);
		background: var(--subtle-fg, var(--control-bg));
		font-weight: 600;
		pointer-events: none;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-grid-total-row .data-row {
		min-height: 36px;
		align-items: center;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-grid-total-row .static-area {
		font-weight: 700;
		white-space: nowrap;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-grid-total-row .text-right .static-area {
		text-align: right;
		direction: ltr;
		display: block;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-grid-total-row .row-index span {
		font-weight: 700;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-section-head {
		display: flex !important;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		flex-wrap: wrap;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-excel-btn {
		margin-inline-start: auto;
		display: inline-flex;
		align-items: center;
		white-space: nowrap;
		flex: 0 0 auto;
		pointer-events: auto;
		z-index: 2;
	}
	.form-section.mfg-pa-doc-breakdown-section .mfg-pa-breakdown-excel-btn .icon {
		width: 14px;
		height: 14px;
	}
	`;
	frappe.dom.set_style(css, style_id);
}

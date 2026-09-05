frappe.provide("erpnext_extensions.account_explorer.core");

/**
 * Analysis Filters — summary-chip / presentation rows.
 * Split from explorer_analysis_filters.js to keep the 800-line architecture gate.
 */
(() => {
	const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;

	const ae_af_is_empty = (value) =>
		value === undefined ||
		value === null ||
		value === "" ||
		(Array.isArray(value) && !value.length);

	const ae_af_parse_key = (key) => {
		const text = String(key || "").trim();
		if (!text) {
			return null;
		}
		if (text.startsWith("dimensions.")) {
			return { kind: "dimension", fieldname: text.slice("dimensions.".length) };
		}
		const top = ["account", "party", "unified_party", "voucher", "currency", "item_group", "item", "warehouse"];
		if (top.includes(text)) {
			return { kind: "top", fieldname: text };
		}
		return { kind: "dimension", fieldname: text };
	};

	Object.assign(AF, {
		build_summary_rows(bag, { document_scope = null, analysis_context = null, metadata = null } = {}) {
			const rows = [];
			const doc = document_scope || {};
			const ctx = analysis_context || {};

			const push_doc = (key, label, value, removable = false) => {
				if (ae_af_is_empty(value) && value !== 0) {
					return;
				}
				rows.push({
					group: "document_scope",
					key: `document.${key}`,
					label,
					value: Array.isArray(value) ? value.join(", ") : String(value),
					origin: "document_scope",
					lifetime: "session",
					removable: !!removable,
				});
			};

			push_doc("company", __("Company"), doc.company);
			push_doc("fiscal_year", __("Fiscal Year"), doc.fiscal_year);
			if (doc.from_date || doc.to_date) {
				push_doc("date_range", __("Date Range"), `${doc.from_date || "…"} → ${doc.to_date || "…"}`);
			}
			push_doc("finance_book", __("Finance Book"), doc.finance_book);
			if (cint(doc.hide_zero_rows) === 0) {
				push_doc("hide_zero_rows", __("Hide Zero Rows"), __("Off"));
			}
			const status = doc.status || {};
			if (cint(status.include_cancelled_entries)) {
				push_doc("include_cancelled", __("Include Cancelled"), __("Yes"));
			}
			if (!cint(status.include_opening_entries)) {
				push_doc("include_opening", __("Include Opening"), __("No"));
			}
			if (!cint(status.include_default_finance_book_entries)) {
				push_doc("include_default_book", __("Default Finance Book Entries"), __("No"));
			}
			if (cint(status.include_period_closing_vouchers)) {
				push_doc("include_pcv", __("Period Closing Vouchers"), __("Yes"));
			}
			if (doc.voucher?.voucher_type || doc.voucher?.voucher_no) {
				push_doc(
					"document_voucher",
					__("Document Voucher"),
					[doc.voucher.voucher_type, doc.voucher.voucher_no].filter(Boolean).join(" / ")
				);
			}
			if (doc.accounting?.account) {
				const accounts = Array.isArray(doc.accounting.account)
					? doc.accounting.account
					: [doc.accounting.account];
				push_doc(
					"document_account",
					__("Document Account"),
					accounts
						.map((acc) => this.format_account_summary_label({ value: acc }))
						.filter(Boolean)
						.join(", ")
				);
			}
			if (doc.accounting?.party) {
				push_doc(
					"document_party",
					__("Document Party"),
					[doc.accounting.party_type, doc.accounting.party].filter(Boolean).join(" / ")
				);
			}
			Object.entries(doc.accounting_dimensions || {}).forEach(([field, value]) => {
				if (ae_af_is_empty(value)) {
					return;
				}
				const dim = (metadata?.dimensions || []).find((item) => item.fieldname === field);
				push_doc(`document_dim_${field}`, dim?.label || field, Array.isArray(value) ? value.join(", ") : value);
			});

			this.list_entries(bag).forEach((entry) => {
				const label = this._label_for_entry(entry, metadata);
				const display = this._display_value(entry);
				rows.push({
					group: "analysis_filters",
					key: entry.key,
					label,
					value: display,
					origin: entry.origin,
					origin_label: entry.meta?.source_axis_label || entry.origin,
					lifetime: entry.lifetime,
					lifetime_label: this._lifetime_label(entry.lifetime),
					removable: entry.removable !== false,
					bound_to: entry.bound_to || null,
					meta: entry.meta || null,
				});
			});

			const axis_map = {
				account_level: __("Account Levels"),
				party: __("Parties"),
				unified_party: __("Unified Parties"),
				dimension: __("Dimensions"),
				currency: __("Currencies"),
				voucher: __("Vouchers"),
				item_group: __("Item Groups"),
				inventory_account: __("Inventory Account"),
				item: __("Items"),
			};
			rows.push({
				group: "presentation",
				key: "presentation.axis",
				label: __("Axis"),
				value: axis_map[ctx.view_axis] || ctx.view_axis || "",
				origin: "presentation",
				lifetime: "session",
				removable: false,
			});
			if (ctx.view_axis === "account_level" && ctx.level_sequence != null) {
				const level = (metadata?.levels || []).find(
					(item) => Number(item.sequence) === Number(ctx.level_sequence)
				);
				const level_label =
					(level && (level.title || level.title_fa || level.label)) || String(ctx.level_sequence);
				rows.push({
					group: "presentation",
					key: "presentation.level",
					label: __("Level"),
					value: level_label,
					origin: "presentation",
					lifetime: "session",
					removable: false,
				});
			}
			if (ctx.view_axis === "dimension" && ctx.dimension_scope?.dimension_type) {
				const field = ctx.dimension_scope.dimension_type;
				const dim = (metadata?.dimensions || []).find((item) => item.fieldname === field);
				rows.push({
					group: "presentation",
					key: "presentation.dimension_type",
					label: __("Dimension Type"),
					value: dim?.label || field,
					origin: "presentation",
					lifetime: "session",
					removable: false,
				});
			}
			if (ctx.detail_mode && ctx.detail_mode !== "summary") {
				rows.push({
					group: "presentation",
					key: "presentation.detail_mode",
					label: __("Detail Mode"),
					value: ctx.detail_mode,
					origin: "presentation",
					lifetime: "session",
					removable: false,
				});
			}

			return rows;
		},

		_label_for_entry(entry, metadata) {
			const parsed = ae_af_parse_key(entry.key);
			if (parsed?.kind === "dimension") {
				const dim = (metadata?.dimensions || []).find((item) => item.fieldname === parsed.fieldname);
				return dim?.label || parsed.fieldname;
			}
			if (entry.key === "account") {
				const meta = entry.meta || {};
				if (meta.is_virtual_group || meta.mode === "virtual_prefix" || cint(meta.is_group)) {
					return __("Account Group");
				}
				return __("Account");
			}
		const map = {
			party: __("Party"),
			unified_party: __("Unified Party"),
			voucher: __("Voucher"),
			currency: __("Currency"),
			item_group: __("Item Group"),
			item: __("Item"),
			warehouse: __("Warehouse"),
		};
			return map[entry.key] || entry.label || entry.key;
		},

		_lifetime_label(lifetime) {
			return { session: __("Session"), drill: __("Drill"), temporary: __("Temporary") }[lifetime] || lifetime || "—";
		}

		/**
		 * Account Filter Summary: always ``code - title`` (never title-only / code-only when both exist).
		 */,

		format_account_summary_label({ code = "", title = "", value = "" } = {}) {
			const c = String(code || "").trim();
			let t = String(title || "").trim();
			const v = String(value || "").trim();
			// DocName patterns: "1110 - Title - AET" or "1110 - Title"
			const parse_name = (raw) => {
				const parts = String(raw || "")
					.split(" - ")
					.map((p) => p.trim())
					.filter(Boolean);
				if (parts.length >= 2 && /^\d[\d.]*$/.test(parts[0])) {
					const number = parts[0];
					const name_parts = parts.length >= 3 ? parts.slice(1, -1) : parts.slice(1);
					return { code: number, title: name_parts.join(" - ") || parts[1] };
				}
				return null;
			};
			if ((!c || !t) && v) {
				const parsed = parse_name(v);
				if (parsed) {
					return `${parsed.code} - ${parsed.title}`;
				}
			}
			if (c && t && t !== c) {
				// Avoid "1110 - 1110 - Title"
				if (t.startsWith(`${c} - `)) {
					return t;
				}
				return `${c} - ${t}`;
			}
			if (c) {
				return c;
			}
			if (t) {
				const parsed = parse_name(t);
				if (parsed) {
					return `${parsed.code} - ${parsed.title}`;
				}
				return t;
			}
			return v;
		},

		_display_value(entry) {
			if (entry?.key === "account") {
				const meta = entry.meta || {};
				const value = entry.value;
				const value_str =
					value && typeof value === "object" && !Array.isArray(value)
						? value.selected_account || value.display || ""
						: Array.isArray(value)
						? value.join(", ")
						: String(value ?? "");
				// Multiple accounts → format each
				if (Array.isArray(value)) {
					return value
						.map((item) =>
							this.format_account_summary_label({
								code: meta.display_code,
								title: meta.display_title || meta.account_name,
								value: item,
							})
						)
						.filter(Boolean)
						.join(", ");
				}
				return this.format_account_summary_label({
					code: meta.display_code || meta.account_number,
					title: meta.display_title || meta.account_name,
					value: meta.display_label || value_str,
				});
			}
			if (entry?.key === "item") {
			const meta = entry.meta || {};
			const code = meta.item_code || entry.value;
			const name = meta.item_name || meta.display_title;
			if (code && name && String(name) !== String(code)) {
				return `${code} - ${name}`;
			}
			return String(code || name || entry.value || "");
		}
		if (entry.meta?.display_label) {
				return String(entry.meta.display_label);
			}
			const value = entry.value;
			if (value && typeof value === "object" && !Array.isArray(value)) {
				return (
					value.display ||
					value.selected_account ||
					value.party ||
					value.unified_party ||
					value.voucher_no ||
					value.currency ||
					value.dimension_value ||
					JSON.stringify(value)
				);
			}
			if (Array.isArray(value)) {
				return value.join(", ");
			}
			return String(value ?? "");
		}
	});
})();

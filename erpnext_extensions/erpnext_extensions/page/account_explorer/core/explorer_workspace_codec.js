frappe.provide("erpnext_extensions.account_explorer.core");

/**
 * Account Explorer workspace URL codec (Wave 3B-3 / ADR-3B-002).
 * Serialize / deserialize DocumentScope + AnalysisContext ↔ query params.
 * Grid presentation stays in User Settings (not duplicated here).
 */

(function () {
	const AE_URL_VERSION = 2;
	const AE_URL_VERSION_LEGACY = 1;
	const AE_URL_VERSION_KEY = "ae_v";
	const AE_URL_STATE_TOKEN_KEY = "ae_state";
	const AE_URL_MAX_LENGTH = 1800;
	const AE_URL_DEBOUNCE_MS = 250;
	const AE_WORKSPACE_SETTINGS_SECTION = "Workspace";
	const AE_WORKSPACE_TOKEN_LIMIT = 24;
	const AE_DEFAULT_AXIS = "account_level";
	const AE_DEFAULT_DETAIL = "summary";
	const AE_DEFAULT_CURRENCY_TYPE = "account_currency";
	const AE_DEFAULT_PAGE = 1;
	const AE_DEFAULT_SORT_ORDER = "asc";
	const AE_STATUS_DEFAULTS = {
		include_opening_entries: 1,
		include_cancelled_entries: 0,
		include_default_finance_book_entries: 1,
		include_period_closing_vouchers: 0,
	};
	const AE_PARAM_ORDER = [
		"ae_v", "ae_state", "company", "fiscal_year", "from_date", "to_date", "finance_book", "hide_zero_rows",
		"axis", "level", "detail", "account", "party_type", "party", "dimension_type", "dimension_value",
		"unified_party", "include_unmapped", "currency_type", "currency", "voucher_type", "voucher_no",
		"against_voucher_type", "against_voucher_no", "reference_no", "inv_ig", "inv_item", "inv_wh",
		"page", "sort", "order", "saved_view",
		"include_opening", "include_cancelled", "include_default_book", "include_pcv",
		"as_mode", "as_account", "as_virtual", "as_level", "dims", "af",
	];

	const core = erpnext_extensions.account_explorer.core;
	core.AE_URL_VERSION = AE_URL_VERSION;
	core.AE_URL_VERSION_LEGACY = AE_URL_VERSION_LEGACY;
	core.AE_URL_VERSION_KEY = AE_URL_VERSION_KEY;
	core.AE_URL_STATE_TOKEN_KEY = AE_URL_STATE_TOKEN_KEY;
	core.AE_URL_MAX_LENGTH = AE_URL_MAX_LENGTH;
	core.AE_URL_DEBOUNCE_MS = AE_URL_DEBOUNCE_MS;
	core.AE_WORKSPACE_SETTINGS_SECTION = AE_WORKSPACE_SETTINGS_SECTION;
	core.AE_WORKSPACE_TOKEN_LIMIT = AE_WORKSPACE_TOKEN_LIMIT;
	core.AE_PARAM_ORDER = AE_PARAM_ORDER;
	core.AE_STATUS_DEFAULTS = AE_STATUS_DEFAULTS;
	core.AE_DEFAULT_AXIS = AE_DEFAULT_AXIS;
	core.AE_DEFAULT_DETAIL = AE_DEFAULT_DETAIL;
	core.AE_DEFAULT_CURRENCY_TYPE = AE_DEFAULT_CURRENCY_TYPE;
	core.AE_DEFAULT_PAGE = AE_DEFAULT_PAGE;
	core.AE_DEFAULT_SORT_ORDER = AE_DEFAULT_SORT_ORDER;

	core.AEWorkspaceCodec = {
	clone(value) {
		return JSON.parse(JSON.stringify(value ?? null));
	},
	is_empty(value) {
		return (
			value === undefined ||
			value === null ||
			value === "" ||
			(Array.isArray(value) && !value.length) ||
			(typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length)
		);
	},
	normalize_list(value) {
		if (this.is_empty(value)) {
			return null;
		}
		if (Array.isArray(value)) {
			const items = [...new Set(value.map((item) => String(item || "").trim()).filter(Boolean))].sort();
			return items.length ? items : null;
		}
		const single = String(value).trim();
		return single ? [single] : null;
	},
	encode_list(value) {
		const list = this.normalize_list(value);
		if (!list) {
			return null;
		}
		return list.length === 1 ? list[0] : JSON.stringify(list);
	},
	decode_list(raw) {
		if (this.is_empty(raw)) {
			return null;
		}
		const text = String(raw);
		if (text.startsWith("[")) {
			try {
				return this.normalize_list(JSON.parse(text));
			} catch (_error) {
				return null;
			}
		}
		return this.normalize_list(text);
	},
	bool_flag(value, fallback = 0) {
		if (value === true || value === 1 || value === "1") {
			return 1;
		}
		if (value === false || value === 0 || value === "0") {
			return 0;
		}
		return fallback ? 1 : 0;
	},
	canonical_date(value) {
		if (!value) {
			return null;
		}
		const text = String(value).trim();
		if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
			return text;
		}
		if (typeof frappe !== "undefined" && frappe.datetime?.user_to_str) {
			try {
				const converted = frappe.datetime.user_to_str(text);
				if (converted && /^\d{4}-\d{2}-\d{2}$/.test(converted)) {
					return converted;
				}
			} catch (_error) {
				/* keep raw */
			}
		}
		return text;
	},
	enabled_axes(metadata = {}) {
		return new Set(
			(metadata.axes || []).filter((axis) => axis?.enabled || axis?.enabled === undefined).map((axis) => axis.id)
		);
	},
	known_dimension_fields(metadata = {}) {
		return new Set((metadata.dimensions || []).map((row) => row.fieldname).filter(Boolean));
	},
	sort_allowed(metadata = {}, view_axis = AE_DEFAULT_AXIS) {
		const columns =
			(view_axis === "party" && metadata.party_columns) ||
			(view_axis === "unified_party" && metadata.unified_party_columns) ||
			(view_axis === "dimension" && metadata.dimension_columns) ||
			(view_axis === "currency" && metadata.currency_columns) ||
			(view_axis === "voucher" && metadata.voucher_columns) ||
			(view_axis === "item_group" && metadata.item_group_columns) ||
			(view_axis === "item" && metadata.item_columns) ||
			(view_axis === "inventory_account" && metadata.inventory_account_columns) ||
			metadata.columns ||
			[];
		return new Set(columns.map((col) => col.fieldname || col.id).filter(Boolean));
	},
	empty_workspace(metadata = {}) {
		return {
			schema_version: AE_URL_VERSION,
			document_scope: {
				company: null,
				fiscal_year: null,
				from_date: null,
				to_date: null,
				finance_book: null,
				hide_zero_rows: 1,
				voucher: {
					voucher_type: null,
					voucher_no: null,
					against_voucher_type: null,
					against_voucher_no: null,
					reference_no: null,
				},
				accounting: { account: null, party_type: null, party: null },
				accounting_dimensions: {},
				currency: { currency_type: AE_DEFAULT_CURRENCY_TYPE, currency: null },
				status: { ...AE_STATUS_DEFAULTS },
				inventory: { item_group: null, item: null, warehouse: null },
			},
			analysis_context: {
				view_axis: AE_DEFAULT_AXIS,
				level_sequence: metadata.default_level_sequence || 1,
				detail_mode: AE_DEFAULT_DETAIL,
				page: AE_DEFAULT_PAGE,
				sort_field: null,
				sort_order: AE_DEFAULT_SORT_ORDER,
				account_scope: {},
				party_scope: {},
				unified_party_scope: {},
				dimension_scope: {},
				voucher_scope: {},
			},
			analysis_filters: erpnext_extensions.account_explorer.core.AnalysisFilters
				? erpnext_extensions.account_explorer.core.AnalysisFilters.empty()
				: { dimensions: {} },
			saved_view: null,
		};
	},
	capture_from_controller(controller) {
		if (!controller) {
			return null;
		}
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		const filters = AF.normalize_bag(
			controller.analysis_filters || controller.store?.get_active_analysis_filters?.() || AF.empty()
		);
		return {
			schema_version: AE_URL_VERSION,
			document_scope: this.clone(controller.document_scope),
			analysis_filters: this.clone(filters),
			analysis_context: {
				view_axis: controller.analysis_context?.view_axis || AE_DEFAULT_AXIS,
				level_sequence: controller.analysis_context?.level_sequence || null,
				detail_mode: controller.analysis_context?.detail_mode || AE_DEFAULT_DETAIL,
				page: cint(controller.analysis_context?.page) || AE_DEFAULT_PAGE,
				sort_field: controller.analysis_context?.sort_field || null,
				sort_order: controller.analysis_context?.sort_order || AE_DEFAULT_SORT_ORDER,
				account_scope: this.clone(controller.analysis_context?.account_scope || {}),
				party_scope: this.clone(controller.analysis_context?.party_scope || {}),
				unified_party_scope: this.clone(controller.analysis_context?.unified_party_scope || {}),
				dimension_scope: this.clone(controller.analysis_context?.dimension_scope || {}),
				voucher_scope: this.clone(controller.analysis_context?.voucher_scope || {}),
			},
			saved_view: controller.active_saved_view?.name || null,
		};
	},
	workspace_to_params(workspace, metadata = {}) {
		const params = new URLSearchParams();
		const put = (key, value) => {
			if (this.is_empty(value)) {
				return;
			}
			params.set(key, String(value));
		};
		put(AE_URL_VERSION_KEY, String(AE_URL_VERSION));
		const scope = workspace.document_scope || {};
		const analysis = workspace.analysis_context || {};
		const status = scope.status || {};
		const accounting = scope.accounting || {};
		const voucher = scope.voucher || {};
		const currency = scope.currency || {};
		const account_scope = analysis.account_scope || {};
		const party_scope = analysis.party_scope || {};
		const unified = analysis.unified_party_scope || {};
		const dimension_scope = analysis.dimension_scope || {};
		const voucher_scope = analysis.voucher_scope || {};
		put("company", scope.company);
		put("fiscal_year", scope.fiscal_year);
		put("from_date", this.canonical_date(scope.from_date));
		put("to_date", this.canonical_date(scope.to_date));
		put("finance_book", scope.finance_book);
		if (cint(scope.hide_zero_rows) === 0) {
			put("hide_zero_rows", "0");
		}
		const axis = analysis.view_axis || AE_DEFAULT_AXIS;
		if (axis !== AE_DEFAULT_AXIS) {
			put("axis", axis);
		}
		if (axis === "account_level" && analysis.level_sequence) {
			put("level", analysis.level_sequence);
		}
		if (analysis.detail_mode && analysis.detail_mode !== AE_DEFAULT_DETAIL) {
			put("detail", analysis.detail_mode);
		}
		put("account", this.encode_list(accounting.account));
		put("party_type", accounting.party_type || party_scope.party_type);
		put("party", this.encode_list(accounting.party || party_scope.selected_party));
		put("dimension_type", dimension_scope.dimension_type);
		put("dimension_value", dimension_scope.selected_dimension_value);
		put("unified_party", unified.selected_unified_party);
		if (cint(unified.include_unmapped)) {
			put("include_unmapped", "1");
		}
		if (currency.currency_type && currency.currency_type !== AE_DEFAULT_CURRENCY_TYPE) {
			put("currency_type", currency.currency_type);
		}
		put("currency", currency.currency);
		put("voucher_type", voucher.voucher_type || voucher_scope.voucher_type);
		put("voucher_no", voucher.voucher_no || voucher_scope.voucher_no);
		put("against_voucher_type", voucher.against_voucher_type);
		put("against_voucher_no", voucher.against_voucher_no);
		put("reference_no", voucher.reference_no);
		const inventory = scope.inventory || {};
		put("inv_ig", this.encode_list(inventory.item_group));
		put("inv_item", this.encode_list(inventory.item));
		put("inv_wh", this.encode_list(inventory.warehouse));
		if (cint(analysis.page) > AE_DEFAULT_PAGE) {
			put("page", analysis.page);
		}
		put("sort", analysis.sort_field);
		if (analysis.sort_order && analysis.sort_order !== AE_DEFAULT_SORT_ORDER) {
			put("order", analysis.sort_order);
		}
		put("saved_view", workspace.saved_view);
		Object.entries(AE_STATUS_DEFAULTS).forEach(([key, fallback]) => {
			const short =
				key === "include_opening_entries"
					? "include_opening"
					: key === "include_cancelled_entries"
						? "include_cancelled"
						: key === "include_default_finance_book_entries"
							? "include_default_book"
							: "include_pcv";
			if (this.bool_flag(status[key], fallback) !== fallback) {
				put(short, String(this.bool_flag(status[key], fallback)));
			}
		});
		if (account_scope.mode && account_scope.mode !== "level") {
			put("as_mode", account_scope.mode);
		}
		put("as_account", account_scope.selected_account);
		if (account_scope.is_virtual_group) {
			put("as_virtual", "1");
		}
		if (account_scope.level_sequence && account_scope.level_sequence !== analysis.level_sequence) {
			put("as_level", account_scope.level_sequence);
		}
		const known_dims = this.known_dimension_fields(metadata);
		const dims = {};
		Object.entries(scope.accounting_dimensions || {}).forEach(([fieldname, value]) => {
			if (!known_dims.size || known_dims.has(fieldname)) {
				const encoded = this.encode_list(value);
				if (encoded) {
					dims[fieldname] = encoded;
				}
			}
		});
		if (Object.keys(dims).length) {
			put("dims", JSON.stringify(dims));
		}
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		const serialized = AF.serialize(workspace.analysis_filters);
		if (serialized) {
			put("af", JSON.stringify(serialized));
		}
		return this.ordered_params(params);
	},
	ordered_params(params) {
		const ordered = new URLSearchParams();
		AE_PARAM_ORDER.forEach((key) => {
			if (params.has(key)) {
				ordered.set(key, params.get(key));
			}
		});
		[...params.keys()]
			.filter((key) => !AE_PARAM_ORDER.includes(key))
			.sort()
			.forEach((key) => ordered.set(key, params.get(key)));
		return ordered;
	},
	params_to_workspace(params, metadata = {}) {
		const warnings = [];
		const get = (key) => (params.has(key) ? params.get(key) : null);
		const workspace = this.empty_workspace(metadata);
		const version = cint(get(AE_URL_VERSION_KEY)) || AE_URL_VERSION;
		if (get(AE_URL_VERSION_KEY) && version && version !== AE_URL_VERSION && version !== AE_URL_VERSION_LEGACY) {
			warnings.push(__("Unsupported workspace URL version was ignored."));
			return { workspace: null, warnings };
		}
		workspace.schema_version = version === AE_URL_VERSION_LEGACY ? AE_URL_VERSION_LEGACY : AE_URL_VERSION;
		workspace.analysis_filters = erpnext_extensions.account_explorer.core.AnalysisFilters.empty();
		workspace.document_scope.company = get("company");
		workspace.document_scope.fiscal_year = get("fiscal_year");
		workspace.document_scope.from_date = this.canonical_date(get("from_date"));
		workspace.document_scope.to_date = this.canonical_date(get("to_date"));
		workspace.document_scope.finance_book = get("finance_book");
		if (get("hide_zero_rows") !== null) {
			workspace.document_scope.hide_zero_rows = this.bool_flag(get("hide_zero_rows"), 1);
		}
		const axis = get("axis") || AE_DEFAULT_AXIS;
		const enabled = this.enabled_axes(metadata);
		if (enabled.size && !enabled.has(axis)) {
			warnings.push(__("Axis {0} is not available and was discarded.", [axis]));
		} else {
			workspace.analysis_context.view_axis = axis;
		}
		if (get("level")) {
			workspace.analysis_context.level_sequence = cint(get("level")) || workspace.analysis_context.level_sequence;
		}
		if (get("detail") === "grouped_gl" || get("detail") === "summary") {
			workspace.analysis_context.detail_mode = get("detail");
		}
		workspace.document_scope.accounting.account = this.decode_list(get("account"));
		workspace.document_scope.accounting.party_type = get("party_type");
		workspace.document_scope.accounting.party = this.decode_list(get("party"));
		workspace.analysis_context.party_scope = {
			party_type: get("party_type"),
			selected_party: this.decode_list(get("party"))?.[0] || null,
		};
		const known_dims = this.known_dimension_fields(metadata);
		const dimension_type = get("dimension_type");
		if (dimension_type && known_dims.size && !known_dims.has(dimension_type)) {
			warnings.push(__("Dimension {0} is not available and was discarded.", [dimension_type]));
		} else if (dimension_type) {
			workspace.analysis_context.dimension_scope.dimension_type = dimension_type;
			workspace.analysis_context.dimension_scope.selected_dimension_value = get("dimension_value");
		}
		workspace.analysis_context.unified_party_scope = {
			selected_unified_party: get("unified_party"),
			include_unmapped: this.bool_flag(get("include_unmapped"), 0),
		};
		workspace.document_scope.currency.currency_type = get("currency_type") || AE_DEFAULT_CURRENCY_TYPE;
		workspace.document_scope.currency.currency = get("currency");
		workspace.document_scope.voucher = {
			voucher_type: get("voucher_type"),
			voucher_no: get("voucher_no"),
			against_voucher_type: get("against_voucher_type"),
			against_voucher_no: get("against_voucher_no"),
			reference_no: get("reference_no"),
		};
		workspace.analysis_context.voucher_scope = {
			voucher_type: get("voucher_type"),
			voucher_no: get("voucher_no"),
		};
		workspace.document_scope.inventory = {
			item_group: this.decode_list(get("inv_ig")),
			item: this.decode_list(get("inv_item")),
			warehouse: this.decode_list(get("inv_wh")),
		};
		workspace.analysis_context.page = Math.max(1, cint(get("page")) || AE_DEFAULT_PAGE);
		const sort_field = get("sort");
		const allowed_sort = this.sort_allowed(metadata, workspace.analysis_context.view_axis);
		if (sort_field && allowed_sort.size && !allowed_sort.has(sort_field)) {
			warnings.push(__("Sort field {0} is not allowed and was discarded.", [sort_field]));
		} else if (sort_field) {
			workspace.analysis_context.sort_field = sort_field;
		}
		workspace.analysis_context.sort_order = get("order") === "desc" ? "desc" : AE_DEFAULT_SORT_ORDER;
		workspace.saved_view = get("saved_view");
		workspace.document_scope.status = {
			include_opening_entries: this.bool_flag(
				get("include_opening"),
				AE_STATUS_DEFAULTS.include_opening_entries
			),
			include_cancelled_entries: this.bool_flag(
				get("include_cancelled"),
				AE_STATUS_DEFAULTS.include_cancelled_entries
			),
			include_default_finance_book_entries: this.bool_flag(
				get("include_default_book"),
				AE_STATUS_DEFAULTS.include_default_finance_book_entries
			),
			include_period_closing_vouchers: this.bool_flag(
				get("include_pcv"),
				AE_STATUS_DEFAULTS.include_period_closing_vouchers
			),
		};
		workspace.analysis_context.account_scope = {
			mode: get("as_mode") || null,
			selected_account: get("as_account"),
			is_virtual_group: this.bool_flag(get("as_virtual"), 0),
			level_sequence: cint(get("as_level")) || null,
		};
		if (get("dims")) {
			try {
				const parsed = JSON.parse(get("dims"));
				Object.entries(parsed || {}).forEach(([fieldname, value]) => {
					if (known_dims.size && !known_dims.has(fieldname)) {
						warnings.push(__("Dimension filter {0} is not available and was discarded.", [fieldname]));
						return;
					}
					const decoded = this.decode_list(value);
					if (decoded) {
						workspace.document_scope.accounting_dimensions[fieldname] = decoded;
					}
				});
			} catch (_error) {
				warnings.push(__("Invalid dimension filters in the URL were ignored."));
			}
		}
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		if (get("af")) {
			try {
				workspace.analysis_filters = AF.deserialize(get("af"), "url_hydrate");
			} catch (_error) {
				warnings.push(__("Invalid analysis filters in the URL were ignored."));
				workspace.analysis_filters = AF.empty();
			}
		} else {
			workspace.analysis_filters = AF.hydrate_from_legacy_scopes(
				AF.empty(),
				workspace.analysis_context,
				workspace.document_scope,
				"legacy_url"
			);
			if (get("currency")) {
				workspace.analysis_filters = AF.set_entry(
					workspace.analysis_filters,
					{
						key: "currency",
						value: get("currency"),
						origin: "legacy_url",
						lifetime: "session",
						meta: { currency_type: get("currency_type") || AE_DEFAULT_CURRENCY_TYPE },
					},
					{ key: "currency", origin: "legacy_url", lifetime: "session" }
				);
			}
		}
		return { workspace, warnings };
	},
	validate_workspace(workspace, metadata = {}) {
		const warnings = [];
		if (!workspace) {
			return { workspace: null, warnings: [__("Workspace URL state was empty.")] };
		}
		const next = this.clone(workspace);
		const companies = new Set((metadata.companies || []).map((row) => row.name || row).filter(Boolean));
		if (next.document_scope?.company && companies.size && !companies.has(next.document_scope.company)) {
			// Soft check — server remains authoritative. Keep company if list absent.
			if (metadata.companies) {
				warnings.push(__("Company {0} is not available and was discarded.", [next.document_scope.company]));
				next.document_scope.company = null;
			}
		}
		const max_page = cint(metadata.max_page_size) || 200;
		if (cint(next.analysis_context?.page) > 5000) {
			warnings.push(__("Page number was out of range and was reset."));
			next.analysis_context.page = AE_DEFAULT_PAGE;
		}
		if (next.analysis_context && !next.analysis_context.sort_order) {
			next.analysis_context.sort_order = AE_DEFAULT_SORT_ORDER;
		}
		if (cint(next.analysis_context?.page_size) > max_page) {
			next.analysis_context.page_size = max_page;
		}
		return { workspace: next, warnings };
	},
	build_url_search(workspace, metadata = {}) {
		const params = this.workspace_to_params(workspace, metadata);
		const query = params.toString();
		return query ? `?${query}` : "";
	},
	signature_for(workspace, metadata = {}) {
		return this.build_url_search(workspace, metadata);
	},
};
})();

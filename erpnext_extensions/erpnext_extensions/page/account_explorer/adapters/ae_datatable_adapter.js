frappe.provide("erpnext_extensions.account_explorer.adapters");

const AE_DT_WIDTH_PROFILES = {
	code: { min: 90, preferred: 120, max: 180 },
	title: { min: 160, preferred: 220, max: 420 },
	amount: { min: 180, preferred: 180, max: 280 },
	voucher_no: { min: 160, preferred: 220, max: 320 },
	remarks: { min: 180, preferred: 260, max: 500 },
	date: { min: 100, preferred: 110, max: 140 },
	default: { min: 90, preferred: 120, max: 300 },
};

const AE_DT_DENSITY_HEIGHTS = {
	compact: 28,
	comfortable: 36,
};

const AE_DT_CLUSTERIZE_ROW_THRESHOLD = 50;
const AE_DT_RESIZE_DEBOUNCE_MS = 150;

const AE_DT_MULTILINE_COLUMN_IDS = new Set(["remarks", "reference", "voucher_title", "identifier_summary"]);

let AE_DT_ACTIVE_MOUNT_COUNT = 0;
let AE_DT_ACTIVE_RESIZE_OBSERVERS = 0;
let AE_DT_LIFECYCLE_MOUNT_COUNT = 0;
let AE_DT_LIFECYCLE_UPDATE_COUNT = 0;
let AE_DT_LIFECYCLE_REFRESH_COUNT = 0;
const AE_DT_TRACKED_HOSTS = new Set();

function ae_dt_track_host(host) {
	if (host) {
		AE_DT_TRACKED_HOSTS.add(host);
	}
}

function ae_dt_untrack_host(host) {
	if (host) {
		AE_DT_TRACKED_HOSTS.delete(host);
	}
}

function ae_dt_count_detached_hosts() {
	let count = 0;
	AE_DT_TRACKED_HOSTS.forEach((host) => {
		if (host && !host.isConnected) {
			count += 1;
		}
	});
	return count;
}

/**
 * Single integration point for Frappe DataTable (ADR-3B-001 / Wave 3B-1).
 *
 * Public API:
 * - mount(container, columns, rows, options)
 * - update(columns, rows, options)
 * - destroy()
 * - is_mounted()
 * - get_checked_rows()
 * - clear_selection()
 * - get_column_state()
 * - apply_column_state(state)
 * - set_loading(is_loading)
 * - show_empty_state(message)
 * - copy_cell_value(row, column_id)
 * - copy_row_tsv(row)
 * - copy_checked_rows_tsv()
 * - set_density(mode)
 * - set_active_row_index(index)
 * - get_active_row_index()
 */
erpnext_extensions.account_explorer.adapters.AEDataTableAdapter = class AEDataTableAdapter {
	static get_active_mount_count() {
		return AE_DT_ACTIVE_MOUNT_COUNT;
	}

	static get_active_resize_observer_count() {
		return AE_DT_ACTIVE_RESIZE_OBSERVERS;
	}

	static get_detached_host_count() {
		return ae_dt_count_detached_hosts();
	}

	static reset_lifecycle_counters() {
		AE_DT_LIFECYCLE_MOUNT_COUNT = 0;
		AE_DT_LIFECYCLE_UPDATE_COUNT = 0;
		AE_DT_LIFECYCLE_REFRESH_COUNT = 0;
	}

	static get_lifecycle_counters() {
		return {
			mount_count: AE_DT_LIFECYCLE_MOUNT_COUNT,
			update_count: AE_DT_LIFECYCLE_UPDATE_COUNT,
			refresh_count: AE_DT_LIFECYCLE_REFRESH_COUNT,
		};
	}

	constructor(events) {
		this.events = events;
		this._container = null;
		this._host = null;
		this._datatable = null;
		this._options = {};
		this._mounted = false;
		this._source_rows = [];
		this._rows_by_key = new Map();
		this._column_defs = [];
		this._loading = false;
		this._mount_generation = 0;
		this._active_row_index = null;
		this._density = "comfortable";
		this._skeleton_el = null;
		this._resize_observer = null;
		this._resize_debounce_timer = null;
		this._last_refresh_signature = null;
		this._perf_stats = {};
		this._resize_listener_bound = false;
	}

	is_mounted() {
		return this._mounted;
	}

	get_container() {
		return this._container;
	}

	should_clusterize(row_count, options = {}) {
		if (options.clusterize !== undefined) {
			return !!options.clusterize;
		}
		return Number(row_count || 0) > AE_DT_CLUSTERIZE_ROW_THRESHOLD;
	}

	_build_refresh_signature(columns, rows) {
		const column_part = (columns || [])
			.map((col) => `${col.id}:${col.width}:${col.sortOrder || "none"}`)
			.join("|");
		const row_count = (rows || []).length;
		const first_key = rows?.[0]?.row_key || "";
		const last_key = rows?.[row_count - 1]?.row_key || "";
		return `${column_part}#${row_count}:${first_key}:${last_key}`;
	}

	get_perf_stats() {
		return { ...this._perf_stats };
	}

	_record_perf_stat(operation, started_at, meta = {}) {
		const elapsed_ms = Math.round(performance.now() - started_at);
		this._perf_stats[operation] = {
			elapsed_ms,
			...meta,
		};
		return elapsed_ms;
	}

	is_available() {
		return typeof window.DataTable === "function";
	}

	async ensure_datatable() {
		if (this.is_available()) {
			return window.DataTable;
		}
		if (frappe.DataTable) {
			window.DataTable = frappe.DataTable;
			return window.DataTable;
		}
		return new Promise((resolve, reject) => {
			frappe.require(
				[
					"/assets/frappe/node_modules/frappe-datatable/dist/frappe-datatable.css",
					"/assets/frappe/node_modules/frappe-datatable/dist/frappe-datatable.js",
				],
				() => {
					if (typeof DataTable !== "undefined") {
						window.DataTable = DataTable;
						frappe.DataTable = DataTable;
						resolve(window.DataTable);
						return;
					}
					reject(new Error("Frappe DataTable failed to load"));
				}
			);
		});
	}

	resolve_column_width_profile(col) {
		if (!col) {
			return AE_DT_WIDTH_PROFILES.default;
		}
		if (col.fieldtype === "Currency" || col.fieldtype === "Int" || col.fieldtype === "Float") {
			return AE_DT_WIDTH_PROFILES.amount;
		}
		const column_id = String(col.id || "");
		if (column_id === "voucher_no") {
			return AE_DT_WIDTH_PROFILES.voucher_no;
		}
		if (column_id === "posting_date") {
			return AE_DT_WIDTH_PROFILES.date;
		}
		if (column_id === "remarks" || column_id === "reference") {
			return AE_DT_WIDTH_PROFILES.remarks;
		}
		if (
			column_id === "display_title" ||
			column_id === "party_name" ||
			column_id === "account_name" ||
			column_id === "voucher_title" ||
			column_id === "primary_member_label" ||
			column_id.startsWith("dim:")
		) {
			return AE_DT_WIDTH_PROFILES.title;
		}
		if (
			column_id === "display_code" ||
			column_id === "party_type" ||
			column_id === "currency" ||
			column_id === "member_count" ||
			column_id === "voucher_type" ||
			column_id === "account"
		) {
			return AE_DT_WIDTH_PROFILES.code;
		}
		return AE_DT_WIDTH_PROFILES.default;
	}

	resolve_column_width(col, options = {}) {
		const profile = this.resolve_column_width_profile(col);
		const persisted = options.column_widths?.[col.id];
		const requested = parseInt(persisted ?? col?.width, 10);
		const base = Number.isFinite(requested) && requested > 0 ? requested : profile.preferred;
		return Math.max(profile.min, Math.min(profile.max, base));
	}

	is_column_sortable(col, options = {}) {
		if (!col?.id || String(col.id).startsWith("__")) {
			return false;
		}
		if (col.sortable === false || options.sortable === false) {
			return false;
		}
		if (col.column_kind === "dimension" || col.column_kind === "dimensions_compact") {
			return false;
		}
		if (String(col.id).startsWith("dim:")) {
			return false;
		}
		if (options.non_sortable_column_ids?.includes(col.id)) {
			return false;
		}
		return true;
	}

	map_columns(ae_columns, options = {}) {
		const sort_field = options.sort_field;
		const sort_order = options.sort_order || "asc";
		const columns = (ae_columns || []).map((col, index) => {
			const sortable = this.is_column_sortable(col, options);
			const mapped = {
				id: col.id,
				name: options.translate ? options.translate(col.label) : col.label,
				width: this.resolve_column_width(col, options),
				editable: false,
				focusable: false,
				sortOrder: sortable && sort_field === col.id ? sort_order : "none",
				format: (value, row, column) => this._format_cell(value, row, column, col, options),
			};
			const classes = [];
			if (index === 0) {
				classes.push("ae-dt-first-col");
			}
			if (col.fieldtype === "Currency") {
				classes.push("ae-dt-amount-col");
			}
			if (!sortable) {
				classes.push("ae-dt-col--nosort");
			}
			if (AE_DT_MULTILINE_COLUMN_IDS.has(col.id)) {
				classes.push("ae-dt-col--multiline");
			} else if (col.fieldtype !== "Currency") {
				classes.push("ae-dt-col--ellipsis");
			}
			if (classes.length) {
				mapped.column_class = classes.join(" ");
			}
			return mapped;
		});
		if (options.actions_column) {
			columns.push({
				id: "__ae_actions",
				name: options.translate ? options.translate(__("Actions")) : __("Actions"),
				width: options.actions_column.width || 220,
				editable: false,
				focusable: false,
				sortOrder: "none",
				column_class: "ae-dt-col--nosort ae-dt-actions-col",
				format: (value, row) => options.render_actions_html?.(this._resolve_source_row(row)) || "",
			});
		}
		return columns;
	}

	map_rows(source_rows, column_defs, options = {}) {
		this._source_rows = source_rows || [];
		this._rows_by_key = new Map();
		const ids = column_defs.map((col) => col.id);
		return this._source_rows.map((row) => {
			if (row?.row_key) {
				this._rows_by_key.set(row.row_key, row);
			}
			const mapped = { row_key: row?.row_key || null };
			ids.forEach((id) => {
				mapped[id] = row[id] ?? "";
			});
			if (options.actions_column) {
				mapped.__ae_actions = "";
			}
			return mapped;
		});
	}

	cancel_pending_mount() {
		this._mount_generation += 1;
	}

	_is_stale_mount(generation) {
		return generation !== this._mount_generation;
	}

	async mount(container, columns = [], rows = [], options = {}) {
		const perf_started_at = performance.now();
		this.cancel_pending_mount();
		const generation = this._mount_generation;
		const DataTable = await this.ensure_datatable();
		if (this._is_stale_mount(generation)) {
			return null;
		}
		this._teardown_instance();
		this._container = container;
		this._options = options || {};
		this._density = options.density || this._density || "comfortable";
		this._column_defs = columns || [];
		const dt_columns = this.map_columns(this._column_defs, options);
		const dt_rows = this.map_rows(rows, dt_columns, options);
		if (this._is_stale_mount(generation)) {
			return null;
		}
		this._host = document.createElement("div");
		this._host.className = "ae-datatable-host";
		ae_dt_track_host(this._host);
		this._host.classList.toggle("ae-datatable-host--compact", this._density === "compact");
		this._host.classList.toggle("ae-datatable-host--comfortable", this._density !== "compact");
		container.innerHTML = "";
		container.appendChild(this._host);
		this._ensure_skeleton();
		const clusterize = this.should_clusterize(rows.length, options);
		this._datatable = new DataTable(
			this._host,
			this._build_options(dt_columns, dt_rows, { ...options, clusterize })
		);
		if (this._is_stale_mount(generation)) {
			this._teardown_instance();
			return null;
		}
		this._mounted = true;
		AE_DT_ACTIVE_MOUNT_COUNT += 1;
		AE_DT_LIFECYCLE_MOUNT_COUNT += 1;
		this._last_refresh_signature = this._build_refresh_signature(dt_columns, dt_rows);
		this._bind_resize_observer();
		this._bind_column_resize_listener();
		this._apply_sticky_first_column();
		this._sync_loading_state();
		await this._finalize_mount(generation);
		if (this._is_stale_mount(generation)) {
			this._teardown_instance();
			return null;
		}
		this._sync_active_row_dom();
		const elapsed_ms = this._record_perf_stat("mount", perf_started_at, {
			row_count: rows.length,
			clusterize,
		});
		this.events?.emit("grid:mounted", {
			columns: dt_columns,
			row_count: rows.length,
			elapsed_ms,
			clusterize,
		});
		return this._datatable;
	}

	async update(columns = [], rows = [], options = {}) {
		const perf_started_at = performance.now();
		AE_DT_LIFECYCLE_UPDATE_COUNT += 1;
		if (!this._mounted || !this._datatable || !this._container?.isConnected) {
			return this.mount(this._container, columns, rows, options);
		}
		const generation = this._mount_generation;
		this._options = { ...this._options, ...(options || {}) };
		if (options.density) {
			this._density = options.density;
		}
		this._column_defs = columns || [];
		const dt_columns = this.map_columns(this._column_defs, this._options);
		const dt_rows = this.map_rows(rows, dt_columns, this._options);
		if (this._is_stale_mount(generation)) {
			return null;
		}
		const clusterize = this.should_clusterize(rows.length, this._options);
		if (this._datatable.options) {
			this._datatable.options.cellHeight = this._resolve_cell_height();
			this._datatable.options.clusterize = clusterize;
		}
		this._host?.classList.toggle("ae-datatable-host--compact", this._density === "compact");
		this._host?.classList.toggle("ae-datatable-host--comfortable", this._density !== "compact");
		const next_signature = this._build_refresh_signature(dt_columns, dt_rows);
		const skipped_refresh = this._last_refresh_signature === next_signature;
		if (!skipped_refresh) {
			this._datatable.refresh(dt_rows, dt_columns);
			AE_DT_LIFECYCLE_REFRESH_COUNT += 1;
			this._last_refresh_signature = next_signature;
		}
		if (this._is_stale_mount(generation)) {
			return null;
		}
		this._apply_sticky_first_column();
		this._sync_loading_state();
		await this._finalize_mount(generation);
		if (this._is_stale_mount(generation)) {
			return null;
		}
		this._sync_active_row_dom();
		const elapsed_ms = this._record_perf_stat("update", perf_started_at, {
			row_count: rows.length,
			clusterize,
			skipped_refresh,
		});
		this.events?.emit("grid:updated", {
			columns: dt_columns,
			row_count: rows.length,
			elapsed_ms,
			clusterize,
		});
		return this._datatable;
	}

	async _await_frames(frame_count = 2) {
		for (let index = 0; index < frame_count; index += 1) {
			await new Promise((resolve) => requestAnimationFrame(resolve));
		}
	}

	async _finalize_mount(generation) {
		await this._await_frames(2);
		if (this._is_stale_mount(generation)) {
			return false;
		}
		this._sync_row_dom_keys();
		await this._await_frames(1);
		if (this._is_stale_mount(generation)) {
			return false;
		}
		this._sync_row_dom_keys();
		return this.is_interaction_ready();
	}

	is_interaction_ready() {
		if (!this._mounted || !this._host) {
			return false;
		}
		if (this._loading) {
			return false;
		}
		const rows = this._host.querySelectorAll(".dt-row:not(.dt-row-header):not(.dt-row-filter)");
		if (!rows.length) {
			return !this._source_rows.length;
		}
		return [...rows].every((row) => row.getAttribute("data-ae-row-key"));
	}

	_resolve_cell_height() {
		return AE_DT_DENSITY_HEIGHTS[this._density] || AE_DT_DENSITY_HEIGHTS.comfortable;
	}

	_build_options(columns, rows, options) {
		const adapter = this;
		return {
			columns,
			data: rows,
			language: frappe.boot?.lang,
			translations: frappe.utils.datatable?.get_translations?.(),
			checkboxColumn: options.checkbox_column ?? true,
			serialNoColumn: false,
			inlineFilters: options.inline_filters ?? true,
			layout: "fixed",
			cellHeight: options.cell_height ?? this._resolve_cell_height(),
			clusterize: options.clusterize ?? this.should_clusterize(rows.length, options),
			disableReorderColumn: false,
			direction: frappe.utils.is_rtl() ? "rtl" : "ltr",
			noDataMessage: options.empty_message || __("No rows in the current result"),
			events: {
				onCheckRow: () => adapter._emit_selection_change(),
				onSortColumn: (column) => {
					if (!column?.id || column.id.startsWith("__")) {
						return;
					}
					if (column.sortOrder === "none") {
						return;
					}
					options.on_server_sort?.(column.id, column);
				},
				onRemoveColumn: (column) => {
					options.on_column_removed?.(column);
					adapter.events?.emit("grid:column_state_changed", {
						columns: adapter.get_column_state(),
					});
				},
				onSwitchColumn: (column1, column2) => {
					options.on_column_switched?.(column1, column2);
					adapter.events?.emit("grid:column_state_changed", {
						columns: adapter.get_column_state(),
					});
				},
			},
		};
	}

	_format_text_cell(display, options = {}) {
		const text = display ?? "";
		const escaped = frappe.utils.escape_html(String(text));
		const full = frappe.utils.escape_html(String(text));
		const multiline = options.multiline;
		const class_name = multiline ? "ae-dt-cell-text ae-dt-cell-text--multiline" : "ae-dt-cell-text";
		return `<span class="${class_name}" title="${full}" aria-label="${full}">${escaped}</span>`;
	}

	_format_cell(value, row, column, source_col, options) {
		const source_row = this._resolve_source_row(row);
		if (column.id === "__ae_actions") {
			return options.render_actions_html?.(source_row) || "";
		}
		if (source_col?.fieldtype === "Currency") {
			const formatted = options.format_amount?.(value, source_row, source_col) || {
				compact: value ?? "",
				full: value ?? "",
			};
			const compact = frappe.utils.escape_html(String(formatted.compact ?? ""));
			const full = frappe.utils.escape_html(String(formatted.full ?? compact));
			return `<span class="ae-amount-compact ae-dt-amount-cell" title="${full}" aria-label="${full}">${compact}</span>`;
		}
		let display = value ?? "";
		if (source_col?.fieldtype === "Date") {
			display = format_ae_date(value);
		}
		const col_index = this._column_defs.findIndex((col) => col.id === source_col.id);
		const drillable =
			col_index === 0 &&
			source_row &&
			source_row.drill_down_enabled !== 0 &&
			source_row.drill_down_enabled !== false;
		if (drillable) {
			const label = frappe.utils.escape_html(String(display));
			const full = frappe.utils.escape_html(String(display));
			return `<span class="ae-drill-cell" title="${full}" aria-label="${full}"><span class="ae-drill-icon" aria-hidden="true">›</span><span class="ae-drill-label ae-dt-cell-text">${label}</span></span>`;
		}
		return this._format_text_cell(display, {
			multiline: AE_DT_MULTILINE_COLUMN_IDS.has(source_col.id),
		});
	}

	_resolve_source_row(row) {
		if (!row) {
			return null;
		}
		if (row.row_key && this._rows_by_key.has(row.row_key)) {
			return this._rows_by_key.get(row.row_key);
		}
		const row_index = row.meta?.rowIndex;
		if (row_index !== undefined && this._source_rows[row_index]) {
			return this._source_rows[row_index];
		}
		if (row.row_key) {
			return this._source_rows.find((item) => item.row_key === row.row_key) || null;
		}
		return null;
	}

	resolve_source_row_by_key(row_key) {
		if (!row_key) {
			return null;
		}
		return this._rows_by_key.get(row_key) || this._source_rows.find((item) => item.row_key === row_key) || null;
	}

	is_interactive_grid_target(target) {
		if (!target?.closest) {
			return true;
		}
		const element = target.nodeType === Node.ELEMENT_NODE ? target : target.parentElement;
		if (!element) {
			return true;
		}
		return !!element.closest(
			[
				"button",
				"a[href]",
				"input",
				"select",
				"textarea",
				"label",
				'[type="checkbox"]',
				".dt-cell__checkbox",
				".ae-voucher-action",
				".ae-grid-toolbar",
				".ae-column-chooser",
				".dt-row-filter",
				".dt-row-header",
				".dt-cell__resize-handle",
				".dt-cell--dragging",
				".dt-dropdown",
				".dt-cell__edit",
				".dt-scrollable__cursor",
			].join(", ")
		);
	}

	resolve_row_from_event(event) {
		const row_element = event?.target?.closest?.(
			".dt-row:not(.dt-row-header):not(.dt-row-filter)"
		);
		if (!row_element || !this._host?.contains(row_element)) {
			return null;
		}
		const row_key = row_element.getAttribute("data-ae-row-key");
		if (row_key) {
			return this.resolve_source_row_by_key(row_key);
		}
		const row_index = Number(row_element.getAttribute("data-row-index"));
		if (!Number.isNaN(row_index) && this._source_rows[row_index]) {
			return this._source_rows[row_index];
		}
		return this._resolve_source_row({ meta: { rowIndex: row_index } });
	}

	_sync_row_dom_keys() {
		if (!this._host || !this._datatable) {
			return;
		}
		const dom_rows = this._host.querySelectorAll(".dt-row:not(.dt-row-header):not(.dt-row-filter)");
		const visible_indices = this._datatable.bodyRenderer?.visibleRowIndices;
		if (visible_indices?.length === dom_rows.length) {
			dom_rows.forEach((element, index) => {
				const source_row = this._source_rows[visible_indices[index]];
				if (source_row?.row_key && element.getAttribute("data-ae-row-key") !== source_row.row_key) {
					element.setAttribute("data-ae-row-key", source_row.row_key);
				}
				this._apply_row_drillable_class(element, source_row, visible_indices[index]);
			});
			return;
		}
		dom_rows.forEach((element) => {
			if (element.getAttribute("data-ae-row-key")) {
				return;
			}
			const row_index = Number(element.getAttribute("data-row-index"));
			const source_row = Number.isNaN(row_index) ? null : this._source_rows[row_index];
			if (source_row?.row_key && element.getAttribute("data-ae-row-key") !== source_row.row_key) {
				element.setAttribute("data-ae-row-key", source_row.row_key);
			}
			this._apply_row_drillable_class(element, source_row, row_index);
		});
	}

	_bind_resize_observer() {
		if (this._resize_observer || !this._container || typeof ResizeObserver === "undefined") {
			return;
		}
		this._resize_observer = new ResizeObserver(() => {
			if (this._resize_debounce_timer) {
				clearTimeout(this._resize_debounce_timer);
			}
			this._resize_debounce_timer = setTimeout(() => {
				this._resize_debounce_timer = null;
				if (this._mounted) {
					this._apply_sticky_first_column();
				}
			}, AE_DT_RESIZE_DEBOUNCE_MS);
		});
		this._resize_observer.observe(this._container);
		AE_DT_ACTIVE_RESIZE_OBSERVERS += 1;
	}

	_unbind_resize_observer() {
		if (this._resize_observer) {
			this._resize_observer.disconnect();
			this._resize_observer = null;
			AE_DT_ACTIVE_RESIZE_OBSERVERS = Math.max(0, AE_DT_ACTIVE_RESIZE_OBSERVERS - 1);
		}
		if (this._resize_debounce_timer) {
			clearTimeout(this._resize_debounce_timer);
			this._resize_debounce_timer = null;
		}
	}

	_apply_row_drillable_class(element, source_row, row_index) {
		if (!element) {
			return;
		}
		const drillable =
			source_row &&
			source_row.drill_down_enabled !== 0 &&
			source_row.drill_down_enabled !== false;
		element.classList.toggle("ae-grid-row--drillable", !!drillable);
		element.classList.toggle("ae-grid-row--active", row_index === this._active_row_index);
		if (drillable) {
			element.setAttribute("title", __("Click to select · Double-click to drill down"));
		} else {
			element.removeAttribute("title");
		}
		if (row_index === this._active_row_index) {
			element.setAttribute("aria-selected", "true");
		} else {
			element.removeAttribute("aria-selected");
		}
	}

	_sync_active_row_dom() {
		if (!this._host) {
			return;
		}
		this._host.querySelectorAll(".dt-row:not(.dt-row-header):not(.dt-row-filter)").forEach((element) => {
			const row_index = Number(element.getAttribute("data-row-index"));
			const is_active = !Number.isNaN(row_index) && row_index === this._active_row_index;
			element.classList.toggle("ae-grid-row--active", is_active);
			if (is_active) {
				element.setAttribute("aria-selected", "true");
			} else {
				element.removeAttribute("aria-selected");
			}
		});
	}

	set_active_row_index(index) {
		if (index === null || index === undefined || Number.isNaN(Number(index))) {
			this._active_row_index = null;
		} else {
			this._active_row_index = Number(index);
		}
		this._sync_active_row_dom();
	}

	get_active_row_index() {
		return this._active_row_index;
	}

	get_active_row() {
		if (this._active_row_index === null || this._active_row_index === undefined) {
			return null;
		}
		return this._source_rows[this._active_row_index] || null;
	}

	toggle_active_row_selection() {
		if (!this._datatable?.rowmanager || this._active_row_index === null) {
			return;
		}
		this._datatable.rowmanager.toggleRow(this._active_row_index);
		this._emit_selection_change();
	}

	_apply_sticky_first_column() {
		if (!this._datatable?.setColumnSticky) {
			return;
		}
		const offset = this._options.checkbox_column === false ? 0 : 1;
		try {
			this._datatable.setColumnSticky(offset, true);
		} catch (error) {
			console.warn("[Account Explorer] unable to sticky first summary column", error);
		}
	}

	_emit_selection_change() {
		const checked = this.get_checked_rows();
		this._options.on_selection_change?.(checked);
		this.events?.emit("grid:selection_changed", { checked_rows: checked });
	}

	get_checked_rows() {
		const indices = this._datatable?.rowmanager?.getCheckedRows?.() || [];
		return indices
			.map((index) => this._source_rows[Number(index)])
			.filter(Boolean)
			.map((row) => (row.row_key ? this.resolve_source_row_by_key(row.row_key) || row : row));
	}

	clear_selection() {
		if (!this._datatable?.datamanager?.rows) {
			return;
		}
		this._datatable.datamanager.rows.forEach((row) => {
			if (row[0]?.content) {
				row[0].content = this._datatable.datamanager.getCheckboxHTML();
			}
		});
		this._datatable.rowmanager?.refreshRows?.();
		this._emit_selection_change();
	}

	get_column_state() {
		return (this._datatable?.getColumns?.() || []).map((col) => ({
			id: col.id,
			name: col.name,
			width: col.width,
			sortOrder: col.sortOrder,
		}));
	}

	apply_column_state(state, { silent = false } = {}) {
		if (!this._datatable || !state?.length) {
			return false;
		}
		const current_columns = this._datatable.getColumns?.() || [];
		const current_data = this._datatable.getData?.() || [];
		const state_by_id = new Map(state.map((col) => [col.id, col]));
		const ordered_ids = state.map((col) => col.id).filter(Boolean);
		const next_columns = [];
		ordered_ids.forEach((column_id) => {
			const col = current_columns.find((item) => item.id === column_id);
			if (!col) {
				return;
			}
			const patch = state_by_id.get(column_id);
			next_columns.push({
				...col,
				width: patch?.width || col.width,
				sortOrder: patch?.sortOrder || col.sortOrder,
			});
		});
		current_columns.forEach((col) => {
			if (!ordered_ids.includes(col.id)) {
				next_columns.push(col);
			}
		});
		const before = JSON.stringify(
			current_columns.map((col) => [col.id, col.width, col.sortOrder])
		);
		const after = JSON.stringify(next_columns.map((col) => [col.id, col.width, col.sortOrder]));
		if (before === after) {
			return false;
		}
		this._datatable.refresh(current_data, next_columns);
		this._apply_sticky_first_column();
		if (!silent) {
			this.events?.emit("grid:column_state_changed", {
				columns: this.get_column_state(),
				silent: false,
			});
		}
		return true;
	}

	_bind_column_resize_listener() {
		if (!this._host || this._resize_listener_bound) {
			return;
		}
		this._resize_listener_bound = true;
		const emit_resize = () => {
			window.setTimeout(() => {
				if (!this._mounted) {
					return;
				}
				const columns = this.get_column_state();
				this._options?.on_column_resized?.(columns);
				this.events?.emit("grid:column_state_changed", {
					columns,
					reason: "resize",
				});
			}, 0);
		};
		// Mouse may leave the handle during drag; bind a one-shot document mouseup after handle mousedown.
		$(this._host).on("mousedown.aeDtColumnResize", ".dt-cell__resize-handle", () => {
			$(document).off("mouseup.aeDtColumnResizeDoc").one("mouseup.aeDtColumnResizeDoc", emit_resize);
		});
	}

	set_density(mode) {
		const next = mode === "compact" ? "compact" : "comfortable";
		if (this._density === next) {
			return;
		}
		this._density = next;
		this._options.density = next;
		if (this._datatable?.options) {
			this._datatable.options.cellHeight = this._resolve_cell_height();
		}
		this._host?.classList.toggle("ae-datatable-host--compact", next === "compact");
		this._host?.classList.toggle("ae-datatable-host--comfortable", next !== "compact");
		if (this._mounted && this._datatable?.options) {
			this._datatable.options.cellHeight = this._resolve_cell_height();
			this._apply_sticky_first_column();
			this._sync_active_row_dom();
		}
	}

	get_density() {
		return this._density || "comfortable";
	}

	set_loading(is_loading) {
		this._loading = !!is_loading;
		this._sync_loading_state();
	}

	show_empty_state(message) {
		this._options.empty_message = message || __("No rows in the current result");
		if (this._datatable?.options) {
			this._datatable.options.noDataMessage = this._options.empty_message;
		}
	}

	_ensure_skeleton() {
		if (!this._host || this._skeleton_el) {
			return;
		}
		this._skeleton_el = document.createElement("div");
		this._skeleton_el.className = "ae-datatable-skeleton";
		this._skeleton_el.setAttribute("aria-hidden", "true");
		this._skeleton_el.innerHTML = Array.from({ length: 5 })
			.map(() => '<div class="ae-datatable-skeleton-row"></div>')
			.join("");
		this._host.appendChild(this._skeleton_el);
	}

	_sync_loading_state() {
		if (!this._host) {
			return;
		}
		this._host.classList.toggle("ae-datatable-host--loading", this._loading);
		if (this._skeleton_el) {
			this._skeleton_el.hidden = !this._loading;
		}
	}

	_format_row_tsv(row, column_ids) {
		return (column_ids || this._column_defs.map((col) => col.id))
			.map((id) => {
				const value = row?.[id];
				if (value === null || value === undefined) {
					return "";
				}
				return String(value).replace(/\t/g, " ").replace(/\r?\n/g, " ");
			})
			.join("\t");
	}

	copy_cell_value(row, column_id) {
		const source_row = row?.row_key ? this.resolve_source_row_by_key(row.row_key) || row : row;
		const value = source_row?.[column_id];
		if (value === null || value === undefined) {
			frappe.show_alert({ message: __("Nothing to copy"), indicator: "orange" });
			return false;
		}
		frappe.utils.copy_to_clipboard(String(value), __("Copied to clipboard"));
		return true;
	}

	copy_row_tsv(row) {
		const source_row = row?.row_key ? this.resolve_source_row_by_key(row.row_key) || row : row;
		if (!source_row) {
			return false;
		}
		const text = this._format_row_tsv(source_row);
		frappe.utils.copy_to_clipboard(text, __("Row copied"));
		return true;
	}

	copy_checked_rows_tsv() {
		const checked = this.get_checked_rows();
		if (!checked.length) {
			frappe.show_alert({ message: __("No rows selected"), indicator: "orange" });
			return false;
		}
		const text = checked.map((row) => this._format_row_tsv(row)).join("\n");
		frappe.utils.copy_to_clipboard(text, __("Selected rows copied"));
		return true;
	}

	get_instance() {
		return this._datatable;
	}

	_teardown_instance() {
		const was_mounted = this._mounted;
		this._unbind_resize_observer();
		if (this._datatable?.destroy) {
			this._datatable.destroy();
		}
		if (this._host) {
			ae_dt_untrack_host(this._host);
			$(this._host).off(".aeDtColumnResize");
		}
		$(document).off("mouseup.aeDtColumnResizeDoc");
		this._datatable = null;
		this._mounted = false;
		if (was_mounted && AE_DT_ACTIVE_MOUNT_COUNT > 0) {
			AE_DT_ACTIVE_MOUNT_COUNT -= 1;
		}
		this._source_rows = [];
		this._rows_by_key = new Map();
		this._column_defs = [];
		this._active_row_index = null;
		this._skeleton_el = null;
		this._last_refresh_signature = null;
		this._options = {};
		if (this._container) {
			this._container.innerHTML = "";
		}
		this._host = null;
		this._resize_listener_bound = false;
	}

	destroy() {
		this.cancel_pending_mount();
		const was_mounted = this._mounted;
		this._teardown_instance();
		this._container = null;
		if (was_mounted) {
			this.events?.emit("grid:destroyed");
		}
	}
};

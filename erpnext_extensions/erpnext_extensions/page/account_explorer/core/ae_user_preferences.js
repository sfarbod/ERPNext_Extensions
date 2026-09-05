frappe.provide("erpnext_extensions.account_explorer.core");

const AE_GRID_PREFS_SCHEMA_VERSION = 2;
const AE_USER_SETTINGS_DOCTYPE = "Account Explorer";
const AE_USER_SETTINGS_GRID_KEY = "Grid";
const AE_GRID_PREFS_DEBOUNCE_MS = 600;
const AE_GRID_PAGE_SIZE_OPTIONS = [25, 50, 100, 200];
const AE_NUMBER_FORMAT_MODES = ["raw", "auto", "thousands", "millions", "billions", "trillions"];
const AE_DENSITY_MODES = ["compact", "comfortable"];
const AE_DIMENSION_LAYOUT_MODES = ["compact", "full"];

const AE_AXIS_KEYS = [
	"account_level",
	"party",
	"unified_party",
	"dimension",
	"currency",
	"item_group",
	"item",
	"voucher",
];

function ae_prefs_is_plain_object(value) {
	return !!value && typeof value === "object" && !Array.isArray(value);
}

function ae_prefs_clone(value) {
	return JSON.parse(JSON.stringify(value ?? null));
}

function ae_prefs_normalize_number_format(value, fallback = "raw") {
	const mode = String(value || fallback)
		.toLowerCase()
		.replace(/[\s-]+/g, "_");
	return AE_NUMBER_FORMAT_MODES.includes(mode) ? mode : fallback;
}

function ae_prefs_settings_number_format(metadata = {}) {
	const raw =
		metadata?.default_amount_display_scale ||
		metadata?.defaults?.number_format ||
		metadata?.defaults?.amount_display_scale ||
		"raw";
	return ae_prefs_normalize_number_format(raw, "raw");
}

function ae_prefs_default_global(metadata = {}) {
	const defaults = metadata?.defaults || {};
	return {
		density: "comfortable",
		page_size: ae_prefs_normalize_page_size(defaults.page_size, 50, metadata),
		// Follow Iran Accounting Settings Default Amount Display Scale (typically Raw).
		number_format: ae_prefs_settings_number_format(metadata),
	};
}

function ae_prefs_default_axis(axis_key, metadata = {}) {
	const global = ae_prefs_default_global(metadata);
	const sort_defaults = {
		account_level: { sort_field: "display_code", sort_order: "asc" },
		party: { sort_field: "party_type", sort_order: "asc" },
		unified_party: { sort_field: "display_title", sort_order: "asc" },
		dimension: { sort_field: "display_code", sort_order: "asc" },
		currency: { sort_field: "currency", sort_order: "asc" },
		voucher: { sort_field: "posting_date", sort_order: "desc" },
		item_group: { sort_field: "display_code", sort_order: "asc" },
		item: { sort_field: "display_code", sort_order: "asc" },
	};
	const sort = sort_defaults[axis_key?.split(":")[0]] || sort_defaults.account_level;
	return {
		visible_columns: [],
		hidden_columns: [],
		column_order: [],
		column_widths: {},
		sticky_column: null,
		sort_field: sort.sort_field,
		sort_order: sort.sort_order,
		density: global.density,
		page_size: global.page_size,
		number_format: global.number_format,
		show_optional_full_voucher_columns: 0,
		dimension_layout: "compact",
		visible_dimension_fields: [],
	};
}

function ae_prefs_default_payload(metadata = {}) {
	const axes = {};
	AE_AXIS_KEYS.forEach((axis_key) => {
		axes[axis_key] = ae_prefs_default_axis(axis_key, metadata);
	});
	return {
		schema_version: AE_GRID_PREFS_SCHEMA_VERSION,
		global: ae_prefs_default_global(metadata),
		axes,
	};
}

function ae_prefs_clamp_width(width, profile = {}) {
	const min = profile.min || 60;
	const max = profile.max || 600;
	const preferred = profile.preferred || width || min;
	const numeric = cint(width);
	if (!numeric) {
		return preferred;
	}
	return Math.max(min, Math.min(max, numeric));
}

function ae_prefs_normalize_density(value, fallback = "comfortable") {
	return AE_DENSITY_MODES.includes(value) ? value : fallback;
}

function ae_prefs_normalize_page_size(value, fallback = 50, metadata = {}) {
	const max_allowed = cint(metadata?.max_page_size) || 200;
	let size = cint(value) || fallback;
	if (!AE_GRID_PAGE_SIZE_OPTIONS.includes(size)) {
		size = AE_GRID_PAGE_SIZE_OPTIONS.includes(fallback) ? fallback : 50;
	}
	return Math.max(25, Math.min(max_allowed, size));
}

function ae_prefs_normalize_sort_order(value, fallback = "asc") {
	const order = String(value || fallback).toLowerCase();
	return order === "desc" ? "desc" : "asc";
}

function ae_prefs_resolve_axis_key(view_axis, dimension_type = null) {
	if (view_axis === "dimension" && dimension_type) {
		return `dimension:${dimension_type}`;
	}
	return view_axis || "account_level";
}

function ae_prefs_reconcile_columns(axis_prefs, allowed_column_ids = [], required_column_id = null) {
	const allowed = new Set((allowed_column_ids || []).filter(Boolean));
	const hidden = new Set((axis_prefs.hidden_columns || []).filter((id) => allowed.has(id)));
	const saved_order = (axis_prefs.column_order || []).filter((id) => allowed.has(id));
	const default_order = (allowed_column_ids || []).filter((id) => allowed.has(id));
	const column_order = [];
	saved_order.forEach((id) => {
		if (!column_order.includes(id)) {
			column_order.push(id);
		}
	});
	default_order.forEach((id) => {
		if (!column_order.includes(id)) {
			column_order.push(id);
		}
	});
	if (required_column_id && allowed.has(required_column_id)) {
		hidden.delete(required_column_id);
	}
	const visible_columns = column_order.filter((id) => !hidden.has(id));
	if (!visible_columns.length && required_column_id && allowed.has(required_column_id)) {
		visible_columns.push(required_column_id);
		hidden.delete(required_column_id);
	}
	const column_widths = {};
	Object.entries(axis_prefs.column_widths || {}).forEach(([column_id, width]) => {
		if (allowed.has(column_id)) {
			column_widths[column_id] = ae_prefs_clamp_width(width);
		}
	});
	let sticky_column = axis_prefs.sticky_column;
	if (sticky_column && !allowed.has(sticky_column)) {
		sticky_column = required_column_id || visible_columns[0] || null;
	}
	return {
		...axis_prefs,
		hidden_columns: [...hidden],
		visible_columns,
		column_order,
		column_widths,
		sticky_column,
	};
}

function normalize_grid_preferences(payload, metadata = {}, reconcile_context = {}) {
	const defaults = ae_prefs_default_payload(metadata);
	if (!payload) {
		return ae_prefs_clone(defaults);
	}
	let source = payload;
	if (typeof payload === "string") {
		try {
			source = JSON.parse(payload);
		} catch (_error) {
			return ae_prefs_clone(defaults);
		}
	}
	if (!ae_prefs_is_plain_object(source)) {
		return ae_prefs_clone(defaults);
	}
	const source_version = cint(source.schema_version) || 0;
	// Unknown future schema → safe reset. v1 upgrades in place to v2.
	if (source_version > AE_GRID_PREFS_SCHEMA_VERSION) {
		return ae_prefs_clone(defaults);
	}
	if (source_version < 1) {
		return ae_prefs_clone(defaults);
	}
	const normalized = ae_prefs_clone(defaults);
	const global = ae_prefs_is_plain_object(source.global) ? source.global : {};
	normalized.global = {
		density: ae_prefs_normalize_density(global.density, defaults.global.density),
		page_size: ae_prefs_normalize_page_size(global.page_size, defaults.global.page_size, metadata),
		number_format: ae_prefs_normalize_number_format(global.number_format, defaults.global.number_format),
	};
	const axes = ae_prefs_is_plain_object(source.axes) ? source.axes : {};
	Object.entries(axes).forEach(([axis_key, axis_source]) => {
		if (!ae_prefs_is_plain_object(axis_source)) {
			return;
		}
		const base = normalized.axes[axis_key] || ae_prefs_default_axis(axis_key, metadata);
		const merged = {
			...base,
			...axis_source,
			column_widths: {
				...(base.column_widths || {}),
				...(axis_source.column_widths || {}),
			},
		};
		merged.hidden_columns = Array.isArray(merged.hidden_columns)
			? [...new Set(merged.hidden_columns.filter(Boolean))]
			: [];
		merged.visible_columns = Array.isArray(merged.visible_columns)
			? [...new Set(merged.visible_columns.filter(Boolean))]
			: [];
		merged.column_order = Array.isArray(merged.column_order)
			? [...new Set(merged.column_order.filter(Boolean))]
			: [];
		merged.visible_dimension_fields = Array.isArray(merged.visible_dimension_fields)
			? [...new Set(merged.visible_dimension_fields.filter(Boolean))]
			: [];
		merged.density = ae_prefs_normalize_density(merged.density, normalized.global.density);
		merged.page_size = ae_prefs_normalize_page_size(merged.page_size, normalized.global.page_size, metadata);
		merged.number_format = ae_prefs_normalize_number_format(merged.number_format, normalized.global.number_format);
		merged.sort_order = ae_prefs_normalize_sort_order(merged.sort_order, base.sort_order);
		merged.dimension_layout = AE_DIMENSION_LAYOUT_MODES.includes(merged.dimension_layout)
			? merged.dimension_layout
			: "compact";
		merged.show_optional_full_voucher_columns = merged.show_optional_full_voucher_columns ? 1 : 0;
		normalized.axes[axis_key] = merged;
	});
	Object.keys(normalized.axes).forEach((axis_key) => {
		const allowed = reconcile_context[axis_key]?.allowed_column_ids;
		if (!allowed?.length) {
			return;
		}
		normalized.axes[axis_key] = ae_prefs_reconcile_columns(
			normalized.axes[axis_key],
			allowed,
			reconcile_context[axis_key]?.required_column_id || null
		);
	});
	// v1→v2: replace leftover schema default Auto with settings Raw (not intentional Auto pick).
	if (source_version < 2) {
		const settings_mode = ae_prefs_settings_number_format(metadata);
		if (normalized.global.number_format === "auto" && settings_mode === "raw") {
			normalized.global.number_format = "raw";
			Object.keys(normalized.axes).forEach((axis_key) => {
				if (normalized.axes[axis_key].number_format === "auto") {
					normalized.axes[axis_key].number_format = "raw";
				}
			});
		}
		normalized.schema_version = AE_GRID_PREFS_SCHEMA_VERSION;
	}
	return normalized;
}

erpnext_extensions.account_explorer.core.normalize_grid_preferences = normalize_grid_preferences;
erpnext_extensions.account_explorer.core.ae_prefs_resolve_axis_key = ae_prefs_resolve_axis_key;
erpnext_extensions.account_explorer.core.ae_prefs_default_payload = ae_prefs_default_payload;
erpnext_extensions.account_explorer.core.AE_GRID_PAGE_SIZE_OPTIONS = AE_GRID_PAGE_SIZE_OPTIONS;
erpnext_extensions.account_explorer.core.AE_NUMBER_FORMAT_MODES = AE_NUMBER_FORMAT_MODES;

erpnext_extensions.account_explorer.core.AEUserPreferences = class AEUserPreferences {
	constructor(controller) {
		this.controller = controller;
		this.store = controller.store;
		this.events = controller.events;
		this.payload = null;
		this._save_timer = null;
		this._save_inflight = null;
		this._save_generation = 0;
		this._hydrating = false;
		this._loaded = false;
		this._last_saved_snapshot = null;
		this._applied_axis_signature = null;
		this._attribution = {};
		this._save_failures = 0;
		this._unsubscribers = [];
		this._bound = false;
	}

	destroy() {
		this._clear_save_timer();
		this.unbind_unload_flush();
		this._unsubscribers.forEach((unsub) => unsub?.());
		this._unsubscribers = [];
		this._bound = false;
		this._applied_axis_signature = null;
	}

	get_axis_key(view_axis = null, dimension_type = null) {
		const axis = view_axis || this.controller.analysis_context?.view_axis || "account_level";
		const dimension =
			dimension_type || this.controller.analysis_context?.dimension_scope?.dimension_type || null;
		return ae_prefs_resolve_axis_key(axis, dimension);
	}

	get_axis_preferences(axis_key = null) {
		const key = axis_key || this.get_axis_key();
		const payload = this.payload || ae_prefs_default_payload(this.controller.metadata);
		if (!payload.axes[key]) {
			payload.axes[key] = ae_prefs_default_axis(key, this.controller.metadata);
		}
		return payload.axes[key];
	}

	_axis_signature(axis_key, axis_prefs, global_prefs) {
		return JSON.stringify({
			axis_key,
			density: axis_prefs.density || global_prefs.density,
			page_size: axis_prefs.page_size || global_prefs.page_size,
			number_format: axis_prefs.number_format || global_prefs.number_format,
			sort_field: axis_prefs.sort_field || null,
			sort_order: axis_prefs.sort_order || "asc",
			hidden_columns: axis_prefs.hidden_columns || [],
			column_order: axis_prefs.column_order || [],
			column_widths: axis_prefs.column_widths || {},
			sticky_column: axis_prefs.sticky_column || null,
			show_optional_full_voucher_columns: axis_prefs.show_optional_full_voucher_columns ? 1 : 0,
			dimension_layout: axis_prefs.dimension_layout || "compact",
			visible_dimension_fields: axis_prefs.visible_dimension_fields || [],
		});
	}

	async load({ force = false } = {}) {
		if (this._loaded && !force) {
			return this.payload;
		}
		this.events?.emit("preferences:loading");
		const load_started = performance.now();
		let raw = {};
		try {
			if (frappe.session.user !== "Guest") {
				// Returns Promise from frappe.call(...).then(...). JSON object keyed by section.
				raw = (await frappe.model.user_settings.get(AE_USER_SETTINGS_DOCTYPE)) || {};
				frappe.model.user_settings[AE_USER_SETTINGS_DOCTYPE] = raw;
			}
		} catch (error) {
			console.warn("[Account Explorer] failed to load grid preferences", error);
			this.events?.emit("preferences:error", { phase: "load", error });
		}
		this._attribution.load_ms = Math.round((performance.now() - load_started) * 100) / 100;
		const normalize_started = performance.now();
		const grid = raw[AE_USER_SETTINGS_GRID_KEY] || raw.Grid || null;
		this.payload = normalize_grid_preferences(grid, this.controller.metadata || {});
		this._attribution.normalize_ms = Math.round((performance.now() - normalize_started) * 100) / 100;
		const migrated = cint(grid?.schema_version) < AE_GRID_PREFS_SCHEMA_VERSION;
		this._last_saved_snapshot = migrated ? null : JSON.stringify(this.payload);
		this._applied_axis_signature = null;
		this._loaded = true;
		this.events?.emit("preferences:loaded", { payload: this.payload, attribution: this._attribution });
		if (migrated) {
			this.schedule_save();
		}
		return this.payload;
	}

	apply_axis_to_controller(axis_key = null, { emit = false, force = false } = {}) {
		if (!this._loaded) {
			return false;
		}
		const key = axis_key || this.get_axis_key();
		const axis_prefs = this.get_axis_preferences(key);
		const global = this.payload?.global || ae_prefs_default_global(this.controller.metadata);
		const signature = this._axis_signature(key, axis_prefs, global);
		if (!force && this._applied_axis_signature === signature) {
			return false;
		}
		const apply_started = performance.now();
		this._hydrating = true;
		try {
			this.controller.grid_density = ae_prefs_normalize_density(
				axis_prefs.density || global.density,
				global.density
			);
			this.controller.number_format_mode = ae_prefs_normalize_number_format(
				axis_prefs.number_format || global.number_format,
				ae_prefs_settings_number_format(this.controller.metadata)
			);
			// Explicit Raw must not remain painted as Auto after preference apply.
			if (typeof this.controller.render_totals === "function") {
				this.controller.render_totals();
			}
			this.controller.analysis_context.page_size = ae_prefs_normalize_page_size(
				axis_prefs.page_size || global.page_size,
				global.page_size,
				this.controller.metadata
			);
			if (axis_prefs.sort_field) {
				this.controller.analysis_context.sort_field = axis_prefs.sort_field;
			}
			this.controller.analysis_context.sort_order = ae_prefs_normalize_sort_order(axis_prefs.sort_order);
			this.controller.grid_hidden_columns = [...(axis_prefs.hidden_columns || [])];
			this.controller.grid_column_order = [...(axis_prefs.column_order || [])];
			this.controller.grid_column_widths = { ...(axis_prefs.column_widths || {}) };
			this.controller.grid_sticky_column = axis_prefs.sticky_column || null;
			this.controller.show_optional_full_voucher_columns = !!axis_prefs.show_optional_full_voucher_columns;
			this.controller.show_full_voucher_dimensions = axis_prefs.dimension_layout === "full";
			if (Array.isArray(axis_prefs.visible_dimension_fields)) {
				this.controller.gl_dimension_column_visibility = {};
				(this.controller.get_gl_dimension_definitions?.() || []).forEach((definition) => {
					this.controller.gl_dimension_column_visibility[definition.fieldname] =
						axis_prefs.visible_dimension_fields.includes(definition.fieldname);
				});
			}
			const presentation = this.controller.build_presentation_state();
			const presentation_signature = JSON.stringify(presentation);
			if (this.controller._presentation_signature !== presentation_signature) {
				this.controller._presentation_signature = presentation_signature;
				this.store.patch({ presentation }, { silent: !emit });
			}
			this._applied_axis_signature = signature;
			if (emit) {
				this.events?.emit("preferences:changed", { axis_key: key });
			}
		} finally {
			this._hydrating = false;
			this._attribution.apply_axis_ms = Math.round((performance.now() - apply_started) * 100) / 100;
		}
		return true;
	}

	capture_from_controller() {
		const axis_key = this.get_axis_key();
		const presentation = this.controller.build_presentation_state();
		const adapter_state = (this.controller.datatable_adapter?.get_column_state?.() || []).filter(
			(col) => col?.id && !String(col.id).startsWith("_")
		);
		const column_widths = { ...(presentation.column_widths || {}) };
		Object.keys(column_widths).forEach((column_id) => {
			if (String(column_id).startsWith("_")) {
				delete column_widths[column_id];
			}
		});
		adapter_state.forEach((col) => {
			if (col?.id && col.width) {
				column_widths[col.id] = col.width;
			}
		});
		const adapter_order = adapter_state.map((col) => col.id).filter(Boolean);
		const presentation_order = (presentation.column_order || []).filter(
			(column_id) => column_id && !String(column_id).startsWith("_")
		);
		const column_order = adapter_order.length ? adapter_order : presentation_order;
		if (!this.payload) {
			this.payload = ae_prefs_default_payload(this.controller.metadata);
		}
		this.payload.global = {
			density: ae_prefs_normalize_density(this.controller.get_grid_density?.()),
			page_size: ae_prefs_normalize_page_size(
				this.controller.analysis_context?.page_size,
				50,
				this.controller.metadata
			),
			number_format: ae_prefs_normalize_number_format(this.controller.number_format_mode),
		};
		// Cascade Numbers mode to every axis so Raw stays Raw across axis switch/reload.
		const cascaded_number_format = this.payload.global.number_format;
		Object.keys(this.payload.axes || {}).forEach((key) => {
			if (this.payload.axes[key] && typeof this.payload.axes[key] === "object") {
				this.payload.axes[key].number_format = cascaded_number_format;
			}
		});
		this.payload.axes[axis_key] = {
			...this.get_axis_preferences(axis_key),
			hidden_columns: [...(presentation.hidden_columns || [])].filter(
				(column_id) => column_id && !String(column_id).startsWith("_")
			),
			visible_columns: [...(presentation.visible_columns || [])].filter(
				(column_id) => column_id && !String(column_id).startsWith("_")
			),
			column_order,
			column_widths,
			sticky_column: this.controller.grid_sticky_column || presentation.sticky_column || null,
			sort_field: presentation.sort_field,
			sort_order: presentation.sort_order,
			density: this.controller.get_grid_density?.(),
			page_size: presentation.page_size,
			number_format: cascaded_number_format,
			show_optional_full_voucher_columns: presentation.show_optional_full_voucher_columns ? 1 : 0,
			dimension_layout: this.controller.show_full_voucher_dimensions ? "full" : "compact",
			visible_dimension_fields: Object.entries(this.controller.gl_dimension_column_visibility || {})
				.filter(([, visible]) => visible !== false)
				.map(([fieldname]) => fieldname),
		};
		return this.payload;
	}

	schedule_save() {
		if (this._hydrating || !this._loaded || frappe.session.user === "Guest") {
			return;
		}
		this._clear_save_timer();
		this._save_timer = setTimeout(() => {
			this._save_timer = null;
			void this.flush_save();
		}, AE_GRID_PREFS_DEBOUNCE_MS);
	}

	_persist_grid_payload(payload) {
		// frappe.model.user_settings.save(doctype, key, object) deep-merges into the section.
		// Preference payloads must replace Grid entirely (reset/delete stale axis keys), so use update().
		// update() returns a jQuery Deferred from frappe.call — wrap with Promise.resolve for .then/.catch.
		const current = $.extend(true, {}, frappe.model.user_settings[AE_USER_SETTINGS_DOCTYPE] || {});
		current[AE_USER_SETTINGS_GRID_KEY] = payload;
		frappe.model.user_settings[AE_USER_SETTINGS_DOCTYPE] = current;
		return Promise.resolve(frappe.model.user_settings.update(AE_USER_SETTINGS_DOCTYPE, current));
	}

	_persist_grid_payload_sync(payload) {
		// Frappe v16 pagehide/beforeunload path.
		// Chromium often aborts classic async:false XHR during reload/unload; use fetch+keepalive
		// (with sync XHR fallback) so the final preference payload still reaches User Settings.
		const current = $.extend(true, {}, frappe.model.user_settings[AE_USER_SETTINGS_DOCTYPE] || {});
		current[AE_USER_SETTINGS_GRID_KEY] = payload;
		frappe.model.user_settings[AE_USER_SETTINGS_DOCTYPE] = current;
		const user_settings_json = JSON.stringify(current);
		let kept_alive = false;
		if (typeof fetch === "function" && typeof URLSearchParams !== "undefined") {
			try {
				const body = new URLSearchParams();
				body.set("doctype", AE_USER_SETTINGS_DOCTYPE);
				body.set("user_settings", user_settings_json);
				fetch("/api/method/frappe.model.utils.user_settings.save", {
					method: "POST",
					headers: {
						Accept: "application/json",
						"X-Frappe-CSRF-Token": frappe.csrf_token,
						"X-Requested-With": "XMLHttpRequest",
					},
					body,
					credentials: "same-origin",
					keepalive: true,
				}).catch(() => {
					/* unload/keepalive failures are non-actionable */
				});
				kept_alive = true;
			} catch (error) {
				console.warn("[Account Explorer] keepalive preference save failed", error);
			}
		}
		if (!kept_alive) {
			frappe.call({
				method: "frappe.model.utils.user_settings.save",
				args: {
					doctype: AE_USER_SETTINGS_DOCTYPE,
					user_settings: user_settings_json,
				},
				async: false,
			});
		}
		return current;
	}

	async flush_save({ sync = false } = {}) {
		if (this._hydrating || !this._loaded || frappe.session.user === "Guest") {
			return;
		}
		this._clear_save_timer();
		const payload = this.capture_from_controller();
		const snapshot = JSON.stringify(payload);
		if (snapshot === this._last_saved_snapshot) {
			return;
		}
		if (this._save_inflight && !sync) {
			// Coalesce overlapping async flushes; unload sync path must still persist latest.
			return this._save_inflight;
		}
		const generation = (this._save_generation += 1);
		this.events?.emit("preferences:saving", { sync: !!sync });
		if (sync) {
			try {
				this._persist_grid_payload_sync(payload);
				this._last_saved_snapshot = snapshot;
				this._save_failures = 0;
				this.events?.emit("preferences:saved", { sync: true });
			} catch (error) {
				this._save_failures += 1;
				console.warn("[Account Explorer] sync preference save failed", error);
				this.events?.emit("preferences:error", { phase: "save", sync: true, error });
			}
			return;
		}
		const save_promise = this._persist_grid_payload(payload)
			.then(() => {
				if (generation !== this._save_generation) {
					return;
				}
				this._last_saved_snapshot = snapshot;
				this._save_failures = 0;
				this.events?.emit("preferences:saved", { sync: false });
			})
			.catch((error) => {
				if (generation !== this._save_generation) {
					return;
				}
				this._save_failures += 1;
				console.warn("[Account Explorer] failed to save grid preferences", error);
				this.events?.emit("preferences:error", { phase: "save", error });
				if (this._save_failures >= 3) {
					frappe.show_alert({
						message: __("Unable to save grid preferences."),
						indicator: "orange",
					});
				}
			})
			.finally(() => {
				if (this._save_inflight === save_promise) {
					this._save_inflight = null;
				}
			});
		this._save_inflight = save_promise;
		return this._save_inflight;
	}

	bind_unload_flush() {
		if (this._unload_bound || typeof window === "undefined") {
			return;
		}
		this._unload_bound = true;
		this._on_document_unload = () => {
			if (this._hydrating || !this._loaded) {
				return;
			}
			const dirty =
				!!this._save_timer ||
				!!this._save_inflight ||
				JSON.stringify(this.capture_from_controller()) !== this._last_saved_snapshot;
			if (!dirty) {
				return;
			}
			// Cancel debounce so async flush cannot race with unload sync/keepalive.
			this._clear_save_timer();
			void this.flush_save({ sync: true });
		};
		window.addEventListener("pagehide", this._on_document_unload);
		window.addEventListener("beforeunload", this._on_document_unload);
	}

	unbind_unload_flush() {
		if (!this._unload_bound || typeof window === "undefined") {
			return;
		}
		window.removeEventListener("pagehide", this._on_document_unload);
		window.removeEventListener("beforeunload", this._on_document_unload);
		this._unload_bound = false;
		this._on_document_unload = null;
	}

	reset_current_axis() {
		const axis_key = this.get_axis_key();
		if (!this.payload) {
			this.payload = ae_prefs_default_payload(this.controller.metadata);
		}
		this.payload.axes[axis_key] = ae_prefs_default_axis(axis_key, this.controller.metadata);
		this._applied_axis_signature = null;
		this.apply_axis_to_controller(axis_key, { emit: true, force: true });
		this.schedule_save();
		this.events?.emit("preferences:reset", { scope: "axis", axis_key });
	}

	reset_all_axes() {
		this.payload = ae_prefs_default_payload(this.controller.metadata);
		this._applied_axis_signature = null;
		this.apply_axis_to_controller(this.get_axis_key(), { emit: true, force: true });
		this.schedule_save();
		this.events?.emit("preferences:reset", { scope: "all" });
	}

	bind_controller_events() {
		if (this._bound) {
			return;
		}
		this._bound = true;
		const schedule = (payload = {}) => {
			if (payload?.silent || this._hydrating) {
				return;
			}
			this.schedule_save();
		};
		this._unsubscribers.push(this.events.subscribe("grid:column_state_changed", schedule));
		this.bind_unload_flush();
	}

	get_attribution() {
		return { ...this._attribution };
	}

	_clear_save_timer() {
		if (this._save_timer) {
			clearTimeout(this._save_timer);
			this._save_timer = null;
		}
	}

	is_hydrating() {
		return this._hydrating;
	}
};

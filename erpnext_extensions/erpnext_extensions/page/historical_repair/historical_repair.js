frappe.provide("erpnext_extensions.historical_repair");

frappe.pages["historical-repair"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Historical Stock Integrity & Repair"),
		single_column: true,
	});
	page.main.addClass("historical-repair-page");
	erpnext_extensions.historical_repair = new HistoricalRepairPage(page);
};

const TOPICS = [
	{ id: "posting", title: __("Posting Order"), section: "Production Posting Order", kpi: "Posting Order" },
	{ id: "wrong", title: __("Wrong Rate"), kpi: "Wrong Rate" },
	{ id: "zero", title: __("Zero / Lost Rate"), kpi: "Zero Rate" },
	{ id: "manufacture", title: __("Manufacture Valuation"), kpi: null },
	{ id: "sle", title: __("SLE / Bin Integrity"), kpi: "Broken Bin" },
	{ id: "gl", title: __("GL Integrity"), kpi: "Broken GL" },
	{ id: "riv", title: __("Failed RIV"), kpi: "Failed RIV" },
];

/** Dashboard chip → topic id (primary surface for that KPI). */
const KPI_TOPIC = {
	"Posting Order": "posting",
	"Wrong Rate": "wrong",
	"Wrong Rate READY": "wrong",
	"Wrong Rate WAITING": "wrong",
	"Wrong Rate MANUAL": "wrong",
	"Wrong Rate Complete": "wrong",
	"Wrong Amount": "wrong",
	"Wrong Valuation": "wrong",
	"Wrong Incoming": "wrong",
	"Wrong Outgoing": "wrong",
	"Wrong Average": "wrong",
	"Zero Rate": "zero",
	"Zero Rate Patient Zero": "zero",
	"I4 Leftover": "sle",
	"READY_I4": "sle",
	"WAITING_I4": "sle",
	"MANUAL_I4": "sle",
	"REPLAY_REQUIRED_I4": "sle",
	"Broken SABB": "sle",
	"Broken Bin": "sle",
	"Waiting Downstream Bin": "sle",
	"Patient Zero": "sle",
	"Broken GL": "gl",
	"GL READY": "gl",
	"GL WAITING": "gl",
	"GL MANUAL": "gl",
	"Failed RIV": "riv",
	"RIV SAFE": "riv",
	"RIV WAITING": "riv",
	"RIV UNSAFE": "riv",
};

const KPI_ORDER = [
	"Integrity Score",
	"Posting Order",
	"Wrong Rate",
	"Wrong Rate READY",
	"Wrong Rate WAITING",
	"Wrong Rate MANUAL",
	"Wrong Rate Complete",
	"Zero Rate",
	"I4 Leftover",
	"Wrong Amount",
	"Wrong Valuation",
	"Wrong Incoming",
	"Wrong Outgoing",
	"Wrong Average",
	"Broken SABB",
	"Broken Bin",
	"Waiting Downstream Bin",
	"Broken GL",
	"GL READY",
	"GL WAITING",
	"GL MANUAL",
	"Failed RIV",
	"RIV SAFE",
	"RIV WAITING",
	"RIV UNSAFE",
	"Patient Zero",
	"Zero Rate Patient Zero",
	"READY_I4",
	"WAITING_I4",
	"MANUAL_I4",
	"REPLAY_REQUIRED_I4",
	"Repairable",
	"Manual",
	"Ambiguous",
	"Replay Pending",
	"Replay Complete",
	"Average Replay Time",
];

class HistoricalRepairPage {
	constructor(page) {
		this.page = page;
		this.ppo = "erpnext_extensions.iran_accounting.stock_posting_order.api";
		this.api = "erpnext_extensions.iran_accounting.historical_stock.api";
		this.topic = "posting";
		this.rows = [];
		this.dry_run_done = false;
		this.impact_done = false;
		this.visible_rows = [];
		this.sort_keys = [];
		this.cancelled = false;
		this.hidden_cols = new Set();
		this.col_filters = {};
		this.last_impact = null;
		this.advanced = false;
		this.last_dashboard = {};
		this.last_scan_all = null;
		this.scan_all_job_id = null;
		this.scan_all_company = null;
		this._last_company_value = null; // null = not bootstrapped yet
		this.kpi_bucket = null;
		this.active_kpi_label = null;
		this.topic_state = {};
		this._reset_topic_state();
		this.access = this._access_from_boot();
		this.$body = $(page.body);
		this.render();
		this._bind_keys();
		this.scan_all({ auto: true });
	}

	_reset_topic_state() {
		this.topic_state = {};
		TOPICS.forEach((t) => {
			this.topic_state[t.id] = {
				loaded: false,
				rows: [],
				company: null,
				filter_fingerprint: null,
				scan_count: null,
				source: null,
			};
		});
	}

	_scan_filter_fingerprint() {
		const f = this.filters();
		return JSON.stringify({
			company: f.company || "",
			item_code: f.item_code || "",
			warehouse: f.warehouse || "",
			batch: f.batch || "",
			work_order: f.work_order || "",
			voucher: f.voucher || "",
			serial_and_batch_bundle: f.serial_and_batch_bundle || "",
			from_date: f.from_date || "",
			to_date: f.to_date || "",
			repair_class: f.repair_class || "",
			planner_status: f.planner_status || "",
			patient_zero: f.patient_zero || "",
		});
	}

	_topic_dashboard_count(topicId) {
		const dash = this.last_dashboard || {};
		const exp = (this.last_scan_all && this.last_scan_all.topic_expectations) || {};
		if (exp[topicId] && exp[topicId].count != null) return Number(exp[topicId].count) || 0;
		const meta = TOPICS.find((t) => t.id === topicId);
		if (meta && meta.kpi && dash[meta.kpi] != null) return Number(dash[meta.kpi]) || 0;
		if (topicId === "sle") {
			return (
				(Number(dash["Broken Bin"]) || 0) +
				(Number(dash["Waiting Downstream Bin"]) || 0) +
				(Number(dash["I4 Leftover"]) || 0) +
				(Number(dash["Broken SABB"]) || 0)
			);
		}
		return null;
	}

	_save_current_topic_state() {
		if (!this.topic || !this.topic_state[this.topic]) return;
		const st = this.topic_state[this.topic];
		st.rows = Array.isArray(this.rows) ? this.rows.slice() : [];
		st.loaded = !!st.loaded;
	}

	_restore_topic_state(id) {
		const st = this.topic_state[id] || { loaded: false, rows: [] };
		const company = this.company && this.company.get_value();
		const fp = this._scan_filter_fingerprint();
		const companyOk = !st.company || st.company === company;
		const filterOk = !st.filter_fingerprint || st.filter_fingerprint === fp;
		if (st.loaded && companyOk && filterOk) {
			this.rows = Array.isArray(st.rows) ? st.rows.slice() : [];
			return;
		}
		this.rows = [];
		if (st.loaded && (!companyOk || !filterOk)) {
			st.loaded = false;
			st.rows = [];
			st.source = null;
			st.scan_count = null;
		}
	}

	_mark_topic_loaded(rows) {
		const company = this.company && this.company.get_value();
		const st = this.topic_state[this.topic] || {};
		st.loaded = true;
		st.rows = Array.isArray(rows) ? rows.slice() : [];
		st.company = company || null;
		st.filter_fingerprint = this._scan_filter_fingerprint();
		st.scan_count = st.rows.length;
		st.source = "topic_scan";
		this.topic_state[this.topic] = st;
		this.rows = st.rows.slice();
	}

	_invalidate_topic_rows(reason) {
		this._reset_topic_state();
		this.rows = [];
		this.dry_run_done = false;
		this.impact_done = false;
		if (this.btn_repair) this._lock_writes(true);
		if (reason) {
			this.$preview &&
				this.$preview.text(
					__("Topic grids cleared ({0}). Click Scan on each tab to load rows for the current filters.", [reason])
				);
		}
	}

	_on_company_change() {
		const company = (this.company && this.company.get_value()) || "";
		// Ignore control bootstrap / duplicate change events so auto Scan All is not wiped.
		if (this._last_company_value === null) {
			this._last_company_value = company;
			return;
		}
		if (company === this._last_company_value) return;
		const prev = this._last_company_value;
		this._last_company_value = company;
		if (this.scan_all_company && company && company !== this.scan_all_company) {
			this.last_dashboard = {};
			this.last_scan_all = null;
			this.scan_all_job_id = null;
			this.render_dashboard({});
		}
		this._invalidate_topic_rows(__("company changed ({0} → {1})", [prev || "—", company || "—"]));
		this.render_table();
		if (company) this.scan_all({ auto: true });
	}

	_topic_unloaded_message() {
		const kpi = this._topic_dashboard_count(this.topic);
		const lines = [
			"<strong>" + __("Rows are not loaded yet.") + "</strong>",
			"<div>" + __("Click Scan to load this topic.") + "</div>",
		];
		if (kpi != null && kpi > 0) {
			lines.push(
				"<div class='hr-empty-kpi'>" +
					__("Dashboard reports {0} for this topic — the grid is empty only because rows are not loaded.", [kpi]) +
					"</div>"
			);
		} else if (this.topic === "manufacture") {
			lines.push(
				"<div class='hr-empty-kpi'>" +
					__("Scan All does not cache Manufacture rows. Click Scan to load Manufacture Valuation.") +
					"</div>"
			);
		} else if (this.last_scan_all) {
			lines.push("<div>" + __("Scan All refreshed the dashboard. Topic grids load on demand.") + "</div>");
		}
		return lines.join("");
	}

	_access_from_boot() {
		const roles = frappe.user_roles || [];
		const user = (frappe.session && frappe.session.user) || "";
		let level = 1;
		if (user === "Administrator" || roles.includes("Administrator")) level = 3;
		else if (roles.includes("System Manager")) level = 2;
		return { level, can_repair: level >= 2, can_admin: level >= 3 };
	}

	render() {
		this.$body.empty();
		this.$shell = $('<div class="hr-shell">').appendTo(this.$body);
		this.$sticky = $('<div class="hr-sticky">').appendTo(this.$shell);
		this.$tabs = $('<div class="hr-tabs">').appendTo(this.$sticky);
		TOPICS.forEach((t) => {
			const $b = $('<button type="button" class="btn btn-xs hr-tab">')
				.attr("data-topic", t.id)
				.attr("title", t.section || t.title)
				.text(t.title)
				.appendTo(this.$tabs);
			if (t.id === this.topic) $b.addClass("btn-primary");
			else $b.addClass("btn-default");
			$b.on("click", () => this.switch_topic(t.id));
		});
		this.$dashboard = $('<div class="hr-dashboard" data-role="dashboard">').appendTo(this.$sticky);
		this.render_dashboard({});
		this.$title = $('<h4 class="hr-section-title">').appendTo(this.$sticky);
		this.$toolbar = $('<div class="hr-toolbar">').appendTo(this.$sticky);
		this._scope_control("company", "Link", "Company", "Company");
		this._scope_control("item", "Link", "Item", "Item");
		this._scope_control("warehouse", "Link", "Warehouse", "Warehouse");
		this._scope_control("batch", "Link", "Batch", "Batch");
		this._scope_control("work_order", "Link", "Work Order", "Work Order");
		this._scope_control("voucher", "Link", "Stock Entry", "Voucher");
		this._scope_control("serial_and_batch_bundle", "Link", "Serial and Batch Bundle", "Serial & Batch Bundle");
		this.from_date = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: { fieldtype: "Date", label: __("From Date") },
			render_input: true,
		});
		this.to_date = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: { fieldtype: "Date", label: __("To Date") },
			render_input: true,
		});
		this.repair_class = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Select",
				label: __("Repair Class"),
				options: ["", "I4_LEFTOVER_REPAIR", "ZERO_RATE", "WRONG_RATE", "POSTING_ORDER", "SLE_BIN", "GL", "FAILED_RIV"],
			},
			render_input: true,
		});
		this.planner_status = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Select",
				label: __("Planner Status"),
				options: ["", "READY", "READY_I4", "WAITING_I4", "WAITING_PATIENT_ZERO", "BLOCKED", "NO_REPAIR_PATH", "MANUAL"],
			},
			render_input: true,
		});
		this.patient_zero = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: { fieldtype: "Data", label: __("Patient Zero") },
			render_input: true,
		});
		const $actions = $('<div class="hr-actions">').appendTo(this.$toolbar);
		const g1 = $('<div class="hr-action-group" data-group="scan">').appendTo($actions);
		this.btn_scan = this._btn(g1, "scan", __("Scan"), () => this.scan(), "btn-default", __("Scan this topic (S)"));
		this.btn_scan_all = this._btn(g1, "scan-all", __("Scan All"), () => this.scan_all(), "btn-default", __("Scan every topic into the dashboard (Shift+A)"));
		this.btn_dry = this._btn(g1, "dry-run", __("Dry Run"), () => this.dry_run(), "btn-primary", __("Preview writes. Never executes (D)"));
		this.btn_clear_filters = this._btn(g1, "clear-filters", __("Clear Filters"), () => this.clear_filters(), "btn-default", __("Clear scope filters"));
		this.btn_save_preset = this._btn(g1, "save-preset", __("Save Preset"), () => this.save_filter_preset(), "btn-default", __("Save current filters"));
		this.btn_load_preset = this._btn(g1, "load-preset", __("Load Preset"), () => this.load_filter_preset(), "btn-default", __("Load saved filters"));
		if (this.access.can_repair) {
			const g2 = $('<div class="hr-action-group" data-group="repair">').appendTo($actions);
			this.btn_repair = this._btn(g2, "repair", __("Repair Selected"), () => this.repair_bulk("selected"), "btn-danger", __("Repair checked EXACT rows"));
			this.btn_repair_i4 = this._btn(g2, "repair-i4", __("Repair I4 Patient Zero"), () => this.repair_i4_patient_zero(), "btn-danger", __("Identity-scoped I4 leftover repair from Patient Zero"));
			this.btn_repair_filter = this._btn(g2, "repair-filter", __("Repair Current Filter"), () => this.repair_bulk("filter"), "btn-danger", __("Repair EXACT rows matching search and column filters"));
			this.btn_repair_page = this._btn(g2, "repair-page", __("Repair Current Page"), () => this.repair_bulk("page"), "btn-danger", __("Repair visible EXACT rows"));
			this.btn_repair_scope = this._btn(g2, "repair-scope", __("Repair Current Scope"), () => this.repair_bulk("scope"), "btn-danger", __("Repair every EXACT row in this topic scan"));
			this.btn_repair_chain = this._btn(g2, "repair-chain", __("Repair Dependency Chain"), () => this.repair_dependency_chain(), "btn-danger", __("Repair only the READY root. Never warehouse/item/company-wide"));
			this.btn_replay_ds = this._btn(g2, "replay-downstream", __("Replay Downstream"), () => this.replay_downstream(), "btn-info", __("Identity-scoped downstream replay"));
			this.btn_rebuild_docs = this._btn(g2, "rebuild-docs", __("Rebuild Affected Documents"), () => this.rebuild_affected_documents(), "btn-info", __("Identity-scoped rebuild"));
			this.btn_repost = this._btn(g2, "repost", __("Repost Selected"), () => this.repost_selected(), "btn-warning", __("One Item+Warehouse RIV. Never global"));
			this._lock_writes(true);
		}
		const g3 = $('<div class="hr-action-group" data-group="inspect">').appendTo($actions);
		this.btn_integrity = this._btn(g3, "integrity", __("Integrity Check"), () => this.integrity(), "btn-default", __("Read-only chain check (I)"));
		this.btn_graph = this._btn(g3, "graph", __("Graph"), () => this.show_graph(), "btn-default", __("Dependency graph (G)"));
		this.btn_root_explorer = this._btn(g3, "root-explorer", __("Root Cause Explorer"), () => this.show_root_cause_explorer(), "btn-primary", __("Healthy → Patient Zero → Replay chain → Current voucher"));
		this.btn_health = this._btn(g3, "identity-health", __("Identity Health"), () => this.show_identity_health(), "btn-default", __("Posting / Wrong Rate / I4 / Bin / GL health panel"));
		this.btn_master_plan = this._btn(g3, "master-plan", __("Master Repair Plan"), () => this.show_master_repair_plan(), "btn-default", __("Database-wide repair roadmap"));
		this.btn_campaign_wizard = this._btn(
			g3,
			"campaign-wizard",
			__("Campaign Wizard"),
			() => this.show_campaign_wizard(),
			"btn-primary",
			__("Preview repair campaigns by class — no bulk apply")
		);
		this.btn_warehouse_plan = this._btn(
			g3,
			"warehouse-plan",
			__("Warehouse Plan"),
			() => this.show_warehouse_plan(),
			"btn-primary",
			__("Plan warehouse-scoped replay for selected posting-order row")
		);
		this.btn_cluster_explorer = this._btn(
			g3,
			"cluster-explorer",
			__("Cluster Explorer"),
			() => this.show_cluster_explorer(),
			"btn-default",
			__("Zero Rate independent SAFE clusters")
		);
		this.btn_validate_dashboard = this._btn(
			g3,
			"validate-dashboard",
			__("Validate Dashboard"),
			() => this.validate_dashboard(),
			"btn-warning",
			__("Compare Dashboard ↔ Planner ↔ Scan ↔ Queue ↔ SQL for every KPI")
		);
		this.btn_rebuild_metrics = this._btn(
			g3,
			"rebuild-metrics",
			__("Rebuild Metrics"),
			() => this.rebuild_metrics(),
			"btn-default",
			__("Force fresh Scan All and refresh dashboard chips")
		);
		this.btn_goto_root = this._btn(g3, "goto-root", __("Go To Root Cause"), () => this.go_to_root_cause(), "btn-primary", __("Filter, select, and show the patient-zero repair plan"));
		this.btn_history = this._btn(g3, "history", __("Repair History"), () => this.show_history(), "btn-default", __("Previous repair runs"));
		if (this.access.can_admin) {
			const $adv = $('<label class="hr-advanced">').appendTo(g3);
			this.$advanced = $('<input type="checkbox" data-role="advanced">').appendTo($adv);
			$adv.append(document.createTextNode(" " + __("Advanced Mode")));
			$adv.attr("title", __("Administrator experimental tools"));
			this.$advanced.on("change", () => {
				this.advanced = this.$advanced.prop("checked");
				this.$admin_group && this.$admin_group.toggle(this.advanced);
			});
			this.$admin_group = $('<div class="hr-action-group" data-group="admin">').appendTo($actions);
			this.btn_resume = this._btn(this.$admin_group, "resume", __("Resume"), () => this.resume());
			this.btn_cancel = this._btn(this.$admin_group, "cancel", __("Cancel"), () => { this.cancelled = true; });
			this.btn_rollback = this._btn(this.$admin_group, "rollback", __("Rollback"), () => this.rollback_last());
			this.btn_benchmark = this._btn(this.$admin_group, "benchmark", __("Benchmark"), () => this.run_benchmark());
			this.$admin_group.toggle(false);
		}
		this.$tools = $('<div class="hr-grid-tools">').appendTo(this.$shell);
		this.$search = $('<input class="form-control input-sm hr-search" data-role="search" placeholder="Search all columns  (/ )">').appendTo(this.$tools);
		this.$search.on("input", () => this.render_table());
		this.$voucher_quick = $('<input class="form-control input-sm hr-voucher-quick" data-role="voucher-quick" placeholder="Voucher quick search (Enter)">').appendTo(this.$tools);
		this.$voucher_quick.on("keydown", (e) => {
			if (e.key === "Enter") {
				e.preventDefault();
				const v = String(this.$voucher_quick.val() || "").trim();
				if (!v) return;
				if (this.voucher) this.voucher.set_value(v);
				this.scan({ select_voucher: v });
			}
		});
		["Select All", "Select None", "Select EXACT", "Select Repairable", "Select Visible Rows", "Select Current Page"].forEach((label) => {
			this._btn(this.$tools, label.toLowerCase().replace(/\s+/g, "-"), __(label), () => this.select_by(label));
		});
		this._btn(this.$tools, "columns", __("Columns"), () => this.toggle_columns());
		this._btn(this.$tools, "export-csv", __("Export CSV"), () => this.export_grid("csv"));
		this._btn(this.$tools, "export-excel", __("Export Excel"), () => this.export_grid("xlsx"));
		this._btn(this.$tools, "copy-selected", __("Copy selected"), () => this.copy_selected());
		this.$kpi_filter = $('<div class="hr-kpi-filter" data-role="kpi-filter" style="display:none">').appendTo(this.$shell);
		this.$columns = $('<div class="hr-columns" data-role="columns" style="display:none">').appendTo(this.$shell);
		this.$wizard = $('<div class="hr-wizard" data-role="wizard">').appendTo(this.$shell);
		this.render_wizard_step(1);
		this.$health = $('<div class="hr-health" data-role="identity-health">').appendTo(this.$shell);
		this.$progress = $('<div class="hr-progress"><div class="hr-progress-bar"></div></div>').appendTo(this.$shell);
		this.$eta = $('<div class="text-muted hr-eta" data-role="eta">').appendTo(this.$shell);
		this.$table = $('<div class="hr-table-wrap">').appendTo(this.$shell);
		this.$recon = $('<div class="hr-recon" data-role="reconstruction">').appendTo(this.$shell);
		this.$deps = $('<div class="hr-deps" data-role="dependency-resolution">').appendTo(this.$shell);
		this.$root_explorer = $('<div class="hr-root-explorer" data-role="root-cause-explorer">').appendTo(this.$shell);
		this.$graph = $('<div class="hr-graph" data-role="graph">').appendTo(this.$shell);
		this.$preview = $('<pre class="hr-preview" data-role="preview">').appendTo(this.$shell);
		this.switch_topic(this.topic);
	}

	_scope_control(name, fieldtype, options, label) {
		this[name] = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: { fieldtype, options, label: __(label) },
			render_input: true,
		});
		if (name === "company") {
			const ctrl = this.company;
			const prev = ctrl.df.onchange;
			ctrl.df.onchange = () => {
				if (typeof prev === "function") prev();
				this._on_company_change();
			};
		}
	}

	_lock_writes(lock) {
		["btn_repair", "btn_repair_filter", "btn_repair_page", "btn_repair_scope", "btn_repair_i4"].forEach((k) => {
			this[k] && this[k].prop("disabled", !!lock);
		});
		if (this.btn_repair_chain && lock) this.btn_repair_chain.prop("disabled", true);
	}

	_bind_keys() {
		$(document).off("keydown.hr-mvr").on("keydown.hr-mvr", (e) => {
			const typing = $(e.target).is("input, textarea, select, [contenteditable=true]");
			if (typing) {
				if (e.key === "Escape") e.target.blur();
				return;
			}
			if (e.key === "/") {
				e.preventDefault();
				this.$search.trigger("focus");
			} else if (e.key === "s" || e.key === "S") this.scan();
			else if (e.key === "a" || e.key === "A") this.scan_all();
			else if (e.key === "d" || e.key === "D") this.dry_run();
			else if (e.key === "g" || e.key === "G") this.show_graph();
			else if (e.key === "i" || e.key === "I") this.integrity();
		});
	}

	_btn($parent, action, label, fn, extra = "btn-default", title) {
		return $(`<button type="button" class="btn ${extra} btn-sm" data-action="${action}">`)
			.text(label)
			.attr("title", title || label)
			.appendTo($parent)
			.on("click", fn);
	}

	switch_topic(id) {
		this._save_current_topic_state();
		this.topic = id;
		this.dry_run_done = false;
		this.impact_done = false;
		this.load_layout();
		if (this.btn_repair) this._lock_writes(true);
		this.$tabs.find(".hr-tab").each((_, el) => {
			const on = el.getAttribute("data-topic") === id;
			$(el).toggleClass("btn-primary", on).toggleClass("btn-default", !on);
		});
		const meta = TOPICS.find((t) => t.id === id);
		this.$title.text(meta.section || meta.title);
		const st = this.topic_state[id] || {};
		if (st.loaded) {
			this._restore_topic_state(id);
			this.$preview.text(__("Showing cached scan for this topic. Re-run Scan if filters changed."));
		} else {
			this.rows = [];
			this.$preview.text(
				__("Scan All fills the dashboard. Rows for this topic are not loaded yet — click Scan. No writes until Repair after Dry Run, Impact, and DATABASE BACKUP REQUIRED.")
			);
		}
		this.render_table();
	}

	filters() {
		return {
			company: this.company.get_value(),
			item_code: this.item && this.item.get_value(),
			warehouse: this.warehouse && this.warehouse.get_value(),
			batch: this.batch && this.batch.get_value(),
			work_order: this.work_order && this.work_order.get_value(),
			voucher: this.voucher && this.voucher.get_value(),
			serial_and_batch_bundle: this.serial_and_batch_bundle && this.serial_and_batch_bundle.get_value(),
			from_date: this.from_date.get_value(),
			to_date: this.to_date.get_value(),
			repair_class: this.repair_class && this.repair_class.get_value(),
			planner_status: this.planner_status && this.planner_status.get_value(),
			patient_zero: this.patient_zero && this.patient_zero.get_value(),
			kpi_bucket: this.kpi_bucket || null,
			include_likely: 1,
		};
	}

	_clear_kpi_filter() {
		this.kpi_bucket = null;
		this.active_kpi_label = null;
		if (this.$kpi_filter) this.$kpi_filter.hide().empty();
	}

	_set_kpi_filter(label, bucket) {
		this.active_kpi_label = label;
		this.kpi_bucket = bucket;
		if (!this.$kpi_filter) return;
		this.$kpi_filter
			.show()
			.empty()
			.append(
				$("<strong>").text(__("Active KPI filter: {0}", [label || bucket || "—"]))
			)
			.append(
				$("<span class='text-muted'>").text(" · " + __("bucket={0}", [bucket || "—"]))
			)
			.append(
				$('<button type="button" class="btn btn-xs btn-default">')
					.text(__("Clear KPI filter"))
					.on("click", () => {
						this._clear_kpi_filter();
						if (this.planner_status) this.planner_status.set_value("");
						this.$search && this.$search.val("");
						this.scan();
					})
			);
	}

	_kpi_bucket_for_label(label) {
		const map = {
			"Wrong Rate": "wrong_rate_active",
			"Wrong Rate READY": "wrong_rate_ready",
			"Wrong Rate WAITING": "wrong_rate_waiting",
			"Wrong Rate MANUAL": "wrong_rate_manual",
			"Wrong Rate Complete": "wrong_rate_complete",
			"READY_I4": "i4_ready",
			"WAITING_I4": "i4_waiting",
			"MANUAL_I4": "i4_manual",
			"REPLAY_REQUIRED_I4": "i4_replay",
			"I4 Leftover": "i4_all",
			"GL READY": "gl_ready",
			"GL WAITING": "gl_waiting",
			"GL MANUAL": "gl_manual",
			"Broken GL": "gl_all",
			"RIV SAFE": "riv_safe",
			"RIV WAITING": "riv_waiting",
			"RIV UNSAFE": "riv_unsafe",
			"Failed RIV": "riv_all",
		};
		return map[label] || null;
	}

	clear_filters() {
		["company", "item", "warehouse", "batch", "work_order", "voucher", "serial_and_batch_bundle", "from_date", "to_date", "repair_class", "planner_status", "patient_zero"].forEach((k) => {
			if (this[k] && this[k].set_value) this[k].set_value("");
		});
		this.$search && this.$search.val("");
		this.$voucher_quick && this.$voucher_quick.val("");
		this.col_filters = {};
		this._clear_kpi_filter();
		this._invalidate_topic_rows(__("filters cleared"));
		this.render_table();
		frappe.show_alert({ message: __("Filters cleared"), indicator: "blue" });
	}

	save_filter_preset() {
		const key = "hr_filter_preset_" + (frappe.session.user || "guest");
		localStorage.setItem(key, JSON.stringify(this.filters()));
		frappe.show_alert({ message: __("Filter preset saved"), indicator: "green" });
	}

	load_filter_preset() {
		const key = "hr_filter_preset_" + (frappe.session.user || "guest");
		let data = {};
		try {
			data = JSON.parse(localStorage.getItem(key) || "{}") || {};
		} catch (e) {
			data = {};
		}
		const map = {
			company: "company",
			item_code: "item",
			warehouse: "warehouse",
			batch: "batch",
			work_order: "work_order",
			voucher: "voucher",
			serial_and_batch_bundle: "serial_and_batch_bundle",
			from_date: "from_date",
			to_date: "to_date",
			repair_class: "repair_class",
			planner_status: "planner_status",
			patient_zero: "patient_zero",
		};
		Object.keys(map).forEach((fk) => {
			const ctrl = this[map[fk]];
			if (ctrl && ctrl.set_value && data[fk] != null) ctrl.set_value(data[fk]);
		});
		this._invalidate_topic_rows(__("filter preset loaded"));
		this.render_table();
		frappe.show_alert({ message: __("Filter preset loaded"), indicator: "green" });
	}

	scope_identity() {
		const f = this.filters();
		const row = this.selected_rows()[0] || {};
		return {
			item_code: f.item_code || row.item || row.item_code,
			warehouse: f.warehouse || row.warehouse,
			batch: f.batch || row.batch,
			work_order: f.work_order || row.work_order,
			voucher: f.voucher || row.voucher || row.outbound_document || row.inbound_document,
			from_date: f.from_date || row.posting_date,
			to_date: f.to_date,
			company: f.company,
		};
	}

	selected_rows() {
		const selected = [];
		this.$table.find("input[type=checkbox][data-idx]:checked").each((_, el) => {
			const idx = parseInt(el.getAttribute("data-idx"), 10);
			if (this.rows[idx]) selected.push(this.rows[idx]);
		});
		return selected;
	}

	scan(opts) {
		const f = this.filters();
		const map = {
			posting: [`${this.ppo}.scan_posting_order_anomalies`, f],
			wrong: [`${this.api}.scan_wrong_rates_api`, f],
			zero: [`${this.api}.scan_zero_rates`, f],
			manufacture: [`${this.api}.scan_manufacture`, f],
			sle: [`${this.api}.scan_sle_bin_api`, f],
			gl: [`${this.api}.scan_gl_api`, f],
			riv: [`${this.api}.scan_failed_riv_api`, f],
		};
		const [method, args] = map[this.topic];
		this.render_wizard_step(2);
		this.start_progress(__("Scanning..."));
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				this.end_progress();
				if (this.cancelled) return;
				const msg = r.message || {};
				const rows = msg.rows || msg || [];
				this._mark_topic_loaded(Array.isArray(rows) ? rows : []);
				this.dry_run_done = false;
				this.impact_done = false;
				if (this.btn_repair) this._lock_writes(true);
				this.render_table();
				const dashN = this._topic_dashboard_count(this.topic);
				let note = __("Scan complete. Run Dry Run before repairing.");
				if (this.kpi_bucket) {
					note =
						__("KPI filter {0} (bucket={1}) → {2} row(s).", [
							this.active_kpi_label || "",
							this.kpi_bucket,
							this.rows.length,
						]) +
						" " +
						__("Only rows in this bucket are shown.");
					const leaked = (this.rows || []).filter((r) => {
						if (this.kpi_bucket === "wrong_rate_manual") {
							return String(r.planner_status || "") === "RATE_REPAIR_COMPLETE";
						}
						return false;
					});
					if (leaked.length) {
						note += " " + __("INVARIANT FAIL: {0} COMPLETE rows leaked into MANUAL.", [leaked.length]);
					}
				} else if (dashN != null && dashN > 0 && this.rows.length === 0) {
					note =
						__("Scan returned 0 rows while dashboard reports {0}. Active filters may narrow the topic dataset — clear filters or document the mismatch.", [dashN]);
				} else if (dashN != null && this.rows.length !== dashN && this.topic !== "sle") {
					note =
						__("Scan complete ({0} rows). Dashboard KPI for this topic is {1} (same company snapshot; client filters may differ).", [
							this.rows.length,
							dashN,
						]);
				}
				this.$preview.text(note);
				this._maybe_redirect_downstream(opts);
				if (opts && opts.select_voucher) this._select_and_show(opts.select_voucher);
			},
			error: (err) => {
				this.end_progress();
				this.$preview.text(__("Scan failed: {0}", [(err && err.message) || __("Request error")]));
			},
		});
	}

	_maybe_redirect_downstream(opts) {
		const voucher = (opts && opts.select_voucher) || (this.voucher && this.voucher.get_value());
		if (!voucher || this.topic !== "sle") return;
		const hit = (this.rows || []).find((r) => String(r.voucher || "") === String(voucher));
		if (!hit) return;
		const pz = hit.root_patient_zero || hit.root_blocker || (hit.patient_zero && hit.patient_zero.voucher_no);
		if (pz && String(pz) !== String(voucher)) {
			frappe.msgprint({
				title: __("Downstream voucher selected"),
				message: __(
					"You selected a downstream voucher.<br><br>Repair must begin from Patient Zero.<br><br><b>Patient Zero:</b> {0}<br><br>Repair must never begin in the middle of a chain.",
					[frappe.utils.escape_html(String(pz))]
				),
				indicator: "orange",
			});
			if (this.patient_zero) this.patient_zero.set_value(pz);
		}
	}

	dry_run() {
		const rows = this.selected_rows();
		const f = this.filters();
		const i4Rows = rows.filter((r) => r.repair_class === "I4_LEFTOVER_REPAIR" || r.planner_status === "READY_I4" || r.topic === "I4_LEFTOVER");
		let method;
		let args;
		if (this.topic === "sle" && (i4Rows.length || (this.repair_class && this.repair_class.get_value() === "I4_LEFTOVER_REPAIR"))) {
			method = `${this.api}.dry_run_i4_api`;
			args = { rows: i4Rows.length ? i4Rows : rows };
		} else {
			const map = {
				posting: [`${this.ppo}.dry_run_posting_order_repair`, rows.length ? { rows } : f],
				wrong: [
					`${this.api}.repair_wrong_rates_selected`,
					{ rows: rows.length ? rows : [], dry_run: 1 },
				],
				zero: [`${this.api}.dry_run_zero_rates`, rows.length ? { rows } : f],
				manufacture: [`${this.api}.dry_run_manufacture`, rows.length ? { rows } : f],
				sle: [`${this.api}.scan_sle_bin_api`, f],
				gl: [`${this.api}.scan_gl_api`, f],
				riv: [`${this.api}.scan_failed_riv_api`, f],
			};
			[method, args] = map[this.topic];
			if (this.topic === "wrong" && !(args.rows && args.rows.length)) {
				frappe.msgprint(__("Select Wrong Rate row(s) for Dry Run. Refusing to dry-run the entire scan."));
				return;
			}
		}
		this.render_wizard_step(2);
		this.start_progress(__("Dry Run..."));
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				this.end_progress();
				this.dry_run_done = true;
				const msg = r.message || {};
				if (msg.rows && Array.isArray(msg.rows)) this._mark_topic_loaded(msg.rows);
				this.render_table();
				this.$preview.text(this.format_preview(msg));
				this._complete_impact(msg);
				this.render_wizard_step(3);
			},
		});
	}

	_complete_impact(scan_msg) {
		const rows = this.selected_rows();
		const payload = rows.length ? rows : (this.rows || []).slice(0, 1);
		if (!payload.length) {
			this.impact_done = false;
			this.last_impact = {
				aborted: true,
				estimated_sql_updates: 0,
				planner_status: "NO_REPAIR_PATH",
				skip_reason: __("No rows. Repair Selected will not write."),
				executable: false,
			};
			this.$preview.text(__("No rows. Repair Selected is disabled."));
			if (this.btn_repair) this._lock_writes(true);
			return;
		}
		frappe.call({
			method: `${this.api}.repair_planner_api`,
			args: { rows: payload },
			callback: (ir) => {
				const impact = ir.message || {};
				this.last_impact = impact;
				const executable = this._impact_executable(impact);
				this.impact_done = executable;
				this.$preview.text((this.format_preview(scan_msg || {}) + "\n\n" + (impact.preview_text || "")).trim());
				this.render_deps(payload[0], impact);
				if (this.btn_repair && this.access.can_repair && this.dry_run_done && executable) {
					this._lock_writes(false);
				} else if (this.btn_repair) {
					this._lock_writes(true);
				}
				this._toggle_chain_button(payload[0], impact);
				if (!executable) {
					frappe.show_alert({
						message: impact.required_action || impact.skip_reason || impact.reason || __("Repair Selected disabled: status is not READY or SQL updates = 0."),
						indicator: "orange",
					});
				}
			},
			error: () => {
				this.impact_done = false;
				if (this.btn_repair) this._lock_writes(true);
			},
		});
	}

	_is_ready(row) {
		return !!(row && /^READY/.test(String(row.planner_status || "")) && Number(row.sql_updates || 0) > 0);
	}

	_impact_executable(impact) {
		const sql = Number(impact && (impact.estimated_sql_updates || impact.sql_updates) || 0);
		return !!(
			impact &&
			impact.executable !== false &&
			!impact.aborted &&
			sql > 0 &&
			/^READY/.test(String(impact.planner_status || ""))
		);
	}

	repair_bulk(kind) {
		if (!this.dry_run_done || !this.impact_done) {
			frappe.msgprint(__("No repair without Dry Run and Impact Analysis."));
			return;
		}
		let rows;
		if (kind === "filter" || kind === "page") rows = this.visible_rows || [];
		else if (kind === "scope") rows = this.rows || [];
		else rows = this.selected_rows();
		rows = (rows || []).filter((r) => this._is_ready(r));
		if (!rows.length && this.topic !== "gl" && this.topic !== "riv") {
			frappe.msgprint(__("No READY rows in this {0}. Nothing executed.", [kind || "selection"]));
			return;
		}
		frappe.call({
			method: `${this.api}.repair_planner_api`,
			args: { rows: rows.length ? rows : this.selected_rows() },
			freeze: true,
			callback: (ir) => {
				const impact = ir.message || {};
				this.last_impact = impact;
				this.$preview.text(impact.preview_text || JSON.stringify(impact, null, 2));
				if (!this._impact_executable(impact)) {
					const reason =
						impact.skip_reason ||
						impact.reason ||
						(impact.abort_reasons && impact.abort_reasons[0] && impact.abort_reasons[0].reason) ||
						__("SQL updates planned: 0 or status is not READY. Repair Selected is disabled. Nothing executed.");
					frappe.msgprint({ title: __("Repair skipped"), message: reason, indicator: "orange" });
					if (this.btn_repair) this._lock_writes(true);
					this.impact_done = false;
					return;
				}
				const warn = __("DATABASE BACKUP REQUIRED. Apply this identity-scoped repair?");
				const chain = (impact.chain_preview || []).join("\n↓\n");
				frappe.confirm(warn + "\n\n" + (chain ? chain + "\n\n" : "") + (impact.preview_text || ""), () => this._execute_repair(rows));
			},
		});
	}

	repair_selected() {
		this.repair_bulk("selected");
	}

	_execute_repair(rows) {
		let method;
		let args;
		const i4Rows = (rows || []).filter(
			(r) => r.repair_class === "I4_LEFTOVER_REPAIR" || r.planner_status === "READY_I4" || r.topic === "I4_LEFTOVER"
		);
		if (this.topic === "posting") {
			method = `${this.ppo}.repair_posting_order_selected`;
			args = { rows, dry_run: 0, expected_signatures: rows.map((r) => r.dependency_signature) };
		} else if (this.topic === "wrong") {
			method = `${this.api}.repair_wrong_rates_selected`;
			args = { rows, dry_run: 0 };
		} else if (this.topic === "zero") {
			method = `${this.api}.repair_zero_rates_selected`;
			args = { rows, dry_run: 0 };
		} else if (this.topic === "manufacture") {
			method = `${this.api}.repair_manufacture_selected_api`;
			args = { rows, dry_run: 0 };
		} else if (this.topic === "sle" && i4Rows.length) {
			method = `${this.api}.repair_i4_patient_zero_api`;
			args = { rows: i4Rows, dry_run: 0 };
		} else if (this.topic === "gl") {
			method = `${this.api}.rebuild_gl_selected`;
			args = { vouchers: rows.map((r) => r.voucher), dry_run: 0 };
		} else if (this.topic === "riv") {
			method = `${this.api}.retry_failed_riv_selected`;
			args = { names: rows.map((r) => r.riv_name), dry_run: 0 };
		} else {
			frappe.msgprint(__("Repair Selected is not enabled for this topic. For I4 leftovers use Repair I4 Patient Zero on SLE / Bin Integrity."));
			return;
		}
		this.render_wizard_step(4);
		this.start_progress(__("Repairing... Updating SLE..."));
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				this.end_progress();
				this.render_wizard_step(7);
				this._show_repair_result(r.message || {});
				this.dry_run_done = false;
				this.impact_done = false;
				if (this.btn_repair) this._lock_writes(true);
				this.integrity();
			},
		});
	}

	repair_i4_patient_zero() {
		let rows = this.selected_rows().filter(
			(r) => r.repair_class === "I4_LEFTOVER_REPAIR" || r.planner_status === "READY_I4" || r.topic === "I4_LEFTOVER"
		);
		if (!rows.length) {
			rows = (this.rows || []).filter((r) => this._is_ready(r) && (r.repair_class === "I4_LEFTOVER_REPAIR" || r.planner_status === "READY_I4"));
		}
		if (!rows.length) {
			frappe.msgprint(__("Select READY_I4 Patient Zero row(s) on SLE / Bin Integrity first."));
			if (this.topic !== "sle") this.switch_topic("sle");
			return;
		}
		if (!this.dry_run_done || !this.impact_done) {
			frappe.msgprint(__("Run Dry Run and Impact Analysis before Repair I4 Patient Zero."));
			return;
		}
		this.render_wizard_step(1);
		frappe.confirm(
			__(
				"DATABASE BACKUP REQUIRED\n\nRepair I4 Patient Zero will:\n- Dry Run (already done)\n- Impact Analysis\n- Savepoint\n- Repair Patient Zero\n- Replay that identity only\n- Rebuild Bin\n- Rebuild affected GL\n- Integrity\n- Re-scan\n\nNever global replay. Never company replay.\n\nContinue?"
			),
			() => this._execute_repair(rows)
		);
	}

	_show_repair_result(msg) {
		this.$preview.text(JSON.stringify(msg, null, 2));
		const applied = (msg.applied || []).filter((a) => a.written !== false && a.status !== "DRY_RUN");
		const blocked = msg.blocked || [];
		const executed = msg.sql_updates_executed;
		const wrote = applied.length > 0 && executed !== 0 && !msg.aborted;
		if (!wrote) {
			const reason =
				msg.skip_reason ||
				msg.reason ||
				(blocked[0] && blocked[0].error) ||
				__("No SQL updates. Nothing was written.");
			frappe.msgprint({
				title: __("Repair skipped"),
				message: __("{0}<br><br>SQL updates executed: {1}. Savepoint created: {2}. Transaction committed: {3}.", [
					reason,
					executed == null ? 0 : executed,
					msg.savepoint_created ? __("Yes") : __("No"),
					msg.transaction_committed ? __("Yes") : __("No"),
				]),
				indicator: "orange",
			});
			return;
		}
		frappe.show_alert({
			message: __("Repair written ({0} row(s), {1} SQL updates). Run Integrity Check. Optional: Repost Selected for this identity (never global).", [
				applied.length,
				executed == null ? applied.length : executed,
			]),
			indicator: "green",
		});
	}

	repost_selected() {
		const scope = this.scope_identity();
		if (!scope.item_code && !scope.warehouse && !scope.batch && !scope.work_order && !scope.voucher) {
			frappe.msgprint(__("Repost needs Item+Warehouse, Batch, Work Order, or Voucher. Company/date alone is a global repost and is blocked."));
			return;
		}
		frappe.call({
			method: `${this.api}.preview_repost`,
			args: scope,
			callback: (r) => {
				const preview = r.message || {};
				this.$preview.text(JSON.stringify(preview, null, 2));
				if (!preview.eligible) {
					frappe.msgprint(preview.reason || __("Repost Selected blocked until the chain passes Integrity. Never global."));
					return;
				}
				frappe.confirm(__("DATABASE BACKUP REQUIRED. Queue Item+Warehouse RIV for this identity only?"), () => {
					frappe.call({
						method: `${this.api}.repost_selected_api`,
						args: { ...scope, dry_run: 0 },
						callback: (rr) => {
							this._show_write_result(rr.message || {}, __("Repost"));
							this.integrity();
						},
					});
				});
			},
		});
	}

	replay_downstream() {
		if (this.topic !== "posting") {
			frappe.msgprint(
				__("Replay Downstream is a Posting Order identity tool. Switch to Posting Order and select the repaired chain.")
			);
			return;
		}
		const rows = this.selected_rows();
		if (!rows.length) {
			frappe.msgprint(__("Select the repaired chain (item + batch). This is not a global replay."));
			return;
		}
		frappe.call({
			method: `${this.ppo}.replay_downstream`,
			args: { rows, dry_run: 1 },
			freeze: true,
			callback: (r) => {
				this.$preview.text(this.format_preview(r.message || {}));
				frappe.confirm(__("DATABASE BACKUP REQUIRED. Apply this identity-scoped downstream replay?"), () => {
					frappe.call({
						method: `${this.ppo}.replay_downstream`,
						args: { rows, dry_run: 0 },
						freeze: true,
						callback: (rr) => {
							this._show_write_result(rr.message || {}, __("Replay Downstream"));
							this.integrity();
						},
					});
				});
			},
		});
	}

	rebuild_affected_documents() {
		if (this.topic !== "posting") {
			frappe.msgprint(
				__("Rebuild Affected Documents is a Posting Order identity tool. Switch to Posting Order and select inbound/outbound rows.")
			);
			return;
		}
		const rows = this.selected_rows();
		if (!rows.length) {
			frappe.msgprint(__("Select the chain to rebuild (inbound + outbound). This is not a global rebuild."));
			return;
		}
		frappe.call({
			method: `${this.ppo}.rebuild_affected_documents`,
			args: { rows, dry_run: 1 },
			freeze: true,
			callback: (r) => {
				this.$preview.text(this.format_preview(r.message || {}));
				frappe.confirm(__("DATABASE BACKUP REQUIRED. Apply identity-scoped rebuild?"), () => {
					frappe.call({
						method: `${this.ppo}.rebuild_affected_documents`,
						args: { rows, dry_run: 0 },
						freeze: true,
						callback: (rr) => {
							this._show_write_result(rr.message || {}, __("Rebuild"));
							this.integrity();
						},
					});
				});
			},
		});
	}

	_show_write_result(msg, title) {
		this.$preview.text(typeof msg === "string" ? msg : JSON.stringify(msg, null, 2));
		if (msg && (msg.aborted || msg.ok === false || msg.eligible === false)) {
			frappe.msgprint({
				title: __("{0} skipped", [title || __("Write")]),
				message: msg.reason || msg.skip_reason || msg.error || __("Nothing was written."),
				indicator: "orange",
			});
			return;
		}
		frappe.msgprint({
			title: __("{0} complete", [title || __("Write")]),
			message: __("Identity-scoped write finished. Integrity Check is running. Optional next step: Repost Selected for this Item+Warehouse (never global)."),
			indicator: "green",
		});
	}

	integrity() {
		const rows = this.selected_rows();
		const vouchers = [];
		rows.forEach((r) => {
			if (r.inbound_document) vouchers.push(r.inbound_document);
			if (r.outbound_document) vouchers.push(r.outbound_document);
			if (r.voucher) vouchers.push(r.voucher);
		});
		const first = rows[0] || {};
		frappe.call({
			method: `${this.api}.integrity_api`,
			args: {
				vouchers,
				item_code: first.item || first.item_code,
				warehouse: first.warehouse,
			},
			callback: (r) => this.$preview.text(JSON.stringify(r.message || {}, null, 2)),
		});
	}

	resume() {
		frappe.call({
			method: `${this.api}.resume_last_run`,
			callback: (r) => this.$preview.text(JSON.stringify(r.message || {}, null, 2)),
		});
	}

	scan_all(opts) {
		const auto = !!(opts && opts.auto);
		this.start_progress();
		if (auto) this.$eta.text(__("Scan All… queued on long worker"));
		else this.$eta.text(__("Scan All… queued"));
		// Async path: enqueue on long queue and poll. Avoids HTTP / proxy timeouts
		// on production-size Scan All (often 20–60s+ synchronously).
		frappe.call({
			method: `${this.api}.start_scan_all_job`,
			args: { company: this.company.get_value() },
			freeze: false,
			callback: (r) => {
				const start = r.message || {};
				const jobId = start.job_id;
				if (!jobId) {
					this.end_progress();
					this.$preview.text(__("Scan All failed to enqueue. No writes were attempted."));
					return;
				}
				if (start.deduplicated) {
					this.$eta.text(__("Scan All already running — attaching to job {0}", [jobId]));
				} else {
					this.$eta.text(__("Scan All queued ({0})", [jobId]));
				}
				this._poll_scan_all_job(jobId, auto, 0);
			},
			error: () => {
				this.end_progress();
				this.$preview.text(__("Scan All failed to enqueue. Retry Scan All. No writes were attempted."));
			},
		});
	}

	_poll_scan_all_job(jobId, auto, attempt) {
		const maxAttempts = 900; // ~30 min at 2s
		frappe.call({
			method: `${this.api}.get_scan_all_job`,
			args: { job_id: jobId },
			freeze: false,
			callback: (r) => {
				const job = r.message || {};
				const status = String(job.status || "");
				const phase = job.phase || status;
				const progress = job.progress != null ? job.progress : "";
				this.$eta.text(
					__("Scan All {0} — {1}{2}", [
						status,
						phase,
						progress !== "" ? ` (${progress}%)` : "",
					])
				);
				if (status === "COMPLETED") {
					this.end_progress();
					const msg = job.result || {};
					this.dry_run_done = false;
					this.impact_done = false;
					this._lock_writes(true);
					this.scan_all_job_id = jobId;
					this.scan_all_company = this.company && this.company.get_value();
					this.last_scan_all = msg;
					this.last_dashboard = msg.dashboard || {};
					this._invalidate_topic_rows(null);
					this.render_dashboard(this.last_dashboard);
					const unloadNote =
						__("Scan All complete. Dashboard KPIs are from this snapshot.") +
						"\n" +
						__("Topic grids are not loaded yet — click Scan on each tab (or a KPI chip) to bind the same dataset.") +
						"\n" +
						this.format_preview(msg);
					this.$preview.text(unloadNote);
					// Auto-load the active topic so the visible grid never looks like "no data"
					// while its dashboard KPI is non-zero.
					this.scan({ after_scan_all: true });
					return;
				}
				if (status === "FAILED" || status === "CANCELLED") {
					this.end_progress();
					this.$preview.text(
						__("Scan All failed: {0}", [job.error || status]) +
							" " +
							__("No writes were attempted.")
					);
					return;
				}
				if (attempt >= maxAttempts) {
					this.end_progress();
					this.$preview.text(__("Scan All polling timed out. Check long worker; job {0}.", [jobId]));
					return;
				}
				setTimeout(() => this._poll_scan_all_job(jobId, auto, attempt + 1), 2000);
			},
			error: () => {
				if (attempt >= 5) {
					this.end_progress();
					this.$preview.text(__("Scan All status poll failed. Job {0}.", [jobId]));
					return;
				}
				setTimeout(() => this._poll_scan_all_job(jobId, auto, attempt + 1), 2000);
			},
		});
	}

	render_dashboard(dash) {
		this.$dashboard.empty();
		KPI_ORDER.forEach((label) => {
			const n = dash && dash[label] != null ? dash[label] : "—";
			const $chip = $('<button type="button" class="hr-kpi">')
				.attr("title", __("Open this problem in the grid"))
				.append($('<span class="hr-kpi-label">').text(label))
				.append($('<b class="hr-kpi-value">').text(n));
			if (label === "Integrity Score") $chip.addClass("hr-kpi-score");
			else if (typeof n === "number" && n > 0) $chip.addClass("hr-kpi-alert");
			$chip.on("click", () => this._open_kpi(label));
			this.$dashboard.append($chip);
		});
	}

	_open_kpi(label) {
		const mapped = KPI_TOPIC[label];
		const bucket = this._kpi_bucket_for_label(label);
		if (!mapped && !bucket) {
			if (/Replay/i.test(label)) {
				this.switch_topic("riv");
				this.scan();
			}
			return;
		}
		// Exact KPI contract: clear incompatible client search / prior planner chips.
		this.$search && this.$search.val("");
		this.col_filters = {};
		if (this.planner_status) this.planner_status.set_value("");
		if (this.patient_zero) this.patient_zero.set_value("");
		if (!(bucket && String(bucket).startsWith("i4")) && this.repair_class) {
			this.repair_class.set_value("");
		}
		const topic = mapped || KPI_TOPIC[label] || this.topic;
		if (topic && this.topic !== topic) this.switch_topic(topic);
		else if (mapped) this.switch_topic(mapped);

		if (bucket && String(bucket).startsWith("i4")) {
			if (this.repair_class) this.repair_class.set_value("I4_LEFTOVER_REPAIR");
		}
		if (bucket) this._set_kpi_filter(label, bucket);
		else this._clear_kpi_filter();
		this.scan({ kpi_label: label });
	}

	select_by(mode) {
		this.$table.find("input[type=checkbox][data-idx]").each((_, el) => {
			const idx = parseInt(el.getAttribute("data-idx"), 10);
			const row = this.rows[idx] || {};
			let on = false;
			if (mode === "Select All") on = true;
			else if (mode === "Select None" || mode === "Unselect All") on = false;
			else if (mode === "Select EXACT") on = this._is_ready(row);
			else if (mode === "Select Repairable") on = this._is_ready(row);
			else if (mode === "Select Visible Rows" || mode === "Select Current Page") on = true;
			el.checked = on;
		});
	}

	export_grid(kind) {
		const cols = this.columns_for_topic();
		const headers = cols.slice(1);
		const body = (this.rows || []).map((row) => this.cells_for_row(row));
		if (kind === "xlsx") {
			frappe.call({
				method: `${this.api}.export_xlsx_api`,
				args: { topic: this.topic, headers, rows: body },
				callback: (r) => {
					const msg = r.message || {};
					if (!msg.filedata) return;
					const bin = atob(msg.filedata);
					const bytes = new Uint8Array(bin.length);
					for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
					const blob = new Blob([bytes], { type: msg.content_type });
					const a = document.createElement("a");
					a.href = URL.createObjectURL(blob);
					a.download = msg.filename || `historical-repair-${this.topic}.xlsx`;
					a.click();
				},
			});
			return;
		}
		const lines = [headers.join(",")];
		body.forEach((cells) => {
			lines.push(cells.map((v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`).join(","));
		});
		const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = `historical-repair-${this.topic}.csv`;
		a.click();
	}

	copy_selected() {
		const text = this.selected_rows()
			.map((r) => this.cells_for_row(r).join("\t"))
			.join("\n");
		if (navigator.clipboard) navigator.clipboard.writeText(text);
		else frappe.msgprint(text);
	}

	show_graph() {
		const row = this.selected_rows()[0] || this.rows[0] || {};
		const voucher = row.voucher || row.outbound_document || row.inbound_document;
		if (!(row.item || row.item_code || row.batch || row.work_order || voucher)) {
			this.$graph.empty();
			this.$preview.text(__("Graph requires a selected item, batch, work order, or voucher."));
			return;
		}
		frappe.call({
			method: `${this.api}.repair_graph_api`,
			args: {
				item: row.item || row.item_code,
				batch: row.batch,
				work_order: row.work_order,
				voucher: row.voucher || row.outbound_document || row.inbound_document,
				warehouse: row.warehouse,
				row,
			},
			callback: (r) => {
				const g = r.message || {};
				const $box = this.$graph.empty();
				(g.nodes || []).forEach((n, i) => {
					if (i) $box.append($('<div class="hr-graph-edge">').text("↓"));
					const $a = $('<a class="hr-link hr-graph-node">').text(n.voucher);
					$a.attr(
						"title",
						[
							n.purpose,
							n.item,
							n.warehouse,
							n.batch,
							`qty ${n.qty}`,
							`rate ${n.rate}`,
							`order ${n.replay_order}`,
							`depth ${n.replay_depth}`,
							n.planner_status || n.repair_status,
							n.blocker || n.reason,
						]
							.filter(Boolean)
							.join(" · ")
					);
					$a.on("click", () => frappe.set_route("Form", "Stock Entry", n.voucher));
					const $meta = $("<div class='hr-graph-meta'>").text(
						`${n.purpose || ""} · ${n.item || ""} · ${n.warehouse || ""} · ${n.batch || ""} · qty ${n.qty ?? ""} · rate ${n.rate ?? ""} · #${n.replay_order ?? ""} · depth ${n.replay_depth ?? ""} · ${n.planner_status || n.repair_status || n.status || ""} · ${n.blocker || n.reason || ""}`
					);
					$box.append($("<div>").append($a).append($meta));
				});
				this.$preview.text(JSON.stringify(g, null, 2));
				this.render_deps(row, g);
			},
		});
	}

	show_history() {
		frappe.call({
			method: `${this.api}.repair_history_api`,
			callback: (r) => this.$preview.text(JSON.stringify(r.message || {}, null, 2)),
		});
	}

	rollback_last() {
		frappe.call({
			method: `${this.api}.repair_history_api`,
			callback: (r) => {
				const run = ((r.message || {}).rows || [])[0];
				if (!run) {
					frappe.msgprint(__("No repair history."));
					return;
				}
				frappe.confirm(__("DATABASE BACKUP REQUIRED. Dry-run rollback of {0}?", [run.repair_run_id]), () => {
					frappe.call({
						method: `${this.api}.rollback_run_api`,
						args: { repair_run_id: run.repair_run_id, dry_run: 1 },
						callback: (rr) => {
							this.$preview.text(JSON.stringify(rr.message || {}, null, 2));
							frappe.confirm(__("DATABASE BACKUP REQUIRED. Apply rollback snapshot?"), () => {
								frappe.call({
									method: `${this.api}.rollback_run_api`,
									args: { repair_run_id: run.repair_run_id, dry_run: 0 },
									callback: (x) => this.$preview.text(JSON.stringify(x.message || {}, null, 2)),
								});
							});
						},
					});
				});
			},
		});
	}

	link_cell(val, doctype) {
		if (!val) return $("<span>");
		const $a = $('<a class="hr-link">').text(String(val));
		$a.on("click", (e) => {
			e.preventDefault();
			frappe.set_route("Form", doctype, String(val));
		});
		return $a;
	}

	format_preview(msg) {
		const lines = [];
		lines.push(`status: ${msg.status || (msg.dry_run ? "DRY_RUN" : "SCAN")}`);
		if (msg.timing) {
			lines.push(`start_local: ${msg.timing.start_local || msg.start_local || ""}`);
			lines.push(`elapsed_seconds: ${msg.timing.elapsed_seconds || msg.duration_s || ""}`);
		}
		if (msg.dashboard) {
			lines.push("Dashboard");
			Object.entries(msg.dashboard).forEach(([k, v]) => lines.push(`${k}: ${v}`));
		}
		if (msg.summary) {
			lines.push(`same_time_groups: ${msg.summary.same_time_groups || 0}`);
			lines.push(`negative_intervals: ${msg.summary.negative_intervals || 0}`);
			lines.push(`CROSS_TIME_REPAIRABLE: ${msg.summary.CROSS_TIME_REPAIRABLE || 0}`);
			lines.push(`NO_REPAIR_NEEDED: ${msg.summary.NO_REPAIR_NEEDED || 0}`);
			lines.push(`REPAIRABLE_SECONDS: ${msg.summary.REPAIRABLE_SECONDS || 0}`);
			lines.push(`REAL_STOCK_SHORTAGE: ${msg.summary.REAL_STOCK_SHORTAGE || 0}`);
			lines.push(`LATER_INBOUND_UNRELATED: ${msg.summary.LATER_INBOUND_UNRELATED || 0}`);
			lines.push(`AMBIGUOUS_DEPENDENCY: ${msg.summary.AMBIGUOUS_DEPENDENCY || 0}`);
			lines.push(`cross_time_exact: ${msg.summary.cross_time_exact || 0}`);
			lines.push(`cross_time_likely: ${msg.summary.cross_time_likely || 0}`);
			lines.push(`cross_time_ambiguous: ${msg.summary.cross_time_ambiguous || 0}`);
		}
		lines.push(`count: ${msg.count ?? (msg.rows || msg.applied || []).length}`);
		lines.push(`eligible: ${(msg.eligible || []).length}`);
		const readyN = (msg.rows || []).filter((r) => /^READY/.test(String(r.planner_status || "")) && Number(r.sql_updates || 0) > 0).length;
		lines.push(`ready: ${readyN}`);
		if (msg.i4_count != null) lines.push(`i4_count: ${msg.i4_count}`);
		if (msg.elapsed_seconds != null) lines.push(`elapsed_seconds: ${msg.elapsed_seconds}`);
		if (msg.estimated_runtime_seconds != null) lines.push(`estimated_runtime_seconds: ${msg.estimated_runtime_seconds}`);
		if (msg.affected_sle != null) lines.push(`Affected SLE: ${msg.affected_sle}`);
		if (msg.affected_bin != null) lines.push(`Affected Bin: ${msg.affected_bin}`);
		if (msg.affected_gl != null) lines.push(`Affected GL: ${msg.affected_gl}`);
		if (msg.affected_riv != null) lines.push(`Affected RIV: ${msg.affected_riv}`);
		if (msg.database_backup_recommended || msg.database_backup_required) lines.push("DATABASE BACKUP REQUIRED");
		(msg.rows || msg.applied || []).slice(0, 40).forEach((row, i) => {
			lines.push("");
			lines.push(`--- case ${i + 1} ${row.voucher || row.chain || row.riv_name || ""} ---`);
			lines.push("Current → Expected → Replay → Affected SLE → Affected Bin → Affected GL → Affected RIV");
			if (row.current_stock_value != null || row.stock_value != null) lines.push(`Current stock_value: ${row.current_stock_value ?? row.stock_value}`);
			if (row.expected_stock_value != null) lines.push(`Expected stock_value: ${row.expected_stock_value}`);
			if (row.repair_class) lines.push(`Repair Class: ${row.repair_class}`);
			if (row.required_action) lines.push(`Required Action: ${row.required_action}`);
			[
				"Item",
				"Warehouse",
				"Batch",
				"Quantity Impact",
				"Valuation Impact",
				"Manufacture Source Rate",
				"Rate Status",
				"Replay Depth",
				"Dependent Count",
				"Affected Vouchers",
				"Estimated Runtime",
				"Replay Scope",
				"Negative Start",
				"Negative Voucher",
				"Later Inbound",
				"Current Outbound Time",
				"Current Inbound Time",
				"Time Gap",
				"Proposed Time",
				"Seconds Shifted",
				"Minimum Qty Before",
				"Minimum Qty After",
				"Dependency Reason",
				"Confidence",
				"Status",
			].forEach((label) => {
				const key = {
					Item: row.item || row.item_code,
					Warehouse: row.warehouse,
					Batch: row.batch,
					"Quantity Impact": row.quantity_impact,
					"Valuation Impact": row.valuation_impact,
					"Manufacture Source Rate": row.manufacture_source_rate,
					"Rate Status": row.rate_status || (row.outbound_gaps && row.outbound_gaps.length ? "STALE / REBUILD REQUIRED" : ""),
					"Replay Depth": row.replay_depth,
					"Dependent Count": row.dependent_count,
					"Affected Vouchers": (row.affected_vouchers || []).join(", "),
					"Estimated Runtime": row.estimated_runtime,
					"Replay Scope": row.replay_scope,
					"Negative Start": row.negative_start,
					"Negative Voucher": row.negative_voucher,
					"Later Inbound": row.later_inbound,
					"Current Outbound Time": row.current_outbound_time,
					"Current Inbound Time": row.current_inbound_time,
					"Time Gap": row.time_gap_seconds,
					"Proposed Time": row.proposed_outbound_time,
					"Seconds Shifted": row.minimum_seconds_label || row.seconds_shifted,
					"Minimum Qty Before": row.min_qty_before,
					"Minimum Qty After": row.min_qty_after,
					"Dependency Reason": row.dependency_reason,
					Confidence: row.confidence,
					Status: row.status,
				}[label];
				if (key !== undefined && key !== null && key !== "") lines.push(`${label}: ${key}`);
			});
			if (row.replay_order && row.replay_order.length) {
				lines.push(`Replay order: ${(row.patient_vouchers || []).join(" → ")} → ${row.replay_order.join(" → ")}`);
			}
			(row.dependents || (row.downstream && row.downstream.dependents) || []).forEach((dep) => {
				lines.push(
					[
						dep.voucher_no,
						dep.classification || "",
						dep.replay_required || "",
						`current ${dep.current_valuation}`,
						`expected ${dep.expected_valuation}`,
						`delta ${dep.difference}`,
						(dep.reasons || []).join(","),
					].join(" | ")
				);
			});
			(row.row_simulation || []).forEach((sim) => {
				lines.push(
					[
						sim.voucher,
						sim.purpose || "",
						`current ${sim.current_time}`,
						`qty ${sim.actual_qty}`,
						`run ${sim.current_running_qty}`,
						`proposed ${sim.proposed_time}`,
						`shift ${sim.seconds_shifted}`,
					].join(" | ")
				);
			});
		});
		return lines.join("\n");
	}

	status_label(row) {
		if (row.planner_status && row.planner_status !== "READY") {
			return row.planner_status;
		}
		if (row.planner_status === "READY") {
			return "READY";
		}
		if (row.status === "NO_REPAIR_NEEDED" || row.optimizer_status === "NO_REPAIR_NEEDED") {
			return __("Repair unnecessary");
		}
		if (row.status === "ELIGIBLE" && row.optimizer_status === "CROSS_TIME_REPAIRABLE") {
			return "CROSS_TIME_REPAIRABLE";
		}
		if (row.status === "ELIGIBLE" && row.optimizer_status === "SAME_TIME_REPAIRABLE") {
			return "SAME_TIME_REPAIRABLE";
		}
		if (row.status === "ORDER_FIXED_RATE_REBUILD_REQUIRED" || row.optimizer_status === "ORDER_FIXED_RATE_REBUILD_REQUIRED") {
			return "ORDER_FIXED_RATE_REBUILD_REQUIRED";
		}
		if (
			row.status === "DOWNSTREAM_REPLAY_REQUIRED" ||
			row.status === "DOWNSTREAM_VALUE_REPLAY_REQUIRED" ||
			row.downstream_status === "DOWNSTREAM_REPLAY_REQUIRED"
		) {
			return "DOWNSTREAM_REPLAY_REQUIRED";
		}
		if (row.status === "DOWNSTREAM_COMPLETE") {
			return "DOWNSTREAM_COMPLETE";
		}
		if (row.status === "DOWNSTREAM_SKIPPED") {
			return "DOWNSTREAM_SKIPPED";
		}
		if (row.status === "INTEGRITY_COMPLETE") {
			return "INTEGRITY_COMPLETE";
		}
		return row.status;
	}

	seconds_label(row) {
		return row.minimum_seconds_label || (row.status === "NO_REPAIR_NEEDED" ? __("Repair unnecessary") : "");
	}

	columns_for_topic() {
		if (this.topic === "posting") {
			return [
				"",
				__("Dependency Chain"),
				__("Inbound Document"),
				__("Outbound Document"),
				__("Item"),
				__("Warehouse"),
				__("Batch/SABB"),
				__("Negative Start"),
				__("Negative Voucher"),
				__("Later Inbound"),
				__("Current Outbound Time"),
				__("Current Inbound Time"),
				__("Time Gap"),
				__("Proposed Time"),
				__("Seconds Shifted"),
				__("Minimum Qty Before"),
				__("Minimum Qty After"),
				__("Valuation Impact"),
				__("Replay Depth"),
				__("Dependent Count"),
				__("Replay Scope"),
				__("Dependency Reason"),
				__("Planner Status"),
				__("SQL Updates"),
				__("Immediate Dependency"),
				__("Root Patient Zero"),
				__("Dependency Depth"),
				__("Repair Order"),
				__("Smallest Safe Scope"),
				__("Dependency Type"),
				__("Escalation Reason"),
				__("Required Action"),
				__("Blocker"),
				__("Confidence"),
				__("Status"),
			];
		}
		if (this.topic === "zero" || this.topic === "wrong") {
			return [
				"",
				__("Voucher"),
				__("Row"),
				__("Purpose"),
				__("Item"),
				__("Warehouse"),
				__("Batch/SABB"),
				__("Current Basic Rate"),
				__("Expected Basic Rate"),
				__("Current Valuation Rate"),
				__("Expected Valuation Rate"),
				__("Current Incoming"),
				__("Expected Incoming"),
				__("Current Outgoing"),
				__("Expected Outgoing"),
				__("Current Amount"),
				__("Expected Amount"),
				__("Difference"),
				__("Rate Source"),
				__("Confidence"),
				__("Repair Required"),
				__("Patient Zero"),
				__("Planner Status"),
				__("SQL Updates"),
				__("Immediate Dependency"),
				__("Root Patient Zero"),
				__("Dependency Depth"),
				__("Repair Order"),
				__("Required Action"),
				__("Blocker"),
				__("Status"),
			];
		}
		if (this.topic === "sle") {
			return [
				"",
				__("Voucher"),
				__("Item"),
				__("Warehouse"),
				__("Repair Class"),
				__("I4 Residual"),
				__("Patient Zero"),
				__("Root Cause"),
				__("Health"),
				__("Replay"),
				__("Planner Status"),
				__("SQL Updates"),
				__("Immediate Dependency"),
				__("Root Patient Zero"),
				__("Required Action"),
				__("Smallest Safe Scope"),
				__("Blocker"),
				__("Status"),
			];
		}
		return ["", __("Voucher"), __("Item"), __("Warehouse"), __("Confidence"), __("Planner Status"), __("Immediate Dependency"), __("Root Patient Zero"), __("Required Action"), __("Blocker"), __("Status")];
	}

	cells_for_row(row) {
		if (this.topic === "posting") {
			return [
				row.chain,
				row.inbound_document,
				row.outbound_document,
				row.item,
				row.warehouse,
				row.batch || row.sabb_outbound || "",
				row.negative_start || row.current_outbound_time,
				row.negative_voucher || row.outbound_document,
				row.later_inbound || row.inbound_document,
				row.current_outbound_time,
				row.current_inbound_time,
				row.time_gap_seconds == null ? "" : String(row.time_gap_seconds),
				row.proposed_outbound_time,
				this.seconds_label(row),
				row.min_qty_before,
				row.min_qty_after,
				row.valuation_impact || "",
				row.replay_depth == null ? "" : String(row.replay_depth),
				row.dependent_count == null ? "" : String(row.dependent_count),
				row.replay_scope || "",
				row.dependency_reason,
				row.planner_status || this.status_label(row),
				row.sql_updates == null ? "" : String(row.sql_updates),
				row.immediate_blocker || "",
				row.root_patient_zero || row.root_blocker || (row.patient_zero && row.patient_zero.voucher_no) || "",
				row.dependency_depth == null ? "" : String(row.dependency_depth),
				row.repair_order || "",
				row.smallest_safe_scope || "",
				row.dependency_type || "",
				row.escalation_reason || "",
				row.required_action || "",
				row.blocker || row.reason || row.skip_reason || "",
				row.confidence,
				this.status_label(row),
			];
		}
		if (this.topic === "zero" || this.topic === "wrong") {
			return [
				row.voucher,
				row.idx,
				row.purpose,
				row.item,
				row.warehouse,
				row.batch,
				row.current_basic_rate ?? row.current_rate,
				row.expected_basic_rate ?? row.proposed_rate,
				row.current_valuation_rate,
				row.expected_valuation_rate,
				row.current_incoming_rate,
				row.expected_incoming_rate,
				row.current_outgoing_rate,
				row.expected_outgoing_rate,
				row.current_amount,
				row.expected_amount,
				row.difference,
				row.rate_source || row.source_of_truth,
				row.confidence,
				row.repair_required ? "YES" : "NO",
				(row.patient_zero && row.patient_zero.voucher_no) || "",
				row.planner_status || this.status_label(row),
				row.sql_updates == null ? "" : String(row.sql_updates),
				row.immediate_blocker || "",
				row.root_patient_zero || row.root_blocker || (row.patient_zero && row.patient_zero.voucher_no) || "",
				row.dependency_depth == null ? "" : String(row.dependency_depth),
				row.repair_order || "",
				row.required_action || "",
				row.blocker || row.reason || row.skip_reason || "",
				this.status_label(row),
			];
		}
		if (this.topic === "sle") {
			const pz = (row.patient_zero && row.patient_zero.voucher_no) || row.root_patient_zero || row.root_blocker || "";
			const health = row.health_score != null ? String(row.health_score) + "%" : row.i4_status || "";
			return [
				row.voucher,
				row.item || row.item_code,
				row.warehouse,
				row.repair_class || "",
				row.residual_stock_value != null ? row.residual_stock_value : row.stock_value || "",
				pz,
				row.root_cause || row.reason || "",
				health,
				row.replay_count == null ? (row.estimated_replay_count == null ? "" : String(row.estimated_replay_count)) : String(row.replay_count),
				row.planner_status || "",
				row.sql_updates == null ? "" : String(row.sql_updates),
				row.immediate_blocker || "",
				row.root_patient_zero || row.root_blocker || "",
				row.required_action || "",
				row.smallest_safe_scope || "IDENTITY",
				row.blocker || row.reason || row.skip_reason || "",
				row.status || row.i4_status || "",
			];
		}
		return [
			row.voucher || row.riv_name,
			row.item || row.item_code,
			row.warehouse,
			row.confidence,
			row.planner_status || "",
			row.immediate_blocker || "",
			row.root_patient_zero || row.root_blocker || "",
			row.required_action || "",
			row.blocker || row.reason || row.skip_reason || "",
			row.status || row.gl_class || row.riv_status,
		];
	}

	render_table() {
		const cols = this.columns_for_topic();
		const q = (this.$search && this.$search.val() ? this.$search.val() : "").toLowerCase();
		const $table = $('<table class="hr-table" data-role="posting-order-table">');
		const $head = $("<tr>");
		const $filters = $("<tr class='hr-filter-row'>");
		cols.forEach((c, i) => {
			if (this.hidden_cols.has(i)) return;
			const $th = $("<th>").text(c.trim());
			if (i > 0) {
				$th.css("cursor", "pointer").on("click", (e) => {
					const dir = this.sort_keys[0] && this.sort_keys[0].i === i && this.sort_keys[0].dir === 1 ? -1 : 1;
					if (e.shiftKey) this.sort_keys = this.sort_keys.filter((s) => s.i !== i).concat([{ i, dir }]);
					else this.sort_keys = [{ i, dir }];
					this.render_table();
				});
			}
			$head.append($th);
			const $inp = $('<input class="form-control input-xs hr-col-filter">').attr("data-col", i).val(this.col_filters[i] || "");
			$inp.on("input", (e) => {
				e.stopPropagation();
				this.col_filters[i] = e.target.value;
				this.render_table();
			});
			$inp.on("click", (e) => e.stopPropagation());
			$filters.append($("<th>").append(i === 0 ? "" : $inp));
		});
		$table.append($("<thead>").append($head).append($filters));
		const $body = $("<tbody>");
		let rows = (this.rows || []).map((row, idx) => ({ row, idx }));
		if (q) {
			rows = rows.filter(({ row }) => JSON.stringify(row).toLowerCase().includes(q));
		}
		Object.entries(this.col_filters || {}).forEach(([i, val]) => {
			if (!val) return;
			const col = parseInt(i, 10) - 1;
			const needle = String(val).toLowerCase();
			rows = rows.filter(({ row }) => String(this.cells_for_row(row)[col] || "").toLowerCase().includes(needle));
		});
		if (this.sort_keys && this.sort_keys.length) {
			rows.sort((a, b) => {
				for (const { i, dir } of this.sort_keys) {
					const av = String(this.cells_for_row(a.row)[i - 1] || "");
					const bv = String(this.cells_for_row(b.row)[i - 1] || "");
					const cmp = av.localeCompare(bv, undefined, { numeric: true });
					if (cmp) return cmp * dir;
				}
				return 0;
			});
		}
		rows.forEach(({ row, idx }) => {
			const $tr = $("<tr>").attr("title", row.blocker || row.reason || row.skip_reason || row.dependency_reason || (row.flags || []).join(", ") || row.rate_source || row.status || "");
			if (this._is_ready(row)) $tr.addClass("hr-row-exact");
			else if (row.confidence === "LIKELY") $tr.addClass("hr-row-likely");
			else if (/POISON|AMBIGUOUS|BLOCKED|MANUAL|WAITING_|NO_REPAIR/i.test(String(row.planner_status || row.status || ""))) $tr.addClass("hr-row-blocked");
			const $cb = $('<input type="checkbox">')
				.attr("data-idx", idx)
				.attr("data-eligible", this._is_ready(row) ? "1" : "0");
			$tr.append($("<td>").append($cb));
			this.cells_for_row(row).forEach((val, i, arr) => {
				if (this.hidden_cols.has(i + 1)) return;
				const $td = $("<td>");
				this.fill_cell($td, val, i, row);
				if (i === arr.length - 1) {
					$td.addClass(`hr-status-${row.status || ""}`);
					if (row.optimizer_status) $td.addClass(`hr-status-${row.optimizer_status}`);
					const sev = this._is_ready(row) ? "ok" : /POISON|AMBIGUOUS|BLOCKED|WAITING_|MANUAL/i.test(String(row.planner_status || row.status || row.confidence || "")) ? "bad" : "warn";
					$td.empty().append($('<span class="hr-badge">').addClass("hr-badge-" + sev).text(String(val == null ? "" : val)));
				}
				$tr.append($td);
			});
			$tr.on("click", (e) => {
				if (e.target && e.target.type === "checkbox") return;
				this.show_reconstruction(row);
				this.render_deps(row);
			});
			$body.append($tr);
		});
		$table.append($body);
		this.visible_rows = rows.map((x) => x.row);
		this.$table.empty().append($table);
		const st = this.topic_state[this.topic] || {};
		if (!st.loaded) {
			this.$table.append($('<div class="hr-empty hr-empty-unload" data-role="rows-not-loaded">').html(this._topic_unloaded_message()));
		} else if (!this.rows.length) {
			const kpi = this._topic_dashboard_count(this.topic);
			let html =
				"<strong>" +
				__("No anomalies in this topic for the current scan filters.") +
				"</strong>";
			if (kpi != null && kpi > 0) {
				html +=
					"<div class='hr-empty-kpi'>" +
					__("Dashboard still reports {0}. Clear voucher/date/warehouse filters or re-run Scan All — silent filter mismatch is not allowed.", [kpi]) +
					"</div>";
			} else {
				html += "<div>" + __("Run Scan All, then Scan this tab if filters changed.") + "</div>";
			}
			this.$table.append($('<div class="hr-empty">').html(html));
		} else if (!rows.length) {
			this.$table.append(
				$('<div class="hr-empty">').html(
					"<strong>" +
						__("No rows match the current search or column filters.") +
						"</strong><div>" +
						__("Underlying scan still has {0} row(s). Clear quick search / column filters.", [this.rows.length]) +
						"</div>"
				)
			);
		}
	}

	toggle_columns() {
		const cols = this.columns_for_topic();
		this.$columns.toggle().empty();
		cols.forEach((c, i) => {
			if (!i) return;
			const $lab = $("<label class='hr-col-choice'>");
			const $cb = $('<input type="checkbox">').prop("checked", !this.hidden_cols.has(i));
			$cb.on("change", () => {
				if ($cb.prop("checked")) this.hidden_cols.delete(i);
				else this.hidden_cols.add(i);
				this.save_layout();
				this.render_table();
			});
			$lab.append($cb).append(document.createTextNode(" " + c));
			this.$columns.append($lab);
		});
	}

	show_reconstruction(row) {
		frappe.call({
			method: `${this.api}.preview_reconstruction_api`,
			args: { row },
			callback: (r) => {
				const p = r.message || {};
				const alts = (p.alternative_sources || [])
					.map((s) => `${s.source}\n${s.rate}`)
					.join("\n\n");
				const lines = [
					"Rate Reconstruction Preview",
					"",
					`Current`,
					`${p.current}`,
					"↓",
					"Expected",
					`${p.expected}`,
					"↓",
					"Difference",
					`${p.difference}`,
					"↓",
					"Chosen Source",
					`${p.chosen_source || ""}`,
					"",
					"Alternative Sources",
					alts || "(none)",
					"",
					`Final ${p.expected}`,
					`Confidence ${p.confidence}`,
				];
				if (p.sources_disagree) lines.push("Sources disagree — not auto-repaired.");
				if (p.bin_used) lines.push("ERROR: Bin was used");
				this.$recon.text(lines.join("\n"));
			},
		});
	}

	start_progress(label) {
		this.cancelled = false;
		this._progress_t0 = Date.now();
		this.$progress.show();
		this.$progress.find(".hr-progress-bar").css("width", "35%");
		this.$eta.text(label || __("Working…"));
	}

	end_progress() {
		const elapsed = this._progress_t0 ? ((Date.now() - this._progress_t0) / 1000).toFixed(1) : "";
		this.$progress.find(".hr-progress-bar").css("width", "100%");
		this.$eta.text(elapsed ? __("Completed · Elapsed {0}s", [elapsed]) : __("Completed"));
		setTimeout(() => this.$progress.hide(), 400);
	}

	fill_cell($td, val, i, row) {
		const s = val == null ? "" : String(val);
		if (!s) {
			$td.text("");
			return;
		}
		const cols = this.columns_for_topic();
		const colName = cols[i + 1] || "";
		const isRootCol = /Root Patient Zero/i.test(colName);
		const tokens = s.split(/\s*(?:→|->|↓)\s*/).filter(Boolean);
		if (tokens.length > 1 && tokens.every((t) => /^MAT-STE-|^MFG-WO-/.test(t))) {
			tokens.forEach((tok, idx) => {
				if (idx) $td.append(document.createTextNode(" → "));
				$td.append(this._dep_link(tok, isRootCol));
			});
			return;
		}
		if (/^MAT-STE-/.test(s) || /^MFG-WO-/.test(s)) {
			if (/^MFG-WO-/.test(s)) $td.append(this.link_cell(s, "Work Order"));
			else $td.append(this._dep_link(s, isRootCol));
			return;
		}
		if (/Item/i.test(colName) && !/Warehouse|Immediate|Root|Repair|Required/i.test(colName)) {
			$td.append(this.link_cell(s, "Item"));
			return;
		}
		if (/^Warehouse$/i.test(colName) || colName === __("Warehouse")) {
			$td.append(this.link_cell(s, "Warehouse"));
			return;
		}
		if ((/Batch/i.test(colName) || colName === __("Batch/SABB")) && row.batch) {
			$td.append(this.link_cell(row.batch, "Batch"));
			return;
		}
		$td.text(s);
	}

	_dep_link(val, filterRepair) {
		if (filterRepair && /^MAT-STE-/.test(String(val))) {
			const $a = $('<a class="hr-link">').text(String(val));
			$a.attr("title", __("Open Historical Repair on this patient zero"));
			$a.on("click", (e) => {
				e.preventDefault();
				e.stopPropagation();
				this.go_to_root_cause(String(val));
			});
			return $a;
		}
		return this.link_cell(val, "Stock Entry");
	}

	render_deps(row, impact) {
		if (!this.$deps) return;
		const src = row || {};
		const tree = src.dependency_tree || (impact && impact.dependency_tree);
		const text = src.tree_text || (impact && impact.tree_text) || "";
		this.$deps.empty();
		this.$deps.append($('<h5 class="hr-section-title">').text(__("Dependency Resolution")));
		const meta = $('<div class="hr-deps-meta">');
		[
			[__("Current Voucher"), src.outbound_document || src.voucher || src.inbound_document],
			[__("Status"), src.planner_status || (impact && impact.planner_status)],
			[__("Current Scope"), src.inbound_document && src.outbound_document ? "BATCH_SCOPED" : "LOCAL_VOUCHER"],
			[__("Required Scope"), src.smallest_safe_scope || (impact && impact.smallest_safe_scope)],
			[__("Batch/SABB"), src.batch],
			[__("Immediate Dependency"), src.immediate_blocker || (impact && impact.immediate_blocker)],
			[__("Root Dependency"), src.root_blocker || (impact && impact.root_blocker)],
			[__("Dependency Type"), src.dependency_type || (impact && impact.dependency_type)],
			[__("Patient Zero"), src.root_patient_zero || src.root_blocker],
			[__("Repair Sequence"), src.repair_order || (impact && impact.repair_sequence)],
			[__("Reason"), src.escalation_reason || src.blocked_because || src.reason || (impact && (impact.escalation_reason || impact.reason))],
			[__("Effect"), this._scope_effect(src, impact)],
			[__("Estimated affected vouchers"), src.estimated_repair_count || (impact && impact.estimated_repair_count)],
			[__("Estimated replay count"), src.replay_count || src.estimated_replay_depth || (impact && (impact.replay_count || impact.estimated_replay_depth))],
			[__("Estimated SQL updates"), src.sql_updates != null ? src.sql_updates : impact && (impact.sql_updates || impact.estimated_sql_updates)],
			[__("Escalation required"), src.escalation_reason || (impact && impact.escalation_reason) ? __("YES") : src.smallest_safe_scope ? __("NO") : ""],
			[__("Required Action"), src.required_action || (impact && impact.required_action)],
		].forEach(([label, val]) => {
			if (val == null || val === "") return;
			const $line = $("<div>");
			$line.append($("<strong>").text(label + ": "));
			if (/^MAT-STE-/.test(String(val)) && label === __("Root blocker")) $line.append(this._dep_link(val, true));
			else if (/^MAT-STE-/.test(String(val))) $line.append(this.link_cell(val, "Stock Entry"));
			else $line.append(document.createTextNode(String(val)));
			meta.append($line);
		});
		this.$deps.append(meta);
		const unrelated = src.unrelated_poison || (impact && impact.unrelated_poison) || [];
		if (unrelated.length) {
			this.$deps.append($("<div class='hr-deps-preview-title'>").text(__("Unrelated poison")));
			unrelated.forEach((p) => {
				const effect = p.effect || "NONE";
				this.$deps.append(
					$("<div>").text(
						`${p.voucher || ""} / batch ${p.batch || ""}  Effect on selected chain: ${effect}`
					)
				);
			});
		}
		if (tree) this.$deps.append(this._tree_el(tree));
		else if (text) this.$deps.append($("<pre class='hr-deps-tree'>").text(text));
		const preview = src.chain_preview || (impact && impact.chain_preview) || [];
		if (preview.length) {
			this.$deps.append($('<div class="hr-deps-preview-title">').text(__("Repair Order")));
			preview.forEach((step, i) => {
				if (i) this.$deps.append($('<div class="hr-graph-edge">').text("↓"));
				this.$deps.append($("<div>").text(step));
			});
		}
		this._toggle_chain_button(src, impact);
	}

	_scope_effect(src, impact) {
		const unrelated = src.unrelated_poison || (impact && impact.unrelated_poison) || [];
		const none = unrelated.filter((p) => !p.effect || p.effect === "NONE");
		if (src.escalation_reason || (impact && impact.escalation_reason)) {
			const n = src.estimated_repair_count || (impact && impact.estimated_repair_count) || "";
			return __("Warehouse moving-average / qty_after rewrite required{0}", [n ? ` (${n} vouchers)` : ""]);
		}
		if (none.length) {
			return __("Unrelated warehouse poison effect NONE");
		}
		if (/^READY/.test(String(src.planner_status || (impact && impact.planner_status) || ""))) {
			return __("Smallest safe scope is sufficient");
		}
		return src.blocked_because || "";
	}

	_tree_el(node, depth) {
		depth = depth || 0;
		const $box = $('<div class="hr-deps-node">').css("margin-left", depth ? 18 : 0);
		const voucher = node.voucher || "";
		const $row = $("<div>");
		if (depth) {
			const rel = node.relation || (node.edge_type === "WAREHOUSE_MA_DEPENDENCY" ? "would rewrite " : "├── waits for ");
			$row.append($("<span class='text-muted'>").text(rel.startsWith("├") ? rel : "├── " + rel + " "));
		}
		if (voucher) $row.append(depth ? this.link_cell(voucher, "Stock Entry") : $("<strong>").text(voucher));
		if (node.edge_type) $row.append($("<span class='text-muted'>").text("  [" + node.edge_type + "]"));
		if (node.status) $row.append($("<span class='text-muted'>").text("  " + node.status));
		if (node.blocked_because) $row.append($("<span class='text-muted'>").text("  (" + node.blocked_because + ")"));
		$box.append($row);
		(node.children || []).forEach((child) => $box.append(this._tree_el(child, depth + 1)));
		if (!depth) {
			const count = node.estimated_count || (node.children || []).length + 1;
			$box.append($("<div class='text-muted'>").text("└── estimated chain: " + count + " vouchers"));
		}
		return $box;
	}

	_toggle_chain_button(row, impact) {
		if (!this.btn_repair_chain) return;
		const rootReady = (row && /^READY/.test(String(row.root_status || ""))) || (impact && /^READY/.test(String(impact.root_status || "")));
		const noPath = (row && row.no_repair_path) || (impact && impact.no_repair_path);
		this.btn_repair_chain.prop("disabled", !(this.access.can_repair && rootReady && !noPath));
	}

	go_to_root_cause(explicit) {
		const row = this.selected_rows()[0] || this.rows[0] || {};
		const root = explicit || row.root_blocker || row.root_patient_zero || row.first_actionable || (row.patient_zero && row.patient_zero.voucher_no);
		if (!root) {
			frappe.msgprint(__("No root cause voucher on this row."));
			return;
		}
		if (this.voucher) this.voucher.set_value(root);
		if (this.patient_zero) this.patient_zero.set_value(root);
		this.$search && this.$search.val(root);
		this.start_progress(__("Finding Patient Zero..."));
		const isI4 =
			row.repair_class === "I4_LEFTOVER_REPAIR" ||
			row.topic === "I4_LEFTOVER" ||
			row.planner_status === "READY_I4" ||
			row.planner_status === "WAITING_I4" ||
			/I4|qty_after_zero/i.test(String(row.required_action || row.reason || ""));
		let topic = "wrong";
		if (row.root_topic === "POSTING_ORDER") topic = "posting";
		else if (isI4 || this.topic === "sle") topic = "sle";
		else if (this.topic === "zero" || row.topic === "ZERO_RATE" || /ZERO/i.test(String(row.zero_class || ""))) topic = "zero";
		else if (this.topic === "wrong" || row.topic === "WRONG_RATE") topic = "wrong";
		if (this.topic !== topic) this.switch_topic(topic);
		if (this.voucher) this.voucher.set_value(root);
		if (topic === "sle" && this.repair_class) this.repair_class.set_value("I4_LEFTOVER_REPAIR");
		if (topic === "wrong") {
			frappe.call({
				method: `${this.api}.scan_wrong_rates_api`,
				args: { voucher: root, company: this.company.get_value() },
				freeze: true,
				callback: (r) => {
					this.end_progress();
					const rows = (r.message && r.message.rows) || [];
					if (rows.length) {
						this._mark_topic_loaded(rows);
						this.dry_run_done = false;
						this.impact_done = false;
						this.render_table();
						this._select_and_show(root);
						return;
					}
					this.scan({ select_voucher: root });
				},
			});
			return;
		}
		this.scan({ select_voucher: root });
	}

	_select_and_show(voucher) {
		const needle = String(voucher);
		let match = (this.rows || []).find((r) => {
			return [r.voucher, r.outbound_document, r.inbound_document, r.root_blocker, r.immediate_blocker].indexOf(needle) >= 0;
		});
		if (!match) match = (this.rows || []).find((r) => JSON.stringify(r).indexOf(needle) >= 0);
		if (!match) {
			this.$preview.text(__("Filtered on {0}. No grid row matched; the Stock Entry is still openable from the tree.", [needle]));
			this.render_deps({ voucher: needle, root_blocker: needle, required_action: __("Scan this voucher") });
			return;
		}
		const idx = this.rows.indexOf(match);
		this.$table.find("input[type=checkbox][data-idx]").prop("checked", false);
		const $cb = this.$table.find(`input[type=checkbox][data-idx="${idx}"]`);
		$cb.prop("checked", true);
		const el = $cb.closest("tr").get(0);
		if (el && el.scrollIntoView) el.scrollIntoView({ block: "center" });
		this.show_reconstruction(match);
		this.render_deps(match);
		this.dry_run_done = false;
		this._complete_impact({ rows: [match] });
	}

	repair_dependency_chain() {
		const row = this.selected_rows()[0] || this.rows[0];
		if (!row) {
			frappe.msgprint(__("Select a row first."));
			return;
		}
		frappe.call({
			method: `${this.api}.repair_dependency_chain_api`,
			args: { row, dry_run: 1 },
			freeze: true,
			callback: (r) => {
				const msg = r.message || {};
				this.$preview.text(JSON.stringify(msg, null, 2));
				this.render_deps(row, msg);
				if (!msg.executable) {
					frappe.msgprint({
						title: __("NO REPAIR PATH"),
						message: msg.required_action || msg.reason || __("Root is not READY. Nothing executed."),
						indicator: "orange",
					});
					return;
				}
				const chain = (msg.chain_preview || []).join("\n↓\n");
				frappe.confirm(__("DATABASE BACKUP REQUIRED. Repair only the root of this chain?") + "\n\n" + chain, () => {
					frappe.call({
						method: `${this.api}.repair_dependency_chain_api`,
						args: { row, dry_run: 0 },
						freeze: true,
						callback: (rr) => {
							this._show_repair_result(rr.message || {});
							this.dry_run_done = false;
							this.impact_done = false;
							this._lock_writes(true);
							this.integrity();
						},
					});
				});
			},
		});
	}

	load_layout() {
		try {
			const raw = localStorage.getItem("hr-layout-" + this.topic);
			if (!raw) return;
			const saved = JSON.parse(raw);
			this.hidden_cols = new Set(saved.hidden_cols || []);
		} catch (e) {
			this.hidden_cols = new Set();
		}
	}

	save_layout() {
		try {
			localStorage.setItem("hr-layout-" + this.topic, JSON.stringify({ hidden_cols: Array.from(this.hidden_cols) }));
		} catch (e) {
			/* ignore quota */
		}
	}

	run_benchmark() {
		frappe.call({
			method: `${this.api}.historical_benchmark_api`,
			args: { company: this.company.get_value() },
			freeze: true,
			callback: (r) => this.$preview.text(JSON.stringify(r.message || {}, null, 2)),
		});
	}

	render_wizard_step(step) {
		if (!this.$wizard) return;
		const steps = [
			__("1 Backup"),
			__("2 Dry Run"),
			__("3 Impact"),
			__("4 Repair"),
			__("5 Replay"),
			__("6 Integrity"),
			__("7 Done"),
		];
		this.$wizard.empty();
		steps.forEach((label, i) => {
			const n = i + 1;
			const $s = $('<span class="hr-wizard-step">').text(label);
			if (n === step) $s.addClass("hr-wizard-active");
			else if (n < step) $s.addClass("hr-wizard-done");
			this.$wizard.append($s);
			if (n < steps.length) this.$wizard.append($('<span class="hr-wizard-arrow">').text(" → "));
		});
	}

	show_root_cause_explorer() {
		const row = this.selected_rows()[0] || this.rows[0] || {};
		const voucher = (this.voucher && this.voucher.get_value()) || row.voucher || row.outbound_document;
		if (!voucher) {
			frappe.msgprint(__("Select a voucher first."));
			return;
		}
		this.start_progress(__("Finding Patient Zero..."));
		frappe.call({
			method: `${this.api}.root_cause_explorer_api`,
			args: {
				voucher,
				item: row.item || row.item_code || (this.item && this.item.get_value()),
				warehouse: row.warehouse || (this.warehouse && this.warehouse.get_value()),
			},
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				this.$root_explorer.empty();
				const $box = $('<div class="hr-explorer-box">').appendTo(this.$root_explorer);
				$box.append($("<h5>").text(__("Root Cause Explorer")));
				[
					[__("Healthy"), msg.healthy_anchor || msg.healthy || "—"],
					[__("First Patient Zero"), msg.patient_zero || msg.root_patient_zero],
					[__("Replay chain"), (msg.replay_order || msg.chain || []).join(" → ") || "—"],
					[__("Current Voucher"), voucher],
					[__("Root cause"), msg.root_cause || msg.reason || "—"],
					[__("Repair order"), (msg.repair_order || []).join(" → ") || "—"],
					[__("Dependency tree"), msg.dependency_summary || ""],
				].forEach(([k, v]) => {
					$box.append($("<div>").append($("<b>").text(k + ": ")).append(document.createTextNode(v == null ? "" : String(v))));
				});
				this.$preview.text(JSON.stringify(msg, null, 2));
			},
		});
	}

	show_identity_health() {
		const row = this.selected_rows()[0] || this.rows[0] || {};
		const item = row.item || row.item_code || (this.item && this.item.get_value());
		const warehouse = row.warehouse || (this.warehouse && this.warehouse.get_value());
		if (!item || !warehouse) {
			frappe.msgprint(__("Select a row with Item and Warehouse, or set filters."));
			return;
		}
		frappe.call({
			method: `${this.api}.identity_health_api`,
			args: { item_code: item, warehouse },
			freeze: true,
			callback: (r) => {
				const msg = r.message || {};
				this.$health.empty();
				const $panel = $('<div class="hr-health-panel">').appendTo(this.$health);
				$panel.append($("<h5>").text(__("Identity Health") + ` · ${item} @ ${warehouse}`));
				const checks = msg.checks || {
					"Posting Order": msg.posting_order,
					"Wrong Rate": msg.wrong_rate,
					I4: msg.i4,
					"Zero Rate": msg.zero_rate,
					Bin: msg.bin,
					GL: msg.gl,
					"Failed RIV": msg.failed_riv,
				};
				Object.entries(checks).forEach(([k, ok]) => {
					const mark = ok === true || ok === "ok" || ok === "✔" ? "✔" : "✘";
					$panel.append($('<div class="hr-health-row">').text(`${k}: ${mark}`));
				});
				const score = msg.overall_health != null ? msg.overall_health : msg.health_score;
				$panel.append($("<div class='hr-health-score'>").text(__("Overall Health") + `: ${score == null ? "—" : score + "%"}`));
			},
		});
	}

	show_master_repair_plan() {
		this.start_progress(__("Building master repair plan..."));
		frappe.call({
			method: `${this.api}.master_repair_plan_api`,
			args: { company: this.company.get_value() },
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				const lines = [
					__("Master Repair Plan") + ` v${msg.version || ""}`,
					msg.message || "",
					"",
					[
						"Class".padEnd(22),
						"Cnt".padStart(5),
						"RDY".padStart(5),
						"WAIT".padStart(5),
						"MAN".padStart(5),
						"AMB".padStart(5),
						"PZ".padStart(4),
						"Risk".padStart(8),
						"Promo",
					].join(" "),
				];
				(msg.classes || msg.roadmap || []).forEach((c, i) => {
					const name = c.repair_class || c.stage || "";
					lines.push(
						[
							String(`${i + 1}. ${name}`).slice(0, 22).padEnd(22),
							String(c.current_count ?? "").padStart(5),
							String(c.READY ?? "").padStart(5),
							String(c.WAITING ?? "").padStart(5),
							String(c.MANUAL ?? "").padStart(5),
							String(c.AMBIGUOUS ?? "").padStart(5),
							String(c.patient_zero_count ?? "").padStart(4),
							String(c.risk || "").slice(0, 8).padStart(8),
							c.promotion_status || c.kind || "",
						].join(" ")
					);
					if (c.notes || c.note) lines.push(`   ${c.notes || c.note}`);
				});
				const graph = msg.dependency_graph || {};
				if ((graph.edges || []).length) {
					lines.push("", __("Dependency edges:"));
					(graph.edges || []).forEach((e) => lines.push(`  ${e.from} → ${e.to} (${e.kind})`));
				}
				this.$preview.text(lines.join("\n"));
			},
		});
	}

	show_campaign_wizard() {
		this.start_progress(__("Loading Campaign Wizard..."));
		frappe.call({
			method: `${this.api}.campaign_wizard_api`,
			args: { company: this.company.get_value() },
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				const lines = [__("Repair Campaign Wizard"), `v${msg.version || ""}`, ""];
				const camps = msg.campaigns || {};
				Object.keys(camps).forEach((k) => {
					const c = camps[k] || {};
					lines.push(`▸ ${k}  promo=${c.promotion_status || "—"}`);
					if (c.exact_ready != null) lines.push(`    exact_ready=${c.exact_ready} independent=${c.independent_roots}`);
					if (c.safe_to_retry != null) lines.push(`    safe_to_retry=${c.safe_to_retry} buckets=${JSON.stringify(c.by_bucket || {})}`);
					if (c.recommended) {
						lines.push(
							`    recommended SAFE_GROUP n=${c.recommended.n_roots} sql≈${c.recommended.estimated_sql_updates} replay≈${c.recommended.estimated_sle_replay}`
						);
					}
					if (c.message) lines.push(`    ${c.message}`);
				});
				lines.push("", __("Forbidden:"), ...(msg.forbidden || []).map((f) => `  ✗ ${f}`));
				this.$preview.text(lines.join("\n"));
			},
			error: () => this.end_progress(),
		});
	}

	show_warehouse_plan() {
		const row = this.selected_rows()[0] || this.rows[0] || {};
		const warehouse = row.warehouse || (this.warehouse && this.warehouse.get_value());
		if (this.topic !== "posting" && !row.outbound_document && !row.inbound_document) {
			frappe.msgprint(
				__("Warehouse Plan expects a Posting Order (or warehouse-escalation) row. Switch to Posting Order, Scan, and select a row.")
			);
			if (this.topic !== "posting") this.switch_topic("posting");
			return;
		}
		if (!row || (!row.outbound_document && !row.inbound_document && !warehouse)) {
			frappe.msgprint(__("Select a posting-order row (or set Warehouse) before Warehouse Plan."));
			return;
		}
		this.start_progress(__("Planning warehouse-scoped repair..."));
		frappe.call({
			method: `${this.api}.warehouse_plan_api`,
			args: { row },
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const plan = r.message || {};
				this.$preview.text(JSON.stringify(plan, null, 2));
				const status = plan.planner_status || plan.status || "";
				if (!plan || plan.error) {
					frappe.msgprint(plan.error || __("Warehouse Plan failed."));
					return;
				}
				frappe.confirm(
					__("Run Warehouse Plan dry-run for this row?") +
						"\n\n" +
						__("Status: {0}", [status]) +
						"\n" +
						__("Warehouse: {0}", [plan.warehouse || warehouse || "—"]),
					() => {
						frappe.call({
							method: `${this.api}.warehouse_dry_run_api`,
							args: { row },
							freeze: true,
							callback: (rr) => {
								const dry = rr.message || {};
								this.$preview.text(JSON.stringify({ plan, dry_run: dry }, null, 2));
								frappe.show_alert({
									message: __("Warehouse dry-run complete (no writes). Review preview before apply."),
									indicator: "blue",
								});
							},
						});
					}
				);
			},
			error: () => {
				this.end_progress();
				// Fallback: warehouse dependency analyzer when row is warehouse-only
				if (!warehouse) return;
				frappe.call({
					method: `${this.api}.warehouse_dependency_api`,
					args: { warehouse, company: this.company.get_value() },
					freeze: true,
					callback: (r2) => this.$preview.text(JSON.stringify(r2.message || {}, null, 2)),
				});
			},
		});
	}

	show_cluster_explorer() {
		if (this.topic !== "zero") {
			frappe.msgprint(__("Cluster Explorer classifies Zero Rate SAFE groups. Switching to Zero / Lost Rate."));
			this.switch_topic("zero");
		}
		this.start_progress(__("Classifying Zero Rate clusters..."));
		frappe.call({
			method: `${this.api}.classify_zero_clusters_api`,
			args: { company: this.company.get_value(), max_cluster: 15 },
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				const cl = msg.clusters || {};
				const lines = [
					__("Cluster Explorer — Zero Rate"),
					`total=${msg.total_zero_rows} exact_ready=${msg.exact_ready} independent=${cl.independent_root_count}`,
					"",
				];
				(cl.safe_groups || []).slice(0, 12).forEach((g, i) => {
					lines.push(
						`SAFE_GROUP ${i + 1}: n=${g.n_roots} sql≈${g.estimated_sql_updates} replay≈${g.estimated_sle_replay} risk=${g.risk}`
					);
					(g.repair_order || []).slice(0, 8).forEach((v) => lines.push(`    ${v}`));
				});
				const rec = cl.recommended_first_group;
				if (rec) {
					lines.push("", __("Recommended first group:"), `  class=${rec.group_class} n=${rec.n_roots}`);
				}
				this.$preview.text(lines.join("\n"));
			},
			error: () => this.end_progress(),
		});
	}

	validate_dashboard() {
		this.start_progress(__("Validating Dashboard ↔ Scan ↔ Planner ↔ SQL..."));
		frappe.call({
			method: `${this.api}.validate_dashboard_api`,
			args: { company: this.company.get_value() },
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				const lines = [
					__("Validate Dashboard"),
					`${__("PASS")}: ${msg.pass_count || 0}  ${__("FAIL")}: ${msg.fail_count || 0}  ${__("All Pass")}: ${msg.all_pass ? "YES" : "NO"}`,
					`${__("Authorize Repairs")}: ${msg.authorize_repairs ? "YES" : "NO — fix mismatches first"}`,
					"",
					[
						"Metric".padEnd(26),
						"Dash".padStart(6),
						"Scan".padStart(6),
						"Plan".padStart(6),
						"SQL".padStart(6),
						"Queue".padStart(6),
						"Status",
					].join(" "),
				];
				(msg.matrix || []).forEach((row) => {
					const cell = (v) => String(v == null ? "—" : v).padStart(6);
					lines.push(
						[
							String(row.metric || "").slice(0, 26).padEnd(26),
							cell(row.dashboard),
							cell(row.scan),
							cell(row.planner),
							cell(row.sql),
							cell(row.queue),
							row.status || "",
						].join(" ")
					);
					if (row.status === "FAIL" && row.reason) {
						lines.push(`  → ${row.reason}`);
					}
				});
				this.$preview.text(lines.join("\n"));
				if (msg.dashboard) {
					this.last_dashboard = msg.dashboard;
					this.render_dashboard(msg.dashboard);
				}
				if (!msg.all_pass) {
					frappe.show_alert({
						message: __("Dashboard validation FAILED — do not repair until all KPIs PASS"),
						indicator: "red",
					});
				} else {
					frappe.show_alert({ message: __("Dashboard validation PASS"), indicator: "green" });
				}
			},
			error: () => this.end_progress(),
		});
	}

	rebuild_metrics() {
		this.start_progress(__("Rebuilding metrics (Scan All)..."));
		frappe.call({
			method: `${this.api}.rebuild_metrics_api`,
			args: { company: this.company.get_value() },
			freeze: true,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				this.last_scan_all = msg;
				this.last_dashboard = msg.dashboard || {};
				this.scan_all_company = this.company && this.company.get_value();
				this._invalidate_topic_rows(null);
				if (msg.dashboard) this.render_dashboard(msg.dashboard);
				this.$preview.text(
					__("Metrics rebuilt from fresh Scan All.") +
						"\n" +
						__("Topic grids cleared — click Scan to load rows for the current topic.")
				);
				this.render_table();
				frappe.show_alert({ message: __("Dashboard refreshed"), indicator: "green" });
			},
			error: () => this.end_progress(),
		});
	}
}


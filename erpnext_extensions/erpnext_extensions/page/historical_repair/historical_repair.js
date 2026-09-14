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
	{ id: "posting", title: __("Posting Order"), section: "Production Posting Order" },
	{ id: "zero", title: __("Zero / Lost Rate") },
	{ id: "manufacture", title: __("Manufacture Valuation") },
	{ id: "sle", title: __("SLE / Bin Integrity") },
	{ id: "gl", title: __("GL Integrity") },
	{ id: "riv", title: __("Failed RIV") },
];

const KPI_ORDER = [
	"Integrity Score",
	"Posting Order",
	"Wrong Rate",
	"Zero Rate",
	"Wrong Amount",
	"Wrong Valuation",
	"Wrong Incoming",
	"Wrong Outgoing",
	"Wrong Average",
	"Broken SABB",
	"Broken Bin",
	"Broken GL",
	"Failed RIV",
	"Patient Zero",
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
		this.access = this._access_from_boot();
		this.$body = $(page.body);
		this.render();
		this._bind_keys();
		this.scan_all({ auto: true });
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
		const $actions = $('<div class="hr-actions">').appendTo(this.$toolbar);
		const g1 = $('<div class="hr-action-group" data-group="scan">').appendTo($actions);
		this.btn_scan = this._btn(g1, "scan", __("Scan"), () => this.scan(), "btn-default", __("Scan this topic (S)"));
		this.btn_scan_all = this._btn(g1, "scan-all", __("Scan All"), () => this.scan_all(), "btn-default", __("Scan every topic into the dashboard (Shift+A)"));
		this.btn_dry = this._btn(g1, "dry-run", __("Dry Run"), () => this.dry_run(), "btn-primary", __("Preview writes. Never executes (D)"));
		if (this.access.can_repair) {
			const g2 = $('<div class="hr-action-group" data-group="repair">').appendTo($actions);
			this.btn_repair = this._btn(g2, "repair", __("Repair Selected"), () => this.repair_bulk("selected"), "btn-danger", __("Repair checked EXACT rows"));
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
		["Select All", "Unselect All", "Select EXACT", "Select Repairable", "Select Visible Rows", "Select Current Page"].forEach((label) => {
			this._btn(this.$tools, label.toLowerCase().replace(/\s+/g, "-"), __(label), () => this.select_by(label));
		});
		this._btn(this.$tools, "columns", __("Columns"), () => this.toggle_columns());
		this._btn(this.$tools, "export-csv", __("Export CSV"), () => this.export_grid("csv"));
		this._btn(this.$tools, "export-excel", __("Export Excel"), () => this.export_grid("xlsx"));
		this._btn(this.$tools, "copy-selected", __("Copy selected"), () => this.copy_selected());
		this.$columns = $('<div class="hr-columns" data-role="columns" style="display:none">').appendTo(this.$shell);
		this.$progress = $('<div class="hr-progress"><div class="hr-progress-bar"></div></div>').appendTo(this.$shell);
		this.$eta = $('<div class="text-muted hr-eta" data-role="eta">').appendTo(this.$shell);
		this.$table = $('<div class="hr-table-wrap">').appendTo(this.$shell);
		this.$recon = $('<div class="hr-recon" data-role="reconstruction">').appendTo(this.$shell);
		this.$deps = $('<div class="hr-deps" data-role="dependency-resolution">').appendTo(this.$shell);
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
	}

	_lock_writes(lock) {
		["btn_repair", "btn_repair_filter", "btn_repair_page", "btn_repair_scope"].forEach((k) => {
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
		this.topic = id;
		this.rows = [];
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
		this.$preview.text(__("Scan All fills the dashboard. Choose a problem, then Dry Run. No writes until Repair after Dry Run, Impact, and DATABASE BACKUP REQUIRED."));
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
			from_date: this.from_date.get_value(),
			to_date: this.to_date.get_value(),
			include_likely: 1,
		};
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
		const map = {
			posting: [`${this.ppo}.scan_posting_order_anomalies`, this.filters()],
			zero: [`${this.api}.scan_zero_rates`, { company: this.company.get_value() }],
			manufacture: [`${this.api}.scan_manufacture`, { company: this.company.get_value() }],
			sle: [`${this.api}.scan_sle_bin_api`, { company: this.company.get_value() }],
			gl: [`${this.api}.scan_gl_api`, { company: this.company.get_value() }],
			riv: [`${this.api}.scan_failed_riv_api`, {}],
		};
		const [method, args] = map[this.topic];
		this.start_progress();
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				this.end_progress();
				if (this.cancelled) return;
				const msg = r.message || {};
				this.rows = msg.rows || msg || [];
				if (!Array.isArray(this.rows)) this.rows = [];
				this.dry_run_done = false;
				this.impact_done = false;
				if (this.btn_repair) this._lock_writes(true);
				this.render_table();
				this.$preview.text(__("Scan complete. Run Dry Run before repairing."));
				if (opts && opts.select_voucher) this._select_and_show(opts.select_voucher);
			},
			error: (err) => {
				this.end_progress();
				this.$preview.text(__("Scan failed: {0}", [(err && err.message) || __("Request error")]));
			},
		});
	}

	dry_run() {
		const rows = this.selected_rows();
		const map = {
			posting: [`${this.ppo}.dry_run_posting_order_repair`, rows.length ? { rows } : this.filters()],
			zero: [`${this.api}.dry_run_zero_rates`, rows.length ? { rows } : { company: this.company.get_value() }],
			manufacture: [`${this.api}.dry_run_manufacture`, rows.length ? { rows } : { company: this.company.get_value() }],
			sle: [`${this.api}.scan_sle_bin_api`, { company: this.company.get_value() }],
			gl: [`${this.api}.scan_gl_api`, { company: this.company.get_value() }],
			riv: [`${this.api}.scan_failed_riv_api`, {}],
		};
		const [method, args] = map[this.topic];
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				this.dry_run_done = true;
				const msg = r.message || {};
				this.rows = msg.rows || this.rows;
				this.render_table();
				this.$preview.text(this.format_preview(msg));
				this._complete_impact(msg);
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
		return !!(row && row.planner_status === "READY" && Number(row.sql_updates || 0) > 0);
	}

	_impact_executable(impact) {
		return (
			!!impact &&
			impact.executable !== false &&
			!impact.aborted &&
			(impact.estimated_sql_updates || impact.sql_updates || 0) > 0 &&
			impact.planner_status === "READY"
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
		if (this.topic === "posting") {
			method = `${this.ppo}.repair_posting_order_selected`;
			args = { rows, dry_run: 0, expected_signatures: rows.map((r) => r.dependency_signature) };
		} else if (this.topic === "zero") {
			method = `${this.api}.repair_wrong_rates_selected`;
			args = { rows, dry_run: 0 };
		} else if (this.topic === "manufacture") {
			method = `${this.api}.repair_manufacture_selected_api`;
			args = { rows, dry_run: 0 };
		} else if (this.topic === "gl") {
			method = `${this.api}.rebuild_gl_selected`;
			args = { vouchers: rows.map((r) => r.voucher), dry_run: 0 };
		} else if (this.topic === "riv") {
			method = `${this.api}.retry_failed_riv_selected`;
			args = { names: rows.map((r) => r.riv_name), dry_run: 0 };
		} else {
			frappe.msgprint(__("Repair Selected is not enabled for this topic. Use Repost Selected after SLE is healthy."));
			return;
		}
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				this._show_repair_result(r.message || {});
				this.dry_run_done = false;
				this.impact_done = false;
				if (this.btn_repair) this._lock_writes(true);
				this.integrity();
			},
		});
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
						callback: (rr) => this.$preview.text(JSON.stringify(rr.message || {}, null, 2)),
					});
				});
			},
		});
	}

	rebuild_affected_documents() {
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
						callback: (rr) => this.$preview.text(this.format_preview(rr.message || {})),
					});
				});
			},
		});
	}

	replay_downstream() {
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
						callback: (rr) => this.$preview.text(this.format_preview(rr.message || {})),
					});
				});
			},
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
		if (auto) this.$eta.text(__("Scan All… filling dashboard"));
		frappe.call({
			method: `${this.api}.scan_all`,
			args: { company: this.company.get_value() },
			freeze: !auto,
			callback: (r) => {
				this.end_progress();
				const msg = r.message || {};
				this.dry_run_done = false;
				this.impact_done = false;
				this._lock_writes(true);
				this.render_dashboard(msg.dashboard || {});
				this.$preview.text(this.format_preview(msg));
			},
			error: () => {
				this.end_progress();
				this.$preview.text(__("Scan All failed. Retry Scan All. No writes were attempted."));
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
		if (/Posting/i.test(label)) this.switch_topic("posting");
		else if (/Zero|Wrong|Rate|Amount|Incoming|Outgoing|Average|Patient|Repairable|Manual|Ambiguous/i.test(label))
			this.switch_topic("zero");
		else if (/Bin|SABB/i.test(label)) this.switch_topic("sle");
		else if (/GL/i.test(label)) this.switch_topic("gl");
		else if (/RIV|Replay/i.test(label)) this.switch_topic("riv");
		else return;
		if (/Amount|Valuation|Incoming|Outgoing|Average|Repairable|Manual|Ambiguous|Patient/i.test(label)) {
			this.$search.val(label.replace("Wrong ", "").replace(" Rate", ""));
		}
		this.scan();
	}

	select_by(mode) {
		this.$table.find("input[type=checkbox][data-idx]").each((_, el) => {
			const idx = parseInt(el.getAttribute("data-idx"), 10);
			const row = this.rows[idx] || {};
			let on = false;
			if (mode === "Select All") on = true;
			else if (mode === "Unselect All") on = false;
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
		lines.push(`ready: ${(msg.rows || []).filter((r) => r.planner_status === "READY" && Number(r.sql_updates || 0) > 0).length}`);
		if (msg.elapsed_seconds != null) lines.push(`elapsed_seconds: ${msg.elapsed_seconds}`);
		(msg.rows || msg.applied || []).slice(0, 40).forEach((row, i) => {
			lines.push("");
			lines.push(`--- case ${i + 1} ${row.voucher || row.chain || row.riv_name || ""} ---`);
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
				__("Required Action"),
				__("Blocker"),
				__("Confidence"),
				__("Status"),
			];
		}
		if (this.topic === "zero") {
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
				row.required_action || "",
				row.blocker || row.reason || row.skip_reason || "",
				row.confidence,
				this.status_label(row),
			];
		}
		if (this.topic === "zero") {
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
		if (!this.rows.length) {
			this.$table.append(
				$('<div class="hr-empty">').html(
					"<strong>" +
						__("No anomalies in this topic.") +
						"</strong><div>" +
						__("Run Scan All, pick a dashboard card, then Scan this tab.") +
						"</div>"
				)
			);
		} else if (!rows.length) {
			this.$table.append($('<div class="hr-empty">').text(__("No rows match the current search or column filters.")));
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

	start_progress() {
		this.cancelled = false;
		this._progress_t0 = Date.now();
		this.$progress.show();
		this.$eta.text(__("Working…"));
	}

	end_progress() {
		const elapsed = this._progress_t0 ? ((Date.now() - this._progress_t0) / 1000).toFixed(1) : "";
		this.$eta.text(elapsed ? __("Elapsed {0}s", [elapsed]) : "");
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
			[__("Immediate blocker"), src.immediate_blocker || (impact && impact.immediate_blocker)],
			[__("Root blocker"), src.root_blocker || (impact && impact.root_blocker)],
			[__("Root status"), src.root_status || (impact && impact.root_status)],
			[__("Dependency depth"), src.dependency_depth != null ? src.dependency_depth : impact && impact.dependency_depth],
			[__("Repair sequence"), src.repair_order || (impact && impact.repair_sequence)],
			[__("Estimated repair count"), src.estimated_repair_count || (impact && impact.estimated_repair_count)],
			[__("Estimated runtime"), src.estimated_runtime || (impact && impact.estimated_replay_seconds)],
			[__("Required action"), src.required_action || (impact && impact.required_action)],
			[__("Blocked because"), src.blocked_because],
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

	_tree_el(node, depth) {
		depth = depth || 0;
		const $box = $('<div class="hr-deps-node">').css("margin-left", depth ? 18 : 0);
		const voucher = node.voucher || "";
		const $row = $("<div>");
		if (depth) $row.append($("<span class='text-muted'>").text("├── waits for "));
		if (voucher) $row.append(depth ? this.link_cell(voucher, "Stock Entry") : $("<strong>").text(voucher));
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
		const rootReady = (row && row.root_status === "READY") || (impact && impact.root_status === "READY");
		const noPath = (row && row.no_repair_path) || (impact && impact.no_repair_path);
		this.btn_repair_chain.prop("disabled", !(this.access.can_repair && rootReady && !noPath));
	}

	go_to_root_cause(explicit) {
		const row = this.selected_rows()[0] || this.rows[0] || {};
		const root = explicit || row.root_blocker || row.root_patient_zero || row.first_actionable;
		if (!root) {
			frappe.msgprint(__("No root cause voucher on this row."));
			return;
		}
		if (this.voucher) this.voucher.set_value(root);
		this.$search && this.$search.val(root);
		const topic = row.root_topic === "POSTING_ORDER" ? "posting" : "zero";
		if (this.topic !== topic) this.switch_topic(topic);
		if (this.voucher) this.voucher.set_value(root);
		if (topic === "zero") {
			frappe.call({
				method: `${this.api}.scan_wrong_rates_api`,
				args: { voucher: root, company: this.company.get_value() },
				freeze: true,
				callback: (r) => {
					const rows = (r.message && r.message.rows) || [];
					if (rows.length) {
						this.rows = rows;
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
}

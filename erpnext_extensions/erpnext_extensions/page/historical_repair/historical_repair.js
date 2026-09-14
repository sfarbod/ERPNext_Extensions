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
		this.$tabs = $('<div class="hr-tabs">').appendTo(this.$body);
		TOPICS.forEach((t) => {
			const $b = $('<button type="button" class="btn btn-xs hr-tab">')
				.attr("data-topic", t.id)
				.text(t.title)
				.appendTo(this.$tabs);
			if (t.id === this.topic) $b.addClass("btn-primary");
			else $b.addClass("btn-default");
			$b.on("click", () => this.switch_topic(t.id));
		});
		this.$dashboard = $('<div class="hr-dashboard" data-role="dashboard">').appendTo(this.$body);
		this.render_dashboard({});
		this.$title = $('<h4 class="hr-section-title">').appendTo(this.$body);
		this.$toolbar = $('<div class="hr-toolbar">').appendTo(this.$body);
		this.company = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: { fieldtype: "Link", options: "Company", label: __("Company") },
			render_input: true,
		});
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
		this.btn_scan = this._btn(g1, "scan", __("Scan"), () => this.scan());
		this.btn_scan_all = this._btn(g1, "scan-all", __("Scan All"), () => this.scan_all());
		this.btn_dry = this._btn(g1, "dry-run", __("Dry Run"), () => this.dry_run(), "btn-primary");
		if (this.access.can_repair) {
			const g2 = $('<div class="hr-action-group" data-group="repair">').appendTo($actions);
			this.btn_repair = this._btn(g2, "repair", __("Repair Selected"), () => this.repair_selected(), "btn-danger");
			this.btn_replay_ds = this._btn(g2, "replay-downstream", __("Replay Downstream"), () => this.replay_downstream(), "btn-info");
			this.btn_rebuild_docs = this._btn(g2, "rebuild-docs", __("Rebuild Affected Documents"), () => this.rebuild_affected_documents(), "btn-info");
			this.btn_repost = this._btn(g2, "repost", __("Repost Selected"), () => this.repost_selected(), "btn-warning");
			this.btn_repair.prop("disabled", true);
		}
		const g3 = $('<div class="hr-action-group" data-group="inspect">').appendTo($actions);
		this.btn_integrity = this._btn(g3, "integrity", __("Integrity Check"), () => this.integrity());
		this.btn_graph = this._btn(g3, "graph", __("Graph"), () => this.show_graph());
		this.btn_history = this._btn(g3, "history", __("Repair History"), () => this.show_history());
		if (this.access.can_repair) {
			const $adv = $('<label class="hr-advanced">').appendTo(g3);
			this.$advanced = $('<input type="checkbox" data-role="advanced">').appendTo($adv);
			$adv.append(document.createTextNode(" " + __("Advanced Mode")));
			this.$advanced.on("change", () => {
				this.advanced = this.$advanced.prop("checked");
				this.$admin_group && this.$admin_group.toggle(this.advanced);
			});
		}
		if (this.access.can_repair) {
			this.$admin_group = $('<div class="hr-action-group" data-group="admin">').appendTo($actions);
			this.btn_resume = this._btn(this.$admin_group, "resume", __("Resume"), () => this.resume());
			this.btn_cancel = this._btn(this.$admin_group, "cancel", __("Cancel"), () => { this.cancelled = true; });
			if (this.access.can_admin) {
				this.btn_rollback = this._btn(this.$admin_group, "rollback", __("Rollback"), () => this.rollback_last());
				this.btn_benchmark = this._btn(this.$admin_group, "benchmark", __("Benchmark"), () => this.run_benchmark());
			}
			this.$admin_group.toggle(false);
		}
		this.$tools = $('<div class="hr-grid-tools">').appendTo(this.$body);
		this.$search = $('<input class="form-control input-sm hr-search" data-role="search" placeholder="Search">').appendTo(this.$tools);
		this.$search.on("input", () => this.render_table());
		["Select All", "Unselect All", "Select EXACT", "Select Repairable", "Select Visible Rows", "Select Current Page"].forEach((label) => {
			this._btn(this.$tools, label.toLowerCase().replace(/\s+/g, "-"), __(label), () => this.select_by(label));
		});
		this._btn(this.$tools, "columns", __("Columns"), () => this.toggle_columns());
		this._btn(this.$tools, "export-csv", __("Export CSV"), () => this.export_grid("csv"));
		this._btn(this.$tools, "export-excel", __("Export Excel"), () => this.export_grid("xlsx"));
		this._btn(this.$tools, "copy-selected", __("Copy selected"), () => this.copy_selected());
		this.$columns = $('<div class="hr-columns" data-role="columns" style="display:none">').appendTo(this.$body);
		this.$progress = $('<div class="hr-progress"><div class="hr-progress-bar"></div></div>').appendTo(this.$body);
		this.$eta = $('<div class="text-muted" data-role="eta">').appendTo(this.$body);
		this.$table = $('<div class="hr-table-wrap">').appendTo(this.$body);
		this.$recon = $('<div class="hr-recon" data-role="reconstruction">').appendTo(this.$body);
		this.$graph = $('<div class="hr-graph" data-role="graph">').appendTo(this.$body);
		this.$preview = $('<pre class="hr-preview" data-role="preview">').appendTo(this.$body);
		this.switch_topic(this.topic);
	}

	_btn($parent, action, label, fn, extra = "btn-default") {
		return $(`<button type="button" class="btn ${extra} btn-sm" data-action="${action}">`)
			.text(label)
			.appendTo($parent)
			.on("click", fn);
	}

	switch_topic(id) {
		this.topic = id;
		this.rows = [];
		this.dry_run_done = false;
		this.impact_done = false;
		this.load_layout();
		if (this.btn_repair) this.btn_repair.prop("disabled", true);
		this.$tabs.find(".hr-tab").each((_, el) => {
			const on = el.getAttribute("data-topic") === id;
			$(el).toggleClass("btn-primary", on).toggleClass("btn-default", !on);
		});
		const meta = TOPICS.find((t) => t.id === id);
		this.$title.text(meta.section || meta.title);
		this.$preview.text(__("Click Scan or Dry Run. No writes until Repair Selected after Dry Run."));
		this.render_table();
	}

	filters() {
		return {
			company: this.company.get_value(),
			from_date: this.from_date.get_value(),
			to_date: this.to_date.get_value(),
			include_likely: 1,
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

	scan() {
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
				if (this.btn_repair) this.btn_repair.prop("disabled", true);
				this.render_table();
				this.$preview.text(__("Scan complete. Run Dry Run before repairing."));
			},
			error: () => this.end_progress(),
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
		const payload = rows.length ? rows : (this.rows || []).filter((r) => r.eligible).slice(0, 25);
		if (!payload.length) {
			this.impact_done = true;
			if (this.btn_repair && this.access.can_repair) this.btn_repair.prop("disabled", false);
			return;
		}
		frappe.call({
			method: `${this.api}.impact_analysis_api`,
			args: { rows: payload },
			callback: (ir) => {
				const impact = ir.message || {};
				this.last_impact = impact;
				this.impact_done = !impact.aborted;
				this.$preview.text((this.format_preview(scan_msg || {}) + "\n\n" + (impact.preview_text || "")).trim());
				if (this.btn_repair && this.access.can_repair && this.dry_run_done && this.impact_done) {
					this.btn_repair.prop("disabled", false);
				}
			},
			error: () => {
				this.impact_done = false;
				if (this.btn_repair) this.btn_repair.prop("disabled", true);
			},
		});
	}

	repair_selected() {
		if (!this.dry_run_done || !this.impact_done) {
			frappe.msgprint(__("No repair without Dry Run and Impact Analysis."));
			return;
		}
		const rows = this.selected_rows().filter((r) => r.eligible);
		if (!rows.length && this.topic !== "gl" && this.topic !== "riv") {
			frappe.msgprint(__("Select at least one eligible EXACT row."));
			return;
		}
		frappe.call({
			method: `${this.api}.impact_analysis_api`,
			args: { rows: rows.length ? rows : this.selected_rows() },
			freeze: true,
			callback: (ir) => {
				const impact = ir.message || {};
				this.last_impact = impact;
				this.$preview.text(impact.preview_text || JSON.stringify(impact, null, 2));
				if (impact.aborted) {
					frappe.msgprint(__("Repair aborted: ambiguity or poison dependency. Nothing executed."));
					return;
				}
				const warn = __("DATABASE BACKUP REQUIRED. Apply this identity-scoped repair?");
				frappe.confirm(warn + "\n\n" + (impact.preview_text || ""), () => this._execute_repair(rows));
			},
		});
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
				this.$preview.text(JSON.stringify(r.message || {}, null, 2));
				this.dry_run_done = false;
				this.impact_done = false;
				if (this.btn_repair) this.btn_repair.prop("disabled", true);
				this.scan();
			},
		});
	}

	repost_selected() {
		const row = this.selected_rows()[0] || {};
		if (!row.item && !row.item_code) {
			frappe.msgprint(__("Select an item + warehouse row. This is not a global repost."));
			return;
		}
		frappe.call({
			method: `${this.api}.preview_repost`,
			args: {
				item_code: row.item || row.item_code,
				warehouse: row.warehouse,
				batch: row.batch,
				from_date: row.posting_date,
			},
			callback: (r) => {
				const preview = r.message || {};
				this.$preview.text(JSON.stringify(preview, null, 2));
				if (!preview.eligible) {
					frappe.msgprint(__("Repost Selected blocked until the chain passes Integrity."));
					return;
				}
				frappe.confirm(__("DATABASE BACKUP REQUIRED. Queue Item+Warehouse RIV for this identity only?"), () => {
					frappe.call({
						method: `${this.api}.repost_selected_api`,
						args: {
							item_code: row.item || row.item_code,
							warehouse: row.warehouse,
							from_date: row.posting_date,
							dry_run: 0,
						},
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

	scan_all() {
		frappe.call({
			method: `${this.api}.scan_all`,
			args: { company: this.company.get_value() },
			freeze: true,
			callback: (r) => {
				const msg = r.message || {};
				this.render_dashboard(msg.dashboard || {});
				this.$preview.text(this.format_preview(msg));
			},
		});
	}

	render_dashboard(dash) {
		this.$dashboard.empty();
		KPI_ORDER.forEach((label) => {
			const n = dash && dash[label] != null ? dash[label] : "—";
			const $chip = $('<button type="button" class="hr-kpi">')
				.append($('<span class="hr-kpi-label">').text(label))
				.append($('<b class="hr-kpi-value">').text(n));
			if (label === "Integrity Score") $chip.addClass("hr-kpi-score");
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
			else if (mode === "Select EXACT") on = row.confidence === "EXACT";
			else if (mode === "Select Repairable") on = !!row.eligible;
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
							n.repair_status,
						]
							.filter(Boolean)
							.join(" · ")
					);
					$a.on("click", () => frappe.set_route("Form", "Stock Entry", n.voucher));
					const $meta = $("<div class='hr-graph-meta'>").text(
						`${n.purpose || ""} · ${n.item || ""} · ${n.warehouse || ""} · ${n.batch || ""} · qty ${n.qty ?? ""} · rate ${n.rate ?? ""} · #${n.replay_order ?? ""} · depth ${n.replay_depth ?? ""} · ${n.repair_status || n.status || ""}`
					);
					$box.append($("<div>").append($a).append($meta));
				});
				this.$preview.text(JSON.stringify(g, null, 2));
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
				__("Status"),
			];
		}
		return ["", __("Voucher"), __("Item"), __("Warehouse"), __("Confidence"), __("Status")];
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
				row.status,
			];
		}
		return [
			row.voucher || row.riv_name,
			row.item || row.item_code,
			row.warehouse,
			row.confidence,
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
			const $tr = $("<tr>").attr("title", row.reason || row.dependency_reason || (row.flags || []).join(", ") || row.rate_source || row.status || "");
			if (row.eligible) $tr.addClass("hr-row-exact");
			else if (row.confidence === "LIKELY") $tr.addClass("hr-row-likely");
			else if (/POISON|AMBIGUOUS|BLOCKED|MANUAL/i.test(String(row.status || ""))) $tr.addClass("hr-row-blocked");
			const $cb = $('<input type="checkbox">')
				.attr("data-idx", idx)
				.attr("data-eligible", row.eligible ? "1" : "0");
			$tr.append($("<td>").append($cb));
			this.cells_for_row(row).forEach((val, i, arr) => {
				if (this.hidden_cols.has(i + 1)) return;
				const $td = $("<td>");
				this.fill_cell($td, val, i, row);
				if (i === arr.length - 1) {
					$td.addClass(`hr-status-${row.status || ""}`);
					if (row.optimizer_status) $td.addClass(`hr-status-${row.optimizer_status}`);
				}
				$tr.append($td);
			});
			$tr.on("click", (e) => {
				if (e.target && e.target.type === "checkbox") return;
				this.show_reconstruction(row);
			});
			$body.append($tr);
		});
		$table.append($body);
		this.$table.empty().append($table);
		if (!this.rows.length) {
			this.$table.append($("<p>").text(__("No anomalies in this topic.")));
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
		this.$progress.find(".hr-progress-bar").css("width", "15%");
		this.$eta.text(__("Working…"));
	}

	end_progress() {
		this.$progress.find(".hr-progress-bar").css("width", "100%");
		const elapsed = this._progress_t0 ? ((Date.now() - this._progress_t0) / 1000).toFixed(1) : "";
		this.$eta.text(elapsed ? __("Elapsed {0}s", [elapsed]) : "");
		setTimeout(() => this.$progress.hide().find(".hr-progress-bar").css("width", "0"), 400);
	}

	fill_cell($td, val, i, row) {
		const s = val == null ? "" : String(val);
		if (!s) {
			$td.text("");
			return;
		}
		if (/^MAT-STE-/.test(s) || /^MFG-WO-/.test(s)) {
			$td.append(this.link_cell(s, /^MFG-WO-/.test(s) ? "Work Order" : "Stock Entry"));
			return;
		}
		const item_i = this.topic === "posting" || this.topic === "zero" ? 3 : 1;
		const wh_i = this.topic === "posting" || this.topic === "zero" ? 4 : 2;
		const batch_i = this.topic === "posting" || this.topic === "zero" ? 5 : -1;
		if (i === item_i) {
			$td.append(this.link_cell(s, "Item"));
			return;
		}
		if (i === wh_i) {
			$td.append(this.link_cell(s, "Warehouse"));
			return;
		}
		if (i === batch_i && row.batch) {
			$td.append(this.link_cell(row.batch, "Batch"));
			return;
		}
		$td.text(s);
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

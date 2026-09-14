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

class HistoricalRepairPage {
	constructor(page) {
		this.page = page;
		this.ppo = "erpnext_extensions.iran_accounting.stock_posting_order.api";
		this.api = "erpnext_extensions.iran_accounting.historical_stock.api";
		this.topic = "posting";
		this.rows = [];
		this.dry_run_done = false;
		this.visible_rows = [];
		this.sort_keys = [];
		this.cancelled = false;
		this.$body = $(page.body);
		this.render();
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
		this.$title = $('<h4 class="hr-section-title">').appendTo(this.$body);
		this.$dashboard = $('<div class="hr-dashboard" data-role="dashboard">').appendTo(this.$body);
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
		this.btn_scan = this._btn($actions, "scan", __("Scan"), () => this.scan());
		this.btn_scan_all = this._btn($actions, "scan-all", __("Scan All"), () => this.scan_all());
		this.btn_dry = this._btn($actions, "dry-run", __("Dry Run"), () => this.dry_run(), "btn-primary");
		this.btn_repair = this._btn($actions, "repair", __("Repair Selected"), () => this.repair_selected(), "btn-danger");
		this.btn_repost = this._btn($actions, "repost", __("Repost Selected"), () => this.repost_selected(), "btn-warning");
		this.btn_integrity = this._btn($actions, "integrity", __("Integrity Check"), () => this.integrity());
		this.btn_rebuild_docs = this._btn(
			$actions,
			"rebuild-docs",
			__("Rebuild Affected Documents"),
			() => this.rebuild_affected_documents(),
			"btn-info"
		);
		this.btn_replay_ds = this._btn(
			$actions,
			"replay-downstream",
			__("Replay Downstream"),
			() => this.replay_downstream(),
			"btn-info"
		);
		this.btn_graph = this._btn($actions, "graph", __("Graph"), () => this.show_graph());
		this.btn_history = this._btn($actions, "history", __("Repair History"), () => this.show_history());
		this.btn_rollback = this._btn($actions, "rollback", __("Rollback"), () => this.rollback_last());
		this.btn_resume = this._btn($actions, "resume", __("Resume"), () => this.resume());
		this.btn_cancel = this._btn($actions, "cancel", __("Cancel"), () => { this.cancelled = true; });
		this.btn_repair.prop("disabled", true);
		this.$tools = $('<div class="hr-grid-tools">').appendTo(this.$body);
		this.$search = $('<input class="form-control input-sm hr-search" data-role="search" placeholder="Search">').appendTo(this.$tools);
		this.$search.on("input", () => this.render_table());
		["Select All", "Unselect All", "Select EXACT", "Select Repairable", "Select Visible Rows"].forEach((label) => {
			this._btn(this.$tools, label.toLowerCase().replace(/\s+/g, "-"), __(label), () => this.select_by(label));
		});
		this._btn(this.$tools, "export-csv", __("Export CSV"), () => this.export_grid("csv"));
		this._btn(this.$tools, "export-excel", __("Export Excel"), () => this.export_grid("excel"));
		this._btn(this.$tools, "copy-selected", __("Copy selected"), () => this.copy_selected());
		this.$progress = $('<div class="hr-progress"><div class="hr-progress-bar"></div></div>').appendTo(this.$body);
		this.$eta = $('<div class="text-muted" data-role="eta">').appendTo(this.$body);
		this.$table = $('<div class="hr-table-wrap">').appendTo(this.$body);
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
		this.btn_repair.prop("disabled", true);
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
		frappe.call({
			method,
			args,
			freeze: true,
			callback: (r) => {
				const msg = r.message || {};
				this.rows = msg.rows || msg || [];
				if (!Array.isArray(this.rows)) this.rows = [];
				this.dry_run_done = false;
				this.btn_repair.prop("disabled", true);
				this.render_table();
				this.$preview.text(__("Scan complete. Run Dry Run before repairing."));
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
				this.btn_repair.prop("disabled", false);
				this.$preview.text(this.format_preview(msg));
			},
		});
	}

	repair_selected() {
		if (!this.dry_run_done) {
			frappe.msgprint(__("No repair without Dry Run."));
			return;
		}
		const rows = this.selected_rows().filter((r) => r.eligible);
		if (!rows.length && this.topic !== "gl" && this.topic !== "riv") {
			frappe.msgprint(__("Select at least one eligible EXACT row."));
			return;
		}
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
				this.btn_repair.prop("disabled", true);
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
				frappe.confirm(__("Queue Item+Warehouse RIV for this identity only?"), () => {
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
				frappe.confirm(__("Apply identity-scoped rebuild? Database backup recommended."), () => {
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
				frappe.confirm(__("Apply this identity-scoped downstream replay? Database backup recommended."), () => {
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
		Object.entries(dash).forEach(([label, n]) => {
			const $chip = $('<button type="button" class="hr-chip">').text(label).append($("<b>").text(n));
			$chip.on("click", () => {
				if (/Posting/i.test(label)) this.switch_topic("posting");
				else if (/Zero|Wrong|Rate|Amount|Incoming|Outgoing|Avg/i.test(label)) this.switch_topic("zero");
				else if (/Bin|SABB/i.test(label)) this.switch_topic("sle");
				else if (/GL/i.test(label)) this.switch_topic("gl");
				else if (/RIV/i.test(label)) this.switch_topic("riv");
				this.scan();
			});
			this.$dashboard.append($chip);
		});
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
			else if (mode === "Select Visible Rows") on = true;
			el.checked = on;
		});
	}

	export_grid(kind) {
		const cols = this.columns_for_topic();
		const lines = [cols.slice(1).join(",")];
		(this.rows || []).forEach((row) => {
			lines.push(
				this.cells_for_row(row)
					.map((v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`)
					.join(",")
			);
		});
		const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = `historical-repair-${this.topic}.${kind === "excel" ? "csv" : "csv"}`;
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
					if (i) $box.append(document.createTextNode(" → "));
					const $a = $('<a class="hr-link">').text(n.voucher).attr("title", `${n.purpose || ""} ${n.item || ""}`);
					$a.on("click", () => frappe.set_route("Form", "Stock Entry", n.voucher));
					$box.append($a);
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
				frappe.confirm(__("Dry-run rollback of {0}?", [run.repair_run_id]), () => {
					frappe.call({
						method: `${this.api}.rollback_run_api`,
						args: { repair_run_id: run.repair_run_id, dry_run: 1 },
						callback: (rr) => {
							this.$preview.text(JSON.stringify(rr.message || {}, null, 2));
							frappe.confirm(__("Apply rollback snapshot?"), () => {
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
				__("Current Rate"),
				__("Historical Rate"),
				__("Proposed Rate"),
				__("Source of Truth"),
				__("Flags"),
				__("Current"),
				__("Expected"),
				__("Difference"),
				__("Confidence"),
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
				row.current_rate,
				row.historical_rate,
				row.proposed_rate,
				row.source_of_truth,
				(row.flags || []).join(",") || row.mismatch_class || "",
				row.current ?? row.current_rate,
				row.expected ?? row.proposed_rate,
				row.difference,
				row.confidence,
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
		cols.forEach((c, i) => {
			const $th = $("<th>").text(c.trim());
			if (i > 0) {
				$th.css("cursor", "pointer").on("click", () => {
					this.sort_keys = [{ i, dir: (this.sort_keys[0] && this.sort_keys[0].i === i && this.sort_keys[0].dir === 1) ? -1 : 1 }];
					this.render_table();
				});
			}
			$head.append($th);
		});
		$table.append($("<thead>").append($head));
		const $body = $("<tbody>");
		let rows = (this.rows || []).map((row, idx) => ({ row, idx }));
		if (q) {
			rows = rows.filter(({ row }) => JSON.stringify(row).toLowerCase().includes(q));
		}
		if (this.sort_keys && this.sort_keys[0]) {
			const { i, dir } = this.sort_keys[0];
			rows.sort((a, b) => {
				const av = String(this.cells_for_row(a.row)[i - 1] || "");
				const bv = String(this.cells_for_row(b.row)[i - 1] || "");
				return av.localeCompare(bv, undefined, { numeric: true }) * dir;
			});
		}
		rows.forEach(({ row, idx }) => {
			const $tr = $("<tr>").attr("title", row.reason || row.dependency_reason || (row.flags || []).join(", ") || row.status || "");
			const $cb = $('<input type="checkbox">')
				.attr("data-idx", idx)
				.attr("data-eligible", row.eligible ? "1" : "0");
			if (row.eligible) $cb.prop("checked", true);
			$tr.append($("<td>").append($cb));
			this.cells_for_row(row).forEach((val, i, arr) => {
				const $td = $("<td>");
				const s = val == null ? "" : String(val);
				if (/^MAT-STE-/.test(s) || (i < 3 && this.topic === "posting" && s && i <= 2)) {
					$td.append(this.link_cell(s, "Stock Entry"));
				} else {
					$td.text(s);
				}
				if (i === arr.length - 1) {
					$td.addClass(`hr-status-${row.status || ""}`);
					if (row.optimizer_status) $td.addClass(`hr-status-${row.optimizer_status}`);
				}
				$tr.append($td);
			});
			$body.append($tr);
		});
		$table.append($body);
		this.$table.empty().append($table);
		if (!this.rows.length) {
			this.$table.append($("<p>").text(__("No anomalies in this topic.")));
		}
	}
}

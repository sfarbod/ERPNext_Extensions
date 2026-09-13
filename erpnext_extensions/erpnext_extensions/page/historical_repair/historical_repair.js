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
		this.btn_dry = this._btn($actions, "dry-run", __("Dry Run"), () => this.dry_run(), "btn-primary");
		this.btn_repair = this._btn($actions, "repair", __("Repair Selected"), () => this.repair_selected(), "btn-danger");
		this.btn_repost = this._btn($actions, "repost", __("Repost Selected"), () => this.repost_selected(), "btn-warning");
		this.btn_integrity = this._btn($actions, "integrity", __("Integrity Check"), () => this.integrity());
		this.btn_resume = this._btn($actions, "resume", __("Resume"), () => this.resume());
		this.btn_repair.prop("disabled", true);
		this.$table = $('<div class="hr-table-wrap">').appendTo(this.$body);
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
			method = `${this.api}.repair_zero_rates_selected`;
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

	format_preview(msg) {
		const lines = [];
		lines.push(`status: ${msg.status || (msg.dry_run ? "DRY_RUN" : "SCAN")}`);
		if (msg.timing) {
			lines.push(`start_local: ${msg.timing.start_local || msg.start_local || ""}`);
			lines.push(`elapsed_seconds: ${msg.timing.elapsed_seconds || msg.duration_s || ""}`);
		}
		if (msg.summary) {
			lines.push(`same_time_groups: ${msg.summary.same_time_groups || 0}`);
			lines.push(`NO_REPAIR_NEEDED: ${msg.summary.NO_REPAIR_NEEDED || 0}`);
			lines.push(`REPAIRABLE_SECONDS: ${msg.summary.REPAIRABLE_SECONDS || 0}`);
			lines.push(`REAL_STOCK_SHORTAGE: ${msg.summary.REAL_STOCK_SHORTAGE || 0}`);
		}
		lines.push(`count: ${msg.count ?? (msg.rows || []).length}`);
		lines.push(`eligible: ${(msg.eligible || []).length}`);
		(msg.rows || []).slice(0, 40).forEach((row, i) => {
			lines.push("");
			lines.push(`--- case ${i + 1} ${row.voucher || row.chain || row.riv_name || ""} ---`);
			[
				"Item",
				"Warehouse",
				"Batch",
				"Patient Zero",
				"Current Rate",
				"Historical Rate",
				"Proposed Rate",
				"Source of Truth",
				"Confidence",
				"Status",
				"Minimum Seconds Required",
			].forEach((label) => {
				const key = {
					Item: row.item || row.item_code,
					Warehouse: row.warehouse,
					Batch: row.batch,
					"Patient Zero": (row.patient_zero && row.patient_zero.voucher_no) || "",
					"Current Rate": row.current_rate,
					"Historical Rate": row.historical_rate,
					"Proposed Rate": row.proposed_rate,
					"Source of Truth": row.source_of_truth,
					Confidence: row.confidence,
					Status: row.status,
					"Minimum Seconds Required": row.minimum_seconds_label,
				}[label];
				if (key !== undefined && key !== null && key !== "") lines.push(`${label}: ${key}`);
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
				__("Opening Qty"),
				__("Current Inbound Time"),
				__("Current Outbound Time"),
				__("Proposed Time"),
				__("Minimum Seconds Required"),
				__("Minimum Qty Before"),
				__("Minimum Qty After"),
				__("Valuation Impact"),
				__("GL Impact"),
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
				row.opening_qty,
				row.current_inbound_time,
				row.current_outbound_time,
				row.proposed_outbound_time,
				this.seconds_label(row),
				row.min_qty_before,
				row.min_qty_after,
				row.valuation_impact,
				row.gl_impact,
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
		const $table = $('<table class="hr-table" data-role="posting-order-table">');
		const $head = $("<tr>");
		cols.forEach((c) => $head.append($("<th>").text(c.trim())));
		$table.append($("<thead>").append($head));
		const $body = $("<tbody>");
		(this.rows || []).forEach((row, idx) => {
			const $tr = $("<tr>");
			const $cb = $('<input type="checkbox">')
				.attr("data-idx", idx)
				.attr("data-eligible", row.eligible ? "1" : "0");
			if (row.eligible) $cb.prop("checked", true);
			$tr.append($("<td>").append($cb));
			this.cells_for_row(row).forEach((val, i, arr) => {
				const $td = $("<td>").text(val == null ? "" : String(val));
				if (i === arr.length - 1) $td.addClass(`hr-status-${row.status}`);
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

frappe.provide("erpnext_extensions.historical_repair");

frappe.pages["historical-repair"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Historical Repair"),
		single_column: true,
	});
	page.main.addClass("historical-repair-page");
	erpnext_extensions.historical_repair = new HistoricalRepairPage(page);
};

class HistoricalRepairPage {
	constructor(page) {
		this.page = page;
		this.api = "erpnext_extensions.iran_accounting.stock_posting_order.api";
		this.rows = [];
		this.dry_run_done = false;
		this.$body = $(page.body);
		this.render();
	}

	render() {
		this.$body.empty();
		$('<h4 class="hr-section-title">')
			.text(__("Production Posting Order"))
			.appendTo(this.$body);
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
		this.btn_scan = $(
			'<button type="button" class="btn btn-default btn-sm" data-action="scan">'
		)
			.text(__("Scan"))
			.appendTo($actions)
			.on("click", () => this.scan());
		this.btn_dry = $(
			'<button type="button" class="btn btn-primary btn-sm" data-action="dry-run">'
		)
			.text(__("Dry Run"))
			.appendTo($actions)
			.on("click", () => this.dry_run());
		this.btn_repair = $(
			'<button type="button" class="btn btn-danger btn-sm" data-action="repair" disabled>'
		)
			.text(__("Repair Selected"))
			.appendTo($actions)
			.on("click", () => this.repair_selected());
		this.btn_integrity = $(
			'<button type="button" class="btn btn-default btn-sm" data-action="integrity">'
		)
			.text(__("Integrity Check"))
			.appendTo($actions)
			.on("click", () => this.integrity());
		this.$table = $('<div class="hr-table-wrap">').appendTo(this.$body);
		this.$preview = $('<pre class="hr-preview" data-role="preview">').appendTo(this.$body);
		this.scan();
	}

	filters() {
		return {
			company: this.company.get_value(),
			from_date: this.from_date.get_value(),
			to_date: this.to_date.get_value(),
			include_likely: 1,
		};
	}

	scan() {
		frappe.call({
			method: `${this.api}.scan_posting_order_anomalies`,
			args: this.filters(),
			freeze: true,
			callback: (r) => {
				this.rows = r.message || [];
				this.dry_run_done = false;
				this.btn_repair.prop("disabled", true);
				this.render_table();
				this.$preview.text(__("Scan complete. Run Dry Run before repairing."));
			},
		});
	}

	selected_rows() {
		const selected = [];
		this.$table.find("input[type=checkbox][data-idx]:checked").each((_, el) => {
			const idx = parseInt(el.getAttribute("data-idx"), 10);
			if (this.rows[idx]) selected.push(this.rows[idx]);
		});
		return selected;
	}

	format_preview(msg) {
		const lines = [];
		lines.push(`status: ${msg.status || "DRY_RUN"}`);
		if (msg.timing) {
			lines.push(`start_local: ${msg.timing.start_local || ""}`);
			lines.push(`end_local: ${msg.timing.end_local || ""}`);
			lines.push(`elapsed_seconds: ${msg.timing.elapsed_seconds || ""}`);
		}
		if (msg.summary) {
			lines.push(`same_time_groups: ${msg.summary.same_time_groups || 0}`);
			lines.push(`NO_REPAIR_NEEDED: ${msg.summary.NO_REPAIR_NEEDED || 0}`);
			lines.push(`REPAIRABLE_SECONDS: ${msg.summary.REPAIRABLE_SECONDS || 0}`);
			lines.push(`REAL_STOCK_SHORTAGE: ${msg.summary.REAL_STOCK_SHORTAGE || 0}`);
			const dist = msg.summary.seconds_distribution || {};
			lines.push(`seconds_distribution: ${JSON.stringify(dist)}`);
		}
		lines.push(`count: ${msg.count ?? (msg.rows || []).length}`);
		lines.push(`eligible: ${(msg.eligible || []).length}`);
		const rows = msg.rows || [];
		rows.slice(0, 40).forEach((row, i) => {
			lines.push("");
			lines.push(`--- case ${i + 1} ${row.chain || ""} ---`);
			lines.push(`Item: ${row.item}`);
			lines.push(`Warehouse: ${row.warehouse}`);
			lines.push(`Batch: ${row.batch || ""}`);
			lines.push(`Posting Date: ${row.posting_date || ""}`);
			lines.push(`Opening Qty: ${row.opening_qty}`);
			lines.push(`Minimum Seconds Required: ${row.minimum_seconds_label || "No change"}`);
			lines.push(`Current Min Qty: ${row.min_qty_before}`);
			lines.push(`Proposed Min Qty: ${row.min_qty_after}`);
			lines.push(`Final Qty Before: ${row.final_qty_before}`);
			lines.push(`Final Qty After: ${row.final_qty_after}`);
			lines.push(`Status: ${row.status}`);
			(row.row_simulation || []).forEach((sim) => {
				lines.push(
					[
						sim.voucher,
						sim.purpose || "",
						`current ${sim.current_time}`,
						`qty ${sim.actual_qty}`,
						`run ${sim.current_running_qty}`,
						`proposed ${sim.proposed_time}`,
						`proposed_run ${sim.proposed_running_qty}`,
						`shift ${sim.seconds_shifted}`,
					].join(" | ")
				);
			});
		});
		return lines.join("\n");
	}

	dry_run() {
		const rows = this.selected_rows();
		frappe.call({
			method: `${this.api}.dry_run_posting_order_repair`,
			args: rows.length ? { rows } : this.filters(),
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
		if (!rows.length) {
			frappe.msgprint(__("Select at least one eligible EXACT row."));
			return;
		}
		frappe.call({
			method: `${this.api}.repair_posting_order_selected`,
			args: {
				rows,
				dry_run: 0,
				expected_signatures: rows.map((r) => r.dependency_signature),
			},
			freeze: true,
			callback: (r) => {
				this.$preview.text(JSON.stringify(r.message || {}, null, 2));
				this.dry_run_done = false;
				this.btn_repair.prop("disabled", true);
				this.scan();
			},
		});
	}

	integrity() {
		const rows = this.selected_rows();
		const vouchers = [];
		rows.forEach((r) => {
			vouchers.push(r.inbound_document, r.outbound_document);
		});
		const first = rows[0] || {};
		frappe.call({
			method: `${this.api}.posting_order_integrity_check`,
			args: {
				vouchers,
				item_code: first.item,
				warehouse: first.warehouse,
			},
			callback: (r) => {
				this.$preview.text(JSON.stringify(r.message || {}, null, 2));
			},
		});
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

	render_table() {
		const cols = [
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
			const cells = [
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
			cells.forEach((val, i) => {
				const $td = $("<td>").text(val == null ? "" : String(val));
				if (i === cells.length - 1) $td.addClass(`hr-status-${row.status}`);
				if (i === 10) $td.addClass("hr-min-seconds");
				$tr.append($td);
			});
			$body.append($tr);
		});
		$table.append($body);
		this.$table.empty().append($table);
		if (!this.rows.length) {
			this.$table.append($("<p>").text(__("No same-time production posting-order anomalies.")));
		}
	}
}

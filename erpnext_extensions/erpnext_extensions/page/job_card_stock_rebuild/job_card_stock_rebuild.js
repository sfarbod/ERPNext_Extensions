frappe.provide("erpnext_extensions.job_card_stock_rebuild");

frappe.pages["job-card-stock-rebuild"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Job Card Stock Rebuild"),
		single_column: true,
	});
	page.main.addClass("jc-stock-rebuild-page");
	erpnext_extensions.job_card_stock_rebuild = new JobCardStockRebuildPage(page);
};

class JobCardStockRebuildPage {
	constructor(page) {
		this.page = page;
		this.api = "erpnext_extensions.iran_accounting.job_card_stock_rebuild.api";
		this.scan = null;
		this.preview = null;
		this.dry = null;
		this.fingerprint = null;
		this.$body = $(page.body);
		this.render();
		this.apply_route_options();
	}

	can_run() {
		const roles = frappe.user_roles || [];
		return frappe.session.user === "Administrator" || roles.includes("System Manager");
	}

	render() {
		this.$body.empty();
		if (!this.can_run()) {
			this.$body.html(
				`<div class="jcsr-alert blocker">${__(
					"Only System Manager can run Job Card Stock Rebuild."
				)}</div>`
			);
			return;
		}

		this.$toolbar = $('<div class="jcsr-toolbar">').appendTo(this.$body);
		this.jc = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Link",
				options: "Job Card",
				label: __("Job Card"),
				reqd: 1,
				change: () => this.reset_state(),
			},
			render_input: true,
		});
		this.item = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Link",
				options: "Item",
				label: __("Item (optional)"),
				change: () => this.reset_state(),
			},
			render_input: true,
		});
		this.batch = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Data",
				label: __("Batch (optional)"),
				change: () => this.reset_state(),
			},
			render_input: true,
		});

		const $actions = $('<div class="jcsr-actions">').appendTo(this.$toolbar);
		this.btn_scan = $(`<button type="button" class="btn btn-default">${__("Scan")}</button>`)
			.appendTo($actions)
			.on("click", () => this.run_scan());
		this.btn_preview = $(
			`<button type="button" class="btn btn-primary" disabled>${__("Preview Rebuild")}</button>`
		)
			.appendTo($actions)
			.on("click", () => this.run_preview());
		this.btn_dry = $(
			`<button type="button" class="btn btn-warning" disabled>${__("Dry Run")}</button>`
		)
			.appendTo($actions)
			.on("click", () => this.run_dry());
		this.btn_apply = $(
			`<button type="button" class="btn btn-danger" disabled>${__("Confirm & Apply Rebuild")}</button>`
		)
			.appendTo($actions)
			.on("click", () => this.run_apply());

		$('<p class="jcsr-help">')
			.text(
				__(
					"Reconstructs Job Card material and secondary tracking from submitted Stock Entries / SLE. Does not cancel, amend, or regenerate Manufacture. SLE and GL are never altered."
				)
			)
			.appendTo(this.$body);

		this.$status = $('<div class="jcsr-status" data-role="status">').appendTo(this.$body);
		this.$materials = $('<div data-role="materials">').appendTo(this.$body);
		this.$secondary = $('<div data-role="secondary">').appendTo(this.$body);
		this.$evidence = $('<div data-role="evidence">').appendTo(this.$body);
		this.$downstream = $('<div data-role="downstream">').appendTo(this.$body);
	}

	apply_route_options() {
		const opts = frappe.route_options || {};
		frappe.route_options = null;
		if (opts.job_card) {
			this.jc.set_value(opts.job_card);
		}
	}

	reset_state() {
		this.scan = null;
		this.preview = null;
		this.dry = null;
		this.fingerprint = null;
		this.btn_preview.prop("disabled", true);
		this.btn_dry.prop("disabled", true);
		this.btn_apply.prop("disabled", true);
		this.$status.empty();
		this.$materials.empty();
		this.$secondary.empty();
		this.$evidence.empty();
		this.$downstream.empty();
	}

	args() {
		return {
			job_card: this.jc.get_value(),
			item: this.item.get_value() || null,
			batch: this.batch.get_value() || null,
		};
	}

	run_scan() {
		const args = this.args();
		if (!args.job_card) {
			frappe.msgprint(__("Job Card is required."));
			return;
		}
		this.reset_state();
		frappe.call({
			method: `${this.api}.scan`,
			args,
			freeze: true,
			freeze_message: __("Scanning stock evidence…"),
			callback: (r) => {
				this.scan = r.message || {};
				this.fingerprint = this.scan.fingerprint;
				this.render_result(this.scan, "SCAN");
				this.btn_preview.prop("disabled", false);
			},
		});
	}

	run_preview() {
		const args = this.args();
		frappe.call({
			method: `${this.api}.preview`,
			args,
			freeze: true,
			callback: (r) => {
				this.preview = r.message || {};
				this.fingerprint = this.preview.fingerprint;
				this.render_result(this.preview, "PREVIEW");
				const ready =
					this.preview.apply_allowed && this.preview.overall_status !== "BALANCED";
				this.btn_dry.prop("disabled", !ready);
				this.btn_apply.prop("disabled", true);
			},
		});
	}

	run_dry() {
		const args = this.args();
		args.fingerprint = this.fingerprint;
		frappe.call({
			method: `${this.api}.dry_run`,
			args,
			freeze: true,
			freeze_message: __("Dry-running rebuild (will roll back)…"),
			callback: (r) => {
				this.dry = r.message || {};
				this.render_result(this.dry, "DRY_RUN");
				this.render_downstream(this.dry.downstream);
				const pass = this.dry.dry_run_status === "DRY_RUN_PASS" && this.dry.apply_allowed;
				this.btn_apply.prop("disabled", !pass);
			},
		});
	}

	run_apply() {
		if (!this.dry || this.dry.dry_run_status !== "DRY_RUN_PASS") {
			frappe.msgprint(__("Successful Dry Run is required before Apply."));
			return;
		}
		frappe.confirm(
			__(
				"Apply Job Card tracking rebuild? Manufacture Stock Entries will NOT be changed. This cannot invent stock."
			),
			() => {
				const args = this.args();
				args.fingerprint = this.fingerprint;
				args.confirm = 1;
				frappe.call({
					method: `${this.api}.apply`,
					args,
					freeze: true,
					freeze_message: __("Applying rebuild…"),
					callback: (r) => {
						const msg = r.message || {};
						this.render_result(msg, "APPLY");
						this.render_downstream(msg.downstream);
						if (msg.overall_status === "REBUILT") {
							frappe.show_alert({
								message: __("Job Card rebuilt. Manufacture repair required: {0}", [
									msg.manufacture_repair_required ? __("YES") : __("NO"),
								]),
								indicator: "green",
							});
						}
						this.btn_apply.prop("disabled", true);
						this.btn_dry.prop("disabled", true);
					},
				});
			}
		);
	}

	render_result(data, mode) {
		const statuses = (data.statuses || []).join(", ");
		const balanced = data.overall_status === "BALANCED";
		const cls = balanced ? "ok" : data.apply_allowed ? "ready" : "warn";
		this.$status.html(`
			<div class="jcsr-alert ${cls}">
				<strong>${frappe.utils.escape_html(mode)}</strong>
				— ${frappe.utils.escape_html(data.overall_status || "")}
				${balanced ? " / " + __("NO REBUILD REQUIRED") : ""}
				<div class="jcsr-meta">
					${__("Statuses")}: ${frappe.utils.escape_html(statuses || "—")}
					<br>${__("Fingerprint")}: <code>${frappe.utils.escape_html(data.fingerprint || "")}</code>
					<br>${__("Manufacture repair required")}: ${
						data.manufacture_repair_required ? __("YES") : __("NO")
					}
					${
						data.manufacture_blocked_by_stage_output_configuration
							? "<br><span class='text-danger'>" +
							  __(
									"JOB_CARD_REBUILD_VALID BUT MANUFACTURE_BLOCKED_BY_STAGE_OUTPUT_CONFIGURATION"
							  ) +
							  "</span>"
							: ""
					}
					${data.posting_order_warning ? "<br>" + __("POSTING_ORDER_WARNING") : ""}
					${data.dry_run_status ? "<br>Dry Run: " + frappe.utils.escape_html(data.dry_run_status) : ""}
					${data.error ? "<br><span class='text-danger'>" + frappe.utils.escape_html(data.error) + "</span>" : ""}
					${data.note ? "<br>" + frappe.utils.escape_html(data.note) : ""}
				</div>
			</div>
		`);
		this.render_materials(data.material_rows || []);
		this.render_secondary((data.secondary && data.secondary.rows) || []);
		this.render_evidence(data.material_rows || []);
		if (balanced) {
			this.btn_apply.prop("disabled", true);
			this.btn_dry.prop("disabled", true);
		}
	}

	render_materials(rows) {
		this.$materials.empty();
		$('<h5 class="jcsr-section-title">').text(__("Raw Materials")).appendTo(this.$materials);
		if (!rows.length) {
			this.$materials.append(`<p class="jcsr-empty">${__("No material evidence.")}</p>`);
			return;
		}
		const $table = $(`
			<table class="jcsr-table">
				<thead>
					<tr>
						<th>${__("Item")}</th>
						<th>${__("Batch")}</th>
						<th>${__("Required")}</th>
						<th>${__("Issued")}</th>
						<th>${__("Returned")}</th>
						<th>${__("Transferred (cur→der)")}</th>
						<th>${__("Actual Consumed")}</th>
						<th>${__("WIP Remainder")}</th>
						<th>${__("Available / Expected")}</th>
						<th>${__("Action")}</th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);
		const $tb = $table.find("tbody");
		rows.forEach((r) => {
			$tb.append(`
				<tr>
					<td>${frappe.utils.escape_html(r.item_code || "")}</td>
					<td>${frappe.utils.escape_html(r.batch_no || "")}</td>
					<td>${flt(r.required_qty)}</td>
					<td>${flt(r.issued)}</td>
					<td>${flt(r.returned)}</td>
					<td>${flt(r.current_transferred_qty)} → ${flt(r.derived_transferred_qty)}</td>
					<td>${flt(r.consumed)}</td>
					<td>${flt(r.wip_remainder)}</td>
					<td>${flt(r.net_available_for_manufacture)}</td>
					<td>${frappe.utils.escape_html(r.action || "")}</td>
				</tr>
			`);
		});
		this.$materials.append($table);
	}

	render_secondary(rows) {
		this.$secondary.empty();
		$('<h5 class="jcsr-section-title">').text(__("Secondary Items")).appendTo(this.$secondary);
		if (!rows.length) {
			this.$secondary.append(`<p class="jcsr-empty">${__("No secondary items.")}</p>`);
			return;
		}
		const $table = $(`
			<table class="jcsr-table">
				<thead>
					<tr>
						<th>${__("Classification")}</th>
						<th>${__("Item")}</th>
						<th>${__("Qty")}</th>
						<th>${__("UOM")}</th>
						<th>${__("Equiv Factor")}</th>
						<th>${__("Parent")}</th>
						<th>${__("Guard")}</th>
						<th>${__("Action")}</th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);
		const $tb = $table.find("tbody");
		rows.forEach((r) => {
			$tb.append(`
				<tr>
					<td>${frappe.utils.escape_html(r.classification || "")}</td>
					<td>${frappe.utils.escape_html(r.item_code || "")}</td>
					<td>${flt(r.qty)}</td>
					<td>${frappe.utils.escape_html(r.uom || "")}</td>
					<td>${r.custom_output_equivalent_factor != null ? r.custom_output_equivalent_factor : ""}</td>
					<td>${frappe.utils.escape_html(r.custom_parent_co_product || "")}</td>
					<td>${frappe.utils.escape_html(r.guard_status || "")}</td>
					<td>${frappe.utils.escape_html(r.action || "")}</td>
				</tr>
			`);
		});
		this.$secondary.append($table);
	}

	render_evidence(rows) {
		this.$evidence.empty();
		$('<h5 class="jcsr-section-title">').text(__("Evidence")).appendTo(this.$evidence);
		rows.forEach((r) => {
			const ev = r.evidence || [];
			if (!ev.length) return;
			const $wrap = $(
				`<details class="jcsr-evidence"><summary>${frappe.utils.escape_html(
					r.item_code
				)} (${ev.length})</summary></details>`
			);
			const $table = $(`
				<table class="jcsr-table">
					<thead>
						<tr>
							<th>${__("Stock Entry")}</th>
							<th>${__("Purpose")}</th>
							<th>${__("Posting")}</th>
							<th>${__("Batch")}</th>
							<th>${__("Qty")}</th>
							<th>${__("S→T WH")}</th>
							<th>${__("job_card_item")}</th>
							<th>${__("Rahkaran")}</th>
							<th>${__("Bucket")}</th>
						</tr>
					</thead>
					<tbody></tbody>
				</table>
			`);
			const $tb = $table.find("tbody");
			ev.forEach((e) => {
				$tb.append(`
					<tr>
						<td>${frappe.utils.escape_html(e.voucher || "")}</td>
						<td>${frappe.utils.escape_html(e.purpose || "")}</td>
						<td>${frappe.utils.escape_html((e.posting_date || "") + " " + (e.posting_time || ""))}</td>
						<td>${frappe.utils.escape_html(e.batch_no || "")}</td>
						<td>${flt(e.qty)}</td>
						<td>${frappe.utils.escape_html((e.s_warehouse || "") + " → " + (e.t_warehouse || ""))}</td>
						<td>${frappe.utils.escape_html(e.job_card_item || "NULL")}</td>
						<td>${frappe.utils.escape_html(e.rahkaran || "")}</td>
						<td>${frappe.utils.escape_html(e.bucket || "")}</td>
					</tr>
				`);
			});
			$wrap.append($table);
			this.$evidence.append($wrap);
		});
	}

	render_downstream(downstream) {
		this.$downstream.empty();
		if (!downstream) return;
		$('<h5 class="jcsr-section-title">')
			.text(__("Downstream Manufacture Preview (not persisted)"))
			.appendTo(this.$downstream);
		const items = (downstream.simulation && downstream.simulation.items) || [];
		if (!items.length) {
			this.$downstream.append(
				`<p class="jcsr-empty">${frappe.utils.escape_html(
					downstream.simulation && downstream.simulation.error
						? downstream.simulation.error
						: __("No simulated items.")
				)}</p>`
			);
			return;
		}
		const $table = $(`
			<table class="jcsr-table">
				<thead>
					<tr>
						<th>${__("Item")}</th>
						<th>${__("Qty")}</th>
						<th>${__("S Warehouse")}</th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);
		const $tb = $table.find("tbody");
		items.forEach((i) => {
			$tb.append(`
				<tr>
					<td>${frappe.utils.escape_html(i.item_code || "")}</td>
					<td>${flt(i.qty)}</td>
					<td>${frappe.utils.escape_html(i.s_warehouse || "")}</td>
				</tr>
			`);
		});
		this.$downstream.append($table);
	}
}

function flt(v) {
	const n = parseFloat(v);
	return isNaN(n) ? 0 : n;
}

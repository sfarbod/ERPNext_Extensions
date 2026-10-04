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
		this.typeApprovals = {}; // secondary_row -> approval payload
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
					"Reconstructs Job Card material and secondary tracking from submitted Stock Entries / SLE. Secondary Item Type changes require explicit per-row approval. Does not cancel, amend, or regenerate Manufacture. SLE and GL are never altered."
				)
			)
			.appendTo(this.$body);

		this.$status = $('<div class="jcsr-status" data-role="status">').appendTo(this.$body);
		this.$materials = $('<div data-role="materials">').appendTo(this.$body);
		this.$secondary = $('<div data-role="secondary">').appendTo(this.$body);
		this.$typeRec = $('<div data-role="type-reconciliation">').appendTo(this.$body);
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
		this.typeApprovals = {};
		this.btn_preview.prop("disabled", true);
		this.btn_dry.prop("disabled", true);
		this.btn_apply.prop("disabled", true);
		this.$status.empty();
		this.$materials.empty();
		this.$secondary.empty();
		this.$typeRec.empty();
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

	collect_type_approvals() {
		return Object.values(this.typeApprovals || {});
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
				// Clear approvals on new preview — never carry into changed evidence
				this.typeApprovals = {};
				this.render_result(this.preview, "PREVIEW");
				const ready =
					this.preview.apply_allowed ||
					(this.preview.secondary_type_suggestions &&
						this.preview.secondary_type_suggestions.approvable_count > 0);
				this.btn_dry.prop("disabled", !ready && this.preview.overall_status === "BALANCED");
				// Allow dry-run whenever preview loaded for selected JC (readiness / type path)
				this.btn_dry.prop("disabled", false);
				this.btn_apply.prop("disabled", true);
			},
		});
	}

	run_dry() {
		const args = this.args();
		args.fingerprint = this.fingerprint;
		args.secondary_type_approvals = JSON.stringify(this.collect_type_approvals());
		frappe.call({
			method: `${this.api}.dry_run`,
			args,
			freeze: true,
			freeze_message: __("Dry-running rebuild (will roll back)…"),
			callback: (r) => {
				this.dry = r.message || {};
				this.render_result(this.dry, "DRY_RUN");
				this.render_downstream(this.dry.manufacture_readiness || this.dry.downstream);
				const pass = this.dry.dry_run_status === "DRY_RUN_PASS";
				const hasApproved = this.collect_type_approvals().length > 0;
				const hasWrites = ((this.dry.writes || []).length > 0);
				const mfgOk =
					(this.dry.manufacture_readiness && this.dry.manufacture_readiness.ok) ||
					this.dry.manufacture_readiness_status === "MANUFACTURE_READINESS_PASS";
				// Apply: tracking writes alone, or approved type changes that pass readiness
				const canApply =
					pass && !this.dry.blockers?.length && (hasWrites || (hasApproved && mfgOk));
				this.btn_apply.prop("disabled", !canApply);
			},
		});
	}

	run_apply() {
		if (!this.dry || this.dry.dry_run_status !== "DRY_RUN_PASS") {
			frappe.msgprint(__("Successful Dry Run is required before Apply."));
			return;
		}
		const approvals = this.collect_type_approvals();
		const confirmHtml = this.build_final_confirmation_html(approvals);
		frappe.confirm(confirmHtml, () => {
			const args = this.args();
			args.fingerprint = this.fingerprint;
			args.confirm = 1;
			args.secondary_type_approvals = JSON.stringify(approvals);
			frappe.call({
				method: `${this.api}.apply`,
				args,
				freeze: true,
				freeze_message: __("Applying rebuild…"),
				callback: (r) => {
					const msg = r.message || {};
					this.render_result(msg, "APPLY");
					this.render_downstream(msg.manufacture_readiness || msg.downstream);
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
					this.typeApprovals = {};
				},
			});
		});
	}

	build_final_confirmation_html(approvals) {
		const jc = frappe.utils.escape_html(this.jc.get_value() || "");
		const writes = (this.dry && this.dry.writes) || [];
		const sugRows =
			(this.dry &&
				this.dry.secondary_type_suggestions &&
				this.dry.secondary_type_suggestions.rows) ||
			[];
		const approvedKeys = new Set(approvals.map((a) => a.secondary_row));
		let approvedLines = approvals
			.map((a) => {
				const row = sugRows.find((r) => r.secondary_row === a.secondary_row) || {};
				return `${frappe.utils.escape_html(a.item_code || row.item_code || "")} ${flt(
					row.qty
				)} ${frappe.utils.escape_html(row.uom || "")}<br>` +
					`${frappe.utils.escape_html(a.current_type)} → ${frappe.utils.escape_html(
						a.approved_type
					)}<br>` +
					`${frappe.utils.escape_html(row.current_iran_class || "")} → ${frappe.utils.escape_html(
						row.suggested_iran_class || ""
					)}`;
			})
			.join("<hr>");
		if (!approvedLines) approvedLines = __("(none)");
		const unapproved = sugRows
			.filter((r) => r.approval_enabled && !approvedKeys.has(r.secondary_row))
			.map(
				(r) =>
					`${frappe.utils.escape_html(r.item_code)}: ${frappe.utils.escape_html(
						r.current_type
					)} → ${frappe.utils.escape_html(r.suggested_type)}`
			)
			.join("<br>");
		const readiness =
			(this.dry && this.dry.manufacture_readiness_status) ||
			(this.dry && this.dry.manufacture_readiness && this.dry.manufacture_readiness.status) ||
			"—";
		return (
			`<div class="jcsr-confirm">` +
			`<p><strong>${__("Selected Job Card")}:</strong> ${jc}</p>` +
			`<p><strong>${__("Tracking Changes")}:</strong> ${writes.length}</p>` +
			`<p><strong>${__("Link Changes")}:</strong> ${
				writes.filter((w) => w.type === "sed_job_card_item").length
			}</p>` +
			`<p><strong>${__("Approved Secondary Type Changes")}:</strong><br>${approvedLines}</p>` +
			`<p><strong>${__("Unapproved Suggestions")}:</strong><br>${
				unapproved || __("(none)")
			}</p>` +
			`<p><strong>${__("Selected Job Card Manufacture Readiness")}:</strong> ${frappe.utils.escape_html(
				readiness
			)}</p>` +
			`<p class="text-danger">${__(
				"Changing Secondary Item Type changes how this item participates in manufacturing costing. The change is applied only if you explicitly approve this row."
			)}</p>` +
			`<p>${__("Apply Job Card tracking rebuild? Manufacture Stock Entries will NOT be changed.")}</p>` +
			`</div>`
		);
	}

	render_result(data, mode) {
		const statuses = (data.statuses || []).join(", ");
		const balanced = data.overall_status === "BALANCED";
		const cls = balanced ? "ok" : data.apply_allowed ? "ready" : "warn";
		const mfgStatus =
			data.manufacture_readiness_status ||
			(data.manufacture_readiness && data.manufacture_readiness.status) ||
			"";
		this.$status.html(`
			<div class="jcsr-alert ${cls}">
				<strong>${frappe.utils.escape_html(mode)}</strong>
				— ${frappe.utils.escape_html(data.overall_status || "")}
				${balanced ? " / " + __("NO REBUILD REQUIRED") : ""}
				<div class="jcsr-meta">
					${__("Statuses")}: ${frappe.utils.escape_html(statuses || "—")}
					<br>${__("Fingerprint")}: <code>${frappe.utils.escape_html(data.fingerprint || "")}</code>
					<br>${__("Fingerprint scope")}: ${frappe.utils.escape_html(
						data.fingerprint_scope || "selected_job_card_dependency_closure"
					)}
					<br>${__("Manufacture repair required")}: ${
						data.manufacture_repair_required ? __("YES") : __("NO")
					}
					<br>${__("Manufacture Readiness")}: <strong>${frappe.utils.escape_html(
						mfgStatus || "—"
					)}</strong>
					${
						data.manufacture_readiness && data.manufacture_readiness.error
							? "<br><span class='text-danger'>" +
							  frappe.utils.escape_html(data.manufacture_readiness.error) +
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
		this.render_type_reconciliation(
			(data.secondary_type_suggestions && data.secondary_type_suggestions.rows) || []
		);
		this.render_evidence(data.material_rows || []);
		if (data.manufacture_readiness) {
			this.render_downstream(data.manufacture_readiness);
		}
		if (balanced && !(data.secondary_type_suggestions || {}).approvable_count) {
			this.btn_apply.prop("disabled", true);
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

	render_type_reconciliation(rows) {
		this.$typeRec.empty();
		$('<h5 class="jcsr-section-title">')
			.text(__("SECONDARY ITEM TYPE RECONCILIATION"))
			.appendTo(this.$typeRec);
		$('<p class="jcsr-help">')
			.text(
				__(
					"Suggested Secondary Type Change. Changing Secondary Item Type changes how this item participates in manufacturing costing. The change is applied only if you explicitly approve this row."
				)
			)
			.appendTo(this.$typeRec);
		if (!rows.length) {
			this.$typeRec.append(`<p class="jcsr-empty">${__("No secondary type suggestions.")}</p>`);
			return;
		}
		const $table = $(`
			<table class="jcsr-table jcsr-type-table">
				<thead>
					<tr>
						<th>${__("Item")}</th>
						<th>${__("Qty")}</th>
						<th>${__("Current Business Type")}</th>
						<th>${__("Suggested Business Type")}</th>
						<th>${__("Current Accounting Classification")}</th>
						<th>${__("Expected Accounting Classification")}</th>
						<th>${__("Confidence")}</th>
						<th>${__("Evidence")}</th>
						<th>${__("Downstream Effect")}</th>
						<th>${__("Change Type")}</th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);
		const $tb = $table.find("tbody");
		rows.forEach((r) => {
			const rowId = r.secondary_row || "";
			const enabled = !!r.approval_enabled;
			const checked = !!this.typeApprovals[rowId];
			const sugType = r.suggested_type || "—";
			const $tr = $(`
				<tr data-secondary-row="${frappe.utils.escape_html(rowId)}">
					<td>${frappe.utils.escape_html(r.item_code || "")}</td>
					<td>${flt(r.qty)} ${frappe.utils.escape_html(r.uom || "")}</td>
					<td>${frappe.utils.escape_html(r.current_type || "")}</td>
					<td>${frappe.utils.escape_html(sugType)}</td>
					<td>${frappe.utils.escape_html(r.current_iran_class || "")}</td>
					<td>${frappe.utils.escape_html(r.suggested_iran_class || "")}</td>
					<td>${frappe.utils.escape_html(r.confidence || "")}</td>
					<td></td>
					<td>${frappe.utils.escape_html(r.downstream_impact || "")}</td>
					<td class="jcsr-approve-cell"></td>
				</tr>
			`);
			const $ev = $("<details><summary>View</summary></details>");
			const $ul = $("<ul>");
			(r.evidence || []).forEach((e) => $ul.append($("<li>").text(e)));
			$ev.append($ul);
			$tr.find("td").eq(7).append($ev);

			const $cb = $(
				`<input type="checkbox" class="jcsr-type-approve" ${enabled ? "" : "disabled"} ${
					checked ? "checked" : ""
				} />`
			);
			// Default unchecked — never preselect
			if (!checked) $cb.prop("checked", false);
			$cb.on("change", () => {
				if ($cb.is(":checked")) {
					frappe.confirm(
						__(
							"Suggested Secondary Type Change: {0} → {1}. Expected accounting classification: {2} → {3}. Approve this row only?",
							[
								r.current_type,
								r.suggested_type,
								r.current_iran_class,
								r.suggested_iran_class,
							]
						),
						() => {
							this.typeApprovals[rowId] = {
								job_card: this.jc.get_value(),
								secondary_row: rowId,
								current_type: r.current_type,
								approved_type: r.suggested_type,
								evidence_fingerprint: r.evidence_fingerprint,
								item_code: r.item_code,
							};
							this.btn_apply.prop("disabled", true); // require new dry-run
						},
						() => {
							$cb.prop("checked", false);
							delete this.typeApprovals[rowId];
						}
					);
				} else {
					delete this.typeApprovals[rowId];
					this.btn_apply.prop("disabled", true);
				}
			});
			$tr.find(".jcsr-approve-cell").append($cb);
			$tb.append($tr);
		});
		this.$typeRec.append($table);
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
			.text(__("Selected Job Card Manufacture Readiness (simulation, not persisted)"))
			.appendTo(this.$downstream);
		const status = downstream.status || (downstream.ok ? "PASS" : "BLOCKED");
		const err = downstream.error || "";
		this.$downstream.append(
			`<div class="jcsr-alert ${downstream.ok ? "ok" : "warn"}">
				<strong>${__("Manufacture Readiness")}:</strong> ${frappe.utils.escape_html(status)}
				${err ? "<br>" + frappe.utils.escape_html(err) : ""}
			</div>`
		);
		const stage = downstream.stage || [];
		if (stage.length) {
			$("<h6>").text(__("Stage-equivalent output set")).appendTo(this.$downstream);
			const $st = $(
				`<table class="jcsr-table"><thead><tr>
					<th>#</th><th>${__("Item")}</th><th>${__("Qty")}</th><th>${__("UOM")}</th>
					<th>${__("Type")}</th><th>${__("Bucket")}</th><th>${__("Factor")}</th>
				</tr></thead><tbody></tbody></table>`
			);
			stage.forEach((r) => {
				$st.find("tbody").append(`
					<tr>
						<td>${r.idx || ""}</td>
						<td>${frappe.utils.escape_html(r.item_code || "")}</td>
						<td>${flt(r.qty)}</td>
						<td>${frappe.utils.escape_html(r.uom || "")}</td>
						<td>${frappe.utils.escape_html(r.sec || "")}</td>
						<td>${frappe.utils.escape_html(r.bucket || "")}</td>
						<td>${r.factor != null ? r.factor : ""}</td>
					</tr>
				`);
			});
			this.$downstream.append($st);
		}
		const items = downstream.rows || (downstream.simulation && downstream.simulation.items) || [];
		if (!items.length) {
			if (!stage.length) {
				this.$downstream.append(
					`<p class="jcsr-empty">${frappe.utils.escape_html(
						err || __("No simulated items.")
					)}</p>`
				);
			}
			return;
		}
		$("<h6>").text(__("Generated draft rows")).appendTo(this.$downstream);
		const $table = $(`
			<table class="jcsr-table">
				<thead>
					<tr>
						<th>${__("Item")}</th>
						<th>${__("Qty")}</th>
						<th>${__("Sec Type")}</th>
						<th>${__("FG")}</th>
						<th>${__("S Warehouse")}</th>
						<th>${__("T Warehouse")}</th>
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
					<td>${frappe.utils.escape_html(i.secondary_item_type || "")}</td>
					<td>${i.is_finished_item ? 1 : 0}</td>
					<td>${frappe.utils.escape_html(i.s_warehouse || "")}</td>
					<td>${frappe.utils.escape_html(i.t_warehouse || "")}</td>
				</tr>
			`);
		});
		this.$downstream.append($table);
		const classified = downstream.classified || {};
		if (Object.keys(classified).length) {
			$("<h6>").text(__("Iran classification buckets")).appendTo(this.$downstream);
			Object.keys(classified).forEach((k) => {
				const rows = classified[k] || [];
				this.$downstream.append(
					`<div><code>${frappe.utils.escape_html(k)}</code>: ${rows
						.map(
							(r) =>
								`${frappe.utils.escape_html(r.item_code)}×${flt(r.qty)}`
						)
						.join(", ")}</div>`
				);
			});
		}
	}
}

function flt(v) {
	const n = parseFloat(v);
	return isNaN(n) ? 0 : n;
}

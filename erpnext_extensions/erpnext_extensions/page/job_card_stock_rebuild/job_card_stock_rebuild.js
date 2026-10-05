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

	/**
	 * Core frappe.request.call ignores opts.timeout. Apply a one-shot XHR timeout
	 * to the next $.ajax only (Manufacture Repair Dry Run / Apply), then restore.
	 * Does not change Desk-wide $.ajaxSettings.
	 */
	_call_mfg_repair(opts, timeout_ms = 600000) {
		const original_ajax = $.ajax;
		let armed = true;
		$.ajax = function (settings) {
			settings = settings || {};
			if (armed) {
				armed = false;
				$.ajax = original_ajax;
				if (settings.timeout == null) {
					settings.timeout = timeout_ms;
				}
			}
			return original_ajax.call(this, settings);
		};
		const user_always = opts.always;
		opts.always = function () {
			if (armed) {
				armed = false;
				$.ajax = original_ajax;
			}
			if (user_always) {
				user_always.apply(this, arguments);
			}
		};
		try {
			return frappe.call(opts);
		} catch (e) {
			if (armed) {
				armed = false;
				$.ajax = original_ajax;
			}
			throw e;
		}
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

		// v5.5.0 — Manufacture Reconciliation (Golden Rule)
		$('<hr class="jcsr-divider">').appendTo(this.$body);
		$('<h3 class="jcsr-section-title">')
			.text(__("Manufacture Reconciliation"))
			.appendTo(this.$body);
		$('<p class="jcsr-help">')
			.html(
				__(
					"Golden Rule for Job Card × Item × Batch. Decide unexplained WIP, preview one canonical Manufacture, Dry Run (rollback), then Apply atomically. Persistent Apply is System Manager only."
				) +
					` · <a href="/app/query-report/Job%20Card%20Golden%20Rule%20Audit">${__(
						"Job Card Golden Rule Audit"
					)}</a>`
			)
			.appendTo(this.$body);
		const $mfgActions = $('<div class="jcsr-actions jcsr-mfg-actions">').appendTo(this.$body);
		this.btn_mfg_scan = $(
			`<button type="button" class="btn btn-default" data-mfg="scan">${__(
				"Scan Manufacture"
			)}</button>`
		)
			.appendTo($mfgActions)
			.on("click", () => this.run_mfg_scan());
		this.btn_mfg_dry = $(
			`<button type="button" class="btn btn-warning" data-mfg="dry" disabled>${__(
				"Dry Run Manufacture Repair"
			)}</button>`
		)
			.appendTo($mfgActions)
			.on("click", () => this.run_mfg_dry());
		this.btn_mfg_apply = $(
			`<button type="button" class="btn btn-danger" data-mfg="apply" disabled>${__(
				"Apply Manufacture Repair"
			)}</button>`
		)
			.appendTo($mfgActions)
			.on("click", () => this.run_mfg_apply());
		this.$mfg = $('<div data-role="manufacture-reconciliation" class="jcsr-mfg">').appendTo(
			this.$body
		);
		this.mfgScan = null;
		this.mfgPlan = null;
		this.mfgDry = null;
		this.mfgFingerprint = null;
		/** @type {Object.<string,{consumed:number,scrap:number,return:number,still:number}>} */
		this.mfgDecisions = {};
		this.mfgDryRunId = null;
		this.mfgDryPollTimer = null;
		this.$mfgDryProgress = $('<div data-role="mfg-dry-progress" class="jcsr-mfg-dry-progress">').appendTo(
			this.$body
		);
	}

	apply_route_options() {
		const opts = frappe.route_options || {};
		frappe.route_options = null;
		if (opts.job_card) {
			this.jc.set_value(opts.job_card);
			this._resume_mfg_dry_if_active(opts.job_card);
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
		this.mfgScan = null;
		this.mfgPlan = null;
		this.mfgDry = null;
		this.mfgFingerprint = null;
		this.mfgDecisions = {};
		this._stop_mfg_dry_poll();
		this.mfgDryRunId = null;
		if (this.$mfg) this.$mfg.empty();
		if (this.$mfgDryProgress) this.$mfgDryProgress.empty();
		if (this.btn_mfg_dry) this.btn_mfg_dry.prop("disabled", true);
		if (this.btn_mfg_apply) this.btn_mfg_apply.prop("disabled", true);
		const jc = this.jc && this.jc.get_value && this.jc.get_value();
		if (jc) this._resume_mfg_dry_if_active(jc);
	}

	mfg_row_key(item_code, batch_no) {
		return String(item_code || "") + "\u0000" + String(batch_no || "");
	}

	/**
	 * Prefill editable disposition fields from backend Scan suggestion.
	 * Does NOT invent a second suggestion engine — maps suggested_action / suggested_qty
	 * (with proposed_* fallback when already filled by the server).
	 */
	init_mfg_decisions_from_scan(rows) {
		this.mfgDecisions = {};
		(rows || []).forEach((r) => {
			const key = this.mfg_row_key(r.item_code, r.batch_no);
			this.mfgDecisions[key] = jcsr_prefill_from_suggestion(r);
		});
	}

	capture_mfg_decisions_from_dom() {
		if (!this.$mfg) return;
		this.$mfg.find("tr[data-item]").each((_, tr) => {
			const $tr = $(tr);
			const key = this.mfg_row_key($tr.attr("data-item"), $tr.attr("data-batch") || "");
			this.mfgDecisions[key] = {
				consumed: flt($tr.find('[data-f="consumed"]').val()),
				scrap: flt($tr.find('[data-f="scrap"]').val()),
				return: flt($tr.find('[data-f="return"]').val()),
				still: flt($tr.find('[data-f="still"]').val()),
			};
		});
	}

	bind_mfg_decision_inputs($root) {
		const self = this;
		$root.find('input[data-f]').on("input change", function () {
			const $tr = $(this).closest("tr[data-item]");
			if (!$tr.length) return;
			const key = self.mfg_row_key($tr.attr("data-item"), $tr.attr("data-batch") || "");
			self.mfgDecisions[key] = {
				consumed: flt($tr.find('[data-f="consumed"]').val()),
				scrap: flt($tr.find('[data-f="scrap"]').val()),
				return: flt($tr.find('[data-f="return"]').val()),
				still: flt($tr.find('[data-f="still"]').val()),
			};
		});
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

	collect_mfg_plan() {
		// Final visible values win — never replace user edits with original Scan suggestion.
		this.capture_mfg_decisions_from_dom();
		const dispositions = [];
		this.$mfg.find("tr[data-item]").each((_, tr) => {
			const $tr = $(tr);
			const key = this.mfg_row_key($tr.attr("data-item"), $tr.attr("data-batch") || "");
			const d = this.mfgDecisions[key] || {
				consumed: flt($tr.find('[data-f="consumed"]').val()),
				scrap: flt($tr.find('[data-f="scrap"]').val()),
				return: flt($tr.find('[data-f="return"]').val()),
				still: flt($tr.find('[data-f="still"]').val()),
			};
			dispositions.push({
				item_code: $tr.attr("data-item"),
				batch_no: $tr.attr("data-batch") || "",
				proposed_consumed: flt(d.consumed),
				proposed_scrap: flt(d.scrap),
				proposed_return: flt(d.return),
				proposed_still_in_wip: flt(d.still),
			});
		});
		const merge_documents = [];
		this.$mfg.find('input[data-merge]:checked').each((_, el) => {
			merge_documents.push($(el).attr("data-merge"));
		});
		const merge_material_issues = [];
		this.$mfg.find('input[data-merge-mi]:checked').each((_, el) => {
			merge_material_issues.push($(el).attr("data-merge-mi"));
		});
		return {
			dispositions,
			merge_documents,
			merge_material_issues,
			stamp_mode: "HISTORICAL",
			fingerprint: this.mfgFingerprint,
		};
	}

	run_mfg_scan() {
		const jc = this.jc.get_value();
		if (!jc) {
			frappe.msgprint(__("Select a Job Card"));
			return;
		}
		frappe.call({
			method: this.api + ".scan_manufacture_reconciliation",
			args: { job_card: jc },
			freeze: true,
			freeze_message: __("Scanning Golden Rule…"),
			callback: (r) => {
				const data = r.message || {};
				this.mfgScan = data.scan;
				this.mfgPlan = data.plan;
				this.mfgFingerprint = (data.plan || {}).fingerprint;
				this.mfgDry = null;
				// NEW SCAN: reinitialize editable decisions from backend suggestions.
				this.init_mfg_decisions_from_scan((data.scan && data.scan.rows) || []);
				this.render_mfg();
				this.btn_mfg_dry.prop("disabled", false);
				this.btn_mfg_apply.prop("disabled", true);
				this._resume_mfg_dry_if_active(jc);
			},
		});
	}

	_mfg_dry_storage_key(jc) {
		return "jcsr_dry_run:" + String(jc || "");
	}

	_stop_mfg_dry_poll() {
		if (this.mfgDryPollTimer) {
			clearInterval(this.mfgDryPollTimer);
			this.mfgDryPollTimer = null;
		}
	}

	_set_mfg_dry_controls(active) {
		if (this.btn_mfg_dry) {
			// Keep clickable while active so a second click can reconnect to the
			// same run (server-side single-flight). Apply stays blocked.
			this.btn_mfg_dry.prop("disabled", false);
			this.btn_mfg_dry.text(
				active ? __("Dry Run in progress…") : __("Dry Run Manufacture Repair")
			);
		}
		if (this.btn_mfg_apply) this.btn_mfg_apply.prop("disabled", true);
	}

	_render_mfg_dry_progress(st) {
		const s = st || {};
		const pct = Math.max(0, Math.min(100, parseInt(s.progress || 0, 10) || 0));
		const phase = s.phase || "";
		const status = s.status || "";
		let valDetail = "";
		if (s.valuation_roots_total) {
			valDetail = ` · ${__("Valuation")} ${s.valuation_roots_done || 0} / ${
				s.valuation_roots_total
			}`;
		}
		const msg = s.message || status || "";
		if (!this.$mfgDryProgress) return;
		this.$mfgDryProgress.html(`
			<div class="jcsr-alert ok" data-role="mfg-dry-progress-box">
				<strong>${frappe.utils.escape_html(status)}</strong>
				— ${frappe.utils.escape_html(phase)} (${pct}%)${frappe.utils.escape_html(valDetail)}
				<div class="jcsr-meta">${frappe.utils.escape_html(msg)}
				· run=${frappe.utils.escape_html(s.run_id || "")}</div>
				<div style="background:#eee;height:8px;border-radius:4px;margin-top:6px;overflow:hidden">
					<div style="background:#f0ad4e;height:8px;width:${pct}%"></div>
				</div>
			</div>
		`);
	}

	_poll_mfg_dry_status(runId) {
		this._stop_mfg_dry_poll();
		const tick = () => {
			frappe.call({
				method: this.api + ".get_manufacture_repair_dry_run_status",
				args: { run_id: runId },
				callback: (r) => {
					const st = r.message || {};
					this._render_mfg_dry_progress(st);
					const terminal = ["PASS", "FAILED", "STALE_PLAN", "BLOCKED", "CANCELLED"].includes(
						st.status
					);
					if (!terminal) return;
					this._stop_mfg_dry_poll();
					this._set_mfg_dry_controls(false);
					this.mfgDryRunId = null;
					const jc = this.jc.get_value();
					try {
						localStorage.removeItem(this._mfg_dry_storage_key(jc));
					} catch (e) {
						/* ignore */
					}
					const result = st.result || {};
					this.mfgDry = Object.assign({}, result, {
						status: st.status === "PASS" ? "DRY_RUN_PASS" : st.status,
						ok: st.status === "PASS",
						error: st.error || result.error,
						mutated: result.mutated === true ? true : false,
						committed: result.committed === true ? true : false,
						canonical_name: result.canonical_name,
						logistics_equivalence: result.logistics_equivalence,
						fingerprint: result.fingerprint,
						phase_timings: result.phase_timings,
						queued: true,
						run_id: runId,
					});
					this.mfgFingerprint = this.mfgDry.fingerprint || this.mfgFingerprint;
					this.render_mfg_dry();
					const pass = this.mfgDry.ok && this.mfgDry.status === "DRY_RUN_PASS";
					this.btn_mfg_apply.prop("disabled", !pass);
					if (this.$mfgDryProgress) {
						const cls = pass ? "ok" : "blocker";
						this.$mfgDryProgress
							.find("[data-role='mfg-dry-progress-box']")
							.removeClass("ok blocker")
							.addClass(cls);
					}
				},
			});
		};
		tick();
		this.mfgDryPollTimer = setInterval(tick, 2000);
	}

	_resume_mfg_dry_if_active(jc) {
		if (!jc) return;
		let stored = null;
		try {
			stored = localStorage.getItem(this._mfg_dry_storage_key(jc));
		} catch (e) {
			stored = null;
		}
		frappe.call({
			method: this.api + ".get_active_manufacture_repair_dry_run",
			args: { job_card: jc },
			callback: (r) => {
				const msg = r.message || {};
				const runId = (msg.active && msg.run_id) || stored;
				if (!runId) return;
				this.mfgDryRunId = runId;
				this._set_mfg_dry_controls(true);
				this._poll_mfg_dry_status(runId);
			},
		});
	}

	run_mfg_dry() {
		const jc = this.jc.get_value();
		const plan = this.collect_mfg_plan();
		this._set_mfg_dry_controls(true);
		this._render_mfg_dry_progress({
			status: "QUEUED",
			phase: "QUEUED",
			progress: 0,
			message: __("Starting Dry Run…"),
		});
		frappe.call({
			method: this.api + ".start_manufacture_repair_dry_run",
			args: { job_card: jc, plan: plan },
			freeze: false,
			callback: (r) => {
				const msg = r.message || {};
				const runId = msg.run_id;
				if (!runId) {
					this._set_mfg_dry_controls(false);
					frappe.msgprint(__("Failed to start Dry Run"));
					return;
				}
				this.mfgDryRunId = runId;
				try {
					localStorage.setItem(this._mfg_dry_storage_key(jc), runId);
				} catch (e) {
					/* ignore */
				}
				this._render_mfg_dry_progress({
					status: msg.status || "QUEUED",
					phase: msg.phase || "QUEUED",
					progress: msg.progress || 0,
					message: msg.message || (msg.already_running ? __("Reconnected") : __("Queued")),
					run_id: runId,
				});
				this._poll_mfg_dry_status(runId);
			},
			error: () => {
				this._set_mfg_dry_controls(false);
			},
		});
	}

	run_mfg_apply() {
		const jc = this.jc.get_value();
		if (this.mfgDryPollTimer || this.mfgDryRunId) {
			frappe.msgprint(__("DRY_RUN_IN_PROGRESS — wait for the queued Dry Run to finish"));
			return;
		}
		frappe.confirm(
			__(
				"Apply will CANCEL/SUBMIT real Stock Entries atomically. Continue only with authorization."
			),
			() => {
				const plan = this.collect_mfg_plan();
				this._call_mfg_repair({
					method: this.api + ".apply_manufacture_repair",
					args: { job_card: jc, plan: plan, confirm: 1 },
					freeze: true,
					freeze_message: __("Applying Manufacture Repair…"),
					callback: (r) => {
						const msg = r.message || {};
						frappe.msgprint({
							title: msg.ok ? __("Apply PASS") : __("Apply FAIL"),
							indicator: msg.ok ? "green" : "red",
							message: `<pre>${frappe.utils.escape_html(
								JSON.stringify(msg, null, 2).slice(0, 4000)
							)}</pre>`,
						});
					},
				});
			}
		);
	}

	render_mfg() {
		this.$mfg.empty();
		const scan = this.mfgScan || {};
		const plan = this.mfgPlan || {};
		const rows = scan.rows || [];
		$("<h4 class='jcsr-section-title'>").text(__("Golden Rule")).appendTo(this.$mfg);
		if (plan.blockers && plan.blockers.length) {
			this.$mfg.append(
				`<div class="jcsr-alert blocker">${frappe.utils.escape_html(
					plan.blockers.join("; ")
				)}</div>`
			);
		}
		const $table = $(`
			<table class="jcsr-table">
				<thead><tr>
					<th>${__("Item")}</th><th>${__("Batch")}</th>
					<th>${__("Issued")}</th><th>${__("Returned")}</th>
					<th>${__("MFG Consume")}</th><th>${__("Material Issue")}</th>
					<th>${__("Scrap")}</th>
					<th>${__("Remaining")}</th><th>${__("Suggested")}</th>
					<th>${__("Consumed*")}</th><th>${__("Scrap*")}</th>
					<th>${__("Return*")}</th><th>${__("Still WIP*")}</th>
					<th>${__("Status")}</th>
				</tr></thead><tbody></tbody>
			</table>
		`);
		const $tb = $table.find("tbody");
		rows.forEach((r) => {
			const sug = r.suggested_action
				? `${r.suggested_action} ${flt(r.suggested_qty)} (${r.confidence || ""})`
				: "";
			const key = this.mfg_row_key(r.item_code, r.batch_no);
			// Preserve user edits across re-render; fall back to Scan suggestion prefill.
			const d = this.mfgDecisions[key] || jcsr_prefill_from_suggestion(r);
			this.mfgDecisions[key] = d;
			$tb.append(`
				<tr data-item="${frappe.utils.escape_html(r.item_code)}"
				    data-batch="${frappe.utils.escape_html(r.batch_no || "")}">
					<td>${frappe.utils.escape_html(r.item_code)}</td>
					<td>${frappe.utils.escape_html(r.batch_no || "")}</td>
					<td>${flt(r.issued)}</td><td>${flt(r.returned)}</td>
					<td>${flt(r.consumed)}</td><td>${flt(r.mi_consumed)}</td>
					<td>${flt(r.scrap)}</td>
					<td>${flt(r.remaining_wip)}</td>
					<td title="${frappe.utils.escape_html(r.reason || "")}">${frappe.utils.escape_html(sug)}</td>
					<td><input data-f="consumed" type="number" step="any" value="${flt(d.consumed)}" style="width:70px"></td>
					<td><input data-f="scrap" type="number" step="any" value="${flt(d.scrap)}" style="width:70px"></td>
					<td><input data-f="return" type="number" step="any" value="${flt(d.return)}" style="width:70px"></td>
					<td><input data-f="still" type="number" step="any" value="${flt(d.still)}" style="width:70px"></td>
					<td>${frappe.utils.escape_html(r.status || "")}</td>
				</tr>
			`);
		});
		this.$mfg.append($table);
		this.bind_mfg_decision_inputs($table);

		$("<h4 class='jcsr-section-title'>").text(__("Documents")).appendTo(this.$mfg);
		const $docTable = $(`
			<table class="jcsr-table" data-role="mfg-documents">
				<thead><tr>
					<th>${__("Document")}</th><th>${__("Purpose")}</th>
					<th>${__("Qty / rows")}</th><th>${__("Ownership")}</th>
					<th>${__("Action")}</th>
				</tr></thead><tbody></tbody>
			</table>
		`);
		const $docTb = $docTable.find("tbody");
		const docs = plan.documents || [];
		docs.forEach((d) => {
			const purpose = d.purpose || "";
			const ownership = d.ownership || d.role || "";
			const qty = d.qty_summary || (d.fg_completed_qty != null ? `FG ${flt(d.fg_completed_qty)}` : "");
			let actionCell = frappe.utils.escape_html(d.role || "");
			if (purpose === "Manufacture" && (d.role === "MERGE" || d.role === "KEEP")) {
				const checked = d.role === "MERGE" ? "checked" : "";
				actionCell = `<label><input type="checkbox" data-merge="${frappe.utils.escape_html(
					d.name
				)}" ${checked}> ${frappe.utils.escape_html(d.role)}</label>`;
			} else if (purpose === "Material Issue") {
				const blocked = d.role === "BLOCKED" || d.shared_document;
				if (blocked) {
					actionCell = `<span class="text-danger">BLOCKED</span>`;
				} else {
					const checked = d.role === "MERGE" || d.propose_merge ? "checked" : "";
					actionCell = `<label><input type="checkbox" data-merge-mi="${frappe.utils.escape_html(
						d.name
					)}" ${checked}> ${d.role === "MERGE" ? "MERGE" : "KEEP / MERGE?"}</label>`;
				}
			} else if (d.role === "TEMP CANCEL / RECREATE") {
				actionCell = `TEMP CANCEL / RECREATE`;
			} else if (d.role === "BLOCKED") {
				actionCell = `<span class="text-danger">BLOCKED</span>`;
			}
			const unrel = (d.unrelated_rows || [])
				.map(
					(r) =>
						`${r.item_code}×${flt(r.qty)}${r.downstream_note ? " [" + r.downstream_note + "]" : ""}`
				)
				.join("; ");
			const rel = (d.related_rows || [])
				.map((r) => `${r.item_code}×${flt(r.qty)}`)
				.join("; ");
			const detail =
				(rel ? `affected: ${rel}` : "") +
				(unrel ? (rel ? " | " : "") + `unrelated: ${unrel}` : "") +
				(d.shared_reason ? ` | ${d.shared_reason}` : d.reason ? ` | ${d.reason}` : "");
			$docTb.append(`
				<tr data-doc="${frappe.utils.escape_html(d.name)}" title="${frappe.utils.escape_html(
					detail || d.reason || ""
				)}">
					<td>${frappe.utils.escape_html(d.name)}</td>
					<td>${frappe.utils.escape_html(purpose)}</td>
					<td>${frappe.utils.escape_html(qty || rel || "")}</td>
					<td>${frappe.utils.escape_html(String(ownership))}</td>
					<td>${actionCell}</td>
				</tr>
			`);
			if (detail && (d.shared_class || d.unrelated_rows)) {
				$docTb.append(`
					<tr class="text-muted" data-doc-detail="${frappe.utils.escape_html(d.name)}">
						<td colspan="5" style="font-size:11px">${frappe.utils.escape_html(detail)}</td>
					</tr>
				`);
			}
		});
		this.$mfg.append($docTable);
		if (plan.minimal_cancel_set && plan.minimal_cancel_set.length) {
			this.$mfg.append(
				`<div class="jcsr-meta" data-role="mfg-min-cancel">Minimal cancel set: ${plan.minimal_cancel_set
					.map((x) => frappe.utils.escape_html(x))
					.join(", ")}</div>`
			);
		}
		const bridge = plan.temporary_bridge || {};
		const shortages = bridge.shortages || [];
		this.$mfg.append(
			`<div class="jcsr-meta" data-role="mfg-temp-bridge">
				<strong>${__("Temporary Receipt Required")}:</strong> ${
					bridge.required ? __("YES") : __("NO")
				}
				${
					bridge.required
						? ` · ${__("Rows")}: ${shortages.length}<br>` +
						  shortages
								.map(
									(s) =>
										`${frappe.utils.escape_html(s.item_code)} / ${frappe.utils.escape_html(
											s.batch_no || ""
										)} @ ${frappe.utils.escape_html(s.warehouse || "")} × ${flt(
											s.shortage_qty
										)} @ ${flt(s.valuation_rate)}`
								)
								.join("<br>")
						: ""
				}
			</div>`
		);

		$("<h4 class='jcsr-section-title'>").text(__("Final Manufacture Preview")).appendTo(this.$mfg);
		const canon = plan.canonical_manufacture || {};
		this.$mfg.append(
			`<div class="jcsr-meta">Stamp: <code>${frappe.utils.escape_html(
				canon.historical_stamp || ""
			)}</code> · FG qty ${flt(canon.fg_completed_qty)} · supersedes ${(
				canon.supersedes || []
			)
				.map((x) => frappe.utils.escape_html(x))
				.join(", ")}</div>`
		);
		const $ct = $(
			`<table class="jcsr-table"><thead><tr>
				<th>${__("Type")}</th><th>${__("Item")}</th><th>${__("Batch")}</th>
				<th>${__("Qty")}</th><th>${__("S / T")}</th><th>${__("Rate source")}</th>
				<th>${__("Source")}</th>
			</tr></thead><tbody></tbody></table>`
		);
		(canon.rows || []).forEach((row) => {
			$ct.find("tbody").append(`
				<tr>
					<td>${frappe.utils.escape_html(row.type || "")}</td>
					<td>${frappe.utils.escape_html(row.item_code || "")}</td>
					<td>${frappe.utils.escape_html(row.batch_no || "")}</td>
					<td>${flt(row.qty)}</td>
					<td>${frappe.utils.escape_html(row.s_warehouse || "")} → ${frappe.utils.escape_html(
						row.t_warehouse || ""
					)}</td>
					<td>${frappe.utils.escape_html(row.rate_source || "")}</td>
					<td>${frappe.utils.escape_html(row.source_lineage || row.source_voucher || "")}</td>
				</tr>
			`);
		});
		this.$mfg.append($ct);
	}

	render_mfg_dry() {
		const d = this.mfgDry || {};
		const cls = d.ok ? "ok" : "blocker";
		let eqHtml = "";
		(d.logistics_equivalence || []).forEach((eq) => {
			eqHtml += `<div class="jcsr-meta">Equivalence ${frappe.utils.escape_html(
				eq.original || ""
			)} → ${frappe.utils.escape_html(eq.recreated || "")}: ${
				eq.ok ? "PASS" : "FAIL"
			}</div>`;
		});
		this.$mfg.prepend(
			`<div class="jcsr-alert ${cls}" data-role="mfg-dry">
				<strong>${frappe.utils.escape_html(d.status || "")}</strong>
				${d.error ? " — " + frappe.utils.escape_html(d.error) : ""}
				<div class="jcsr-meta">mutated=${d.mutated} committed=${d.committed}
				canonical=${frappe.utils.escape_html(d.canonical_name || "")}</div>
				${eqHtml}
			</div>`
		);
	}
}

function flt(v) {
	const n = parseFloat(v);
	return isNaN(n) ? 0 : n;
}

/**
 * Map backend Scan suggestion → editable disposition quantities.
 * Canonical actions from golden_rule / scan response only — no second inference engine.
 *
 * Concrete recommendations (including LOW confidence) prefill.
 * MANUAL REVIEW / AMBIGUOUS / BLOCKED / empty → leave unset (zeros).
 */
function jcsr_prefill_from_suggestion(row) {
	const r = row || {};
	const action = String(r.suggested_action || r.disposition || "")
		.trim()
		.toUpperCase()
		.replace(/\s+/g, "_");
	const qty = flt(r.suggested_qty);
	const out = { consumed: 0, scrap: 0, return: 0, still: 0 };

	if (action === "CONSUMED" && qty > 0) {
		out.consumed = qty;
		return out;
	}
	if ((action === "SCRAP" || action === "COMPONENT_SCRAP") && qty > 0) {
		out.scrap = qty;
		return out;
	}
	if (action === "RETURN" && qty > 0) {
		out.return = qty;
		return out;
	}
	if ((action === "STILL_IN_WIP" || action === "STILL_WIP") && qty > 0) {
		out.still = qty;
		return out;
	}

	// Fallback: trust server-filled proposed_* when already allocated to a concrete bucket.
	const pc = flt(r.proposed_consumed);
	const ps = flt(r.proposed_scrap);
	const pr = flt(r.proposed_return);
	const pw = flt(r.proposed_still_in_wip);
	if (pc + ps + pr + pw > 0) {
		out.consumed = pc;
		out.scrap = ps;
		out.return = pr;
		out.still = pw;
	}
	return out;
}

// Expose for Playwright / unit evaluation in the desk page context.
if (typeof window !== "undefined") {
	window.jcsr_prefill_from_suggestion = jcsr_prefill_from_suggestion;
}

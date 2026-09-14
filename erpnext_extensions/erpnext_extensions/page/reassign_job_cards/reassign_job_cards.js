frappe.provide("erpnext_extensions.reassign_job_cards");

frappe.pages["reassign-job-cards"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Reassign Job Cards"),
		single_column: true,
	});
	page.main.addClass("reassign-jc-page");
	erpnext_extensions.reassign_job_cards = new ReassignJobCardsPage(page);
};

class ReassignJobCardsPage {
	constructor(page) {
		this.page = page;
		this.api = "erpnext_extensions.stock_extensions.job_card_reassign.api";
		this.job_cards = [];
		this.selected = new Set();
		this.preview = null;
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
				`<div class="rjc-alert blocker">${__("Only System Manager can reassign Job Cards.")}</div>`
			);
			return;
		}
		this.$toolbar = $('<div class="rjc-toolbar">').appendTo(this.$body);
		this.source = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Link",
				options: "Work Order",
				label: __("Source Work Order"),
				reqd: 1,
				change: () => this.load_job_cards(),
			},
			render_input: true,
		});
		this.target = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Link",
				options: "Work Order",
				label: __("Target Work Order"),
				reqd: 1,
				change: () => this.reset_preview(),
			},
			render_input: true,
		});
		this.$reason_wrap = $('<div class="rjc-reason">').appendTo(this.$toolbar);
		this.reason = frappe.ui.form.make_control({
			parent: this.$reason_wrap,
			df: { fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
			render_input: true,
		});
		const $actions = $('<div class="rjc-actions">').appendTo(this.$toolbar);
		this.btn_preview = $(
			`<button type="button" class="btn btn-primary">${__("Preview")}</button>`
		)
			.appendTo($actions)
			.on("click", () => this.run_preview());
		this.btn_execute = $(
			`<button type="button" class="btn btn-danger" disabled>${__("Reassign")}</button>`
		)
			.appendTo($actions)
			.on("click", () => this.run_execute());

		$('<p class="rjc-help">')
			.text(
				__(
					"Select Job Cards on the source Work Order. Related Stock Entries are discovered for validation only. SLE and GL are not reposted."
				)
			)
			.appendTo(this.$body);

		$('<h5 class="rjc-section-title">').text(__("Job Cards")).appendTo(this.$body);
		this.$jc = $('<div data-role="job-cards">').appendTo(this.$body);
		this.$preview = $('<div data-role="preview">').appendTo(this.$body);
		this.render_job_cards();
	}

	apply_route_options() {
		const opts = frappe.route_options || {};
		frappe.route_options = null;
		if (opts.source_work_order) {
			this.source.set_value(opts.source_work_order);
		}
		if (opts.target_work_order) {
			this.target.set_value(opts.target_work_order);
		}
		this.preselect = opts.job_card || null;
		if (opts.source_work_order) {
			this.load_job_cards();
		}
	}

	reset_preview() {
		this.preview = null;
		this.btn_execute.prop("disabled", true);
		this.$preview.empty();
	}

	load_job_cards() {
		this.reset_preview();
		const source = this.source.get_value();
		if (!source) {
			this.job_cards = [];
			this.selected.clear();
			this.render_job_cards();
			return;
		}
		frappe.call({
			method: `${this.api}.get_job_cards`,
			args: { source_work_order: source },
			callback: (r) => {
				this.job_cards = r.message || [];
				this.selected = new Set();
				if (this.preselect && this.job_cards.some((j) => j.name === this.preselect)) {
					this.selected.add(this.preselect);
				}
				this.preselect = null;
				this.render_job_cards();
			},
		});
	}

	render_job_cards() {
		this.$jc.empty();
		if (!this.job_cards.length) {
			this.$jc.html(`<p class="rjc-empty">${__("No Job Cards on the source Work Order.")}</p>`);
			return;
		}
		const $table = $(`
			<table class="rjc-table">
				<thead>
					<tr>
						<th></th>
						<th>${__("Job Card")}</th>
						<th>${__("Operation")}</th>
						<th>${__("Finished Good")}</th>
						<th>${__("Status")}</th>
						<th>${__("Qty")}</th>
						<th>${__("Completed")}</th>
						<th>${__("Manufactured")}</th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);
		const $body = $table.find("tbody");
		this.job_cards.forEach((jc) => {
			const $tr = $(`
				<tr>
					<td><input type="checkbox"></td>
					<td><a href="/app/job-card/${encodeURIComponent(jc.name)}">${frappe.utils.escape_html(jc.name)}</a></td>
					<td>${frappe.utils.escape_html(jc.operation || "")}</td>
					<td>${frappe.utils.escape_html(jc.finished_good || "")}</td>
					<td>${frappe.utils.escape_html(jc.status || "")}</td>
					<td>${jc.for_quantity || 0}</td>
					<td>${jc.total_completed_qty || 0}</td>
					<td>${jc.manufactured_qty || 0}</td>
				</tr>
			`);
			const $cb = $tr.find("input");
			$cb.prop("checked", this.selected.has(jc.name));
			$cb.on("change", () => {
				if ($cb.is(":checked")) this.selected.add(jc.name);
				else this.selected.delete(jc.name);
				this.reset_preview();
			});
			$body.append($tr);
		});
		this.$jc.append($table);
		const $sel = $(`<button type="button" class="btn btn-xs btn-default">${__("Select all")}</button>`);
		$sel.on("click", () => {
			this.job_cards.forEach((jc) => this.selected.add(jc.name));
			this.render_job_cards();
			this.reset_preview();
		});
		this.$jc.prepend($sel);
	}

	run_preview() {
		const source = this.source.get_value();
		const target = this.target.get_value();
		const reason = this.reason.get_value();
		if (!source || !target) {
			frappe.msgprint(__("Source and Target Work Order are required."));
			return;
		}
		if (!this.selected.size) {
			frappe.msgprint(__("Select at least one Job Card."));
			return;
		}
		frappe.call({
			method: `${this.api}.preview`,
			args: {
				source_work_order: source,
				target_work_order: target,
				job_cards: Array.from(this.selected),
				reason: reason || "",
			},
			freeze: true,
			freeze_message: __("Building preview"),
			callback: (r) => {
				this.preview = r.message;
				this.render_preview();
			},
		});
	}

	render_preview() {
		const p = this.preview || {};
		this.$preview.empty();
		this.btn_execute.prop("disabled", !p.can_execute);

		$('<h5 class="rjc-section-title">').text(__("Validation")).appendTo(this.$preview);
		(p.blockers || []).forEach((b) => {
			this.$preview.append(
				`<div class="rjc-alert blocker">${frappe.utils.escape_html(b.message || b.code)}</div>`
			);
		});
		(p.warnings || []).forEach((w) => {
			this.$preview.append(
				`<div class="rjc-alert warning">${frappe.utils.escape_html(w.message || w.code)}</div>`
			);
		});
		if (!(p.blockers || []).length && !(p.warnings || []).length) {
			this.$preview.append(`<div class="rjc-alert">${__("No blockers. Preview is ready to execute.")}</div>`);
		}

		$('<h5 class="rjc-section-title">').text(__("Selected Job Cards")).appendTo(this.$preview);
		this.$preview.append(this._jc_preview_table(p));

		$('<h5 class="rjc-section-title">').text(__("Stock Entries")).appendTo(this.$preview);
		this.$preview.append(this._se_table(p.stock_entries || []));

		$('<h5 class="rjc-section-title">').text(__("Work Order Impact (estimate)")).appendTo(this.$preview);
		this.$preview.append(this._impact_table(p));

		if (p.source_remaining_job_cards === 0) {
			this.$preview.append(
				`<div class="rjc-alert warning">${__(
					"Source Work Order will have no remaining Job Cards. It will not be deleted."
				)}</div>`
			);
		}
	}

	_jc_preview_table(p) {
		const mapping = p.operation_mapping || {};
		const rows = (p.job_cards || [])
			.map((jc) => {
				const m = mapping[jc.name] || {};
				const action = m.action || (m.ok ? "REUSE" : "");
				const actionLabel =
					action === "CREATE"
						? __("CREATE TARGET OPERATION")
						: action === "REUSE"
							? __("REUSE")
							: m.ok
								? __("OK")
								: frappe.utils.escape_html(m.error || "");
				const targetLabel =
					action === "CREATE"
						? `${frappe.utils.escape_html(m.target_operation || "")} / ${frappe.utils.escape_html(
								m.target_finished_good || "-"
						  )}`
						: frappe.utils.escape_html(m.target_operation || "");
				return `<tr>
					<td><a href="/app/job-card/${encodeURIComponent(jc.name)}">${frappe.utils.escape_html(jc.name)}</a></td>
					<td>${frappe.utils.escape_html(jc.status || "")}</td>
					<td>${frappe.utils.escape_html(jc.operation || "")} / ${frappe.utils.escape_html(
					jc.finished_good || "-"
				)}</td>
					<td>${targetLabel}</td>
					<td><span class="rjc-badge ${(action || "").toLowerCase()}">${actionLabel}</span></td>
					<td>${jc.total_completed_qty || 0} / ${jc.manufactured_qty || 0}</td>
				</tr>`;
			})
			.join("");
		return $(`
			<table class="rjc-table">
				<thead>
					<tr>
						<th>${__("Job Card")}</th>
						<th>${__("Status")}</th>
						<th>${__("Source Operation")}</th>
						<th>${__("Target Operation")}</th>
						<th>${__("Action")}</th>
						<th>${__("Completed / Manufactured")}</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
			</table>
		`);
	}

	_se_table(rows) {
		if (!rows.length) {
			return $(`<p class="rjc-empty">${__("No related Stock Entries.")}</p>`);
		}
		const body = rows
			.map((r) => {
				const cls = (r.classification || "").toLowerCase();
				return `<tr>
					<td><a href="/app/stock-entry/${encodeURIComponent(r.name)}">${frappe.utils.escape_html(r.name)}</a></td>
					<td>${frappe.utils.escape_html(r.purpose || "")}${r.is_return ? " / " + __("Return") : ""}</td>
					<td>${r.docstatus}</td>
					<td>${frappe.utils.escape_html(r.work_order || "")}</td>
					<td>${frappe.utils.escape_html(r.job_card || "")}</td>
					<td><span class="rjc-badge ${cls}">${frappe.utils.escape_html(r.classification || "")}</span></td>
					<td>${frappe.utils.escape_html(r.reason || "")}</td>
				</tr>`;
			})
			.join("");
		return $(`
			<table class="rjc-table">
				<thead>
					<tr>
						<th>${__("Stock Entry")}</th>
						<th>${__("Purpose")}</th>
						<th>${__("Docstatus")}</th>
						<th>${__("Work Order")}</th>
						<th>${__("Job Card")}</th>
						<th>${__("Class")}</th>
						<th>${__("Note")}</th>
					</tr>
				</thead>
				<tbody>${body}</tbody>
			</table>
		`);
	}

	_impact_table(p) {
		const before_s = (p.counters_before || {}).source || {};
		const before_t = (p.counters_before || {}).target || {};
		const after = p.counters_after_estimate || {};
		const after_s = after.source || {};
		const after_t = after.target || {};
		const hist = p.historical_qty || {};
		const cell = (wo, field) => `${wo[field] || 0}`;
		const planned = hist.planned_qty != null ? hist.planned_qty : cell(before_t, "qty");
		const qtyRow = `
			<tr>
				<td>${__("Work Order Qty (planned)")}</td>
				<td>${cell(before_s, "qty")}</td>
				<td>${cell(after_s, "qty")}</td>
				<td>${planned}</td>
				<td>${planned}${
			hist.historical_exceeds_planned_qty
				? " / " + __("unchanged; history may exceed plan")
				: ""
		}</td>
			</tr>`;
		return $(`
			<table class="rjc-table">
				<thead>
					<tr>
						<th></th>
						<th>${__("Source before")}</th>
						<th>${__("Source after")}</th>
						<th>${__("Target before")}</th>
						<th>${__("Target after")}</th>
					</tr>
				</thead>
				<tbody>
					${qtyRow}
					<tr><td>${__("Produced Qty")}</td><td>${cell(before_s, "produced_qty")}</td><td>${cell(after_s, "produced_qty")}</td><td>${cell(before_t, "produced_qty")}</td><td>${cell(after_t, "produced_qty")}</td></tr>
					<tr><td>${__("Process Loss Qty")}</td><td>${cell(before_s, "process_loss_qty")}</td><td>${cell(after_s, "process_loss_qty")}</td><td>${cell(before_t, "process_loss_qty")}</td><td>${cell(after_t, "process_loss_qty")}</td></tr>
					<tr><td>${__("Transferred Qty")}</td><td>${cell(before_s, "material_transferred_for_manufacturing")}</td><td>${cell(after_s, "material_transferred_for_manufacturing")}</td><td>${cell(before_t, "material_transferred_for_manufacturing")}</td><td>${cell(after_t, "material_transferred_for_manufacturing")}</td></tr>
				</tbody>
			</table>
		`);
	}

	run_execute() {
		if (!this.preview || !this.preview.can_execute) {
			frappe.msgprint(__("Run a valid Preview first."));
			return;
		}
		const reason = this.reason.get_value();
		if (!reason) {
			frappe.msgprint(__("A reason is required."));
			return;
		}
		frappe.confirm(
			__("Reassign the selected Job Cards? Stock Ledger and GL will not be reposted."),
			() => {
				frappe.call({
					method: `${this.api}.execute`,
					args: {
						preview_token: this.preview.preview_token,
						reason: reason,
					},
					freeze: true,
					freeze_message: __("Reassigning Job Cards"),
					callback: (r) => {
						const msg = r.message || {};
						frappe.show_alert({
							message: __("Reassigned {0} Job Cards. Audit: {1}", [
								(msg.moved_job_cards || []).length,
								msg.log,
							]),
							indicator: "green",
						});
						if (msg.source_empty) {
							frappe.msgprint(
								__(
									"Source Work Order has no remaining Job Cards. It was not deleted."
								)
							);
						}
						this.reset_preview();
						this.load_job_cards();
					},
				});
			}
		);
	}
}

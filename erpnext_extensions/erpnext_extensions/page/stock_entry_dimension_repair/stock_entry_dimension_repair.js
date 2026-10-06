frappe.provide("erpnext_extensions.stock_entry_dimension_repair");

frappe.pages["stock-entry-dimension-repair"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Stock Entry Dimension Repair"),
		single_column: true,
	});
	erpnext_extensions.stock_entry_dimension_repair = new StockEntryDimensionRepairPage(page);
};

class StockEntryDimensionRepairPage {
	constructor(page) {
		this.page = page;
		this.api = "erpnext_extensions.iran_accounting.manufacturing_dimension.api";
		this.scan = null;
		this.preview = null;
		this.dry = null;
		this.fingerprint = null;
		this.$body = $(page.body);
		this.render();
	}

	can_run() {
		const roles = frappe.user_roles || [];
		return (
			frappe.session.user === "Administrator" ||
			roles.includes("System Manager") ||
			roles.includes("Accounts Manager")
		);
	}

	render() {
		this.$body.empty();
		if (!this.can_run()) {
			this.$body.html(
				`<div class="alert alert-danger">${__(
					"Accounts Manager or System Manager required."
				)}</div>`
			);
			return;
		}

		this.$toolbar = $('<div class="se-dim-toolbar" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin-bottom:12px;">').appendTo(this.$body);
		this.se = frappe.ui.form.make_control({
			parent: this.$toolbar,
			df: {
				fieldtype: "Link",
				options: "Stock Entry",
				label: __("Stock Entry"),
				reqd: 1,
				change: () => this.reset_state(),
			},
			render_input: true,
		});
		this.$toolbar.append(
			`<button class="btn btn-primary btn-sm" id="se-dim-scan">${__("Scan")}</button>`
		);
		this.$toolbar.find("#se-dim-scan").on("click", () => this.run_scan());

		this.$groups = $('<div class="se-dim-groups" style="margin-bottom:12px;"></div>').appendTo(this.$body);
		this.$table = $('<div class="se-dim-table"></div>').appendTo(this.$body);

		this.$targets = $('<div class="se-dim-targets" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:12px 0;"></div>').appendTo(this.$body);
		this.dept = frappe.ui.form.make_control({
			parent: this.$targets,
			df: { fieldtype: "Link", options: "Department", label: __("Target Department"), reqd: 1 },
			render_input: true,
		});
		this.cc = frappe.ui.form.make_control({
			parent: this.$targets,
			df: { fieldtype: "Link", options: "Cost Center", label: __("Target Cost Center"), reqd: 1 },
			render_input: true,
		});

		this.$actions = $('<div class="se-dim-actions" style="display:flex;gap:8px;margin-bottom:12px;"></div>').appendTo(this.$body);
		this.$actions.append(`<button class="btn btn-default btn-sm" id="se-dim-preview">${__("Preview")}</button>`);
		this.$actions.append(`<button class="btn btn-default btn-sm" id="se-dim-dry">${__("Dry Run")}</button>`);
		this.$actions.append(`<button class="btn btn-danger btn-sm" id="se-dim-apply" disabled>${__("Apply Repair")}</button>`);
		this.$actions.find("#se-dim-preview").on("click", () => this.run_preview());
		this.$actions.find("#se-dim-dry").on("click", () => this.run_dry());
		this.$actions.find("#se-dim-apply").on("click", () => this.run_apply());

		this.$out = $('<div class="se-dim-out"></div>').appendTo(this.$body);
	}

	reset_state() {
		this.scan = null;
		this.preview = null;
		this.dry = null;
		this.fingerprint = null;
		this.$groups.empty();
		this.$table.empty();
		this.$out.empty();
		this.$actions.find("#se-dim-apply").prop("disabled", true);
	}

	selected_rows() {
		const names = [];
		this.$table.find("input.se-dim-select:checked").each(function () {
			names.push($(this).data("row"));
		});
		return names;
	}

	run_scan() {
		const name = this.se.get_value();
		if (!name) {
			frappe.msgprint(__("Select a Stock Entry"));
			return;
		}
		this.reset_state();
		frappe.call({
			method: `${this.api}.scan`,
			args: { stock_entry: name },
			freeze: true,
			callback: (r) => {
				this.scan = r.message || {};
				this.render_scan();
			},
		});
	}

	render_scan() {
		const s = this.scan || {};
		let html = `<div><b>${s.name || ""}</b> — ${s.purpose || ""} — docstatus ${s.docstatus}
			— ${__("Status")}: ${s.status}</div>`;
		(s.groups || []).forEach((g, i) => {
			html += `<div style="margin-top:6px;"><b>Group ${String.fromCharCode(65 + i)}:</b> ${frappe.utils.escape_html(g.label)}
				(${g.row_count} ${__("rows")})</div>`;
		});
		this.$groups.html(html);

		let table = `<table class="table table-bordered table-condensed"><thead><tr>
			<th></th><th>idx</th><th>${__("Item")}</th><th>${__("Department")}</th><th>${__("Cost Center")}</th>
		</tr></thead><tbody>`;
		(s.rows || []).forEach((row) => {
			table += `<tr>
				<td><input type="checkbox" class="se-dim-select" data-row="${row.row_name}"></td>
				<td>${row.idx}</td>
				<td>${frappe.utils.escape_html(row.item_code || "")}</td>
				<td>${frappe.utils.escape_html(row.department || "(blank)")}</td>
				<td>${frappe.utils.escape_html(row.cost_center || "(blank)")}</td>
			</tr>`;
		});
		table += "</tbody></table>";
		this.$table.html(table);
	}

	run_preview() {
		const name = this.se.get_value();
		const selected = this.selected_rows();
		frappe.call({
			method: `${this.api}.preview`,
			args: {
				stock_entry: name,
				selected_row_names: selected,
				target_department: this.dept.get_value(),
				target_cost_center: this.cc.get_value(),
			},
			freeze: true,
			callback: (r) => {
				this.preview = r.message || {};
				const changes = this.preview.changes || [];
				let html = `<h4>${__("Preview")}</h4>`;
				if (!changes.length) {
					html += `<div>${__("No dimension changes proposed.")}</div>`;
				} else {
					html += "<ul>";
					changes.forEach((c) => {
						html += `<li>Row ${c.before.idx} ${frappe.utils.escape_html(c.before.item_code)}:
							${frappe.utils.escape_html(c.before.department || "(blank)")} / ${frappe.utils.escape_html(c.before.cost_center || "(blank)")}
							→ ${frappe.utils.escape_html(c.after.department)} / ${frappe.utils.escape_html(c.after.cost_center)}</li>`;
					});
					html += "</ul>";
				}
				this.$out.html(html);
			},
		});
	}

	run_dry() {
		const name = this.se.get_value();
		const selected = this.selected_rows();
		frappe.call({
			method: `${this.api}.dry_run`,
			args: {
				stock_entry: name,
				selected_row_names: selected,
				target_department: this.dept.get_value(),
				target_cost_center: this.cc.get_value(),
			},
			freeze: true,
			callback: (r) => {
				this.dry = r.message || {};
				this.fingerprint = this.dry.fingerprint || null;
				this.render_dry();
				this.$actions.find("#se-dim-apply").prop("disabled", !this.fingerprint);
			},
		});
	}

	render_dry() {
		const d = this.dry || {};
		const fmt = (rows) => {
			if (!rows || !rows.length) return "<div>(empty)</div>";
			let t = `<table class="table table-bordered table-condensed"><thead><tr>
				<th>account</th><th>cost_center</th><th>department</th><th>debit</th><th>credit</th>
			</tr></thead><tbody>`;
			rows.forEach((row) => {
				t += `<tr>
					<td>${frappe.utils.escape_html(row.account || "")}</td>
					<td>${frappe.utils.escape_html(row.cost_center || "")}</td>
					<td>${frappe.utils.escape_html(row.department || "")}</td>
					<td>${format_currency(row.debit || 0)}</td>
					<td>${format_currency(row.credit || 0)}</td>
				</tr>`;
			});
			t += "</tbody></table>";
			return t;
		};
		const diff = d.gl_diff || {};
		let html = `<h4>${__("Dry Run")}</h4>
			<div>${__("Fingerprint")}: <code>${frappe.utils.escape_html(d.fingerprint || "")}</code></div>
			<div>${__("DB clean")}: ${d.db_clean ? "YES" : "NO"} | ${__("SLE impact")}: ${JSON.stringify(d.sle_impact)}</div>
			<h5>${__("Current GL")}</h5>${fmt(d.current_gl)}
			<h5>${__("Proposed GL")}</h5>${fmt(d.proposed_gl)}
			<h5>${__("Difference")}</h5>
			<pre style="white-space:pre-wrap;">${frappe.utils.escape_html(JSON.stringify({ removed: diff.removed, added: diff.added, identical: diff.identical }, null, 2))}</pre>`;
		this.$out.html(html);
	}

	run_apply() {
		if (!this.fingerprint) {
			frappe.msgprint(__("Run Dry Run first"));
			return;
		}
		frappe.confirm(
			__("Apply Dimension Repair to this Stock Entry? This rebuilds GL for the voucher. SLE will not be touched."),
			() => {
				frappe.call({
					method: `${this.api}.apply`,
					args: {
						stock_entry: this.se.get_value(),
						selected_row_names: this.selected_rows(),
						target_department: this.dept.get_value(),
						target_cost_center: this.cc.get_value(),
						fingerprint: this.fingerprint,
						confirm: 1,
					},
					freeze: true,
					callback: (r) => {
						frappe.msgprint(__("Repair applied: {0}", [(r.message || {}).name || ""]));
						this.fingerprint = null;
						this.$actions.find("#se-dim-apply").prop("disabled", true);
						this.run_scan();
					},
				});
			}
		);
	}
}

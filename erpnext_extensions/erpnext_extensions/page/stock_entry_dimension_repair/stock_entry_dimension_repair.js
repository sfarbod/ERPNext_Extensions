frappe.provide("erpnext_extensions.stock_entry_dimension_repair");

function _safe_make_app_page(opts) {
	// Some container/Playwright environments expose navigator.language as
	// "en-US@posix", which crashes Intl.Locale inside AltShortcutGroup during
	// make_app_page. Harden without changing global Desk behaviour permanently.
	const keys = frappe.ui && frappe.ui.keys;
	const orig = keys && keys.get_shortcut_group;
	if (orig) {
		keys.get_shortcut_group = function () {
			try {
				return orig.apply(this, arguments);
			} catch (e) {
				return {
					add_item() {},
					add() {},
					add_shortcut() {},
				};
			}
		};
	}
	try {
		return frappe.ui.make_app_page(opts);
	} finally {
		if (orig) {
			keys.get_shortcut_group = orig;
		}
	}
}

frappe.pages["stock-entry-dimension-repair"].on_page_load = function (wrapper) {
	const page = _safe_make_app_page({
		parent: wrapper,
		title: __("Stock Entry Dimension Repair"),
		single_column: true,
	});
	page.main.addClass("se-dim-repair-page");
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
				`<div class="alert alert-danger se-dim-alert">${__(
					"Accounts Manager or System Manager required."
				)}</div>`
			);
			return;
		}

		// Section 1 — Stock Entry
		this.$sec1 = $(
			`<div class="se-dim-section" id="se-dim-sec-stock">
				<h4>${__("1. Stock Entry")}</h4>
				<div class="se-dim-toolbar"></div>
				<div class="se-dim-meta"></div>
				<div class="se-dim-groups"></div>
				<div class="se-dim-table"></div>
			</div>`
		).appendTo(this.$body);

		this.se = frappe.ui.form.make_control({
			parent: this.$sec1.find(".se-dim-toolbar"),
			df: {
				fieldtype: "Link",
				options: "Stock Entry",
				label: __("Stock Entry"),
				reqd: 1,
				change: () => this.reset_downstream(),
			},
			render_input: true,
		});
		this.$sec1.find(".se-dim-toolbar").append(
			`<button class="btn btn-primary btn-sm" id="se-dim-scan">${__("Scan")}</button>`
		);
		this.$sec1.find("#se-dim-scan").on("click", () => this.run_scan());

		// Section 2 — Target + Preview
		this.$sec2 = $(
			`<div class="se-dim-section" id="se-dim-sec-target">
				<h4>${__("2. Target Dimension")}</h4>
				<div class="se-dim-targets"></div>
				<div class="se-dim-actions-preview" style="margin-top:8px;"></div>
				<div class="se-dim-preview"></div>
			</div>`
		).appendTo(this.$body);

		this.dept = frappe.ui.form.make_control({
			parent: this.$sec2.find(".se-dim-targets"),
			df: {
				fieldtype: "Link",
				options: "Department",
				label: __("Target Department"),
				reqd: 1,
				change: () => this.invalidate_dry_run(),
			},
			render_input: true,
		});
		this.cc = frappe.ui.form.make_control({
			parent: this.$sec2.find(".se-dim-targets"),
			df: {
				fieldtype: "Link",
				options: "Cost Center",
				label: __("Target Cost Center"),
				reqd: 1,
				change: () => this.invalidate_dry_run(),
			},
			render_input: true,
		});
		this.$sec2.find(".se-dim-actions-preview").append(
			`<button class="btn btn-default btn-sm" id="se-dim-preview">${__("Preview")}</button>`
		);
		this.$sec2.find("#se-dim-preview").on("click", () => this.run_preview());

		// Section 3/4 — Dry Run
		this.$sec3 = $(
			`<div class="se-dim-section" id="se-dim-sec-dry">
				<h4>${__("3. Dry Run")}</h4>
				<div class="se-dim-actions-dry" style="margin-bottom:8px;"></div>
				<div class="se-dim-dry"></div>
			</div>`
		).appendTo(this.$body);
		this.$sec3.find(".se-dim-actions-dry").append(
			`<button class="btn btn-default btn-sm" id="se-dim-dry">${__("Dry Run")}</button>`
		);
		this.$sec3.find("#se-dim-dry").on("click", () => this.run_dry());

		// Section 5 — Apply
		this.$sec4 = $(
			`<div class="se-dim-section" id="se-dim-sec-apply">
				<h4>${__("4. Apply Repair")}</h4>
				<div class="se-dim-actions-apply"></div>
			</div>`
		).appendTo(this.$body);
		this.$sec4.find(".se-dim-actions-apply").append(
			`<button class="btn btn-danger btn-sm" id="se-dim-apply" disabled>${__(
				"Apply Repair"
			)}</button>`
		);
		this.$sec4.find("#se-dim-apply").on("click", () => this.run_apply());

		this.$err = $('<div class="se-dim-error"></div>').appendTo(this.$body);
	}

	reset_downstream() {
		this.scan = null;
		this.preview = null;
		this.invalidate_dry_run();
		this.$sec1.find(".se-dim-meta, .se-dim-groups, .se-dim-table").empty();
		this.$sec2.find(".se-dim-preview").empty();
		this.$sec3.find(".se-dim-dry").empty();
		this.$err.empty();
	}

	invalidate_dry_run() {
		this.dry = null;
		this.fingerprint = null;
		this.$sec4.find("#se-dim-apply").prop("disabled", true);
		this.$sec3.find(".se-dim-dry").empty();
	}

	selected_rows() {
		const names = [];
		this.$sec1.find("input.se-dim-select:checked").each(function () {
			names.push($(this).data("row"));
		});
		return names;
	}

	show_error(err) {
		const msg =
			(err && (err.message || err.exc || err)) || __("Unexpected error");
		const text = typeof msg === "string" ? msg : JSON.stringify(msg);
		this.$err.html(`<div class="alert alert-danger se-dim-alert">${frappe.utils.escape_html(text)}</div>`);
		frappe.msgprint({ title: __("Error"), indicator: "red", message: text });
	}

	call_api(method, args, on_ok) {
		this.$err.empty();
		frappe.call({
			method: `${this.api}.${method}`,
			args,
			freeze: true,
			callback: (r) => {
				if (r.exc) {
					this.show_error(r.exc);
					return;
				}
				try {
					on_ok(r.message || {});
				} catch (e) {
					this.show_error(e);
				}
			},
			error: (r) => this.show_error((r && r.message) || r),
		});
	}

	run_scan() {
		const name = this.se.get_value();
		if (!name) {
			frappe.msgprint(__("Select a Stock Entry"));
			return;
		}
		this.reset_downstream();
		this.call_api("scan", { stock_entry: name }, (msg) => {
			this.scan = msg;
			this.render_scan();
		});
	}

	render_scan() {
		const s = this.scan || {};
		const mixed = (s.groups || []).length > 1;
		this.$sec1.find(".se-dim-meta").html(`
			<div class="se-dim-meta-grid">
				<div><b>${__("Stock Entry")}:</b> ${frappe.utils.escape_html(s.name || "")}</div>
				<div><b>${__("Purpose")}:</b> ${frappe.utils.escape_html(s.purpose || "")}</div>
				<div><b>${__("Docstatus")}:</b> ${s.docstatus}</div>
				<div><b>${__("Posting")}:</b> ${frappe.utils.escape_html(
					`${s.posting_date || ""} ${s.posting_time || ""}`
				)}</div>
				<div><b>${__("Status")}:</b>
					<span class="se-dim-status ${mixed ? "mixed" : "uniform"}">${frappe.utils.escape_html(
						s.status || ""
					)}</span>
				</div>
			</div>`);

		let groups = "";
		(s.groups || []).forEach((g, i) => {
			groups += `<div class="se-dim-group ${mixed ? "mixed-group" : ""}">
				<b>Group ${String.fromCharCode(65 + i)}:</b>
				${frappe.utils.escape_html(g.label)} (${g.row_count} ${__("rows")})
			</div>`;
		});
		this.$sec1.find(".se-dim-groups").html(groups);

		const majorityKey =
			(s.groups || []).length > 1
				? (s.groups || []).slice().sort((a, b) => b.row_count - a.row_count)[0].label
				: null;

		let table = `<table class="table table-bordered table-condensed se-dim-rows">
			<thead><tr>
				<th></th><th>${__("Idx")}</th><th>${__("Item Code")}</th><th>${__("Item Name")}</th>
				<th>${__("Department")}</th><th>${__("Cost Center")}</th>
			</tr></thead><tbody>`;
		(s.rows || []).forEach((row) => {
			const key = `${row.department || "(blank)"} / ${row.cost_center || "(blank)"}`;
			const minority = mixed && majorityKey && key !== majorityKey;
			table += `<tr class="${minority ? "se-dim-row-diff" : ""}">
				<td><input type="checkbox" class="se-dim-select" data-row="${frappe.utils.escape_html(
					row.row_name
				)}"></td>
				<td>${row.idx}</td>
				<td>${frappe.utils.escape_html(row.item_code || "")}</td>
				<td>${frappe.utils.escape_html(row.item_name || "")}</td>
				<td>${frappe.utils.escape_html(row.department || "(blank)")}</td>
				<td>${frappe.utils.escape_html(row.cost_center || "(blank)")}</td>
			</tr>`;
		});
		table += "</tbody></table>";
		this.$sec1.find(".se-dim-table").html(table);
		this.$sec1.find("input.se-dim-select").on("change", () => this.invalidate_dry_run());
	}

	run_preview() {
		const name = this.se.get_value();
		const selected = this.selected_rows();
		if (!selected.length) {
			frappe.msgprint(__("Select at least one row"));
			return;
		}
		this.invalidate_dry_run();
		this.call_api(
			"preview",
			{
				stock_entry: name,
				selected_row_names: selected,
				target_department: this.dept.get_value(),
				target_cost_center: this.cc.get_value(),
			},
			(msg) => {
				this.preview = msg;
				this.render_preview();
			}
		);
	}

	render_preview() {
		const changes = (this.preview || {}).changes || [];
		let html = `<h5>${__("Preview")}</h5>`;
		if (!changes.length) {
			html += `<div>${__("No dimension changes proposed.")}</div>`;
		} else {
			html += `<table class="table table-bordered table-condensed"><thead><tr>
				<th>${__("Idx")}</th><th>${__("Item")}</th>
				<th>${__("Current Department")}</th><th>${__("New Department")}</th>
				<th>${__("Current Cost Center")}</th><th>${__("New Cost Center")}</th>
			</tr></thead><tbody>`;
			changes.forEach((c) => {
				html += `<tr>
					<td>${c.before.idx}</td>
					<td>${frappe.utils.escape_html(c.before.item_code || "")}</td>
					<td>${frappe.utils.escape_html(c.before.department || "(blank)")}</td>
					<td>${frappe.utils.escape_html(c.after.department || "")}</td>
					<td>${frappe.utils.escape_html(c.before.cost_center || "(blank)")}</td>
					<td>${frappe.utils.escape_html(c.after.cost_center || "")}</td>
				</tr>`;
			});
			html += "</tbody></table>";
		}
		this.$sec2.find(".se-dim-preview").html(html);
	}

	run_dry() {
		const name = this.se.get_value();
		const selected = this.selected_rows();
		if (!selected.length) {
			frappe.msgprint(__("Select at least one row"));
			return;
		}
		this.call_api(
			"dry_run",
			{
				stock_entry: name,
				selected_row_names: selected,
				target_department: this.dept.get_value(),
				target_cost_center: this.cc.get_value(),
			},
			(msg) => {
				this.dry = msg;
				this.fingerprint = msg.fingerprint || null;
				this.render_dry();
				const ok = !!(this.fingerprint && msg.db_clean && (msg.validation || {}).ok !== false);
				this.$sec4.find("#se-dim-apply").prop("disabled", !ok);
			}
		);
	}

	_gl_table(rows) {
		if (!rows || !rows.length) return `<div>${__("(empty)")}</div>`;
		let t = `<table class="table table-bordered table-condensed"><thead><tr>
			<th>${__("Account")}</th><th>${__("Department")}</th><th>${__("Cost Center")}</th>
			<th>${__("Debit")}</th><th>${__("Credit")}</th>
		</tr></thead><tbody>`;
		rows.forEach((row) => {
			t += `<tr>
				<td>${frappe.utils.escape_html(row.account || "")}</td>
				<td>${frappe.utils.escape_html(row.department || "")}</td>
				<td>${frappe.utils.escape_html(row.cost_center || "")}</td>
				<td>${format_currency(row.debit || 0)}</td>
				<td>${format_currency(row.credit || 0)}</td>
			</tr>`;
		});
		t += "</tbody></table>";
		return t;
	}

	render_dry() {
		const d = this.dry || {};
		const diff = d.gl_diff || {};
		const sle = d.sle_impact || {};
		let html = `
			<div class="se-dim-dry-meta">
				<div><b>${__("Fingerprint")}:</b> <code>${frappe.utils.escape_html(d.fingerprint || "")}</code></div>
				<div><b>${__("DB clean")}:</b> ${d.db_clean ? "YES" : "NO"}</div>
				<div><b>${__("SLE Impact")}:</b> ${
					sle.changed === false
						? __("No Change Expected")
						: frappe.utils.escape_html(JSON.stringify(sle))
				}</div>
			</div>
			<h5>${__("CURRENT GL")}</h5>${this._gl_table(d.current_gl)}
			<h5>${__("PROPOSED GL")}</h5>${this._gl_table(d.proposed_gl)}
			<h5>${__("GL DIFF")}</h5>
			<pre class="se-dim-diff">${frappe.utils.escape_html(
				JSON.stringify({ identical: diff.identical, removed: diff.removed, added: diff.added }, null, 2)
			)}</pre>`;
		this.$sec3.find(".se-dim-dry").html(html);
	}

	run_apply() {
		if (!this.fingerprint) {
			frappe.msgprint(__("Run Dry Run first"));
			return;
		}
		const confirm_text = __(
			"This will change Department/Cost Center on the selected submitted Stock Entry rows and rebuild the GL for this Stock Entry. Stock Ledger Entries will not be changed."
		);
		frappe.confirm(confirm_text, () => {
			this.call_api(
				"apply",
				{
					stock_entry: this.se.get_value(),
					selected_row_names: this.selected_rows(),
					target_department: this.dept.get_value(),
					target_cost_center: this.cc.get_value(),
					fingerprint: this.fingerprint,
					confirm: 1,
				},
				(msg) => {
					frappe.msgprint(__("Repair applied: {0}", [(msg && msg.name) || ""]));
					this.invalidate_dry_run();
					this.run_scan();
				}
			);
		});
	}
}

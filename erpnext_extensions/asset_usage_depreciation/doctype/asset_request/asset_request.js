// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("Asset Request", {
	setup(frm) {
		frm.set_query("requested_item_code", "items", () => ({
			filters: { is_fixed_asset: 1, disabled: 0, is_grouped_asset: 0 },
		}));
		frm.set_query("fulfilled_item_code", "items", () => ({
			filters: { is_fixed_asset: 1, disabled: 0, is_grouped_asset: 0 },
		}));
		frm.set_query("fulfilled_purchase_item", "items", () => ({
			filters: { is_fixed_asset: 1, disabled: 0, is_grouped_asset: 0 },
		}));
		frm.set_query("preferred_asset", "items", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn] || {};
			return {
				filters: {
					company: doc.company,
					docstatus: 1,
					item_code: row.fulfilled_item_code || row.requested_item_code || undefined,
				},
			};
		});
		frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("cost_center", "items", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("project", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("project", "items", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("employee", () => ({ filters: { company: frm.doc.company, status: "Active" } }));
		if (erpnext.accounts && erpnext.accounts.dimensions) {
			erpnext.accounts.dimensions.setup_dimension_filters(frm, frm.doctype);
		}
	},
	onload(frm) {
		if (frm.is_new() && !frm.doc.employee) {
			frappe.db.get_value("Employee", { user_id: frappe.session.user, status: "Active" }, "name", (r) => {
				if (r && r.name) {
					frm.set_value("employee", r.name);
				}
			});
		}
		if (erpnext.accounts && erpnext.accounts.dimensions) {
			erpnext.accounts.dimensions.setup_dimension_filters(frm, frm.doctype);
		}
	},
	refresh(frm) {
		bind_header_dimension_propagation(frm);
		frm.trigger("toggle_fulfillment_buttons");
		render_fulfillment_panel(frm);
	},
	company(frm) {
		if (erpnext.accounts && erpnext.accounts.dimensions) {
			erpnext.accounts.dimensions.update_dimension(frm, frm.doctype);
		}
	},
	cost_center(frm) {
		fill_empty_item_dimensions(frm, "cost_center");
	},
	project(frm) {
		fill_empty_item_dimensions(frm, "project");
	},
	items_add(frm, cdt, cdn) {
		copy_header_dimensions_to_row(frm, cdt, cdn);
	},
	toggle_fulfillment_buttons(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}
		const approved = frm.doc.workflow_state === "Approved";
		const fulfilled = frm.doc.fulfillment_status === "Fulfilled";
		const can_fulfill =
			frappe.user_roles.includes("Asset Manager") || frappe.user_roles.includes("System Manager");
		// Fully Fulfilled requests hide Issue/Purchase; partial may still fulfill remainder.
		if (!can_fulfill || !approved || fulfilled) {
			return;
		}
		frm
			.add_custom_button(__("Check Availability"), () => check_availability(frm))
			.attr("title", __("Count and list pool assets that can fulfill this request. Does not reserve assets or create documents."));
		frm
			.add_custom_button(__("Issue from Pool"), () => open_pool_picker(frm))
			.attr("title", __("Select pool asset(s), confirm, then create Asset Movement to the requester. Does not create a purchase."));
		frm
			.add_custom_button(__("Request Purchase"), () => request_purchase(frm))
			.attr("title", __("Create a Material Request for the fulfilled item. Does not issue a pool asset."));
	},
});

frappe.ui.form.on("Asset Request Item", {
	items_add(frm, cdt, cdn) {
		copy_header_dimensions_to_row(frm, cdt, cdn);
	},
	requested_item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.fulfilled_item_code && row.requested_item_code) {
			frappe.model.set_value(cdt, cdn, "fulfilled_item_code", row.requested_item_code);
		}
		refresh_available_qty(frm, cdt, cdn);
	},
	fulfilled_item_code(frm, cdt, cdn) {
		refresh_available_qty(frm, cdt, cdn);
	},
	qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (flt(row.qty) < 1) {
			frappe.model.set_value(cdt, cdn, "qty", 1);
		}
	},
});

function get_dimension_fieldnames() {
	const fields = ["cost_center", "project"];
	const dims = (erpnext.accounts && erpnext.accounts.dimensions && erpnext.accounts.dimensions.accounting_dimensions) || [];
	dims.forEach((d) => {
		const fn = d.fieldname || d;
		if (fn && !fields.includes(fn)) {
			fields.push(fn);
		}
	});
	return fields;
}

function copy_header_dimensions_to_row(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row) {
		return;
	}
	get_dimension_fieldnames().forEach((fn) => {
		if (!frappe.meta.has_field(cdt, fn) || !frappe.meta.has_field(frm.doctype, fn)) {
			return;
		}
		if (!row[fn] && frm.doc[fn]) {
			frappe.model.set_value(cdt, cdn, fn, frm.doc[fn]);
		}
	});
}

function fill_empty_item_dimensions(frm, fieldname) {
	(frm.doc.items || []).forEach((row) => {
		if (!row[fieldname] && frm.doc[fieldname]) {
			frappe.model.set_value(row.doctype, row.name, fieldname, frm.doc[fieldname]);
		}
	});
}

function bind_header_dimension_propagation(frm) {
	get_dimension_fieldnames().forEach((fn) => {
		if (fn === "cost_center" || fn === "project") {
			return;
		}
		const field = frm.get_field(fn);
		if (!field || field.df._ar_dim_bound) {
			return;
		}
		field.df._ar_dim_bound = 1;
		const original = field.df.onchange;
		field.df.onchange = function () {
			if (original) {
				original.apply(this, arguments);
			}
			fill_empty_item_dimensions(frm, fn);
		};
	});
}

function refresh_available_qty(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!frm.doc.company || !row.requested_item_code) {
		return;
	}
	frappe.call({
		method: "erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request.get_available_asset_count",
		args: {
			company: frm.doc.company,
			requested_item_code: row.requested_item_code,
			requested_asset_category: row.requested_asset_category,
			fulfilled_item_code: row.fulfilled_item_code,
			exclude_request: frm.doc.name,
		},
		callback(r) {
			frappe.model.set_value(cdt, cdn, "available_qty", r.message || 0);
		},
	});
}


function can_fulfill(frm) {
	return (
		frappe.user_roles.includes("Asset Manager") || frappe.user_roles.includes("System Manager")
	);
}

function render_fulfillment_panel(frm) {
	const wrap = frm.get_field("fulfillment_html");
	if (!wrap) {
		return;
	}
	if (frm.doc.docstatus !== 1) {
		wrap.$wrapper.html(
			`<p class="text-muted">${__("Fulfillment starts after the request is Approved.")}</p>`
		);
		return;
	}
	const fulfillment_label = fulfillment_status_label(frm.doc.fulfillment_status);
	const qty_rows = (frm.doc.items || [])
		.map((row) => {
			const requested = cint(row.qty);
			const delivered = cint(row.delivered_qty);
			const remaining =
				row.remaining_qty != null ? cint(row.remaining_qty) : Math.max(0, requested - delivered);
			return `<tr>
				<td>${frappe.utils.escape_html(row.requested_item_code || "")}</td>
				<td>${requested}</td>
				<td>${delivered}</td>
				<td>${remaining}</td>
			</tr>`;
		})
		.join("");
	const qty_table = qty_rows
		? `<table class="table table-bordered" style="max-width: 640px; margin-bottom: 12px">
			<thead><tr>
				<th>${__("Item")}</th>
				<th>${__("Requested Quantity")}</th>
				<th>${__("Delivered Quantity")}</th>
				<th>${__("Remaining Quantity")}</th>
			</tr></thead>
			<tbody>${qty_rows}</tbody>
		</table>`
		: "";
	const done_banner =
		frm.doc.fulfillment_status === "Fulfilled"
			? `<div class="alert alert-success" style="margin-bottom: 12px">
				<strong>${__("Fully Fulfilled")} / ${__("تحویل کامل")}</strong>
				— ${__("All requested assets have been delivered via submitted Asset Movement.")}
			</div>`
			: "";
	const status_html = `
		<div class="ar-fulfillment-status" style="margin-bottom: 12px">
			<p><strong>${__("Approval Status")}:</strong> ${frappe.utils.escape_html(frm.doc.workflow_state || frm.doc.status || "")}</p>
			<p><strong>${__("Fulfillment Status")}:</strong> ${frappe.utils.escape_html(fulfillment_label)}</p>
			<p><strong>${__("Delivered Qty")}:</strong> ${cint(frm.doc.delivered_qty)}</p>
		</div>${done_banner}${qty_table}`;
	if (!can_fulfill(frm) || frm.doc.workflow_state !== "Approved") {
		wrap.$wrapper.html(
			status_html +
				`<p class="text-muted">${__("Available assets are shown to the Asset Manager.")}</p>`
		);
		return;
	}
	frappe.call({
		method: "erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request.get_pool_picker",
		args: { name: frm.doc.name },
		callback(r) {
			wrap.$wrapper.html(status_html + pool_table_html(r.message || {}));
		},
	});
}

function fulfillment_status_label(status) {
	if (status === "Fulfilled") {
		return `${__("Fully Fulfilled")} (${__("Fulfilled")})`;
	}
	if (status === "Waiting for fulfillment") {
		return `${__("Not Fulfilled")} (${__("Waiting for fulfillment")})`;
	}
	return __(status || "");
}

function cint(v) {
	const n = parseInt(v, 10);
	return Number.isFinite(n) ? n : 0;
}

function pool_table_html(data) {
	const lines = data.lines || [];
	if (!lines.length) {
		return `<p class="text-muted">${__("No pool candidates.")}</p>`;
	}
	const rows = [];
	lines.forEach((line) => {
		(line.candidates || []).forEach((c) => {
			const match =
				c.match_type === "exact" ? __("Exact") : __("Category substitute");
			rows.push(
				`<tr>
					<td>${frappe.utils.escape_html(line.requested_item_code || "")}</td>
					<td>${frappe.utils.escape_html(c.name || "")}</td>
					<td>${frappe.utils.escape_html(c.item_code || "")}</td>
					<td>${frappe.utils.escape_html(c.asset_category || "")}</td>
					<td>${frappe.utils.escape_html(c.location || "")}</td>
					<td>${frappe.utils.escape_html(match)}</td>
				</tr>`
			);
		});
		if (!(line.candidates || []).length) {
			rows.push(
				`<tr><td>${frappe.utils.escape_html(line.requested_item_code || "")}</td>
				<td colspan="5" class="text-muted">${__("No pool assets")}</td></tr>`
			);
		}
	});
	return `<p><strong>${__("Available Assets")}</strong></p>
		<table class="table table-bordered ar-pool-table" style="max-width: 960px">
			<thead><tr>
				<th>${__("Requested")}</th><th>${__("Asset")}</th><th>${__("Item")}</th>
				<th>${__("Category")}</th><th>${__("Location")}</th><th>${__("Match")}</th>
			</tr></thead>
			<tbody>${rows.join("")}</tbody>
		</table>`;
}

function check_availability(frm) {
	frappe.call({
		method: "erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request.check_availability",
		args: { name: frm.doc.name },
		freeze: true,
		callback(r) {
			frm.reload_doc();
			const n = (r.message && r.message.available_asset_count) || 0;
			frappe.show_alert({
				message: __("Availability checked. {0} pool asset(s) found. No documents created.", [n]),
				indicator: "green",
			});
		},
	});
}

function request_purchase(frm) {
	frappe.confirm(
		__("Create a Material Request for this approved Asset Request? This does not issue a pool asset."),
		() => {
			frappe.call({
				method: "erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request.request_purchase",
				args: { name: frm.doc.name },
				freeze: true,
				callback(r) {
					frm.reload_doc();
					if (r.message && r.message.material_request) {
						frappe.set_route("Form", "Material Request", r.message.material_request);
					}
				},
			});
		}
	);
}

function open_pool_picker(frm) {
	frappe.call({
		method: "erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request.get_pool_picker",
		args: { name: frm.doc.name },
		freeze: true,
		callback(r) {
			show_pool_picker_dialog(frm, r.message || {});
		},
	});
}

function show_pool_picker_dialog(frm, data) {
	const lines = data.lines || [];
	const has_any = lines.some((l) => (l.candidates || []).length);
	if (!has_any) {
		frappe.msgprint(__("No pool assets. Use Request Purchase."));
		return;
	}
	let html = `<div class="ar-pool-picker">`;
	lines.forEach((line, idx) => {
		html += `<p><strong>${frappe.utils.escape_html(line.requested_item_code || "")}</strong>
			${__("Qty")}: ${cint(line.remaining_qty)}</p>`;
		html += `<table class="table table-bordered"><thead><tr>
			<th></th><th>${__("Asset")}</th><th>${__("Item")}</th><th>${__("Match")}</th>
			</tr></thead><tbody>`;
		let pre = cint(line.remaining_qty);
		(line.candidates || []).forEach((c) => {
			const checked = pre > 0 ? "checked" : "";
			if (pre > 0) pre -= 1;
			const match = c.match_type === "exact" ? __("Exact") : __("Category substitute");
			html += `<tr>
				<td><input type="checkbox" class="ar-pool-asset" data-row="${frappe.utils.escape_html(line.item_row)}"
					data-asset="${frappe.utils.escape_html(c.name)}" data-match="${frappe.utils.escape_html(c.match_type)}" ${checked}></td>
				<td>${frappe.utils.escape_html(c.name)}</td>
				<td>${frappe.utils.escape_html(c.item_code || "")}</td>
				<td>${frappe.utils.escape_html(match)}</td>
			</tr>`;
		});
		html += `</tbody></table>`;
	});
	html += `</div>`;
	const d = new frappe.ui.Dialog({
		title: __("Issue from Pool"),
		fields: [{ fieldname: "picker_html", fieldtype: "HTML" }],
		primary_action_label: __("Confirm Issue"),
		primary_action() {
			const selections = [];
			let has_sub = false;
			d.$wrapper.find("input.ar-pool-asset:checked").each(function () {
				const $el = $(this);
				const match = $el.attr("data-match");
				if (match === "substitute") has_sub = true;
				selections.push({
					item_row: $el.attr("data-row"),
					asset: $el.attr("data-asset"),
				});
			});
			if (!selections.length) {
				frappe.msgprint(__("Select at least one pool asset."));
				return;
			}
			const run = () => {
				d.hide();
				frappe.call({
					method: "erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request.issue_from_pool",
					args: {
						name: frm.doc.name,
						selections,
						confirm_substitution: has_sub ? 1 : 0,
					},
					freeze: true,
					callback(r) {
						frm.reload_doc();
						if (r.message && r.message.asset_movement) {
							frappe.set_route("Form", "Asset Movement", r.message.asset_movement);
						}
					},
				});
			};
			if (has_sub) {
				frappe.confirm(
					__("One or more selected assets are category substitutes. Continue?"),
					run
				);
			} else {
				run();
			}
		},
	});
	d.fields_dict.picker_html.$wrapper.html(html);
	d.show();
}

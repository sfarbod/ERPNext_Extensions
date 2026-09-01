// Copyright (c) 2026, Farbod Siyahpoosh and contributors
// For license information, please see license.txt
//
// PM Clearance: settlement lines (Purchase Invoice and/or Supplier Advance) + PM Request allocation.

const SETTLEMENT_PI = "Purchase Invoice";
const SETTLEMENT_SA = "Supplier Advance";

frappe.ui.form.on("PM Clearance", {
	employee(frm) {
		frm._pm_alloc_select_holder_shown = 0;
		frm._pm_no_holder_msg_done = 0;
		frm.trigger("refresh_holder_pending");
	},
	company(frm) {
		frm._pm_alloc_select_holder_shown = 0;
		frm._pm_no_holder_msg_done = 0;
		frm.trigger("refresh_holder_pending");
	},
	transaction_date(frm) {
		frm.trigger("refresh_holder_pending");
	},
	workflow_state(frm) {
		setup_settlement_buttons(frm);
		if (erpnext_extensions?.petty_management?.apply_pending_remark_only_lock) {
			erpnext_extensions.petty_management.apply_pending_remark_only_lock(frm);
		}
	},
	status(frm) {
		setup_settlement_buttons(frm);
	},
	docstatus(frm) {
		setup_settlement_buttons(frm);
	},
	journal_entry(frm) {
		setup_settlement_buttons(frm);
	},
	request_allocations_add(frm) {
		frm.trigger("recalc_totals");
		setTimeout(() => refresh_request_allocation_row_columns(frm), 0);
	},
	request_allocations_remove(frm) {
		frm.trigger("recalc_totals");
	},
	setup(frm) {
		if (erpnext_extensions?.petty_management?.install_workflow_reject_filter) {
			erpnext_extensions.petty_management.install_workflow_reject_filter();
		}
		if (!frappe._pm_clearance_link_debug_bound) {
			frappe._pm_clearance_link_debug_bound = true;
			try {
				const enabled =
					(localStorage && localStorage.getItem("pm_clearance_link_debug") === "1") ||
					(window && window.PM_CLEARANCE_LINK_DEBUG === 1);
				if (enabled && typeof frappe.call === "function") {
					const orig = frappe.call;
					frappe.call = function (opts) {
						try {
							const method = opts && (opts.method || "");
							const args = (opts && opts.args) || {};
							if (
								method === "frappe.desk.search.search_link" &&
								(args.doctype === "Purchase Invoice" ||
									args.doctype === "Purchase Order")
							) {
								console.debug("[PM Clearance][link] search_link req", {
									doctype: args.doctype,
									txt: args.txt,
									query: args.query,
									filters: args.filters,
								});
								const cb = opts.callback;
								opts.callback = function (r) {
									try {
										console.debug("[PM Clearance][link] search_link resp", {
											doctype: args.doctype,
											count: Array.isArray(r)
												? r.length
												: Array.isArray(r?.message)
												? r.message.length
												: null,
											sample: Array.isArray(r)
												? r[0]
												: Array.isArray(r?.message)
												? r.message[0]
												: null,
											raw: r,
										});
									} catch (_e) {
										/* ignore */
									}
									return cb ? cb.apply(this, arguments) : undefined;
								};
							}
						} catch (_e) {
							/* ignore */
						}
						return orig.apply(this, arguments);
					};
					console.debug("[PM Clearance][debug] link debug enabled");
				}
			} catch (_e) {
				/* ignore */
			}
		}

		frm.set_query("pm_opening_advance", "request_allocations", () => {
			const ready =
				frm.doc.employee &&
				frm.doc.company &&
				frm.doc.holder &&
				(frm.doc.petty_cash_account || "").trim();
			if (!ready) {
				return { filters: { name: ["=", ""] } };
			}
			return {
				query: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.pm_opening_advance_query_for_pm_clearance",
				filters: {
					company: frm.doc.company,
					holder: frm.doc.holder,
					petty_cash_account: frm.doc.petty_cash_account,
					pm_clearance: frm.doc.name || null,
				},
			};
		});
		frm.set_query("pm_request", "request_allocations", () => {
			const ready =
				frm.doc.employee &&
				frm.doc.company &&
				frm.doc.holder &&
				(frm.doc.petty_cash_account || "").trim();
			if (!ready && !frm._pm_alloc_select_holder_shown) {
				frappe.show_alert({
					message: __("Select Employee/Holder first."),
					indicator: "orange",
				});
				frm._pm_alloc_select_holder_shown = 1;
			}
			if (ready) {
				frm._pm_alloc_select_holder_shown = 0;
			}
			return {
				query: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.pm_request_query_for_pm_clearance",
				filters: {
					employee: frm.doc.employee,
					company: frm.doc.company,
					holder: frm.doc.holder,
					petty_cash_account: frm.doc.petty_cash_account,
					pm_clearance: frm.doc.name || null,
				},
			};
		});
		frm.set_query("purchase_invoice", "details", (doc, cdt, cdn) => {
			try {
				const enabled =
					(localStorage && localStorage.getItem("pm_clearance_link_debug") === "1") ||
					(window && window.PM_CLEARANCE_LINK_DEBUG === 1);
				if (enabled) {
					console.debug("[PM Clearance][set_query] purchase_invoice(details)", {
						cdt,
						cdn,
					});
				}
			} catch (_e) {
				/* ignore */
			}
			const row = locals[cdt]?.[cdn];
			if (!row || (row.settlement_type || SETTLEMENT_PI) !== SETTLEMENT_PI) {
				return { filters: { name: ["=", ""] } };
			}
			if (!frm.doc.company) {
				return { filters: { name: ["=", ""] } };
			}
			const filters = { company: frm.doc.company };
			if (row.supplier) {
				filters.supplier = row.supplier;
			}
			return {
				query: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.purchase_invoice_query_for_pm_clearance",
				filters,
			};
		});
		frm.set_query("purchase_order", "details", (doc, cdt, cdn) => {
			try {
				const enabled =
					(localStorage && localStorage.getItem("pm_clearance_link_debug") === "1") ||
					(window && window.PM_CLEARANCE_LINK_DEBUG === 1);
				if (enabled) {
					console.debug("[PM Clearance][set_query] purchase_order(details)", {
						cdt,
						cdn,
					});
				}
			} catch (_e) {
				/* ignore */
			}
			const row = locals[cdt]?.[cdn];
			if (!row || (row.settlement_type || SETTLEMENT_PI) !== SETTLEMENT_SA) {
				return { filters: { name: ["=", ""] } };
			}
			if (!frm.doc.company) {
				return { filters: { name: ["=", ""] } };
			}
			const filters = { company: frm.doc.company };
			if (row.supplier) {
				filters.supplier = row.supplier;
			}
			return {
				query: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.purchase_order_query_for_pm_clearance",
				filters,
			};
		});
	},
	refresh_holder_pending(frm) {
		if (!can_mutate_derived_fields(frm)) {
			setup_settlement_buttons(frm);
			return;
		}
		if (!frm.doc.employee || !frm.doc.company) {
			set_form_value_if_changed(frm, "holder", "");
			set_form_value_if_changed(frm, "petty_cash_account", "");
			set_availability_fields(frm, {});
			frm._pm_clearance_prev_holder = undefined;
			frm.trigger("recalc_totals");
			setup_settlement_buttons(frm);
			return;
		}
		if (frm.doc.company) {
			frappe.db.get_value("Company", frm.doc.company, "default_currency", (cur) => {
				if (!can_mutate_derived_fields(frm)) {
					return;
				}
				if (cur && cur.default_currency) {
					set_form_value_if_changed(frm, "currency", cur.default_currency);
				}
			});
		}
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.get_pm_clearance_holder_context",
			args: {
				employee: frm.doc.employee,
				company: frm.doc.company,
				posting_date: frm.doc.transaction_date,
			},
			callback(resp) {
				const r = resp.message || {};
				if (!can_mutate_derived_fields(frm)) {
					setup_settlement_buttons(frm);
					return;
				}
				const prev = frm._pm_clearance_prev_holder;
				if (!r || !r.name) {
					set_form_value_if_changed(frm, "holder", "");
					set_form_value_if_changed(frm, "petty_cash_account", "");
					set_availability_fields(frm, {});
					if (prev) {
						(frm.doc.request_allocations || []).forEach((row) => {
							if (!row.is_legacy_row && row.pm_request) {
								clear_allocation_row(row);
							}
						});
					}
					frm._pm_clearance_prev_holder = "";
					if (!frm._pm_no_holder_msg_done) {
						frappe.msgprint({
							title: __("PM Holder"),
							message: __(
								"No PM Holder found for this employee and company. Please create PM Holder first."
							),
							indicator: "orange",
						});
						frm._pm_no_holder_msg_done = 1;
					}
					frm.trigger("recalc_totals");
					setup_settlement_buttons(frm);
					return;
				}
				frm._pm_no_holder_msg_done = 0;
				if (prev !== undefined && prev && prev !== r.name) {
					(frm.doc.request_allocations || []).forEach((row) => {
						if (!row.is_legacy_row && (row.pm_request || row.pm_opening_advance)) {
							clear_allocation_row(row);
						}
					});
				}
				frm._pm_clearance_prev_holder = r.name;
				set_form_value_if_changed(frm, "holder", r.name);
				if (r.name) {
					frappe
						.xcall("frappe.desk.search.get_link_title", {
							doctype: "PM Holder",
							docname: r.name,
						})
						.then((title) => {
							if (title) {
								frappe.utils.add_link_title("PM Holder", r.name, title);
								frm.refresh_field("holder");
							}
						});
				}
				set_form_value_if_changed(frm, "petty_cash_account", r.petty_cash_account);
				set_availability_fields(frm, r);
				set_form_value_if_changed(frm, "total_cleared_amount", r.consumed_amount || 0);
				set_form_value_if_changed(frm, "total_funded_amount", r.total_funded_amount || 0);
				frm.trigger("recalc_totals");
				setup_settlement_buttons(frm);
			},
		});
	},
	details_add(frm) {
		frm.trigger("recalc_totals");
	},
	details_remove(frm) {
		frm.trigger("recalc_totals");
	},
	refresh(frm) {
		frappe.workflow.setup(frm.doctype);
		if (erpnext_extensions?.petty_management?.apply_pending_remark_only_lock) {
			erpnext_extensions.petty_management.apply_pending_remark_only_lock(frm);
		}
		setup_settlement_buttons(frm);
		frm.trigger("pm_refresh_pi_readiness_banner");
		if (frm.doc.employee && frm.doc.company && can_mutate_derived_fields(frm)) {
			frm.trigger("refresh_holder_pending");
		} else if ((!frm.doc.employee || !frm.doc.company) && can_mutate_derived_fields(frm)) {
			set_form_value_if_changed(frm, "holder", "");
			set_form_value_if_changed(frm, "petty_cash_account", "");
			set_availability_fields(frm, {});
			frm._pm_clearance_prev_holder = undefined;
			frm.trigger("recalc_totals");
			setup_settlement_buttons(frm);
		} else {
			update_settlement_balance_intro(
				frm,
				settlement_lines_total(frm),
				request_allocations_total(frm)
			);
			setup_settlement_buttons(frm);
		}
	},
	pm_refresh_pi_readiness_banner(frm) {
		if (!frm.doc || !frm.doc.name || frm.is_new()) {
			frm.dashboard.clear_headline();
			return;
		}
		const flags = frm._pm_action_flags || frm._pm_clearance_apply_flags || {};
		const title = (flags.workflow_state_title || "").trim();
		if (title !== "Pending Finance Review") {
			if (frm._pm_pi_readiness_banner) {
				frm.dashboard.clear_headline();
				frm._pm_pi_readiness_banner = false;
			}
			return;
		}
		if (flags.pi_ready === false && flags.pi_readiness_message) {
			frm.dashboard.set_headline_alert(
				frappe.utils.escape_html(flags.pi_readiness_message).replace(/\n/g, "<br>"),
				"orange"
			);
			frm._pm_pi_readiness_banner = true;
			return;
		}
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.get_pm_clearance_pi_readiness",
			args: { pm_clearance: frm.doc.name },
			callback(r) {
				const data = r.message || {};
				if (!data.ready && data.message) {
					frm.dashboard.set_headline_alert(
						frappe.utils.escape_html(data.message).replace(/\n/g, "<br>"),
						"orange"
					);
					frm._pm_pi_readiness_banner = true;
				} else if (frm._pm_pi_readiness_banner) {
					frm.dashboard.clear_headline();
					frm._pm_pi_readiness_banner = false;
				}
			},
		});
	},
	recalc_totals(frm) {
		let settled = 0;
		(frm.doc.details || []).forEach((r) => {
			settled += flt(r.allocated_amount);
		});
		const req_total = request_allocations_total(frm);
		if (can_mutate_derived_fields(frm)) {
			(frm.doc.details || []).forEach((r) => {
				set_child_value_if_changed(
					r.doctype,
					r.name,
					"amount_plus_tax",
					flt(r.allocated_amount)
				);
			});
			set_form_value_if_changed(frm, "total_expense_without_tax", 0);
			set_form_value_if_changed(frm, "total_tax_amount", 0);
			set_form_value_if_changed(frm, "total_expense_amount", settled);
			set_form_value_if_changed(frm, "total_petty_cash", settled);
			set_form_value_if_changed(
				frm,
				"remaining_amount",
				flt(frm.doc.total_available || frm.doc.pending_amount) - settled
			);
			frm.refresh_field("details");
			frm.refresh_field("request_allocations");
		}
		update_settlement_balance_intro(frm, settled, req_total);
		setup_settlement_buttons(frm);
		setTimeout(() => refresh_request_allocation_row_columns(frm), 0);
	},
});

function update_settlement_balance_intro(frm, settled_total, req_total) {
	frm.set_intro(null);

	const settled = flt(settled_total);
	const allocated = flt(req_total);
	const has_alloc_rows = (frm.doc.request_allocations || []).length > 0;
	const has_settlement_lines = (frm.doc.details || []).length > 0;

	if (!has_alloc_rows && !has_settlement_lines) {
		return;
	}

	if (!has_alloc_rows) {
		frm.set_intro(
			__(
				"Add PM Request allocation lines; total must match settlement lines (Purchase Invoice + Supplier Advance)."
			),
			"orange"
		);
		return;
	}

	if (settled <= 0 && allocated <= 0) {
		return;
	}

	if (Math.abs(settled - allocated) > 0.005) {
		frm.set_intro(
			__("Settlement total ({0}) must equal PM Request allocation total ({1}).", [
				format_amount_plain(frm, settled),
				format_amount_plain(frm, allocated),
			]),
			"orange"
		);
		return;
	}

	if (settled > 0) {
		frm.set_intro(
			__("Settlement and PM Request allocation totals match: {0}", [
				format_amount_plain(frm, settled),
			]),
			"green"
		);
	}
}

function strip_html_from_formatted(text) {
	const raw = text === undefined || text === null ? "" : String(text);
	return raw.replace(/<[^>]*>/g, "").trim();
}

function format_currency(v, currency) {
	const amount = flt(v);
	const cur =
		currency ||
		(frappe.defaults &&
			frappe.defaults.get_default &&
			frappe.defaults.get_default("currency")) ||
		"";
	try {
		if (typeof frappe.format === "function") {
			const formatted = frappe.format(amount, {
				fieldtype: "Currency",
				options: cur || undefined,
			});
			if (formatted !== undefined && formatted !== null && String(formatted).trim() !== "") {
				return strip_html_from_formatted(formatted);
			}
		}
	} catch (e) {
		/* use numeric fallback */
	}
	return `${amount.toLocaleString()} ${cur}`.trim();
}

function format_amount_plain(frm, v) {
	const currency = (frm && frm.doc && frm.doc.currency) || "";
	return format_currency(v, currency);
}

function escape_html(value) {
	const raw = value === undefined || value === null ? "" : String(value);
	if (frappe.utils && frappe.utils.escape_html) {
		return frappe.utils.escape_html(raw);
	}
	return raw.replace(/[&<>"']/g, (ch) => {
		return {
			"&": "&amp;",
			"<": "&lt;",
			">": "&gt;",
			'"': "&quot;",
			"'": "&#39;",
		}[ch];
	});
}

function form_is_dirty(frm) {
	return typeof frm.is_dirty === "function" ? frm.is_dirty() : !!frm.doc.__unsaved;
}

function can_mutate_derived_fields(frm) {
	return frm.doc.docstatus === 0 && (frm.is_new() || form_is_dirty(frm));
}

const NUMERIC_FIELDS = new Set([
	"allocated_amount",
	"amount_plus_tax",
	"available_amount",
	"current_petty_balance",
	"paid_amount",
	"funded_available",
	"opening_available",
	"pending_amount",
	"total_available",
	"previously_allocated_amount",
	"remaining_amount",
	"request_amount",
	"total_cleared_amount",
	"total_expense_amount",
	"total_expense_without_tax",
	"total_funded_amount",
	"total_petty_cash",
	"total_tax_amount",
]);

function values_match(fieldname, current, incoming) {
	const cur = current === undefined || current === null ? "" : current;
	const next = incoming === undefined || incoming === null ? "" : incoming;
	if (cur === "" || next === "") {
		return String(cur) === String(next);
	}
	if (NUMERIC_FIELDS.has(fieldname)) {
		return Math.abs(flt(cur) - flt(next)) < 0.005;
	}
	return String(cur) === String(next);
}

function set_form_value_if_changed(frm, fieldname, value) {
	if (!values_match(fieldname, frm.doc[fieldname], value)) {
		frm.set_value(fieldname, value);
	}
}

function set_child_value_if_changed(cdt, cdn, fieldname, value) {
	const row = locals[cdt] && locals[cdt][cdn];
	if (row && !values_match(fieldname, row[fieldname], value)) {
		frappe.model.set_value(cdt, cdn, fieldname, value);
	}
}

function set_availability_fields(frm, r) {
	const funded = flt(r.funded_available_amount);
	const opening = flt(r.opening_available_amount);
	const total = flt(
		r.total_available_amount !== undefined ? r.total_available_amount : r.current_balance
	);
	set_form_value_if_changed(frm, "funded_available", funded);
	set_form_value_if_changed(frm, "opening_available", opening);
	set_form_value_if_changed(frm, "total_available", total);
	set_form_value_if_changed(frm, "pending_amount", total);
	set_form_value_if_changed(frm, "current_petty_balance", total);
}

function clear_allocation_row(row) {
	set_child_value_if_changed(row.doctype, row.name, "funding_source_type", "PM Request");
	set_child_value_if_changed(row.doctype, row.name, "pm_request", "");
	set_child_value_if_changed(row.doctype, row.name, "pm_opening_advance", "");
	set_child_value_if_changed(row.doctype, row.name, "allocated_amount", 0);
	set_child_value_if_changed(row.doctype, row.name, "request_amount", 0);
	set_child_value_if_changed(row.doctype, row.name, "paid_amount", 0);
	set_child_value_if_changed(row.doctype, row.name, "previously_allocated_amount", 0);
	set_child_value_if_changed(row.doctype, row.name, "available_amount", 0);
}

function preview_settlement_entry(frm) {
	const settled = (frm.doc.details || []).reduce((s, r) => s + flt(r.allocated_amount), 0);
	if (!frm.doc.details || frm.doc.details.length === 0 || settled <= 0) {
		frappe.msgprint(__("Add at least one settlement line with amount to preview."));
		return;
	}
	const preview_args =
		frm.is_new() || form_is_dirty(frm)
			? { doc: JSON.stringify(frm.doc) }
			: { pm_clearance: frm.doc.name };
	frappe.call({
		method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.preview_pm_clearance_settlement",
		args: preview_args,
		freeze: true,
		freeze_message: __("Building preview…"),
		callback(r) {
			if (!r.message) return;
			const d = r.message;
			const auto = d.auto_submit_journal_entry;
			const jeNote = auto
				? __(
						"If you run Settle now, the Journal Entry will be created and submitted (per PM Settings)."
				  )
				: __(
						"If you run Settle now, the Journal Entry will be created as Draft until it is submitted manually."
				  );

			let html = '<div class="small">';
			html +=
				"<p><strong>" + __("Company") + "</strong>: " + escape_html(d.company) + "</p>";
			html +=
				"<p><strong>" +
				__("Posting Date") +
				"</strong>: " +
				escape_html(d.posting_date) +
				"</p>";
			html +=
				"<p><strong>" +
				__("PM Clearance") +
				"</strong>: " +
				escape_html(d.pm_clearance || frm.doc.name || "") +
				"</p>";
			html +=
				"<p><strong>" +
				__("Total Debit") +
				"</strong>: " +
				format_currency(d.total_debit) +
				" &nbsp;|&nbsp; <strong>" +
				__("Total Credit") +
				"</strong>: " +
				format_currency(d.total_credit) +
				"</p>";
			if (d.is_balanced === false) {
				html +=
					'<div class="alert alert-danger small">' +
					__("Debit and credit totals do not match (difference {0}).").format(
						format_currency(d.debit_credit_difference || 0)
					) +
					"</div>";
			}
			html += '<p class="alert alert-warning small mb-0">' + escape_html(jeNote) + "</p>";
			html += "</div>";

			const rows = d.accounts || [];
			const show_cc = rows.some((a) => (a.cost_center || "").trim());
			const show_proj = rows.some((a) => (a.project || "").trim());
			const show_party = rows.some(
				(a) => (a.party_type || "").trim() || (a.party || "").trim()
			);
			const show_ref = rows.some(
				(a) => (a.reference_type || "").trim() || (a.reference_name || "").trim()
			);

			html +=
				'<div class="table-responsive" style="max-width:100%;overflow-x:auto;">' +
				'<table class="table table-bordered table-sm table-hover mb-0" style="min-width:640px;font-size:12px;">' +
				"<thead><tr>" +
				"<th>" +
				__("Type") +
				"</th><th>" +
				__("Account") +
				"</th>";
			if (show_party) {
				html += "<th>" + __("Party Type") + "</th><th>" + __("Party") + "</th>";
			}
			if (show_ref) {
				html += "<th>" + __("Reference Type") + "</th><th>" + __("Reference") + "</th>";
			}
			html +=
				"<th class='text-end'>" +
				__("Debit") +
				"</th><th class='text-end'>" +
				__("Credit") +
				"</th>";
			if (show_cc) {
				html += "<th>" + __("Cost Center") + "</th>";
			}
			if (show_proj) {
				html += "<th>" + __("Project") + "</th>";
			}
			html += "</tr></thead><tbody>";
			rows.forEach((a) => {
				html +=
					"<tr><td>" +
					escape_html(a.line_type || "") +
					"</td><td>" +
					escape_html(a.account || "") +
					"</td>";
				if (show_party) {
					html +=
						"<td>" +
						escape_html(a.party_type || "") +
						"</td><td>" +
						escape_html(a.party || "") +
						"</td>";
				}
				if (show_ref) {
					html +=
						"<td>" +
						escape_html(a.reference_type || "") +
						"</td><td>" +
						escape_html(a.reference_name || "") +
						"</td>";
				}
				html +=
					"<td class='text-end'>" +
					format_currency(a.debit_in_account_currency) +
					"</td><td class='text-end'>" +
					format_currency(a.credit_in_account_currency) +
					"</td>";
				if (show_cc) {
					html += "<td>" + escape_html(a.cost_center || "") + "</td>";
				}
				if (show_proj) {
					html += "<td>" + escape_html(a.project || "") + "</td>";
				}
				html += "</tr>";
			});
			let colSpan = 2 + (show_party ? 2 : 0) + (show_ref ? 2 : 0);
			html +=
				"<tr class='table-light'><td colspan='" +
				colSpan +
				"' class='text-end'><strong>" +
				__("Totals") +
				"</strong></td><td class='text-end'><strong>" +
				format_currency(d.total_debit) +
				"</strong></td><td class='text-end'><strong>" +
				format_currency(d.total_credit) +
				"</strong></td>";
			if (show_cc) {
				html += "<td></td>";
			}
			if (show_proj) {
				html += "<td></td>";
			}
			html += "</tr></tbody></table></div>";
			html +=
				"<p class='text-muted'>" +
				__("This is a preview only; no Journal Entry was created.") +
				"</p>";

			frappe.msgprint({
				title: __("Settlement Journal Entry Preview"),
				message: html,
				wide: true,
			});
		},
	});
}

function remove_pm_clearance_toolbar_buttons(frm) {
	const labels = [
		"Preview Settlement Entry",
		"Settle Petty Cash",
		"Open Settlement Journal Entry",
	];
	labels.forEach((raw) => {
		const L = __(raw);
		frm.remove_custom_button(L);
		frm.page.remove_inner_button(L);
		frm.page.remove_inner_button(raw);
	});
}
function sync_lifecycle_display_from_flags(frm, flags) {
	if (!flags) {
		return;
	}
	if (flags.lifecycle_state && frm.doc.status !== flags.lifecycle_state) {
		frm.doc.status = flags.lifecycle_state;
		frm.refresh_field("status");
	}
	// Do not set workflow_state on the client; use apply_workflow / reload only.
}

function hide_workflow_reject_when_locked(frm, flags) {
	frm._pm_action_flags = flags || {};
	if (erpnext_extensions?.petty_management?.refresh_workflow_actions) {
		erpnext_extensions.petty_management.refresh_workflow_actions(frm);
	}
}

function setup_settlement_buttons(frm) {
	// Do not wipe buttons before flags return — concurrent refresh races would
	// briefly remove custom actions (same class of bug as PM Request toolbar).
	if (frm.is_new()) {
		remove_pm_clearance_toolbar_buttons(frm);
		const run_preview = () => preview_settlement_entry(frm);
		frm.add_custom_button(__("Preview Settlement Entry"), run_preview);
		return;
	}

	frm._pm_clearance_toolbar_request_seq = cint(frm._pm_clearance_toolbar_request_seq || 0) + 1;
	const requestSeq = frm._pm_clearance_toolbar_request_seq;

	frappe.call({
		method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.get_pm_clearance_action_flags",
		args: { pm_clearance: frm.doc.name },
		callback(r) {
			if (r.exc || requestSeq !== frm._pm_clearance_toolbar_request_seq) {
				return;
			}
			const flags = r.message || {};
			frm._pm_action_flags = flags;
			frm._pm_clearance_apply_flags = flags;
			sync_lifecycle_display_from_flags(frm, flags);
			hide_workflow_reject_when_locked(frm, flags);
			apply_pm_clearance_custom_buttons(frm, flags);
		},
	});
}

function apply_pm_clearance_custom_buttons(frm, flags) {
	remove_pm_clearance_toolbar_buttons(frm);
	if (!flags) {
		return;
	}
	if (flags.can_preview) {
		frm.add_custom_button(__("Preview Settlement Entry"), () =>
			preview_settlement_entry(frm)
		);
	}
	if (flags.can_settle) {
		frm.add_custom_button(
			__("Settle Petty Cash"),
			() => {
				frappe.call({
					method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.settle_petty_cash",
					args: { pm_clearance: frm.doc.name },
					freeze: true,
					freeze_message: __("Settling petty cash…"),
					callback(res) {
						if (res.exc) return;
						const je = res.message && res.message.journal_entry;
						frappe.show_alert({
							message: je
								? __("Settlement Journal Entry {0} created", [je])
								: __("Settlement Journal Entry created"),
							indicator: "green",
						});
						frm.reload_doc();
					},
					error(res) {
						const msg =
							(res && res.message) ||
							(res &&
								res._server_messages &&
								frappe.utils.parse_json(res._server_messages)) ||
							__("Could not settle petty cash");
						frappe.msgprint({
							title: __("Settlement failed"),
							message: msg,
							indicator: "red",
						});
					},
				});
			},
			null
		);
		if (frm.change_custom_button_type) {
			frm.change_custom_button_type(__("Settle Petty Cash"), null, "primary");
		}
	}
	if (flags.can_open_je && flags.journal_entry) {
		frm.add_custom_button(__("Open Settlement Journal Entry"), () =>
			frappe.set_route("Form", "Journal Entry", flags.journal_entry)
		);
	}
}

window.pm_clearance_reapply_custom_toolbar = function (frm) {
	if (!frm || frm.doctype !== "PM Clearance") {
		return;
	}
	const flags = frm._pm_clearance_apply_flags || frm._pm_action_flags;
	if (!flags) {
		return;
	}
	apply_pm_clearance_custom_buttons(frm, flags);
};

frappe.ui.form.on("PM Clearance Detail", {
	settlement_type(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.settlement_type === SETTLEMENT_PI) {
			frappe.model.set_value(cdt, cdn, "purchase_order", "");
			frappe.model.set_value(cdt, cdn, "supplier_advance_account", "");
		} else {
			frappe.model.set_value(cdt, cdn, "purchase_invoice", "");
			frappe.model.set_value(cdt, cdn, "outstanding_amount", 0);
		}
		frm.trigger("recalc_totals");
	},
	purchase_invoice(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if ((row.settlement_type || SETTLEMENT_PI) !== SETTLEMENT_PI) {
			return;
		}
		if (!row.purchase_invoice) {
			frappe.model.set_value(cdt, cdn, "supplier", "");
			frappe.model.set_value(cdt, cdn, "outstanding_amount", 0);
			frappe.model.set_value(cdt, cdn, "allocated_amount", 0);
			frm.trigger("recalc_totals");
			return;
		}
		frappe.db.get_doc("Purchase Invoice", row.purchase_invoice).then((pi) => {
			if (cint(pi.docstatus) === 2) {
				frappe.msgprint({
					title: __("Purchase Invoice"),
					message: __("Cancelled Purchase Invoices cannot be used on PM Clearance."),
					indicator: "red",
				});
				frappe.model.set_value(cdt, cdn, "purchase_invoice", "");
				return;
			}
			if (![0, 1].includes(cint(pi.docstatus))) {
				frappe.model.set_value(cdt, cdn, "purchase_invoice", "");
				return;
			}
			frappe.model.set_value(cdt, cdn, "supplier", pi.supplier);
			const ceiling =
				cint(pi.docstatus) === 0
					? flt(pi.grand_total || pi.rounded_total || 0)
					: flt(pi.outstanding_amount || 0);
			frappe.model.set_value(cdt, cdn, "outstanding_amount", ceiling);
			const cur = flt(row.allocated_amount);
			if (!cur) {
				frappe.model.set_value(cdt, cdn, "allocated_amount", ceiling);
			}
			frm.trigger("recalc_totals");
			frm.trigger("pm_refresh_pi_readiness_banner");
		});
	},
	purchase_order(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if ((row.settlement_type || SETTLEMENT_PI) !== SETTLEMENT_SA) {
			return;
		}
		if (!row.purchase_order) {
			frappe.model.set_value(cdt, cdn, "supplier", "");
			frm.trigger("recalc_totals");
			return;
		}
		frappe.db.get_value("Purchase Order", row.purchase_order, "supplier", (r) => {
			if (r) {
				frappe.model.set_value(cdt, cdn, "supplier", r.supplier);
			}
			frm.trigger("recalc_totals");
		});
	},
	allocated_amount(frm) {
		frm.trigger("recalc_totals");
	},
});

function settlement_lines_total(frm) {
	return (frm.doc.details || []).reduce((s, r) => s + flt(r.allocated_amount), 0);
}

function request_allocations_total(frm) {
	return (frm.doc.request_allocations || []).reduce((s, r) => s + flt(r.allocated_amount), 0);
}

function allocated_on_other_pm_request_rows(frm, cdn) {
	let s = 0;
	(frm.doc.request_allocations || []).forEach((r) => {
		if (r.name !== cdn) {
			s += flt(r.allocated_amount);
		}
	});
	return s;
}

function refresh_request_allocation_row_columns(frm, cdn, cdt) {
	const grid = frm.fields_dict.request_allocations?.grid;
	if (!grid) {
		return;
	}
	const apply_row = (row) => {
		if (!row?.name) {
			return;
		}
		const grid_row = grid.grid_rows_by_docname?.[row.name];
		if (!grid_row?.toggle_display) {
			return;
		}
		const is_opening = row.funding_source_type === "PM Opening Advance";
		grid_row.toggle_display("pm_request", !is_opening);
		grid_row.toggle_display("pm_opening_advance", is_opening);
	};
	if (cdn) {
		const row = cdt ? locals[cdt]?.[cdn] : locals["PM Clearance Request Allocation"]?.[cdn];
		apply_row(row);
		return;
	}
	(frm.doc.request_allocations || []).forEach(apply_row);
}

frappe.ui.form.on("PM Clearance Request Allocation", {
	form_render(frm, cdt, cdn) {
		refresh_request_allocation_row_columns(frm, cdn, cdt);
	},
	funding_source_type(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_legacy_row) {
			return;
		}
		if ((row.funding_source_type || "PM Request") === "PM Opening Advance") {
			set_child_value_if_changed(cdt, cdn, "pm_request", "");
		} else {
			set_child_value_if_changed(cdt, cdn, "pm_opening_advance", "");
		}
		set_child_value_if_changed(cdt, cdn, "allocated_amount", 0);
		set_child_value_if_changed(cdt, cdn, "request_amount", 0);
		set_child_value_if_changed(cdt, cdn, "paid_amount", 0);
		set_child_value_if_changed(cdt, cdn, "previously_allocated_amount", 0);
		set_child_value_if_changed(cdt, cdn, "available_amount", 0);
		frm.trigger("recalc_totals");
		refresh_request_allocation_row_columns(frm, cdn, cdt);
	},
	pm_opening_advance(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_legacy_row) {
			return;
		}
		if (!frm.doc.holder || !frm.doc.petty_cash_account) {
			frappe.msgprint(__("Select Employee/Holder first."));
			frappe.model.set_value(cdt, cdn, "pm_opening_advance", "");
			return;
		}
		if (!row.pm_opening_advance) {
			frm.trigger("recalc_totals");
			return;
		}
		set_child_value_if_changed(cdt, cdn, "funding_source_type", "PM Opening Advance");
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.get_opening_advance_allocation_context",
			args: {
				pm_opening_advance: row.pm_opening_advance,
				pm_clearance: frm.doc.name || null,
				company: frm.doc.company,
				employee: frm.doc.employee,
				holder: frm.doc.holder,
				petty_cash_account: frm.doc.petty_cash_account,
			},
			callback(r) {
				if (!r.message) return;
				const m = r.message;
				set_child_value_if_changed(cdt, cdn, "request_amount", m.request_amount);
				set_child_value_if_changed(cdt, cdn, "paid_amount", m.paid_amount);
				set_child_value_if_changed(
					cdt,
					cdn,
					"previously_allocated_amount",
					m.previously_allocated_amount
				);
				set_child_value_if_changed(cdt, cdn, "available_amount", m.available_amount);
				if (flt(m.available_amount) <= 0) {
					frappe.msgprint({
						title: __("Opening Advance"),
						message: __("This opening advance has no available balance."),
						indicator: "orange",
					});
				}
				const settled = settlement_lines_total(frm);
				const other = allocated_on_other_pm_request_rows(frm, cdn);
				const remaining = Math.max(0, settled - other);
				const avail = flt(m.available_amount);
				if (remaining > 0 && avail > 0 && !flt(row.allocated_amount)) {
					set_child_value_if_changed(
						cdt,
						cdn,
						"allocated_amount",
						Math.min(avail, remaining)
					);
				}
				frm.refresh_field("request_allocations");
				frm.trigger("recalc_totals");
			},
		});
	},
	pm_request(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_legacy_row) {
			return;
		}
		if (
			!frm.doc.employee ||
			!frm.doc.company ||
			!frm.doc.holder ||
			!frm.doc.petty_cash_account
		) {
			frappe.msgprint(__("Select Employee/Holder first."));
			frappe.model.set_value(cdt, cdn, "pm_request", "");
			return;
		}
		set_child_value_if_changed(cdt, cdn, "funding_source_type", "PM Request");
		set_child_value_if_changed(cdt, cdn, "pm_opening_advance", "");
		if (!row.pm_request) {
			set_child_value_if_changed(cdt, cdn, "request_amount", 0);
			set_child_value_if_changed(cdt, cdn, "paid_amount", 0);
			set_child_value_if_changed(cdt, cdn, "previously_allocated_amount", 0);
			set_child_value_if_changed(cdt, cdn, "available_amount", 0);
			frm.trigger("recalc_totals");
			return;
		}
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance.get_pm_request_allocation_context",
			args: {
				pm_request: row.pm_request,
				pm_clearance: frm.doc.name || null,
				company: frm.doc.company,
				employee: frm.doc.employee,
				holder: frm.doc.holder,
				petty_cash_account: frm.doc.petty_cash_account,
			},
			callback(r) {
				if (!r.message) return;
				const m = r.message;
				set_child_value_if_changed(cdt, cdn, "request_amount", m.request_amount);
				set_child_value_if_changed(cdt, cdn, "paid_amount", m.paid_amount);
				set_child_value_if_changed(
					cdt,
					cdn,
					"previously_allocated_amount",
					m.previously_allocated_amount
				);
				set_child_value_if_changed(cdt, cdn, "available_amount", m.available_amount);
				const settled = settlement_lines_total(frm);
				const other = allocated_on_other_pm_request_rows(frm, cdn);
				const remaining = Math.max(0, settled - other);
				const avail = flt(m.available_amount);
				if (remaining > 0) {
					const suggested = Math.min(avail, remaining);
					if (suggested <= 0) {
						frappe.msgprint(
							__(
								"No PM Request balance available for allocation (paid {0}, already reserved {1}).",
								[
									format_currency(m.paid_amount),
									format_currency(m.previously_allocated_amount),
								]
							)
						);
					} else if (!flt(row.allocated_amount)) {
						set_child_value_if_changed(cdt, cdn, "allocated_amount", suggested);
					}
				} else {
					set_child_value_if_changed(cdt, cdn, "allocated_amount", "");
					frappe.msgprint(
						__("Total settlement is already fully allocated on this clearance.")
					);
				}
				frm.refresh_field("request_allocations");
				frm.trigger("recalc_totals");
			},
		});
	},
	allocated_amount(frm) {
		frm.trigger("recalc_totals");
	},
});

function flt(v) {
	const parsed = parseFloat(v);
	return Number.isFinite(parsed) ? parsed : 0;
}

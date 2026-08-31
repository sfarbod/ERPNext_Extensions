// Copyright (c) 2026, Farbod Siyahpoosh and contributors
// For license information, please see license.txt

frappe.ui.form.on("PM Request", {
	setup(frm) {
		frm._pm_toolbar_applied_version = 0;
		frm._pm_pe_list_applied_version = 0;
		frm._pm_pe_response_version = "0";
		if (erpnext_extensions?.petty_management?.install_workflow_reject_filter) {
			erpnext_extensions.petty_management.install_workflow_reject_filter();
		}
		bind_pm_request_funding_realtime(frm);
		frm.set_query("employee_bank_account", () => {
			if (!frm.doc.employee) {
				return {
					query: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.get_employee_bank_account_query",
					filters: { employee: "", company: frm.doc.company || "" },
				};
			}
			return {
				query: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.get_employee_bank_account_query",
				filters: { employee: frm.doc.employee, company: frm.doc.company || "" },
			};
		});
	},
	employee(frm) {
		frm.set_value("employee_bank_account", null);
		frm.trigger("refresh_holder_balances");
	},
	company(frm) {
		frm.set_value("employee_bank_account", null);
		frm.trigger("refresh_holder_balances");
	},
	transaction_date(frm) {
		frm.trigger("refresh_holder_balances");
	},
	refresh(frm) {
		frappe.workflow.setup(frm.doctype);
		frm.trigger("recalc_totals");
		if (!frm.is_new() && frm.doc.docstatus === 1) {
			frm.trigger("refresh_payment_entry_list");
		}
		schedule_pm_request_toolbar(frm);
	},
	onload_post_render(frm) {
		expand_pm_request_main_sections(frm);
		schedule_pm_request_toolbar(frm);
	},
	details_add(frm) {
		frm.trigger("recalc_totals");
	},
	details_remove(frm) {
		frm.trigger("recalc_totals");
	},
	refresh_holder_balances(frm) {
		if (!frm.doc.employee || !frm.doc.company) {
			return;
		}
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.get_pm_request_holder_context",
			args: {
				employee: frm.doc.employee,
				company: frm.doc.company,
				posting_date: frm.doc.transaction_date,
			},
			callback(resp) {
				const r = resp.message || {};
				if (!r || !r.name) {
					return;
				}
				frm.set_value("holder", r.name);
				frm.set_value("petty_cash_account", r.petty_cash_account);
				frm.set_value("max_balance_for_petty_cash", r.max_balance);
				frm.set_value("previous_balance", r.current_balance);

				const setBankFromList = () => {
					frappe.db
						.get_list("Bank Account", {
							filters: {
								party_type: "Employee",
								party: frm.doc.employee,
								company: frm.doc.company,
								disabled: 0,
							},
							fields: ["name"],
							limit: 2,
						})
						.then((rows) => {
							if (r.default_employee_bank_account) {
								frm.set_value(
									"employee_bank_account",
									r.default_employee_bank_account
								);
							} else if (rows.length === 1) {
								frm.set_value("employee_bank_account", rows[0].name);
							}
						});
				};

				if (r.default_employee_bank_account) {
					frm.set_value("employee_bank_account", r.default_employee_bank_account);
				} else {
					setBankFromList();
				}
			},
		});
	},
	refresh_payment_entry_list(frm) {
		if (frm.is_new() || frm.doc.docstatus !== 1) {
			return;
		}
		const field = frm.fields_dict.payment_entries_html;
		if (!field) {
			return;
		}
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.get_pm_request_payment_entries",
			args: { pm_request: frm.doc.name },
			callback(r) {
				if (r.exc) {
					render_pm_request_payment_entry_table(field.$wrapper, [], frm.doc.currency, {
						failed: true,
					});
					log_pm_pe_list_error(r.exc);
					return;
				}
				apply_pm_request_pe_list_payload(
					frm,
					field.$wrapper,
					r.message || {},
					frm.doc.currency
				);
			},
			error(r) {
				render_pm_request_payment_entry_table(field.$wrapper, [], frm.doc.currency, {
					failed: true,
				});
				log_pm_pe_list_error(r);
			},
		});
	},
	setup_pm_request_toolbar(frm) {
		if (frm.is_new() || !frm.doc.name) {
			remove_pm_request_toolbar_buttons(frm);
			return;
		}
		frm._pm_toolbar_request_seq = cint(frm._pm_toolbar_request_seq || 0) + 1;
		const requestSeq = frm._pm_toolbar_request_seq;

		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.get_pm_request_action_flags",
			args: { pm_request: frm.doc.name },
			callback(r) {
				if (r.exc || requestSeq !== frm._pm_toolbar_request_seq) {
					return;
				}
				const f = r.message || {};
				const incoming = cint(f.response_version_id || 0);
				const applied = cint(frm._pm_toolbar_applied_version || 0);
				if (incoming > 0 && incoming < applied) {
					return;
				}
				if (incoming >= applied) {
					frm._pm_toolbar_applied_version = incoming;
				}
				apply_pm_request_action_ui(frm, f);
			},
		});
	},
	recalc_totals(frm) {
		let t = 0;
		(frm.doc.details || []).forEach((r) => {
			t += flt(r.advance_amount);
		});
		if (frm.doc.docstatus === 0) {
			frm.set_value("total_requested_amount", t);
			(frm.doc.details || []).forEach((r) => {
				const row = locals[r.doctype][r.name];
				row.percent_of_total = t ? (flt(r.advance_amount) / t) * 100 : 0;
			});
			frm.refresh_field("details");
		}
	},
});

function bind_pm_request_funding_realtime(frm) {
	if (frappe._pm_request_funding_realtime_bound) {
		return;
	}
	frappe._pm_request_funding_realtime_bound = true;
	frappe.realtime.on("pm_request_funding_updated", (data) => {
		if (typeof frappe.ui.form.get_open_form !== "function") {
			return;
		}
		const active = frappe.ui.form.get_open_form();
		if (!active || active.doctype !== "PM Request" || !active.doc?.name) {
			return;
		}
		if (data.pm_request !== active.doc.name) {
			return;
		}
		if (data.response_version_id) {
			active._pm_pe_response_version = String(data.response_version_id);
		}
		active.trigger("refresh_payment_entry_list");
		if (active.dashboard) {
			active.dashboard._fetched_counts = false;
			active.dashboard.set_open_count();
		}
		schedule_pm_request_toolbar(active);
	});
}

function schedule_pm_request_toolbar(frm) {
	if (frm.is_new() || !frm.doc.name) {
		return;
	}
	clearTimeout(frm._pm_toolbar_debounce);
	const run = () => frm.trigger("setup_pm_request_toolbar");
	frm._pm_toolbar_debounce = setTimeout(run, 120);
	$(frm.wrapper)
		.off("render_complete.pm_request_toolbar")
		.one("render_complete.pm_request_toolbar", () => {
			clearTimeout(frm._pm_toolbar_debounce);
			frm._pm_toolbar_debounce = setTimeout(run, 0);
		});
}

const PM_REQUEST_EXPAND_SECTIONS = [
	"section_main",
	"section_amounts",
	"section_payment_entries",
	"section_details",
	"section_close",
	"section_remark",
];

function expand_pm_request_main_sections(frm) {
	if (!frm.layout || !frm.layout.sections) {
		return;
	}
	setTimeout(() => {
		PM_REQUEST_EXPAND_SECTIONS.forEach((fieldname) => {
			const section = frm.layout.sections.find((s) => s.df?.fieldname === fieldname);
			if (section && !section.wrapper.hasClass("hide-control")) {
				section.collapse(false);
			}
		});
	}, 0);
}

function apply_pm_request_action_ui(frm, f) {
	frm._pm_action_flags = f || {};
	remove_pm_request_toolbar_buttons(frm);
	apply_pm_request_toolbar(frm, f);
	suppress_generic_frappe_cancel_button(frm);
	suppress_generic_frappe_delete_button(frm);
	apply_pm_request_intro(frm, f);
	// Rebuild workflow Actions with can_reject filter (survives clear_actions_menu races).
	if (erpnext_extensions?.petty_management?.refresh_workflow_actions) {
		erpnext_extensions.petty_management.refresh_workflow_actions(frm);
	}
}

/** Re-apply custom buttons from cached flags (called after workflow Actions rebuild). */
window.pm_request_reapply_custom_toolbar = function (frm) {
	if (!frm || frm.doctype !== "PM Request" || !frm._pm_action_flags) {
		return;
	}
	remove_pm_request_toolbar_buttons(frm);
	apply_pm_request_toolbar(frm, frm._pm_action_flags);
	apply_pm_request_page_actions(frm, frm._pm_action_flags);
};

function apply_pm_request_pe_list_payload(frm, $wrapper, payload, currency) {
	const incoming = cint(payload.response_version_id || 0);
	const applied = cint(frm._pm_pe_list_applied_version || 0);
	if (incoming < applied) {
		return;
	}
	frm._pm_pe_list_applied_version = incoming;
	frm._pm_pe_response_version = String(payload.response_version_id || incoming);
	const rows = Array.isArray(payload.payment_entries) ? payload.payment_entries : [];
	render_pm_request_payment_entry_table($wrapper, rows, currency);
}

function apply_pm_request_toolbar(frm, f) {
	const runCreate = (paidAmount) => {
		const args = { pm_request: frm.doc.name };
		if (paidAmount != null && paidAmount !== "") {
			args.paid_amount = paidAmount;
		}
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.create_payment_entry",
			args,
			freeze: true,
			freeze_message: __("Creating Payment Entry…"),
			callback(r) {
				if (r.exc) {
					return;
				}
				frappe.show_alert({ message: __("Payment Entry created"), indicator: "green" });
				const msg = r.message || {};
				if (msg.response_version_id) {
					frm._pm_pe_response_version = String(msg.response_version_id);
					frm._pm_pe_list_applied_version = Math.max(
						cint(frm._pm_pe_list_applied_version || 0),
						cint(msg.response_version_id) - 1
					);
				}
				// Deterministic desk sync after whitelisted create (server also publishes realtime).
				frm.trigger("refresh_payment_entry_list");
				schedule_pm_request_toolbar(frm);
			},
			error(r) {
				frappe.msgprint({
					title: __("Payment Entry failed"),
					message: parse_pm_request_server_error(r),
					indicator: "red",
				});
			},
		});
	};

	const promptCreatePe = () => {
		const remaining = flt(f.remaining_to_pay);
		const defaultAmt = remaining > 0 ? remaining : flt(frm.doc.total_requested_amount);
		frappe.prompt(
			[
				{
					fieldname: "paid_amount",
					fieldtype: "Currency",
					label: __("Payment Amount"),
					default: defaultAmt,
					reqd: 1,
				},
			],
			(values) => runCreate(values.paid_amount),
			__("Create Payment Entry"),
			__("Create")
		);
	};

	const runClose = () => {
		const remaining = flt(f.remaining_to_pay);
		const fields = [];
		if (remaining > 0) {
			fields.push({
				fieldname: "close_reason",
				fieldtype: "Select",
				label: __("Close Reason"),
				options: [
					"Budget Limitation",
					"Partial Approval",
					"Cancelled by Requester",
					"Other",
				].join("\n"),
				reqd: 1,
			});
			fields.push({
				fieldname: "close_reason_detail",
				fieldtype: "Small Text",
				label: __("Close Reason Detail"),
				depends_on: "eval:doc.close_reason=='Other'",
			});
		} else {
			fields.push({
				fieldname: "close_reason",
				fieldtype: "Select",
				label: __("Close Reason (optional)"),
				options: [
					"",
					"Budget Limitation",
					"Partial Approval",
					"Cancelled by Requester",
					"Other",
				].join("\n"),
			});
			fields.push({
				fieldname: "close_reason_detail",
				fieldtype: "Small Text",
				label: __("Close Reason Detail"),
				depends_on: "eval:doc.close_reason=='Other'",
			});
		}
		frappe.prompt(
			fields,
			(values) => {
				frappe.call({
					method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.close_pm_request",
					args: {
						pm_request: frm.doc.name,
						close_reason: values.close_reason || null,
						close_reason_detail: values.close_reason_detail || null,
					},
					freeze: true,
					callback(r) {
						if (r.exc) {
							return;
						}
						frappe.show_alert({ message: __("PM Request closed"), indicator: "blue" });
						frm.reload_doc();
					},
				});
			},
			__("Close PM Request"),
			__("Close")
		);
	};

	// Gate funding actions on server business flags (finance-cleared), not workflow title literals.
	// Cancel/Delete use frm.page.add_action_item via pm_request_reapply_custom_toolbar (standard Actions menu).
	if (!cint(f.is_closed)) {
		if (f.can_create_payment_entry) {
			add_pm_request_toolbar_button(frm, __("Create Payment Entry"), promptCreatePe);
			if (frm.change_custom_button_type) {
				frm.change_custom_button_type(__("Create Payment Entry"), null, "primary");
			}
		}
		if (f.can_close_pm_request) {
			add_pm_request_toolbar_button(frm, __("Close PM Request"), runClose);
		}
	}
}

function apply_pm_request_page_actions(frm, f) {
	if (!frm.page || typeof frm.page.add_action_item !== "function") {
		return;
	}
	if (f.can_cancel_pm_request) {
		frm.page.add_action_item(__("Cancel PM Request"), () => runCancelPmRequest(frm));
	}
	if (f.can_delete_pm_request) {
		frm.page.add_action_item(__("Delete PM Request"), () => runDeletePmRequest(frm));
	}
}

function runCancelPmRequest(frm) {
	frappe.confirm(__("Cancel this PM Request?"), () => {
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.cancel_pm_request",
			args: { pm_request: frm.doc.name },
			freeze: true,
			freeze_message: __("Cancelling PM Request…"),
			callback(r) {
				if (r.exc) {
					return;
				}
				frappe.show_alert({ message: __("PM Request cancelled"), indicator: "orange" });
				frm.reload_doc();
			},
			error(r) {
				frappe.msgprint({
					title: __("Cancel failed"),
					message: parse_pm_request_server_error(r),
					indicator: "red",
				});
			},
		});
	});
}

function runDeletePmRequest(frm) {
	frappe.confirm(__("Delete this PM Request permanently?"), () => {
		frappe.call({
			method: "erpnext_extensions.petty_management.doctype.pm_request.pm_request.delete_pm_request",
			args: { pm_request: frm.doc.name },
			freeze: true,
			freeze_message: __("Deleting PM Request…"),
			callback(r) {
				if (r.exc) {
					return;
				}
				frappe.show_alert({ message: __("PM Request deleted"), indicator: "green" });
				frappe.set_route("List", "PM Request");
			},
			error(r) {
				frappe.msgprint({
					title: __("Delete failed"),
					message: parse_pm_request_server_error(r),
					indicator: "red",
				});
			},
		});
	});
}

function suppress_generic_frappe_cancel_button(frm) {
	// v4.8.5: business Cancel PM Request replaces Frappe Cancel (DocPerm-independent).
	if (!frm.page) {
		return;
	}
	if (frm.page.btn_secondary && frm.page.btn_secondary.length) {
		const label = (frm.page.btn_secondary.text() || "").trim();
		if (/^Cancel$/i.test(label)) {
			frm.page.clear_secondary_action();
		}
	}
	if (frm.page.menu) {
		frm.page.menu.find('a[data-label="Cancel"]').parent().hide();
	}
	if (frm.page.menu_btn_group) {
		frm.page.menu_btn_group.find('a[data-label="Cancel"]').parent().hide();
	}
}

function suppress_generic_frappe_delete_button(frm) {
	// v4.8.6: business Delete PM Request replaces Frappe Delete (DocPerm-independent).
	if (!frm.page || !frm.page.menu) {
		return;
	}
	frm.page.menu.find('a[data-label="Delete"]').parent().hide();
	if (frm.page.menu_btn_group) {
		frm.page.menu_btn_group.find('a[data-label="Delete"]').parent().hide();
	}
}

function unique_pm_ui_messages(messages) {
	const seen = new Set();
	const out = [];
	(messages || []).forEach((raw) => {
		const text = (raw || "").toString().trim();
		if (!text || seen.has(text)) {
			return;
		}
		seen.add(text);
		out.push(text);
	});
	return out;
}

function apply_pm_request_intro(frm, f) {
	// Clear any prior dashboard headline workaround (Option A: Status field is lifecycle SoT).
	if (frm.dashboard && typeof frm.dashboard.clear_headline === "function") {
		frm.dashboard.clear_headline();
	}
	frm.set_intro("");
	frm._pm_intro_applied_text = "";

	const messages = unique_pm_ui_messages(f.ui_messages);
	if (!messages.length) {
		return;
	}
	const text = messages.join(" ");
	frm._pm_intro_applied_text = text;
	const color = (f.business_status_indicator || (cint(f.is_closed) ? "blue" : "orange")).toString();
	frm.set_intro(text, color);
}

function route_pm_request_payment_entries(frm, flags) {
	const filters = (flags && flags.payment_entry_list_filters) || { reference_no: frm.doc.name };
	frappe.route_options = filters;
	frappe.set_route("List", "Payment Entry");
}

function render_pm_request_payment_entry_table($wrapper, rows, currency, opts) {
	opts = opts || {};
	const esc = frappe.utils.escape_html;
	let html = `<div id="pm-request-pe-list" class="pm-request-pe-list">`;
	if (opts.failed) {
		html += `<p class="text-muted small">${__(
			"Payment Entry list could not be loaded. Refresh the page to try again."
		)}</p>`;
	}
	html += `<table class="table table-bordered table-sm" style="margin-top:0">`;
	html += `<thead><tr>`;
	html += `<th>${__("Payment Entry")}</th>`;
	html += `<th>${__("Amount")}</th>`;
	html += `<th>${__("Status")}</th>`;
	html += `<th>${__("Posting Date")}</th>`;
	html += `</tr></thead><tbody>`;
	if (!rows.length) {
		html += `<tr><td colspan="4" class="text-muted">${__(
			"No Payment Entries linked yet."
		)}</td></tr>`;
	} else {
		rows.forEach((row) => {
			const link = `<a href="/app/payment-entry/${encodeURIComponent(
				row.payment_entry
			)}">${esc(row.payment_entry)}</a>`;
			html += `<tr data-pe-status="${esc(row.status || "")}">`;
			html += `<td>${link}</td>`;
			html += `<td>${format_currency(row.amount, currency)}</td>`;
			html += `<td>${esc(row.status || "")}</td>`;
			html += `<td>${esc(row.posting_date || "")}</td>`;
			html += `</tr>`;
		});
	}
	html += `</tbody></table></div>`;
	$wrapper.html(html);
}

function log_pm_pe_list_error(err) {
	try {
		if (typeof console !== "undefined" && console.debug) {
			console.debug("[PM Request] payment entry list fetch failed", err);
		}
	} catch (e) {
		/* ignore */
	}
}

function format_currency(amount, currency) {
	if (typeof frappe.format === "function") {
		return frappe.format(amount, { fieldtype: "Currency", options: currency });
	}
	return flt(amount);
}

function add_pm_request_toolbar_button(frm, label, fn) {
	frm.add_custom_button(label, fn);
}

function remove_pm_request_toolbar_buttons(frm) {
	const labels = [
		"Create Payment Entry",
		"Open Payment Entry",
		"View Payment Entries",
		"Close PM Request",
		"Cancel PM Request",
		"Delete PM Request",
	];
	labels.forEach((raw) => {
		const L = __(raw);
		frm.remove_custom_button(L);
		if (frm.page) {
			frm.page.remove_inner_button(L);
			if (frm.page.actions) {
				const enc = encodeURIComponent(L);
				frm.page.actions.find(`a.dropdown-item[data-label="${enc}"]`).remove();
			}
		}
	});
}

frappe.ui.form.on("PM Request Detail", {
	advance_amount(frm) {
		frm.trigger("recalc_totals");
	},
});

function parse_pm_request_server_error(r) {
	if (r && r.message && typeof r.message === "string") {
		return r.message;
	}
	if (r && r.exc && typeof r.exc === "string") {
		const lockMatch = r.exc.match(/Lock wait timeout|QueryTimeoutError/i);
		if (lockMatch) {
			return __(
				"This PM Request is currently being processed. Please refresh and try again."
			);
		}
	}
	if (r && r._server_messages) {
		try {
			const raw = frappe.utils.parse_json(r._server_messages);
			const list = Array.isArray(raw) ? raw : [raw];
			const parts = list
				.map((item) => {
					const row = typeof item === "string" ? frappe.utils.parse_json(item) : item;
					return (row && row.message) || "";
				})
				.filter(Boolean);
			if (parts.length) {
				return parts.join("\n");
			}
		} catch (e) {
			/* use fallback */
		}
	}
	return __("Could not complete the request.");
}

function flt(v) {
	const parsed = parseFloat(v);
	return Number.isFinite(parsed) ? parsed : 0;
}

function cint(v) {
	const parsed = parseInt(v, 10);
	return Number.isFinite(parsed) ? parsed : 0;
}

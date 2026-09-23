// Copyright (c) 2026, Farbod Siyahpoosh and contributors
// Settlement visibility (Payment Entry vs Post Dated Cheque) — Step 3 + Step 5 UX guidance, cheque_management.

frappe.provide("erpnext_extensions.cheque_management");
frappe.provide("erpnext_extensions.pr_workflow");

/** Frappe's ``frappe.model.has_workflow`` only sees Workflow rows in ``locals``; desk boot tells us the truth. */
(function () {
	if (erpnext_extensions.pr_workflow._has_workflow_patched) {
		return;
	}
	const boot = typeof frappe !== "undefined" && frappe.boot ? frappe.boot : {};
	if (!boot.pdc_payment_request_has_active_workflow) {
		return;
	}
	erpnext_extensions.pr_workflow._has_workflow_patched = true;
	const orig = frappe.model.has_workflow.bind(frappe.model);
	frappe.model.has_workflow = function (doctype) {
		if (doctype === "Payment Request") {
			return true;
		}
		return orig(doctype);
	};
})();

const PDC_SETTLEMENT_DOCTYPES = ["Sales Invoice", "Purchase Invoice", "Payment Request"];

/** Same epsilon as server-side capacity checks (see ``pdc_settlement_capacity``). */
const PDC_SETTLEMENT_EPS = 1e-6;

function pdc_flt(value, fallback = 0) {
	// Frappe desk builds vary; `frappe.utils.flt` is not always present.
	try {
		if (frappe && frappe.utils && typeof frappe.utils.flt === "function") {
			return frappe.utils.flt(value);
		}
	} catch (e) {
		// ignore
	}
	const v = parseFloat(value);
	return Number.isFinite(v) ? v : fallback;
}

function pdc_cint(value, fallback = 0) {
	// Frappe desk builds vary; `frappe.utils.cint` is not always present.
	try {
		if (frappe && frappe.utils && typeof frappe.utils.cint === "function") {
			return frappe.utils.cint(value);
		}
	} catch (e) {
		// ignore
	}
	const v = parseInt(value, 10);
	return Number.isFinite(v) ? v : fallback;
}

function pdc_debug_enabled() {
	try {
		return !!(
			window &&
			window.localStorage &&
			window.localStorage.getItem("pdc_debug") === "1"
		);
	} catch (e) {
		return false;
	}
}

function pdc_debug_log(...args) {
	if (!pdc_debug_enabled()) return;
	// eslint-disable-next-line no-console
	console.log(...args);
}

/** Infer eligibility when the summary flag is missing (older payloads / serialization edge cases). Mirrors server rules. */
function pdc_infer_payment_request_settlement_eligible(frm) {
	if (!frm || frm.doctype !== "Payment Request" || !frm.doc) {
		return false;
	}
	try {
		const boot = (typeof frappe !== "undefined" && frappe.boot) || {};
		const states = boot.pdc_pr_settlement_eligible_workflow_states;
		const hasWsField =
			frappe.meta &&
			frappe.meta.has_field &&
			frappe.meta.has_field("Payment Request", "workflow_state");
		if (hasWsField && Array.isArray(states) && states.length) {
			const ws = (frm.doc.workflow_state || "").trim();
			return Boolean(ws && states.includes(ws));
		}
	} catch (e) {
		// ignore
	}
	return pdc_cint(frm.doc.docstatus) === 1;
}

/**
 * Payment Request: settlement/PDC UI follows server ``payment_request_settlement_eligible`` when present.
 * Falls back to boot workflow-state list + ``workflow_state`` on the form, then ``docstatus == 1``.
 */
function pdc_payment_request_settlement_eligible(frm) {
	if (!frm || frm.doctype !== "Payment Request") {
		return pdc_cint(frm && frm.doc && frm.doc.docstatus) === 1;
	}
	const s = frm._pdc_settlement_summary;
	if (s && Object.prototype.hasOwnProperty.call(s, "payment_request_settlement_eligible")) {
		const raw = s.payment_request_settlement_eligible;
		if (typeof raw === "boolean") {
			return raw;
		}
		if (raw === 1 || raw === "1" || raw === "true") {
			return true;
		}
		if (raw === 0 || raw === "0" || raw === "false") {
			return false;
		}
	}
	return pdc_infer_payment_request_settlement_eligible(frm);
}

function pdc_settlement_format_money(value, currency) {
	const v = pdc_flt(value);
	return frappe.format(v, { fieldtype: "Currency", options: currency || undefined });
}

function pdc_settlement_fully_covered_html() {
	return `<div class="alert alert-success border mt-2 mb-0 p-2 small pdc-settlement-fully-covered">
		<strong>${__("Fully covered")}</strong>. ${__(
		"There is no remaining settlement capacity on this document. Payment Entry and effective Post Dated Cheque allocations use the available amount (see table above)."
	)}
	</div>`;
}

function pdc_payment_request_neutral_path_guidance_html() {
	// Keep HTML markup outside __() so translations cannot leave literal <b>/<div> tags as text,
	// and so frappe.utils.is_html / .html() always receive real markup.
	const deferred = `${__("Use")} <b>${frappe.utils.escape_html(
		__("Create Post Dated Cheque")
	)}</b> ${__("for deferred settlement by cheque.")}`;
	const immediate = `${__("Use")} <b>${frappe.utils.escape_html(__("Create"))}</b> → <b>${frappe.utils.escape_html(
		__("Payment")
	)}</b> ${__("for an immediate bank payment (Payment Entry).")}`;
	return `<div class="alert alert-light border mt-2 mb-0 p-2 small pdc-path-guidance">
		<div class="mb-1"><strong>${frappe.utils.escape_html(__("Settlement options"))}</strong></div>
		<div>${deferred}</div>
		<div class="mt-1">${immediate}</div>
	</div>`;
}

function pdc_settlement_path_guidance_html(is_pdc_mode) {
	const deferred = `${__("Use")} <b>${frappe.utils.escape_html(
		__("Create Post Dated Cheque")
	)}</b> ${__("for deferred settlement by cheque.")}`;
	const immediate = `${__("Use")} <b>${frappe.utils.escape_html(__("Create"))}</b> → <b>${frappe.utils.escape_html(
		__("Payment")
	)}</b> ${__("to record an immediate bank payment (Payment Entry).")}`;
	if (is_pdc_mode === true) {
		return `<div class="alert alert-light border mt-2 mb-0 p-2 small pdc-path-guidance">
			<div class="mb-1"><strong>${frappe.utils.escape_html(
				__("Recommended (this Mode of Payment)")
			)}</strong>: ${deferred}</div>
			<div><strong>${frappe.utils.escape_html(__("Immediate payment (exception)"))}</strong>: ${immediate}</div>
		</div>`;
	}
	if (is_pdc_mode === false) {
		return `<div class="alert alert-light border mt-2 mb-0 p-2 small pdc-path-guidance">
			<div class="mb-1"><strong>${frappe.utils.escape_html(__("Recommended"))}</strong>: ${immediate}</div>
			<div><strong>${frappe.utils.escape_html(__("Deferred cheque"))}</strong>: ${deferred}</div>
		</div>`;
	}
	return `<div class="alert alert-light border mt-2 mb-0 p-2 small pdc-path-guidance">
		<div class="mb-1"><strong>${frappe.utils.escape_html(__("Immediate payment"))}</strong>: ${immediate}</div>
		<div><strong>${frappe.utils.escape_html(__("Deferred cheque"))}</strong>: ${deferred}</div>
	</div>`;
}

function pdc_settlement_capacity_note_html(remaining_balance) {
	const rem = pdc_flt(remaining_balance);
	if (rem > PDC_SETTLEMENT_EPS) {
		return "";
	}
	return `<div class="alert alert-warning mt-2 mb-0 p-2 small">
		<strong>${__("No remaining settlement capacity")}</strong>. ${__(
		"Further allocation against this document may be blocked by validation. Use this only if you are correcting data or your process allows it."
	)}
	</div>`;
}

function pdc_settlement_render_dashboard(frm, data, path_options) {
	if (!frm.dashboard || !data || !Object.keys(data).length) {
		return;
	}
	// Async races / repeated refresh can stack identical dashboard sections — remove ours before re-adding.
	if (frm.dashboard.parent && frm.dashboard.parent.length) {
		frm.dashboard.parent.find(".form-dashboard-section.custom").each(function () {
			const $sec = $(this);
			if ($sec.find(".pdc-settlement-summary").length) {
				$sec.remove();
			}
		});
	}
	path_options = path_options || {};
	const cur = data.currency || frm.doc.currency;
	const rows = [
		[__("Paid by Payment Entry"), pdc_settlement_format_money(data.payment_entry_amount, cur)],
		[
			__("Covered by Direct Post Dated Cheque"),
			pdc_settlement_format_money(data.effective_pdc_amount, cur),
		],
		[
			__("Applied from PDC Advances"),
			pdc_settlement_format_money(data.pdc_advance_applied_amount, cur),
		],
		[__("Remaining balance"), pdc_settlement_format_money(data.remaining_balance, cur)],
	];
	const inner = rows
		.map(
			([label, amt]) =>
				`<tr><td class="text-muted" style="width:55%">${frappe.utils.escape_html(
					label
				)}</td><td class="text-right"><strong>${amt}</strong></td></tr>`
		)
		.join("");
	const basis = pdc_settlement_format_money(data.financial_basis_amount, cur);
	const rem = pdc_flt(data.remaining_balance);
	// Never show "Fully covered" until the document is settlement-eligible (PR: workflow-aware).
	const fully_settled =
		frm.doctype === "Payment Request"
			? pdc_payment_request_settlement_eligible(frm) && rem <= PDC_SETTLEMENT_EPS
			: pdc_cint(frm.doc && frm.doc.docstatus) === 1 && rem <= PDC_SETTLEMENT_EPS;
	const path_html = fully_settled
		? pdc_settlement_fully_covered_html()
		: frm.doctype === "Payment Request"
		? pdc_payment_request_neutral_path_guidance_html()
		: pdc_settlement_path_guidance_html(null);
	const cap_html = fully_settled
		? ""
		: pdc_settlement_capacity_note_html(data.remaining_balance);
	const html = `<div class="small pdc-settlement-summary">
		<p class="text-muted mb-2" style="margin-bottom:8px">${__(
			"Document total"
		)}: <strong>${basis}</strong></p>
		<table class="table table-bordered" style="margin-bottom:0"><tbody>${inner}</tbody></table>
		${path_html}
		${cap_html}
		<p class="text-muted mt-2" style="margin-top:8px;font-size:11px">${frappe.utils.escape_html(
			`${__(
				"Remaining balance is what is left after Post Dated Cheque coverage. Unpaid amount on this document in ERPNext:"
			)} ${pdc_settlement_format_money(data.document_outstanding, cur)}`
		)}</p>
	</div>`;
	frm.dashboard.add_section(html, __("Settlement (Cheque & Payment)"), "custom");
	frm.dashboard.show();
}

/**
 * When ``remaining_balance`` from :func:`get_pdc_settlement_summary` is ~0, Payment Entry + effective PDC
 * have no room for new claims. ERPNext still shows **Create → Payment** while ``outstanding_amount`` is
 * non-zero because PDC does not reduce ERPNext outstanding — remove misleading settlement actions.
 *
 * Does not remove non-settlement **Create** actions (e.g. Return / Credit Note, Delivery Note).
 */
function pdc_apply_settlement_capacity_action_gates(frm) {
	const CREATE = __("Create");

	const clear_si_pi_intro = () => {
		if (frm._pdc_fully_settled_intro) {
			frm.set_intro("");
			frm._pdc_fully_settled_intro = false;
		}
	};

	const clear_pr_intro = () => {
		if (frm._pdc_pr_fully_settled_intro) {
			if (frm.doc.status !== "Failed") {
				frm.set_intro("");
			}
			frm._pdc_pr_fully_settled_intro = false;
		}
	};

	if (!frm._pdc_settlement_ready || !frm._pdc_settlement_summary) {
		clear_si_pi_intro();
		clear_pr_intro();
		return;
	}
	if (frm.doctype === "Payment Request") {
		if (!pdc_payment_request_settlement_eligible(frm)) {
			clear_si_pi_intro();
			clear_pr_intro();
			return;
		}
	} else if (pdc_cint(frm.doc.docstatus) !== 1) {
		clear_si_pi_intro();
		clear_pr_intro();
		return;
	}

	const rem = pdc_flt(frm._pdc_settlement_summary.remaining_balance);
	if (rem > PDC_SETTLEMENT_EPS) {
		clear_si_pi_intro();
		clear_pr_intro();
		return;
	}

	// Payment Request: MoP recommendation headline becomes misleading once capacity is zero.
	if (frm.doctype === "Payment Request") {
		frm.dashboard.clear_headline();
	}

	// ERPNext settlement entry points (labels match ERPNext v15 SI / PI / PR client scripts).
	frm.remove_custom_button(__("Payment"), CREATE);
	frm.remove_custom_button(__("Create Payment Entry"));
	// Payment Request is an orchestration doc, not a settlement action: do not gate it on SI / PI.
	if (frm.doctype === "Payment Request") {
		frm.remove_custom_button(__("Payment Request"), CREATE);
	}

	// This app — same labels as ``pdc_create_from_source.js`` / ``payment_request.js``.
	const PDC_MENU = __("Post Dated Cheque");
	const PDC_LEGACY = __("Create Post Dated Cheque");
	// Payment Request uses both a Create-menu entry and an optional top-level PDC button.
	// Remove both (translated + raw) to avoid label/translation mismatches across builds.
	const PDC_MENU_RAW = "Post Dated Cheque";
	const PDC_PRIMARY_RAW = "Create Post Dated Cheque";
	frm.remove_custom_button(PDC_MENU, CREATE);
	frm.remove_custom_button(PDC_LEGACY, CREATE);
	frm.remove_custom_button(PDC_MENU);
	frm.remove_custom_button(PDC_LEGACY);
	frm.remove_custom_button(PDC_MENU_RAW, CREATE);
	frm.remove_custom_button(PDC_PRIMARY_RAW, CREATE);
	frm.remove_custom_button(PDC_MENU_RAW);
	frm.remove_custom_button(PDC_PRIMARY_RAW);

	if (frm.doctype === "Sales Invoice" || frm.doctype === "Purchase Invoice") {
		frm.set_intro(
			__(
				"No remaining settlement capacity: submitted Payment Entry and effective Post Dated Cheque allocations fully cover the amount available for new settlement (see Settlement section)."
			),
			"green"
		);
		frm._pdc_fully_settled_intro = true;
	} else if (frm.doctype === "Payment Request" && frm.doc.status !== "Failed") {
		// UI-only “paid-like” signal: do not mutate ERPNext **status** (would be unsafe vs gateways / accounting).
		frm.set_intro(
			__(
				"Fully covered for settlement: submitted Payment Entry and effective Post Dated Cheque allocations leave no remaining capacity (see Settlement section). Further payment or PDC actions are hidden."
			),
			"green"
		);
		frm._pdc_pr_fully_settled_intro = true;
	}
}

/**
 * Payment Request: **Create Post Dated Cheque** when settlement-eligible (workflow / docstatus rules).
 * Visibility does **not** depend on Mode of Payment — only on eligibility and remaining capacity.
 * Must run **after** :func:`pdc_apply_settlement_capacity_action_gates` when ``remaining_balance > 0``.
 */
function pdc_render_payment_request_path_ui(frm) {
	const PRIMARY = __("Create Post Dated Cheque");
	const MENU = __("Post Dated Cheque");
	const CREATE = __("Create");

	const strip_pr_pdc = () => {
		frm.remove_custom_button(MENU, CREATE);
		frm.remove_custom_button(PRIMARY, CREATE);
		frm.remove_custom_button(MENU);
		frm.remove_custom_button(PRIMARY);
	};

	strip_pr_pdc();
	frm.dashboard.clear_headline();

	if (
		frm.is_new() ||
		!frm.doc ||
		!frm.doc.name ||
		!pdc_payment_request_settlement_eligible(frm)
	) {
		pdc_debug_log("[PDC][PR] skip: not settlement-eligible", {
			name: frm.doc && frm.doc.name,
			inferred: pdc_infer_payment_request_settlement_eligible(frm),
			summary_flag:
				frm._pdc_settlement_summary &&
				frm._pdc_settlement_summary.payment_request_settlement_eligible,
		});
		return;
	}
	if (!frm._pdc_settlement_ready || !frm._pdc_settlement_summary) {
		pdc_debug_log("[PDC][PR] skip: settlement not ready", {
			ready: frm._pdc_settlement_ready,
			summary: frm._pdc_settlement_summary,
		});
		return;
	}
	const rem = pdc_flt(frm._pdc_settlement_summary.remaining_balance);
	if (rem <= PDC_SETTLEMENT_EPS) {
		pdc_debug_log("[PDC][PR] skip: remaining<=eps", { rem, eps: PDC_SETTLEMENT_EPS });
		return;
	}

	pdc_debug_log("[PDC][PR] render", {
		name: frm.doc.name,
		rem,
	});

	const headline = `<div class="small"><b>${frappe.utils.escape_html(
		__("Create Post Dated Cheque")
	)}</b> ${frappe.utils.escape_html(
		__(
			"is available when this Payment Request is settlement-eligible (independent of Mode of Payment). For immediate bank payment use"
		)
	)} <b>${frappe.utils.escape_html(__("Create"))}</b> → <b>${frappe.utils.escape_html(
		__("Payment")
	)}</b>.</div>`;
	frm.dashboard.set_headline(headline, "blue", true);

	const open_pdc = () => erpnext_extensions.cheque_management.open_pdc_from_form(frm);
	// Leave ERPNext **Create Payment Entry** as ``btn-primary``; PDC is a second action (default style).
	frm.add_custom_button(PRIMARY, open_pdc);
}

function pdc_settlement_load(frm) {
	if (frm.is_new() || !frm.doc.name) {
		return;
	}
	// Defensive: never let our async logic break ERPNext's toolbar rendering.
	if (!frm || !frm.doc) {
		return;
	}
	const after_summary = (summary) => {
		try {
			frm._pdc_settlement_summary = summary && summary.reference_name ? summary : null;
			frm._pdc_settlement_ready = !!frm._pdc_settlement_summary;
			if (frm._pdc_settlement_summary) {
				pdc_settlement_render_dashboard(frm, frm._pdc_settlement_summary, {});
			}
			pdc_apply_settlement_capacity_action_gates(frm);
			if (frm.doctype === "Payment Request") {
				pdc_debug_log("[PDC][PR] after_summary", {
					name: frm.doc && frm.doc.name,
					remaining_balance:
						frm._pdc_settlement_summary &&
						frm._pdc_settlement_summary.remaining_balance,
				});
				pdc_render_payment_request_path_ui(frm);
				// ERPNext rebuilds inner toolbar after refresh / workflow load; re-apply PDC (PE styling unchanged).
				[200, 500, 1200].forEach((ms) => {
					setTimeout(() => {
						try {
							pdc_render_payment_request_path_ui(frm);
						} catch (e2) {
							// eslint-disable-next-line no-console
							console.error(
								"[erpnext_extensions] PDC PR button re-render failed",
								e2
							);
						}
					}, ms);
				});
			}
			if (
				(frm.doctype === "Sales Invoice" || frm.doctype === "Purchase Invoice") &&
				typeof erpnext_extensions.cheque_management.ensure_si_pi_pdc_create_button ===
					"function"
			) {
				erpnext_extensions.cheque_management.ensure_si_pi_pdc_create_button(frm);
			}
		} catch (e) {
			// Intentionally swallow to avoid breaking standard toolbar/actions.
			// eslint-disable-next-line no-console
			console.error("[erpnext_extensions] PDC settlement UI failed", e);
		}
	};
	if (frm.doctype === "Payment Request") {
		frappe.call({
			method: "erpnext_extensions.cheque_management.pdc_settlement_summary.get_pdc_settlement_summary",
			args: {
				reference_doctype: frm.doctype,
				reference_name: frm.doc.name,
			},
			callback: (r) => {
				if (r.exc) {
					return;
				}
				after_summary(r.message || {});
			},
		});
		return;
	}
	frappe.call({
		method: "erpnext_extensions.cheque_management.pdc_settlement_summary.get_pdc_settlement_summary",
		args: {
			reference_doctype: frm.doctype,
			reference_name: frm.doc.name,
		},
		callback: (r) => {
			if (r.exc) {
				return;
			}
			after_summary(r.message || {}, {});
		},
	});
}

PDC_SETTLEMENT_DOCTYPES.forEach((dt) => {
	frappe.ui.form.on(dt, {
		refresh(frm) {
			// Defer to ensure ERPNext has built the standard Create menu/buttons first.
			setTimeout(() => pdc_settlement_load(frm), 0);
		},
	});
	if (dt === "Payment Request") {
		frappe.ui.form.on("Payment Request", {
			company(frm) {
				setTimeout(() => pdc_settlement_load(frm), 0);
			},
			workflow_state(frm) {
				setTimeout(() => pdc_settlement_load(frm), 0);
			},
		});
	}
});

erpnext_extensions.cheque_management.apply_settlement_capacity_action_gates =
	pdc_apply_settlement_capacity_action_gates;

/** Called after ``frm.toolbar.set_primary_action()`` / workflow toolbar rebuilds (see ``payment_request.js``). */
erpnext_extensions.cheque_management.pdc_pr_after_toolbar_refresh = function (frm) {
	if (!frm || frm.doctype !== "Payment Request" || frm.is_new()) {
		return;
	}
	if (!frm._pdc_settlement_ready || !frm._pdc_settlement_summary) {
		return;
	}
	try {
		pdc_render_payment_request_path_ui(frm);
	} catch (e) {
		// eslint-disable-next-line no-console
		console.error("[erpnext_extensions] pdc_pr_after_toolbar_refresh failed", e);
	}
};

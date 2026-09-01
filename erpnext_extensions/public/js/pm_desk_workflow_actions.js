// Copyright (c) 2026, Farbod Siyahpoosh and contributors
// For license information, please see license.txt
//
// Shared Desk helpers for Petty Management forms that use Frappe Workflow.
// Custom funding/settlement buttons must NOT use page.add_action_item — that menu is
// cleared by frappe/public/js/frappe/form/workflow.js show_actions().

frappe.provide("erpnext_extensions.petty_management");

/**
 * Install once: filter workflow Actions menu using frm._pm_action_flags.can_reject.
 * Replaces fragile setTimeout DOM scrubbing after clear_actions_menu races.
 */
erpnext_extensions.petty_management.install_workflow_reject_filter = function () {
	if (frappe.ui.form._pm_workflow_reject_filter_installed) {
		return;
	}
	if (!frappe.ui.form.States || !frappe.ui.form.States.prototype) {
		return;
	}
	frappe.ui.form._pm_workflow_reject_filter_installed = true;

	const orig_show_actions = frappe.ui.form.States.prototype.show_actions;
	frappe.ui.form.States.prototype.show_actions = function () {
		const frm = this.frm;
		const is_pm =
			frm && (frm.doctype === "PM Request" || frm.doctype === "PM Clearance");
		if (!is_pm) {
			return orig_show_actions.apply(this, arguments);
		}

		if (frm.doc.__islocal) {
			this.set_default_state();
			return;
		}
		if (frm.doc.__unsaved === 1) {
			return;
		}

		const me = this;
		frappe.workflow.get_transitions(frm.doc).then((transitions) => {
			frm.page.clear_actions_menu();
			let added = false;
			const flags = frm._pm_action_flags;
			const block_reject = flags && flags.can_reject === false;

			const has_approval_access = (transition) => {
				const user = frappe.session.user;
				return (
					user === "Administrator" ||
					transition.allow_self_approval ||
					user !== frm.doc.owner
				);
			};

			(transitions || []).forEach((d) => {
				if (block_reject && d.action === "PM Reject") {
					return;
				}
				if (frappe.user_roles.includes(d.allowed) && has_approval_access(d)) {
					added = true;
					frm.page.add_action_item(__(d.action), function () {
						if (
							frappe.workflow?.workflows?.[frm.doctype]?.enable_action_confirmation
						) {
							frappe.confirm(__("Are you sure you want to {0}?", [d.action]), () =>
								me.handle_workflow_action(d)
							);
						} else {
							me.handle_workflow_action(d);
						}
					});
				}
			});

			me.setup_btn(added);

			// Cancel/Delete use page.add_action_item in pm_request_reapply_custom_toolbar (standard Actions menu).
			// but form refresh can clear them around the same time as workflow rebuild.
			// Re-stamp from cached flags whenever workflow Actions are rendered.
			if (
				frm.doctype === "PM Request" &&
				typeof window.pm_request_reapply_custom_toolbar === "function"
			) {
				window.pm_request_reapply_custom_toolbar(frm);
			}
			if (
				frm.doctype === "PM Clearance" &&
				typeof window.pm_clearance_reapply_custom_toolbar === "function"
			) {
				window.pm_clearance_reapply_custom_toolbar(frm);
			}
		});
	};
};

/**
 * Rebuild workflow Actions using current frm._pm_action_flags (no setTimeout).
 */
erpnext_extensions.petty_management.refresh_workflow_actions = function (frm) {
	if (!frm || !frm.states || typeof frm.states.show_actions !== "function") {
		return;
	}
	if (frm.is_new && frm.is_new()) {
		return;
	}
	frm.states.show_actions();
};

const PM_PENDING_REMARK_EDIT_STATES = {
	"PM Request": new Set([
		"Pending Approval",
		"Pending Manager Approval",
		"Pending CEO Approval",
		"Pending Finance Approval",
	]),
	"PM Clearance": new Set([
		"Pending Approval",
		"Pending Manager Approval",
		"Pending Finance Review",
	]),
};

function pm_is_pending_remark_edit(frm) {
	if (!frm?.doc || cint(frm.doc.docstatus) !== 0) {
		return false;
	}
	const states = PM_PENDING_REMARK_EDIT_STATES[frm.doctype];
	if (!states) {
		return false;
	}
	return states.has(frm.doc.workflow_state || "");
}

/**
 * While Pending*: lock every field except Remarks. Save remains enabled for remark-only edits.
 */
erpnext_extensions.petty_management.apply_pending_remark_only_lock = function (frm) {
	if (!frm || (frm.doctype !== "PM Request" && frm.doctype !== "PM Clearance")) {
		return;
	}
	const pending = pm_is_pending_remark_edit(frm);
	if (!pending) {
		if (frm._pm_pending_remark_lock_applied) {
			frm.refresh_fields();
			delete frm._pm_pending_remark_lock_applied;
		}
		return;
	}
	frm._pm_pending_remark_lock_applied = true;
	Object.keys(frm.fields_dict || {}).forEach((fn) => {
		const field = frm.fields_dict[fn];
		const df = field?.df || {};
		if (!fn || ["Tab Break", "Section Break", "Column Break", "HTML"].includes(df.fieldtype)) {
			return;
		}
		if (fn === "remark") {
			frm.set_df_property(fn, "read_only", 0);
			return;
		}
		if (df.fieldtype === "Table") {
			frm.set_df_property(fn, "read_only", 1);
			if (field.grid) {
				field.grid.df.read_only = 1;
				field.grid.refresh();
			}
			return;
		}
		frm.set_df_property(fn, "read_only", 1);
	});
};

function cint(v) {
	const parsed = parseInt(v, 10);
	return Number.isFinite(parsed) ? parsed : 0;
}

// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
//
// Job Card · Move Materials / Submit honor original + substitute transfers
//
// Core compares each Job Card Item row's transferred_qty to required_qty.
// When a substitute is posted with Stock Entry Detail.original_item pointing
// at the demand item (often on a separate supply row), Core still treats the
// original row as short and hides Submit.
//
// Server onload exposes ee_materials_fully_transferred from the same
// attribution used by JobCard.get_rows_to_move / has_short_transferred_material.
// This script wraps get_job_steps so Move Materials "done" uses that flag.

frappe.ui.form.on("Job Card", {
	setup(frm) {
		frm.events._ee_wrap_job_steps_for_alt_transfer(frm);
		frm.events._ee_wrap_move_materials_for_alt_transfer(frm);
	},

	refresh(frm) {
		frm.events._ee_wrap_job_steps_for_alt_transfer(frm);
		frm.events._ee_wrap_move_materials_for_alt_transfer(frm);
		frm.events._ee_refresh_alt_transfer_status(frm);
	},

	_ee_wrap_job_steps_for_alt_transfer(frm) {
		if (frm.events._ee_alt_transfer_steps_wrapped) return;

		const original = frm.events.get_job_steps;
		if (typeof original !== "function") return;

		frm.events.get_job_steps = function (frm, has_items) {
			const steps = original.call(frm.events, frm, has_items);
			const move = steps.find((step) => step.label === __("Move Materials"));
			if (!move) return steps;

			const onload = frm.doc.__onload || {};
			if (typeof onload.ee_materials_fully_transferred === "boolean") {
				move.done = onload.ee_materials_fully_transferred;
			}
			return steps;
		};

		frm.events._ee_alt_transfer_steps_wrapped = true;
	},

	// Route Move Materials through the module whitelist API (permission-checked,
	// substitute-aware). Avoids relying solely on the monkey-patched doc method
	// path for RPC registration.
	_ee_wrap_move_materials_for_alt_transfer(frm) {
		if (frm.events._ee_alt_transfer_move_wrapped) return;
		if (typeof frm.events.move_materials !== "function") return;
		if (typeof frm.events.show_shortage_dialog !== "function") return;

		frm.events.move_materials = function (frm) {
			if (!frm.doc.name) return;
			frappe.call({
				method: "erpnext_extensions.stock_extensions.job_card_alternative_transfer.api.get_material_shortages_for_job_card",
				args: { job_card: frm.doc.name },
				freeze: true,
			}).then(({ message }) => {
				if (!message?.length) {
					frm.trigger("make_stock_entry");
					return;
				}
				frm.events.show_shortage_dialog(frm, message);
			});
		};

		frm.events._ee_alt_transfer_move_wrapped = true;
	},

	_ee_refresh_alt_transfer_status(frm) {
		if (frm.doc.docstatus !== 0 || frm.is_new() || frm.is_dirty()) return;
		if (!(frm.doc.items || []).length) return;
		if (frm.doc.skip_material_transfer || frm.doc.is_corrective_job_card) return;

		const job_card = frm.doc.name;
		frappe.call({
			method: "erpnext_extensions.stock_extensions.job_card_alternative_transfer.api.get_material_transfer_status_for_job_card",
			args: { job_card },
			callback(r) {
				if (!r.message || frm.doc.name !== job_card) return;

				frm.doc.__onload = frm.doc.__onload || {};
				frm.doc.__onload.ee_materials_fully_transferred = !!r.message.fully_transferred;
				frm.doc.__onload.ee_material_shortfalls = r.message.shortfalls || [];
				frm.doc.__onload.ee_attributed_by_item = r.message.attributed_by_item || {};

				const has_items = !!(frm.doc.items || []).length;
				// Rebuild step pills with the authoritative flag.
				if (typeof frm.events.make_dashboard === "function") {
					frm.events.make_dashboard(frm, has_items);
				}
				frm.events._ee_apply_submit_gate(frm, has_items);
			},
		});
	},

	_ee_apply_submit_gate(frm, has_items) {
		if (frm.doc.docstatus !== 0 || frm.is_new() || frm.is_dirty()) return;

		const steps = frm.events.get_job_steps(frm, has_items);
		const blocked = steps.slice(0, -1).some((step) => !step.done);

		if (blocked) {
			frm.page.clear_primary_action();
			return;
		}

		// Core may have cleared Submit before onload/async status arrived.
		frm.page.set_primary_action(__("Submit"), () => frm.savesubmit());
		if (typeof frm.events.keep_one_primary_button === "function") {
			frm.events.keep_one_primary_button(frm);
		}
	},
});

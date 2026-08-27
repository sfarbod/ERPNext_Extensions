// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.ui.form.on("Daily Production Log", {
	setup(frm) {
		frm.set_query("work_order", () => ({
			filters: {
				docstatus: 1,
				status: ["not in", ["Completed", "Stopped", "Closed", "Cancelled"]],
			},
		}));

		frm.set_query("operation", () => {
			const ops = (frm.__wo_operations || []).map((r) => r.operation);
			return { filters: { name: ["in", ops.length ? ops : ["__none__"]] } };
		});

		frm.set_query("output_batch_no", () => {
			const fg = frm.__operation_fg;
			return { filters: fg ? { item: fg } : {} };
		});
	},

	refresh(frm) {
		frm.trigger("load_work_order_operations");
		frm.trigger("render_status");

		if (!frm.is_new() && frm.doc.status !== "Done" && frm.doc.status !== "Running") {
			frm.add_custom_button(__("Run"), () => frm.trigger("run_cycle")).addClass("btn-primary");
		}
	},

	work_order(frm) {
		frm.__wo_operations = null;
		frm.set_value("operation", null);
		frm.set_value("operation_row_id", 0);
		frm.trigger("load_work_order_operations");
	},

	operation(frm) {
		frm.trigger("set_operation_row_id");
	},

	employee(frm) {
		frm.trigger("default_from_time");
	},

	async load_work_order_operations(frm) {
		if (!frm.doc.work_order) return;
		const r = await frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Work Order Operation",
				parent: "Work Order",
				filters: { parent: frm.doc.work_order, parenttype: "Work Order" },
				fields: ["idx", "operation", "finished_good", "completed_qty", "workstation"],
				order_by: "idx asc",
				limit_page_length: 0,
			},
		});
		frm.__wo_operations = r.message || [];
		frm.trigger("set_operation_row_id");
	},

	set_operation_row_id(frm) {
		const rows = (frm.__wo_operations || []).filter((r) => r.operation === frm.doc.operation);
		if (!rows.length) {
			frm.__operation_fg = null;
			return;
		}
		const row = rows.find((r) => r.idx === frm.doc.operation_row_id) || rows[0];
		frm.__operation_fg = row.finished_good;
		if (frm.doc.operation_row_id !== row.idx) {
			frm.set_value("operation_row_id", row.idx);
		}
	},

	async default_from_time(frm) {
		// Default From = the operator's last Done cycle's To — the usual "next shift" case.
		if (!frm.doc.employee || frm.doc.from_time) return;
		const r = await frappe.db.get_list("Daily Production Log", {
			filters: { employee: frm.doc.employee, status: "Done" },
			fields: ["to_time"],
			order_by: "to_time desc",
			limit: 1,
		});
		if (r && r.length && r[0].to_time) {
			frm.set_value("from_time", r[0].to_time);
		}
	},

	run_cycle(frm) {
		const go = () =>
			frappe.call({
				method: "erpnext_extensions.daily_production.runner.run",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __("Running production cycle…"),
				always() {
					frm.reload_doc();
				},
			});

		if (frm.is_dirty()) {
			frm.save().then(go);
		} else {
			go();
		}
	},

	render_status(frm) {
		if (frm.is_new()) return;
		const color = { Draft: "gray", Running: "blue", Done: "green", Failed: "red" }[frm.doc.status] || "gray";
		frm.page.set_indicator(__(frm.doc.status), color);
		if (frm.doc.status === "Failed" && frm.doc.error_log) {
			const first = String(frm.doc.error_log).split("\n").filter(Boolean).pop() || "";
			frm.dashboard.set_headline(`<span class="text-danger">${frappe.utils.escape_html(first)}</span>`);
		}
	},
});

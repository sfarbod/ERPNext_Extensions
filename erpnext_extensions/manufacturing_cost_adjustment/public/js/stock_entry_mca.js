// Copyright (c) 2026, ERPNext Extensions contributors
// MCA integration on Stock Entry form — mirror working Client Script pattern

frappe.ui.form.on("Stock Entry", {
	refresh(frm) {
		if (frm.fields_dict.additional_costs && frm.doc.docstatus === 1) {
			frm.set_df_property("additional_costs", "read_only", 1);
			const grid = frm.fields_dict.additional_costs.grid;
			if (grid && grid.wrapper) {
				grid.wrapper.find(".grid-add-row, .grid-remove-rows, .grid-download, .grid-upload").hide();
			}
		}

		if (frm.doc.docstatus !== 1) return;
		if (!["Manufacture", "Repack"].includes(frm.doc.purpose)) return;

		frm.add_custom_button(__("Add Post-Submission Cost"), () => {
			frappe.call({
				method:
					"erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment.manufacturing_cost_adjustment.make_mca_from_stock_entry",
				args: { stock_entry: frm.doc.name },
				freeze: true,
				callback(r) {
					if (!r.message) return;
					const doc = frappe.model.sync(r.message)[0];
					frappe.set_route("Form", doc.doctype, doc.name);
				},
			});
		});
	},
});

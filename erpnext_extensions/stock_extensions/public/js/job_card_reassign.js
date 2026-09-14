frappe.ui.form.on("Job Card", {
	refresh(frm) {
		const roles = frappe.user_roles || [];
		const allowed =
			frappe.session.user === "Administrator" || roles.includes("System Manager");
		if (!allowed || !frm.doc.work_order || frm.doc.docstatus === 2) {
			return;
		}
		frm.add_custom_button(
			__("Reassign Work Order"),
			() => {
				frappe.route_options = {
					source_work_order: frm.doc.work_order,
					job_card: frm.doc.name,
				};
				frappe.set_route("reassign-job-cards");
			},
			__("Actions")
		);
	},
});

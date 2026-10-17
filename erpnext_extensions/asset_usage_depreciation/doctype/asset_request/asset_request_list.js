// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT

frappe.listview_settings["Asset Request"] = {
	add_fields: [
		"status",
		"workflow_state",
		"fulfillment_status",
		"requested_qty",
		"delivered_qty",
		"remaining_qty",
		"docstatus",
	],
	get_indicator(doc) {
		// Prefer fulfillment colour when approved; otherwise show approval stage.
		const approval = doc.workflow_state || doc.status || "";
		const fulfillment = doc.fulfillment_status || "";

		if (approval === "Rejected" || approval === "Cancelled" || cint(doc.docstatus) === 2) {
			return [__(approval || "Cancelled"), "red", "status,=," + (doc.status || approval)];
		}
		if (fulfillment === "Fulfilled") {
			return [__("Fulfilled"), "green", "fulfillment_status,=,Fulfilled"];
		}
		if (fulfillment === "Partially Fulfilled") {
			return [__("Partially Fulfilled"), "orange", "fulfillment_status,=,Partially Fulfilled"];
		}
		if (fulfillment === "Issued from pool" || fulfillment === "Purchase requested") {
			return [__(fulfillment), "orange", "fulfillment_status,=," + fulfillment];
		}
		if (fulfillment === "Waiting for fulfillment" && approval === "Approved") {
			return [__("Waiting for fulfillment"), "orange", "fulfillment_status,=,Waiting for fulfillment"];
		}
		if (approval === "Approved") {
			return [__("Approved"), "blue", "workflow_state,=,Approved"];
		}
		if (String(approval).startsWith("Pending")) {
			return [__(approval), "orange", "workflow_state,=," + approval];
		}
		return [__(approval || "Draft"), "gray", "status,=,Draft"];
	},
};

function cint(v) {
	const n = parseInt(v, 10);
	return Number.isFinite(n) ? n : 0;
}

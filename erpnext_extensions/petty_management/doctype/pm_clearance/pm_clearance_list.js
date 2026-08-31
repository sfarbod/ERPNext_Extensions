frappe.listview_settings["PM Clearance"] = {
	add_fields: ["status", "journal_entry", "remark"],
	get_indicator(doc) {
		const lifecycle = (doc.status || "").trim();
		const colors = {
			Draft: "grey",
			"Pending Finance Review": "orange",
			Approved: "blue",
			"Pending Journal Entry Submission": "yellow",
			Settled: "green",
			Rejected: "red",
			Cancelled: "darkgrey",
		};
		return [__(lifecycle || "Draft"), colors[lifecycle] || "grey", "status,=," + lifecycle];
	},
};

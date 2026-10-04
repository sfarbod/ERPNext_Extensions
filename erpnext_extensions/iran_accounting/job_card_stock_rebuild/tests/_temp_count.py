import frappe


def count():
	n = frappe.db.sql(
		"select count(*) from `tabStock Entry` where remarks like %s",
		("%JC_REPAIR_TEMP_BRIDGE%",),
	)[0][0]
	return {"count": int(n)}

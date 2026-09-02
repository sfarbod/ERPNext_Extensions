frappe.provide("erpnext_extensions.account_explorer");

/**
 * Presentation-only date formatting for Account Explorer grids/headers.
 * Uses Frappe datetime helpers (Persian Calendar patches str_to_user when Jalali is active).
 * Raw row/API values remain canonical ISO YYYY-MM-DD.
 */
function format_ae_date(value) {
	if (value === null || value === undefined || value === "") {
		return "";
	}
	try {
		return frappe.datetime.str_to_user(value, false, true);
	} catch (e) {
		return String(value);
	}
}

erpnext_extensions.account_explorer.format_ae_date = format_ae_date;

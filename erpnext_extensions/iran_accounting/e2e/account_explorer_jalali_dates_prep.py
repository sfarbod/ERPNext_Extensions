# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""E2E prep for Account Explorer Jalali date display (v5.1.0)."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.account_explorer.query_spec import _resolve_fiscal_year


@frappe.whitelist()
def prepare_jalali_dates_e2e() -> dict:
	"""Return scope + sample voucher with canonical ISO and expected Jalali display."""
	_ensure_jalali_enabled_for_e2e()
	company = frappe.defaults.get_global_default("company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	if not company:
		company = frappe.get_all("Company", limit=1)[0].name

	fiscal_year, from_date, to_date = _resolve_fiscal_year(company, None, None, None)

	gle = frappe.db.sql(
		"""
		SELECT posting_date, voucher_type, voucher_no
		FROM `tabGL Entry`
		WHERE company = %s AND is_cancelled = 0
		ORDER BY posting_date DESC
		LIMIT 1
		""",
		company,
		as_dict=True,
	)
	if not gle:
		frappe.throw("No GL entries available for Jalali date E2E prep")

	row = gle[0]
	posting_iso = str(row.posting_date)
	expected_jalali = posting_iso
	try:
		from persian_calendar.utils.jalali import toshamshi

		expected_jalali = toshamshi(posting_iso, format="YYYY/MM/DD")
	except Exception:
		pass

	payload = {
		"company": company,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"fiscal_year": fiscal_year,
		"voucher_type": row.voucher_type,
		"voucher_no": row.voucher_no,
		"posting_date_iso": posting_iso,
		"posting_date_jalali": expected_jalali,
		"jalali_pattern": r"^\d{4}/\d{2}/\d{2}$",
		"iso_pattern": r"^\d{4}-\d{2}-\d{2}$",
	}
	return payload


def _ensure_jalali_enabled_for_e2e() -> None:
	"""Ensure Persian Calendar boot path is active for Playwright (non-destructive idempotent)."""
	if not frappe.db.exists("DocType", "Jalali Settings"):
		return
	settings = frappe.get_single("Jalali Settings")
	changed = False
	if not cint(settings.enable_jalali):
		settings.enable_jalali = 1
		changed = True
	if (settings.default_calendar or "") != "Jalali":
		settings.default_calendar = "Jalali"
		changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()
	frappe.cache.hdel("bootinfo", "Administrator")

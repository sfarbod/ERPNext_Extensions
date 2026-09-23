"""Profile major Historical Repair scanners (read-only)."""
from __future__ import annotations
import json
from time import perf_counter

def run(company=None):
	company = company or "اسپاد فارمد دارو"
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from frappe.utils import nowdate

	def mark(name, fn):
		a = perf_counter()
		try:
			res = fn()
			ok = True
			err = None
			n = res.get("count") if isinstance(res, dict) else None
			if n is None and isinstance(res, dict):
				n = len(res.get("rows") or [])
		except Exception as e:
			ok = False
			err = f"{type(e).__name__}: {e}"
			n = None
			res = None
		return {"scanner": name, "seconds": round(perf_counter() - a, 3), "ok": ok, "count": n, "error": err}

	rows = [
		mark("posting_order", lambda: run_full_history_scan(company=company)),
		mark("zero_rate", lambda: scan_zero_rate_rows(company=company)),
		mark("wrong_rate", lambda: scan_wrong_rates(company=company, limit=4000)),
		mark("i1", lambda: scan_i1_negative_rate(company=company, limit=2000)),
		mark("i4", lambda: scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)),
		mark("sle_bin", lambda: scan_sle_bin(company=company, limit=2000)),
		mark("gl", lambda: scan_gl_integrity(company=company, limit=500)),
		mark("failed_riv", lambda: scan_failed_riv(limit=2000)),
	]
	rows.sort(key=lambda r: -(r["seconds"] or 0))
	print(json.dumps({"company": company, "scanners": rows, "slowest": rows[0]["scanner"] if rows else None}, ensure_ascii=False, indent=2))
	return rows

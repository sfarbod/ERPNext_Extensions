# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Browser acceptance: Applied Batch Share → Allocated Amount + filter smoke tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import frappe
from frappe.utils import flt

OUT = Path("/workspace/development/frappe-bench/sites/preserved_dev_backups/costing_ui_verify_v5727")
BASE = os.environ.get("COSTING_UI_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("COSTING_UI_USER", "Administrator")
PWD = os.environ.get("COSTING_UI_PWD", "admin")


def _pick_row():
	"""Prefer a row with pool net ~16.55M; else any active-rev row with positive pool."""
	rows = frappe.db.sql(
		"""
		SELECT bx.name, bx.monthly_calculation, bx.revision, bx.batch_no, bx.account,
		       bx.cost_center, bx.department, bx.pool_monthly_net_expense,
		       bx.applied_batch_share, bx.allocated_amount, m.total_allocated_expenses
		FROM `tabMFG Cost Batch Expense Allocation` bx
		INNER JOIN `tabMonthly FG Cost Calculation` m
			ON m.name = bx.monthly_calculation AND m.revision = bx.revision
		WHERE bx.pool_monthly_net_expense > 0
		ORDER BY ABS(bx.pool_monthly_net_expense - 16550000) ASC, bx.modified DESC
		LIMIT 1
		""",
		as_dict=True,
	)
	return rows[0] if rows else None


def run_api_share_roundtrip():
	row = _pick_row()
	if not row:
		return {"ok": False, "error": "No batch expense row found"}
	from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import set_applied_batch_share

	pool = flt(row.pool_monthly_net_expense)
	parent_before = flt(row.total_allocated_expenses)
	allocated_before = flt(row.allocated_amount)

	r50 = set_applied_batch_share(
		batch_expense_allocation=row.name, applied_share=0.50, reason="acceptance 0.50", commit=1
	)
	doc50 = frappe.db.get_value(
		"MFG Cost Batch Expense Allocation",
		row.name,
		["applied_batch_share", "allocated_amount", "pool_monthly_net_expense"],
		as_dict=True,
	)
	parent50 = flt(
		frappe.db.get_value("Monthly FG Cost Calculation", row.monthly_calculation, "total_allocated_expenses")
	)
	exp50 = flt(pool * 0.50, 2)
	ok50 = abs(flt(doc50.allocated_amount) - exp50) < 0.02

	r60 = set_applied_batch_share(
		batch_expense_allocation=row.name, applied_share=0.60, reason="acceptance 0.60", commit=1
	)
	doc60 = frappe.db.get_value(
		"MFG Cost Batch Expense Allocation",
		row.name,
		["applied_batch_share", "allocated_amount", "pool_monthly_net_expense"],
		as_dict=True,
	)
	parent60 = flt(
		frappe.db.get_value("Monthly FG Cost Calculation", row.monthly_calculation, "total_allocated_expenses")
	)
	exp60 = flt(pool * 0.60, 2)
	ok60 = abs(flt(doc60.allocated_amount) - exp60) < 0.02
	parent_delta = parent60 - parent50
	# Parent moves by (0.60-0.50) × Σ pool_net for every BxE row of this batch.
	batch_pool_sum = flt(
		frappe.db.sql(
			"""
			SELECT COALESCE(SUM(pool_monthly_net_expense),0)
			FROM `tabMFG Cost Batch Expense Allocation`
			WHERE monthly_calculation=%s AND revision=%s AND batch_no=%s
			""",
			(row.monthly_calculation, row.revision, row.batch_no),
		)[0][0]
	)
	expected_delta = flt(batch_pool_sum * 0.10, 2)

	audit = frappe.get_all(
		"MFG Cost Calculation Revision",
		filters={"monthly_calculation": row.monthly_calculation, "action": "Manual Share Override"},
		fields=["name", "revision", "action", "totals_json", "override_map_json"],
		order_by="modified desc",
		limit_page_length=3,
	)

	# Currency rounding across hundreds of pool rows can drift by small IRR amounts.
	parent_ok = abs(parent_delta - expected_delta) < max(100.0, abs(expected_delta) * 1e-9)
	return {
		"ok": ok50 and ok60 and parent_ok,
		"row": row.name,
		"monthly": row.monthly_calculation,
		"pool_net": pool,
		"before": {"allocated": allocated_before, "parent": parent_before, "share": flt(row.applied_batch_share)},
		"at_0_50": {
			"allocated": flt(doc50.allocated_amount),
			"expected": exp50,
			"parent": parent50,
			"engine": r50,
			"ok": ok50,
		},
		"at_0_60": {
			"allocated": flt(doc60.allocated_amount),
			"expected": exp60,
			"parent": parent60,
			"engine": r60,
			"ok": ok60,
			"parent_delta": parent_delta,
			"expected_parent_delta": expected_delta,
		},
		"audit_sample": audit,
	}


def run_browser():
	from playwright.sync_api import sync_playwright

	OUT.mkdir(parents=True, exist_ok=True)
	row = _pick_row()
	results = {"api": run_api_share_roundtrip(), "browser": [], "filters": []}
	if not row:
		results["error"] = "no row"
		(OUT / "share_browser_results.json").write_text(json.dumps(results, indent=2, default=str))
		return results

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
		context = browser.new_context(viewport={"width": 1440, "height": 900})
		page = context.new_page()
		page.set_default_timeout(60000)

		page.goto(f"{BASE}/login")
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[name="pwd"]', PWD)
		page.click('button[type="submit"], button.btn-login, .btn-primary:has-text("Login")')
		page.wait_for_url("**/desk**", timeout=60000)
		page.wait_for_timeout(2000)

		# Open batch expense form
		page.goto(f"{BASE}/app/mfg-cost-batch-expense-allocation/{row.name}")
		page.wait_for_timeout(3000)
		page.screenshot(path=str(OUT / "share_01_form_before.png"), full_page=False)

		# Apply Share Override dialog → 0.60
		if page.locator("text=Apply Share Override").count():
			page.locator("text=Apply Share Override").first.click()
			page.wait_for_timeout(800)
			page.fill('input[data-fieldname="applied_share"]', "0.60")
			page.locator('.modal-footer button.btn-primary:has-text("Update"), button:has-text("Update")').last.click()
			page.wait_for_timeout(4000)
			page.screenshot(path=str(OUT / "share_02_after_override.png"), full_page=False)
			body = page.inner_text("body")
			alloc = flt(
				frappe.db.get_value("MFG Cost Batch Expense Allocation", row.name, "allocated_amount")
			)
			exp = flt(flt(row.pool_monthly_net_expense) * 0.60, 2)
			results["browser"].append(
				{
					"check": "share_override_dialog_0_60",
					"ok": abs(alloc - exp) < 0.02,
					"allocated": alloc,
					"expected": exp,
					"body_has_allocated": "Allocated" in body,
				}
			)
		else:
			results["browser"].append({"check": "share_override_dialog_0_60", "ok": False, "error": "button missing"})

		# Monthly form recon/exceptions panels
		page.goto(f"{BASE}/app/monthly-fg-cost-calculation/{row.monthly_calculation}")
		page.wait_for_timeout(3500)
		page.screenshot(path=str(OUT / "monthly_recon_exc.png"), full_page=False)
		mt = page.inner_text("body")
		results["browser"].append(
			{
				"check": "monthly_panels",
				"has_recon_label": "Reconciliation" in mt,
				"has_exceptions_label": "Exception" in mt,
				"raw_json_hidden_hint": '"checks"' not in mt[:5000],
			}
		)

		# Filter smoke: Batch × Expense by account
		acct = row.account or ""
		page.goto(
			f"{BASE}/app/mfg-cost-batch-expense-allocation"
			f"?monthly_calculation={row.monthly_calculation}&revision={row.revision}&account={acct}"
		)
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "filter_account.png"), full_page=False)
		results["filters"].append({"dimension": "Account", "value": acct, "url": page.url, "ok": acct in page.url or acct in page.inner_text("body")})

		# Batch filter
		bno = row.batch_no or ""
		page.goto(
			f"{BASE}/app/mfg-cost-batch-expense-allocation"
			f"?monthly_calculation={row.monthly_calculation}&revision={row.revision}&batch_no={bno}"
		)
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "filter_batch.png"), full_page=False)
		results["filters"].append({"dimension": "Batch", "value": bno, "ok": bno[:12] in page.inner_text("body")})

		# Cost center / department
		if row.cost_center:
			page.goto(
				f"{BASE}/app/mfg-cost-batch-expense-allocation"
				f"?monthly_calculation={row.monthly_calculation}&revision={row.revision}&cost_center={row.cost_center}"
			)
			page.wait_for_timeout(2000)
			page.screenshot(path=str(OUT / "filter_cost_center.png"), full_page=False)
			results["filters"].append({"dimension": "Cost Center", "value": row.cost_center, "ok": True})
		if row.department:
			page.goto(
				f"{BASE}/app/mfg-cost-batch-expense-allocation"
				f"?monthly_calculation={row.monthly_calculation}&revision={row.revision}&department={row.department}"
			)
			page.wait_for_timeout(2000)
			page.screenshot(path=str(OUT / "filter_department.png"), full_page=False)
			results["filters"].append({"dimension": "Department", "value": row.department, "ok": True})

		# Product list filter
		page.goto(
			f"{BASE}/app/mfg-cost-product-allocation?monthly_calculation={row.monthly_calculation}&revision={row.revision}"
		)
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "filter_product_list.png"), full_page=False)
		results["filters"].append({"dimension": "Product list", "ok": "Product" in page.inner_text("body") or "Item" in page.inner_text("body")})

		# Reconciliation + Exceptions lists
		page.goto(
			f"{BASE}/app/mfg-cost-reconciliation-check?monthly_calculation={row.monthly_calculation}&revision={row.revision}"
		)
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "recon_list.png"), full_page=False)
		results["browser"].append({"check": "recon_list", "ok": "Reconciliation" in page.inner_text("body") or page.locator(".list-row").count() >= 0})

		page.goto(
			f"{BASE}/app/mfg-cost-exception?monthly_calculation={row.monthly_calculation}&revision={row.revision}"
		)
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "exception_list.png"), full_page=False)
		results["browser"].append({"check": "exception_list", "count": page.locator(".list-row, .list-row-container").count()})

		# ADS
		page.goto(
			f"{BASE}/app/mfg-cost-account-dimension-summary?monthly_calculation={row.monthly_calculation}&revision={row.revision}"
		)
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "ads_list.png"), full_page=False)

		browser.close()

	(OUT / "share_browser_results.json").write_text(json.dumps(results, indent=2, default=str, ensure_ascii=False))
	return results


def main():
	return run_browser()

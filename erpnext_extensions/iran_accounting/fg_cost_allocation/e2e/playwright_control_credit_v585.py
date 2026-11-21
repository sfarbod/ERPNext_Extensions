# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Playwright E2E for MFG Cost Control Credit Allocation (v5.8.5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

OUT = Path("/tmp/control_credit_v585/ui")
BASE = os.environ.get("COSTING_UI_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("COSTING_UI_USER", "Administrator")
PWD = os.environ.get("COSTING_UI_PWD", "admin")
COMPANY = "اسپاد فارمد دارو"


def run():
	from playwright.sync_api import sync_playwright

	OUT.mkdir(parents=True, exist_ok=True)
	results = []

	# Resolve a known Calculated credit allocation and a PA with allocated OH
	import frappe

	sites_path = os.environ.get("FRAPPE_SITES_PATH", str(Path("/workspace/development/frappe-bench/sites")))
	frappe.init(site=os.environ.get("FRAPPE_SITE", "development.localhost"), sites_path=sites_path)
	frappe.connect()
	frappe.set_user("Administrator")
	credit_name = frappe.db.get_value(
		"MFG Cost Control Credit Allocation",
		{"persian_period": "1405-03", "status": "Calculated"},
		"name",
	)
	pa_name = frappe.db.sql(
		"""
		SELECT pa.name FROM `tabMFG Cost Product Allocation` pa
		INNER JOIN `tabMonthly FG Cost Calculation` m ON m.name = pa.monthly_calculation
		WHERE m.persian_period = '1405-03'
		  AND IFNULL(pa.breakdown_total_allocated_overhead_share, 0) > 0
		LIMIT 1
		"""
	)
	pa_name = pa_name[0][0] if pa_name else None
	zero_month = frappe.db.get_value(
		"MFG Cost Control Credit Allocation",
		{"persian_period": "1405-01", "status": "Calculated"},
		"name",
	)
	frappe.destroy()

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
		page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
		page.set_default_timeout(60000)
		page.goto(f"{BASE}/login")
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[name="pwd"]', PWD)
		page.click('button[type="submit"], button.btn-login, .btn-primary:has-text("Login")')
		page.wait_for_timeout(2000)
		# Frappe may land on /app or /desk depending on version/route map.
		try:
			page.wait_for_url(lambda url: "/app" in url or "/desk" in url, timeout=60000)
		except Exception:
			page.goto(f"{BASE}/app")
			page.wait_for_timeout(2000)
		page.wait_for_timeout(1500)

		# 1) List new DocType
		page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation")
		page.wait_for_timeout(3000)
		page.screenshot(path=str(OUT / "01_credit_list.png"), full_page=False)
		body = page.inner_text("body")
		results.append(
			{
				"check": "credit_list",
				"ok": "MFGCCCA" in body or "Control Credit" in body or "1405" in body,
			}
		)

		# 2) Open Khordad calculated doc
		if credit_name:
			page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation/{quote(credit_name)}")
			page.wait_for_timeout(4000)
			page.screenshot(path=str(OUT / "02_credit_khordad.png"), full_page=True)
			probe = page.evaluate(
				"""() => {
					const g = (f) => {
						const el = document.querySelector(`[data-fieldname="${f}"] .control-value, [data-fieldname="${f}"] input, [data-fieldname="${f}"]`);
						return el ? (el.value || el.innerText || '').trim() : '';
					};
					const btns = Array.from(document.querySelectorAll('button')).map(b => b.innerText.trim());
					return {
						period: g('persian_period'),
						status: g('status'),
						labor_elig: g('labor_eligible_credit'),
						oh_elig: g('overhead_eligible_credit'),
						has_sources: !!document.querySelector('[data-fieldname="sources"]'),
						has_batches: !!document.querySelector('[data-fieldname="batch_rows"]'),
						has_products: !!document.querySelector('[data-fieldname="product_rows"]'),
						calc_btn: btns.some(t => /Calculate Credit Allocation/i.test(t)),
					};
				}"""
			)
			results.append(
				{
					"check": "credit_form_khordad",
					"ok": bool(probe.get("has_sources") and probe.get("has_batches") and "1405-03" in str(probe.get("period"))),
					"probe": probe,
				}
			)
		else:
			results.append({"check": "credit_form_khordad", "ok": False, "error": "no credit doc"})

		# 3) Zero-credit month Farvardin
		if zero_month:
			page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation/{quote(zero_month)}")
			page.wait_for_timeout(3000)
			page.screenshot(path=str(OUT / "03_credit_farvardin_zero.png"), full_page=False)
			probe = page.evaluate(
				"""() => {
					const g = (f) => {
						const el = document.querySelector(`[data-fieldname="${f}"] .control-value, [data-fieldname="${f}"] input`);
						return el ? (el.value || el.innerText || '').replace(/,/g,'') : '';
					};
					return { oh: g('overhead_eligible_credit'), lab: g('labor_eligible_credit'), status: g('status') };
				}"""
			)
			import re

			def _num(v):
				digits = re.sub(r"[^\d.\-]", "", str(v or "0"))
				try:
					return float(digits or 0)
				except Exception:
					return -1

			oh_v = _num(probe.get("oh"))
			lab_v = _num(probe.get("lab"))
			results.append(
				{
					"check": "zero_credit_month",
					"ok": oh_v == 0 and lab_v == 0,
					"probe": probe,
				}
			)
		else:
			results.append({"check": "zero_credit_month", "ok": False, "error": "missing farvardin doc"})

		# 4) Product Allocation breakdown fields
		if pa_name:
			page.goto(f"{BASE}/app/mfg-cost-product-allocation/{quote(pa_name)}")
			page.wait_for_timeout(4500)
			page.screenshot(path=str(OUT / "04_product_allocation_breakdown.png"), full_page=True)
			probe = page.evaluate(
				"""() => {
					const fields = [
						'allocated_labor_share','allocated_overhead_share',
						'unallocated_labor','unallocated_overhead',
						'labor_amount','overhead_amount'
					];
					const present = {};
					for (const f of fields) {
						present[f] = !!document.querySelector(`[data-fieldname="${f}"]`);
					}
					const excel = Array.from(document.querySelectorAll('button, a.btn'))
						.some(b => /excel|xlsx|خروجی/i.test(b.innerText));
					return { present, excel, title: document.title };
				}"""
			)
			results.append(
				{
					"check": "pa_breakdown_four_fields",
					"ok": all(probe["present"].get(f) for f in (
						"allocated_labor_share",
						"allocated_overhead_share",
						"unallocated_labor",
						"unallocated_overhead",
					)),
					"probe": probe,
				}
			)
		else:
			results.append({"check": "pa_breakdown_four_fields", "ok": False, "error": "no PA with alloc OH"})

		# 5) Existing Batch Expense list still works
		page.goto(f"{BASE}/app/mfg-cost-batch-expense-allocation")
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "05_batch_expense_regression.png"), full_page=False)
		results.append(
			{
				"check": "batch_expense_regression",
				"ok": "Batch" in page.inner_text("body") or "Expense" in page.inner_text("body") or "Applied" in page.inner_text("body"),
			}
		)

		# 6) New form create path (Draft save)
		page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation/new")
		page.wait_for_timeout(3000)
		page.screenshot(path=str(OUT / "06_credit_new.png"), full_page=False)
		results.append(
			{
				"check": "credit_new_form",
				"ok": bool(page.query_selector('[data-fieldname="company"]'))
				and bool(page.query_selector('[data-fieldname="persian_year"]')),
			}
		)

		browser.close()

	summary = {
		"passed": sum(1 for r in results if r.get("ok")),
		"failed": sum(1 for r in results if not r.get("ok")),
		"total": len(results),
		"results": results,
		"evidence_dir": str(OUT),
	}
	(OUT / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
	print(json.dumps(summary, ensure_ascii=False, indent=2))
	return summary


if __name__ == "__main__":
	summary = run()
	raise SystemExit(0 if summary["failed"] == 0 else 1)

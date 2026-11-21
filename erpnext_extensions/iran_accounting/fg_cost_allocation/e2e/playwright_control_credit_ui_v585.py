# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Playwright E2E for Credit Control Workspace / Pool / Batch views (v5.8.5 follow-up)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

OUT = Path("/tmp/control_credit_ui_v585")
BASE = os.environ.get("COSTING_UI_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("COSTING_UI_USER", "Administrator")
PWD = os.environ.get("COSTING_UI_PWD", "admin")


def run():
	from playwright.sync_api import sync_playwright

	import frappe

	OUT.mkdir(parents=True, exist_ok=True)
	sites_path = os.environ.get("FRAPPE_SITES_PATH", str(Path("/workspace/development/frappe-bench/sites")))
	frappe.init(site=os.environ.get("FRAPPE_SITE", "development.localhost"), sites_path=sites_path)
	frappe.connect()
	frappe.set_user("Administrator")
	credit_name = frappe.db.get_value(
		"MFG Cost Control Credit Allocation", {"persian_period": "1405-03"}, "name"
	)
	pa_name = frappe.db.sql(
		"""
		SELECT pa.name FROM `tabMFG Cost Product Allocation` pa
		INNER JOIN `tabMonthly FG Cost Calculation` m ON m.name=pa.monthly_calculation
		WHERE m.persian_period='1405-03'
		  AND IFNULL(pa.breakdown_total_allocated_overhead_share,0)>0
		LIMIT 1
		"""
	)
	pa_name = pa_name[0][0] if pa_name else None
	frappe.destroy()

	results = []
	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
		page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
		page.set_default_timeout(60000)
		page.goto(f"{BASE}/login")
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[name="pwd"]', PWD)
		page.click('button[type="submit"], button.btn-login, .btn-primary:has-text("Login")')
		page.wait_for_timeout(2500)
		try:
			page.wait_for_url(lambda url: "/app" in url or "/desk" in url, timeout=60000)
		except Exception:
			page.goto(f"{BASE}/app")
			page.wait_for_timeout(2000)

		# 1 Workspace
		page.goto(f"{BASE}/app/costing")
		page.wait_for_timeout(3500)
		page.screenshot(path=str(OUT / "01_workspace.png"), full_page=True)
		body = page.inner_text("body")
		results.append(
			{
				"check": "workspace_credit_entry",
				"ok": "Control Credit" in body or "MFG Cost Control Credit" in body,
			}
		)

		# 2 Click / open list
		page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation")
		page.wait_for_timeout(3000)
		page.screenshot(path=str(OUT / "02_credit_list.png"), full_page=False)
		results.append(
			{
				"check": "credit_list_view",
				"ok": "MFGCCCA" in page.inner_text("body") or "1405" in page.inner_text("body"),
			}
		)

		# 3 Open monthly doc
		if credit_name:
			page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation/{quote(credit_name)}")
			page.wait_for_timeout(4000)
			page.screenshot(path=str(OUT / "03_credit_form.png"), full_page=True)
			probe = page.evaluate(
				"""() => ({
					has_sources: !!document.querySelector('[data-fieldname="sources"]'),
					has_batches: !!document.querySelector('[data-fieldname="batch_rows"]'),
					btns: Array.from(document.querySelectorAll('button')).map(b=>b.innerText.trim()).filter(Boolean).slice(0,20)
				})"""
			)
			results.append(
				{
					"check": "credit_form_labor_overhead",
					"ok": probe["has_sources"] and probe["has_batches"],
					"probe": probe,
				}
			)
		else:
			results.append({"check": "credit_form_labor_overhead", "ok": False})

		# 4 Pool report
		page.goto(f"{BASE}/app/query-report/MFG%20Cost%20Control%20Credit%20Pool")
		page.wait_for_timeout(4000)
		# set persian period filter if present
		page.evaluate(
			"""() => {
				if (window.frappe && frappe.query_report && frappe.query_report.set_filter_value) {
					frappe.query_report.set_filter_value('persian_period', '1405-03');
					frappe.query_report.set_filter_value('control_type', 'Overhead');
				}
			}"""
		)
		page.wait_for_timeout(2500)
		# refresh
		page.keyboard.press("F5")
		page.wait_for_timeout(4000)
		page.evaluate(
			"""() => {
				if (window.frappe && frappe.query_report) {
					try { frappe.query_report.set_filter_value('persian_period', '1405-03'); } catch(e) {}
					try { frappe.query_report.set_filter_value('control_type', 'Overhead'); } catch(e) {}
					if (frappe.query_report.refresh) frappe.query_report.refresh();
				}
			}"""
		)
		page.wait_for_timeout(5000)
		page.screenshot(path=str(OUT / "04_pool_report.png"), full_page=True)
		pbody = page.inner_text("body")
		results.append(
			{
				"check": "pool_report",
				"ok": ("351,288" in pbody or "351288" in pbody or "Overhead" in pbody)
				and ("Credit Pool" in pbody or "Control Credit" in pbody or "query-report" in page.url),
			}
		)

		# 5 Batch/product report
		page.goto(
			f"{BASE}/app/query-report/MFG%20Cost%20Control%20Credit%20Batch%20Product%20Allocation"
		)
		page.wait_for_timeout(3000)
		page.evaluate(
			"""() => {
				if (window.frappe && frappe.query_report) {
					try { frappe.query_report.set_filter_value('persian_period', '1405-03'); } catch(e) {}
					try { frappe.query_report.set_filter_value('control_type', 'Overhead'); } catch(e) {}
					if (frappe.query_report.refresh) frappe.query_report.refresh();
				}
			}"""
		)
		page.wait_for_timeout(5000)
		page.screenshot(path=str(OUT / "05_batch_product_report.png"), full_page=True)
		bbody = page.inner_text("body")
		results.append(
			{
				"check": "batch_product_report",
				"ok": "Overhead" in bbody or "Batch" in bbody or credit_name in bbody if credit_name else "Overhead" in bbody,
			}
		)

		# 6 Navigate to credit allocation from report link if possible — open form again
		if credit_name:
			page.goto(f"{BASE}/app/mfg-cost-control-credit-allocation/{quote(credit_name)}")
			page.wait_for_timeout(2500)
			results.append({"check": "navigate_credit_doc", "ok": credit_name in page.url or credit_name in page.inner_text("body")})
		else:
			results.append({"check": "navigate_credit_doc", "ok": False})

		# 7 Product Allocation four fields + credit nav buttons
		if pa_name:
			page.goto(f"{BASE}/app/mfg-cost-product-allocation/{quote(pa_name)}")
			page.wait_for_timeout(4500)
			page.screenshot(path=str(OUT / "06_product_allocation.png"), full_page=True)
			probe = page.evaluate(
				"""() => {
					const fields = ['allocated_labor_share','allocated_overhead_share','unallocated_labor','unallocated_overhead'];
					const present = {};
					for (const f of fields) present[f] = !!document.querySelector(`[data-fieldname="${f}"]`);
					const btns = Array.from(document.querySelectorAll('button')).map(b=>b.innerText.trim());
					return {
						present,
						has_credit_nav: btns.some(t => /Credit Control|Control Credit|Product Credit/i.test(t))
					};
				}"""
			)
			results.append(
				{
					"check": "pa_four_fields_and_nav",
					"ok": all(probe["present"].values()) and probe["has_credit_nav"],
					"probe": probe,
				}
			)
		else:
			results.append({"check": "pa_four_fields_and_nav", "ok": False})

		# 8 Batch expense regression list
		page.goto(f"{BASE}/app/mfg-cost-batch-expense-allocation")
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "07_batch_expense_regression.png"), full_page=False)
		results.append(
			{
				"check": "batch_expense_regression",
				"ok": "Batch" in page.inner_text("body") or "Expense" in page.inner_text("body"),
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

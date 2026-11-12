"""Desk UI regression for Costing 5.7.27 QA (Table grids, monthly, share edit)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

OUT = Path("/tmp/costing_qa_v5727/ui")
BASE = os.environ.get("COSTING_UI_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("COSTING_UI_USER", "Administrator")
PWD = os.environ.get("COSTING_UI_PWD", "admin")
COMPANY = "اسپاد فارمد دارو"


def run():
	from playwright.sync_api import sync_playwright

	OUT.mkdir(parents=True, exist_ok=True)
	results = []
	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
		page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
		page.set_default_timeout(45000)
		page.goto(f"{BASE}/login")
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[name="pwd"]', PWD)
		page.click('button[type="submit"], button.btn-login, .btn-primary:has-text("Login")')
		page.wait_for_url("**/desk**", timeout=60000)
		page.wait_for_timeout(1500)

		# Workspace / module navigation
		page.goto(f"{BASE}/app/costing")
		page.wait_for_timeout(3000)
		body = page.inner_text("body")
		page.screenshot(path=str(OUT / "workspace_costing.png"), full_page=False)
		results.append(
			{
				"check": "costing_workspace",
				"ok": "Cost" in body or "بهای" in body or "Monthly" in body or "Expense" in body,
				"url": page.url,
			}
		)

		# Settings form — standard tables, no excel attach
		page.goto(f"{BASE}/app/finished-goods-cost-allocation-settings/{quote(COMPANY)}")
		page.wait_for_timeout(4000)
		page.screenshot(path=str(OUT / "settings.png"), full_page=True)
		probe = page.evaluate(
			"""() => ({
				accounts_excel: !!document.querySelector('[data-fieldname="accounts_excel"]'),
				departments_excel: !!document.querySelector('[data-fieldname="departments_excel"]'),
				selling_price_list: !!document.querySelector('[data-fieldname="selling_price_list"]'),
				rule_generation_html: !!document.querySelector('[data-fieldname="rule_generation_html"]'),
				rule_generation_summary: !!document.querySelector('[data-fieldname="rule_generation_summary"]'),
				uom_validation_summary: !!document.querySelector('[data-fieldname="uom_validation_summary"]'),
				download_btns: Array.from(document.querySelectorAll('button, a.btn')).map(b => b.innerText.trim()).filter(t => /download|csv|excel/i.test(t)).slice(0, 12),
				generate_btn: Array.from(document.querySelectorAll('button')).some(b => /Generate Rules from Excel/i.test(b.innerText)),
			})"""
		)
		results.append(
			{
				"check": "settings_ux",
				"ok": (not probe["accounts_excel"])
				and (not probe["rule_generation_html"])
				and bool(probe["selling_price_list"])
				and (probe["rule_generation_summary"] or probe["uom_validation_summary"] or probe["generate_btn"]),
				"probe": probe,
			}
		)

		# Monthly list + Farvardin form
		page.goto(f"{BASE}/app/monthly-fg-cost-calculation")
		page.wait_for_timeout(3000)
		page.screenshot(path=str(OUT / "monthly_list.png"), full_page=False)
		results.append(
			{
				"check": "monthly_list",
				"ok": "1405-01" in page.inner_text("body") or "1405-02" in page.inner_text("body"),
			}
		)

		# Open Farvardin doc if link present
		link = page.locator("a:has-text('MFGCC-')").first
		if link.count():
			href = link.get_attribute("href") or ""
			if href:
				page.goto(f"{BASE}{href}" if href.startswith("/") else href)
				page.wait_for_timeout(4000)
				page.screenshot(path=str(OUT / "monthly_form.png"), full_page=True)
				mbody = page.inner_text("body")
				results.append(
					{
						"check": "monthly_form",
						"ok": "Persian" in mbody or "1405" in mbody or "Allocated" in mbody,
						"has_period": "1405" in mbody,
					}
				)

		# Batch × Expense list
		page.goto(f"{BASE}/app/mfg-cost-batch-expense-allocation")
		page.wait_for_timeout(3000)
		page.screenshot(path=str(OUT / "batch_expense_list.png"), full_page=False)
		results.append(
			{
				"check": "batch_expense_list",
				"ok": "Batch" in page.inner_text("body") or "Applied" in page.inner_text("body") or "Expense" in page.inner_text("body"),
			}
		)

		# Product allocation list
		page.goto(f"{BASE}/app/mfg-cost-product-allocation")
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "product_list.png"), full_page=False)
		results.append(
			{
				"check": "product_list",
				"ok": "Product" in page.inner_text("body") or "Item" in page.inner_text("body"),
			}
		)

		# Expense Account Dimension Rule list + count hint
		page.goto(f"{BASE}/app/expense-account-dimension-rule")
		page.wait_for_timeout(3500)
		page.screenshot(path=str(OUT / "expense_rules.png"), full_page=False)
		rbody = page.inner_text("body")
		results.append(
			{
				"check": "expense_rules_list",
				"ok": "Account" in rbody or "Department" in rbody,
				"mentions_3026": "3026" in rbody or "3,026" in rbody,
			}
		)

		browser.close()

	summary = {
		"all_ok": all(r.get("ok") for r in results),
		"results": results,
		"screenshot_dir": str(OUT),
	}
	(OUT / "ui_report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
	print(json.dumps(summary, indent=2, ensure_ascii=False))
	return summary


if __name__ == "__main__":
	run()

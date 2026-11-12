# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Browser verify Settings grids + Product Allocation form after 5.7.27 enhancements."""

from __future__ import annotations

import json
import os
from pathlib import Path

OUT = Path("/workspace/development/frappe-bench/sites/preserved_dev_backups/costing_ui_verify_v5727b")
BASE = os.environ.get("COSTING_UI_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("COSTING_UI_USER", "Administrator")
PWD = os.environ.get("COSTING_UI_PWD", "admin")


def run():
	from playwright.sync_api import sync_playwright

	OUT.mkdir(parents=True, exist_ok=True)
	results = []
	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
		page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
		page.set_default_timeout(60000)
		page.goto(f"{BASE}/login")
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[name="pwd"]', PWD)
		page.click('button[type="submit"], button.btn-login, .btn-primary:has-text("Login")')
		page.wait_for_url("**/desk**", timeout=60000)
		page.wait_for_timeout(2000)

		# Settings
		page.goto(
			f"{BASE}/app/finished-goods-cost-allocation-settings/%D8%A7%D8%B3%D9%BE%D8%A7%D8%AF%20%D9%81%D8%A7%D8%B1%D9%85%D8%AF%20%D8%AF%D8%A7%D8%B1%D9%88"
		)
		page.wait_for_timeout(5000)
		page.screenshot(path=str(OUT / "settings_form.png"), full_page=True)
		body = page.inner_text("body")
		field_probe = page.evaluate(
			"""() => ({
				accounts_excel: !!document.querySelector('[data-fieldname="accounts_excel"]'),
				departments_excel: !!document.querySelector('[data-fieldname="departments_excel"]'),
				selling_price_list: !!document.querySelector('[data-fieldname="selling_price_list"]'),
				rule_generation_html: !!document.querySelector('[data-fieldname="rule_generation_html"]'),
				uom_validation_html: !!document.querySelector('[data-fieldname="uom_validation_html"]'),
			})"""
		)
		# open Expense Rules menu
		if page.locator("button:has-text('Expense Rules'), .btn-group:has-text('Expense Rules')").count():
			page.locator("button:has-text('Expense Rules'), .inner-group-button:has-text('Expense Rules')").first.click()
			page.wait_for_timeout(500)
		results.append(
			{
				"check": "settings_grids",
				"field_probe": field_probe,
				"has_rule_generation_label": "Rule Generation" in body or "Expense Account" in body,
				"has_uom_grid_hint": "Item Primary UOMs" in body or "Primary UOM" in body,
				"has_excel_attach": bool(field_probe.get("accounts_excel") and field_probe.get("departments_excel")),
				"has_selling_price_list": bool(field_probe.get("selling_price_list")),
				"has_generate_excel_btn": page.locator("text=Generate Rules from Excel").count() > 0
				or page.locator("text=Preview Excel Rule Generation").count() > 0,
			}
		)
		page.locator('[data-fieldname="rule_generation_html"]').first.scroll_into_view_if_needed()
		page.wait_for_timeout(500)
		page.screenshot(path=str(OUT / "settings_audit_grids.png"), full_page=False)

		# Product Allocation list (empty after cleanup — still check form meta via new doc if needed)
		page.goto(f"{BASE}/app/mfg-cost-product-allocation")
		page.wait_for_timeout(2500)
		page.screenshot(path=str(OUT / "product_allocation_list.png"), full_page=False)
		results.append(
			{
				"check": "product_list_filters",
				"ok": "Product" in page.inner_text("body") or "Batch" in page.inner_text("body"),
			}
		)

		# Monthly list empty
		page.goto(f"{BASE}/app/monthly-fg-cost-calculation")
		page.wait_for_timeout(2000)
		page.screenshot(path=str(OUT / "monthly_list_cleared.png"), full_page=False)
		results.append(
			{
				"check": "calculations_cleared",
				"body_sample": page.inner_text("body")[:500],
			}
		)

		browser.close()
	(OUT / "browser_results.json").write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2))
	return {"out": str(OUT), "results": results}


def main():
	return run()

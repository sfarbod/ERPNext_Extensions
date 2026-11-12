# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Headless Chromium UI verification for Costing module (dev only)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

OUT = Path("/workspace/development/frappe-bench/sites/preserved_dev_backups/costing_ui_verify")
BASE = os.environ.get("COSTING_UI_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("COSTING_UI_USER", "Administrator")
PWD = os.environ.get("COSTING_UI_PWD", "admin")


def run():
	from playwright.sync_api import sync_playwright

	OUT.mkdir(parents=True, exist_ok=True)
	results = []

	def shot(page, name, note=""):
		path = OUT / f"{name}.png"
		page.screenshot(path=str(path), full_page=False)
		results.append({"shot": str(path), "note": note, "url": page.url})
		return path

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
		context = browser.new_context(viewport={"width": 1400, "height": 900})
		page = context.new_page()
		page.set_default_timeout(60000)

		# Login
		page.goto(f"{BASE}/login")
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[name="pwd"]', PWD)
		page.click('button[type="submit"], button.btn-login, .btn-primary:has-text("Login")')
		page.wait_for_url("**/desk**", timeout=60000)
		page.wait_for_timeout(2500)
		shot(page, "01_desk_home", "Desk after fresh login")

		# Look for Costing tile
		costing_visible = page.locator("text=Costing").count() > 0
		results.append({"check": "desk_costing_label", "ok": costing_visible})
		# Try click workspace/app icon
		tile = page.locator(".desktop-icon, .icon-grid, .workspace-sidebar, body").locator("text=Costing").first
		try:
			tile.click(timeout=5000)
			page.wait_for_timeout(2000)
		except Exception as e:
			# navigate directly
			page.goto(f"{BASE}/app/costing")
			page.wait_for_timeout(2500)
			results.append({"check": "tile_click_fallback", "error": str(e)})
		shot(page, "02_costing_workspace", "Costing workspace / module")

		# Sidebar English labels sample
		body_text = page.inner_text("body")
		results.append(
			{
				"check": "english_sidebar_labels",
				"ok": all(
					x in body_text
					for x in ["Costing", "Settings"]
				)
				or "Cost Allocation" in body_text
				or "Monthly FG" in body_text,
				"has_persian_tile": "بهای تمام شده" in body_text,
			}
		)

		# Monthly list
		page.goto(f"{BASE}/app/monthly-fg-cost-calculation")
		page.wait_for_timeout(2500)
		shot(page, "03_monthly_list", "Monthly FG Cost Calculation list")
		# persian period column / filter
		results.append(
			{
				"check": "list_has_1405_01",
				"ok": "1405-01" in page.inner_text("body") or "MFGCC-2026-00001" in page.inner_text("body"),
			}
		)

		# Open month 1 form
		page.goto(f"{BASE}/app/monthly-fg-cost-calculation/MFGCC-2026-00001")
		page.wait_for_timeout(3500)
		shot(page, "04_monthly_form", "Monthly calculation form with period/totals")
		form_text = page.inner_text("body")
		results.append(
			{
				"check": "form_persian_period",
				"ok": "1405-01" in form_text,
				"has_totals": "Total Allocated" in form_text or "total_allocated" in form_text.lower(),
			}
		)

		# Account dimension section
		results.append(
			{
				"check": "account_dimension_section",
				"ok": "Account / Dimension" in form_text or "Account Dimension" in form_text,
			}
		)

		# Clickable total — try clicking Total Allocated Expenses control
		try:
			page.locator("text=Total Allocated Expenses").first.click(timeout=3000)
			page.wait_for_timeout(500)
			# click the value near it
			page.locator('[data-fieldname="total_allocated_expenses"] .control-value, [data-fieldname="total_allocated_expenses"] .like-disabled-input').first.click(timeout=3000)
			page.wait_for_timeout(2500)
			shot(page, "05_click_total_allocated", "After click Total Allocated Expenses")
			results.append(
				{
					"check": "click_total_allocated_nav",
					"ok": "batch-expense" in page.url or "Batch" in page.inner_text("body"),
					"url": page.url,
				}
			)
		except Exception as e:
			results.append({"check": "click_total_allocated_nav", "ok": False, "error": str(e)})

		# Batch expense list filtered
		page.goto(
			f"{BASE}/app/mfg-cost-batch-expense-allocation?monthly_calculation=MFGCC-2026-00001&revision=1"
		)
		page.wait_for_timeout(3000)
		shot(page, "06_batch_expense_list", "Batch × Expense Pool list + footer totals")
		results.append(
			{
				"check": "list_footer_totals_js_loaded",
				"ok": page.evaluate("() => !!(window.frappe && frappe.listview_settings && frappe.listview_settings['MFG Cost Batch Expense Allocation'])")
				or page.locator(".costing-list-totals").count() > 0,
			}
		)

		# Open one batch expense and try Apply Share Override button
		page.goto(f"{BASE}/app/mfg-cost-batch-expense-allocation")
		page.wait_for_timeout(2000)
		# open first row if present
		try:
			page.locator(".list-row, .list-row-container a").first.click(timeout=5000)
			page.wait_for_timeout(2500)
			shot(page, "07_batch_expense_form", "Batch expense form")
			has_btn = page.locator("text=Apply Share Override").count() > 0
			results.append({"check": "apply_share_override_button", "ok": has_btn})
		except Exception as e:
			results.append({"check": "apply_share_override_button", "ok": False, "error": str(e)})

		# Settings / Item Primary UOMs
		page.goto(f"{BASE}/app/finished-goods-cost-allocation-settings/%D8%A7%D8%B3%D9%BE%D8%A7%D8%AF%20%D9%81%D8%A7%D8%B1%D9%85%D8%AF%20%D8%AF%D8%A7%D8%B1%D9%88")
		page.wait_for_timeout(3500)
		shot(page, "08_settings_uom", "Cost Allocation Settings Item Primary UOMs")
		st = page.inner_text("body")
		results.append(
			{
				"check": "settings_uom_grid",
				"ok": "Item Primary UOMs" in st or "Primary UOM" in st,
				"has_add_items_btn": page.locator("text=Add Items to Costing Scope").count() > 0
				or "Add Items" in st,
			}
		)

		# ADS list
		page.goto(
			f"{BASE}/app/mfg-cost-account-dimension-summary?monthly_calculation=MFGCC-2026-00001&revision=1"
		)
		page.wait_for_timeout(2500)
		shot(page, "09_ads_list", "Account / Dimension Summary list")

		# Expense pools list
		page.goto(f"{BASE}/app/mfg-cost-expense-pool?monthly_calculation=MFGCC-2026-00001&revision=1")
		page.wait_for_timeout(2500)
		shot(page, "10_expense_pools", "Expense Pools list")

		browser.close()

	report = {"base": BASE, "out": str(OUT), "results": results}
	(OUT / "browser_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
	return report


if __name__ == "__main__":
	print(json.dumps(run(), ensure_ascii=False, indent=2))

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Browser verify Settings UX corrections (5.7.27). No Generate. No monthly calc."""

from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE = "http://development.localhost:8000"
USER = "Administrator"
PWD = "admin"
SHOT_DIR = Path("/tmp/costing_settings_ux_v5727")
REPORT = SHOT_DIR / "report.json"


def login(page):
	page.goto(f"{SITE}/login", wait_until="domcontentloaded")
	page.fill('input[data-fieldname="email"], input#login_email, input[type="text"]', USER)
	page.fill('input[data-fieldname="password"], input#login_password, input[type="password"]', PWD)
	page.click('button.btn-login, button[type="submit"]')
	page.wait_for_load_state("networkidle")
	time.sleep(1.5)


def main():
	SHOT_DIR.mkdir(parents=True, exist_ok=True)
	out = {"ok": True, "checks": {}, "shots": []}
	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True)
		context = browser.new_context(viewport={"width": 1440, "height": 900})
		page = context.new_page()
		login(page)

		page.goto(
			f"{SITE}/app/finished-goods-cost-allocation-settings/%D8%A7%D8%B3%D9%BE%D8%A7%D8%AF%20%D9%81%D8%A7%D8%B1%D9%85%D8%AF%20%D8%AF%D8%A7%D8%B1%D9%88",
			wait_until="domcontentloaded",
		)
		page.wait_for_selector(".form-layout", timeout=60000)
		time.sleep(2.5)

		# Field presence / absence
		absent = []
		for fn in ("accounts_excel", "departments_excel", "exclude_admin_staff_cost_centers", "rule_generation_html", "uom_validation_html"):
			el = page.locator(f'[data-fieldname="{fn}"]')
			visible = el.count() > 0 and el.first.is_visible()
			absent.append({"field": fn, "absent_or_hidden": not visible})
		present = []
		for fn in ("expense_account_dimension_rules", "item_primary_uoms", "rule_generation_summary", "uom_validation_summary"):
			el = page.locator(f'[data-fieldname="{fn}"]')
			present.append({"field": fn, "visible": el.count() > 0 and el.first.is_visible()})

		body = page.inner_text("body")
		out["checks"]["no_excel_attach_visible"] = all(x["absent_or_hidden"] for x in absent[:2])
		out["checks"]["no_admin_exclusion_visible"] = absent[2]["absent_or_hidden"] and "Exclude Admin/Staff" not in body
		out["checks"]["no_html_summary_fields"] = all(x["absent_or_hidden"] for x in absent[3:])
		out["checks"]["standard_grids_present"] = all(x["visible"] for x in present)
		out["checks"]["field_probe"] = {"absent": absent, "present": present}

		# Screenshot top of form
		shot1 = SHOT_DIR / "01_settings_top.png"
		page.screenshot(path=str(shot1), full_page=False)
		out["shots"].append(str(shot1))

		# Scroll to rule generation summary grid
		page.locator('[data-fieldname="rule_generation_summary"]').first.scroll_into_view_if_needed()
		time.sleep(0.8)
		shot2 = SHOT_DIR / "02_rule_generation_summary_grid.png"
		page.screenshot(path=str(shot2), full_page=False)
		out["shots"].append(str(shot2))

		# UOM validation summary
		page.locator('[data-fieldname="uom_validation_summary"]').first.scroll_into_view_if_needed()
		time.sleep(0.8)
		# Count rows in UOM summary grid
		uom_rows = page.locator('[data-fieldname="uom_validation_summary"] .grid-body .grid-row').count()
		out["checks"]["uom_summary_row_count_visible"] = uom_rows
		shot3 = SHOT_DIR / "03_uom_validation_summary_grid.png"
		page.screenshot(path=str(shot3), full_page=False)
		out["shots"].append(str(shot3))

		# Download buttons on child tables
		download_visibility = {}
		for fn in ("item_primary_uoms", "expense_account_dimension_rules", "rule_generation_summary", "uom_validation_summary"):
			page.locator(f'[data-fieldname="{fn}"]').first.scroll_into_view_if_needed()
			time.sleep(0.4)
			btn = page.locator(f'[data-fieldname="{fn}"] .grid-download')
			download_visibility[fn] = {
				"count": btn.count(),
				"visible": btn.count() > 0 and btn.first.is_visible(),
			}
		out["checks"]["download_buttons"] = download_visibility

		# Trigger download on UOM validation summary (has 220 rows)
		page.locator('[data-fieldname="uom_validation_summary"]').first.scroll_into_view_if_needed()
		time.sleep(0.5)
		# Ensure download helper ran
		page.evaluate(
			"""() => {
				if (window.cur_frm && window.erpnext_extensions && erpnext_extensions.costing) {
					erpnext_extensions.costing.enable_child_table_downloads(cur_frm);
				}
			}"""
		)
		time.sleep(0.5)
		btn = page.locator('[data-fieldname="uom_validation_summary"] .grid-download')
		if btn.count() and btn.first.is_visible():
			with page.expect_download(timeout=30000) as dl_info:
				btn.first.click()
			download = dl_info.value
			dest = SHOT_DIR / f"uom_validation_summary_export_{download.suggested_filename}"
			download.save_as(str(dest))
			text = dest.read_text(encoding="utf-8", errors="ignore")
			out["checks"]["uom_download"] = {
				"ok": True,
				"file": str(dest),
				"bytes": dest.stat().st_size,
				"has_item_header": "Item Code" in text or "item_code" in text,
				"line_count": len(text.splitlines()),
			}
			shot4 = SHOT_DIR / "04_after_uom_download.png"
			page.screenshot(path=str(shot4), full_page=False)
			out["shots"].append(str(shot4))
		else:
			out["checks"]["uom_download"] = {"ok": False, "reason": "Download button not visible"}
			out["ok"] = False

		# Preview via desk button (does not generate)
		page.locator('button:has-text("Expense Rules"), .btn-group:has-text("Expense Rules")').first.click(timeout=5000)
		time.sleep(0.3)
		# Try menu item
		preview_btn = page.locator('a:has-text("Preview Excel Rule Generation"), button:has-text("Preview Excel Rule Generation"), .dropdown-item:has-text("Preview Excel Rule Generation")')
		if preview_btn.count():
			preview_btn.first.click()
			page.wait_for_selector(".msgprint, .modal-dialog", timeout=60000)
			time.sleep(1)
			modal_text = page.inner_text(".msgprint, .modal-body")
			out["checks"]["preview_modal_has_3026"] = "3026" in modal_text
			shot5 = SHOT_DIR / "05_preview_modal.png"
			page.screenshot(path=str(shot5), full_page=False)
			out["shots"].append(str(shot5))
			# close modal
			page.keyboard.press("Escape")
		else:
			out["checks"]["preview_modal_has_3026"] = None
			out["checks"]["preview_button_missing"] = True

		# Full page screenshot
		shot6 = SHOT_DIR / "06_settings_full.png"
		page.screenshot(path=str(shot6), full_page=True)
		out["shots"].append(str(shot6))

		browser.close()

	REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
	print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
	main()

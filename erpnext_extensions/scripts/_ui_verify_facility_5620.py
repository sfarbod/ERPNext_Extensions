#!/usr/bin/env python3
"""Headless Playwright UI verification for Facility v5.6.20 features.

Runs against development.localhost. Saves screenshots under
sites/development.localhost/public/files/ui_verify_5620/.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("UI_BASE_URL", "http://development.localhost:8000")
USER = os.environ.get("UI_USER", "Administrator")
PWD = os.environ.get("UI_PWD", "admin")

FACILITIES = {
	"draft_dp": "FAC-2026-13568",
	"active_dp": "FAC-2026-13569",
	"normal": "FAC-2026-13570",
	"incomplete": "FAC-2026-13571",
}

OUT = Path("/workspace/development/frappe-bench/sites/development.localhost/public/files/ui_verify_5620")


def login(page):
	page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
	page.wait_for_timeout(500)
	# Frappe login form
	if page.locator('input[data-fieldname="usr"], input#login_email, input[type="text"]').count():
		page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
		page.fill('input[data-fieldname="pwd"], input#login_password, input[type="password"]', PWD)
		page.click('button[type="submit"], .btn-login, button:has-text("Login")')
	# Frappe v16 may land on /desk or /app
	page.wait_for_url(lambda url: "/app" in url or "/desk" in url, timeout=60000)
	page.wait_for_timeout(2000)
	# Prefer modern desk route
	if "/desk" in page.url and "/app" not in page.url:
		page.goto(f"{BASE}/app", wait_until="domcontentloaded", timeout=60000)
		page.wait_for_timeout(1500)


def open_facility(page, name: str):
	page.goto(f"{BASE}/app/facility/{name}", wait_until="domcontentloaded", timeout=60000)
	page.wait_for_selector('.form-layout, .page-container', timeout=60000)
	# Wait for form scripts / grids
	page.wait_for_timeout(2500)


def check_facility(page, key: str, name: str) -> dict:
	open_facility(page, name)
	console_errors = []

	def on_console(msg):
		if msg.type == "error":
			console_errors.append(msg.text)

	page.on("console", on_console)

	# Re-open after listener
	page.reload(wait_until="domcontentloaded")
	page.wait_for_timeout(3000)

	# Evaluate Desk DOM
	result = page.evaluate(
		"""() => {
		const out = {
			pdc_section_visible: false,
			pdc_table_visible: false,
			pdc_footer_visible: false,
			pdc_footer_text: '',
			schedule_footer_visible: false,
			schedule_footer_text: '',
			is_debt_purchase: null,
			status: null,
			pdc_rows: 0,
			schedule_rows: 0,
		};
		const frm = (cur_frm && cur_frm.doctype === 'Facility') ? cur_frm : null;
		if (!frm) {
			out.error = 'no_cur_frm';
			return out;
		}
		out.is_debt_purchase = cint(frm.doc.is_debt_purchase);
		out.status = frm.doc.status;
		out.pdc_rows = (frm.doc.debt_purchase_pdcs || []).length;
		out.schedule_rows = (frm.doc.repayment_schedule || []).length;

		const sec = frm.fields_dict.section_debt_purchase_pdcs;
		const tbl = frm.fields_dict.debt_purchase_pdcs;
		const sched = frm.fields_dict.repayment_schedule;
		const visible = (field) => {
			if (!field || !field.$wrapper) return false;
			const el = field.$wrapper.get(0);
			if (!el) return false;
			const style = window.getComputedStyle(el);
			if (style.display === 'none' || style.visibility === 'hidden') return false;
			// Frappe often hides via .hide-control
			if (el.classList.contains('hide-control')) return false;
			if (el.offsetParent === null && style.position !== 'fixed') {
				// still may be in collapsed section; check dimensions
				const r = el.getBoundingClientRect();
				if (r.width === 0 && r.height === 0) return false;
			}
			return true;
		};
		out.pdc_section_visible = visible(sec);
		out.pdc_table_visible = visible(tbl);

		const pdcFooter = document.querySelector('.ee-facility-grid-footer-pdc');
		const schFooter = document.querySelector('.ee-facility-grid-footer-schedule');
		out.pdc_footer_visible = !!(pdcFooter && pdcFooter.offsetParent !== null && getComputedStyle(pdcFooter).display !== 'none');
		out.schedule_footer_visible = !!(schFooter && schFooter.offsetParent !== null && getComputedStyle(schFooter).display !== 'none');
		out.pdc_footer_text = pdcFooter ? pdcFooter.innerText : '';
		out.schedule_footer_text = schFooter ? schFooter.innerText : '';
		out.pdc_footer_in_dom = !!pdcFooter;
		out.schedule_footer_in_dom = !!schFooter;
		return out;
	}"""
	)
	shot = OUT / f"{key}_{name}.png"
	page.screenshot(path=str(shot), full_page=True)
	result["screenshot"] = str(shot)
	result["console_errors"] = console_errors[:20]
	result["facility"] = name
	result["key"] = key
	return result


def main():
	OUT.mkdir(parents=True, exist_ok=True)
	report = {"base": BASE, "results": []}
	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True)
		context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="fa-IR")
		page = context.new_page()
		try:
			login(page)
		except Exception as e:
			report["login_error"] = str(e)
			page.screenshot(path=str(OUT / "login_fail.png"), full_page=True)
			print(json.dumps(report, ensure_ascii=False, indent=2))
			browser.close()
			sys.exit(2)

		for key, name in FACILITIES.items():
			try:
				report["results"].append(check_facility(page, key, name))
			except Exception as e:
				report["results"].append({"key": key, "facility": name, "error": str(e)})

		# Active editing: add/remove schedule row and confirm footer refresh
		try:
			open_facility(page, FACILITIES["draft_dp"])
			page.wait_for_timeout(2000)
			before = page.evaluate(
				"""() => {
				const el = document.querySelector('.ee-facility-grid-footer-schedule');
				return el ? el.innerText : '';
			}"""
			)
			# Add a schedule row via form API
			after = page.evaluate(
				"""() => {
				const frm = cur_frm;
				frm.add_child('repayment_schedule', {
					due_date: frappe.datetime.add_days(frappe.datetime.get_today(), 120),
					principal_amount: 1,
					profit_amount: 1,
				});
				frm.refresh_field('repayment_schedule');
				if (erpnext_extensions.facility_management.grid_footer) {
					erpnext_extensions.facility_management.grid_footer.refresh_all(frm);
				}
				return new Promise(resolve => setTimeout(() => {
					const el = document.querySelector('.ee-facility-grid-footer-schedule');
					resolve(el ? el.innerText : '');
				}, 500));
			}"""
			)
			# Don't save the extra row — discard by reload later
			report["footer_refresh"] = {"before": before, "after": after, "changed": before != after}
			page.screenshot(path=str(OUT / "footer_refresh.png"), full_page=True)
		except Exception as e:
			report["footer_refresh_error"] = str(e)

		browser.close()

	print(json.dumps(report, ensure_ascii=False, indent=2))
	# Exit non-zero if critical UI missing on draft DP
	draft = next((r for r in report["results"] if r.get("key") == "draft_dp"), {})
	ok = (
		draft.get("pdc_table_visible")
		and (draft.get("pdc_footer_visible") or draft.get("pdc_footer_in_dom"))
		and (draft.get("schedule_footer_visible") or draft.get("schedule_footer_in_dom"))
	)
	sys.exit(0 if ok else 1)


if __name__ == "__main__":
	main()

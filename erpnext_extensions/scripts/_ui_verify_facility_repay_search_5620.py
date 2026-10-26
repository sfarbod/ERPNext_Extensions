#!/usr/bin/env python3
"""Playwright verification: Facility Repayment Link search (v5.6.20)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("UI_BASE_URL", "http://development.localhost:8000")
USER = os.environ.get("UI_USER", "Administrator")
PWD = os.environ.get("UI_PWD", "admin")
OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/public/files/facility_repay_search_5620"
)

ACTIVE_LOAN = os.environ.get("ACTIVE_LOAN_FACILITY", "")  # filled by caller if known
DRAFT_LOAN = "FAC-2026-13826"  # Draft; must stay excluded


def login(page):
	page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
	page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
	page.fill('input[data-fieldname="pwd"], input#login_password, input[type="password"]', PWD)
	page.click('button[type="submit"], .btn-login')
	page.wait_for_url(lambda u: "/app" in u or "/desk" in u, timeout=60000)
	page.wait_for_timeout(1200)


def open_new_repayment(page):
	page.goto(f"{BASE}/app/facility-repayment/new", wait_until="domcontentloaded", timeout=60000)
	page.wait_for_timeout(2500)


def search_facility(page, txt: str) -> dict:
	page.click('[data-fieldname="facility"] input')
	page.fill('[data-fieldname="facility"] input', "")
	page.type('[data-fieldname="facility"] input', txt, delay=40)
	page.wait_for_timeout(1400)
	return page.evaluate(
		"""() => {
		const ul = document.querySelector('.awesomplete ul');
		const items = ul
			? [...ul.querySelectorAll('li')].map((el) => (el.innerText || '').trim()).filter(Boolean)
			: [];
		const results = items.filter(
			(t) =>
				!t.startsWith('Filtered by') &&
				!t.includes('Create a new') &&
				!t.includes('Advanced Search')
		);
		const filterLine = items.find((t) => t.startsWith('Filtered by')) || '';
		return { results, filterLine, raw: items };
	}"""
	)


def api_search(page, txt: str) -> list:
	return page.evaluate(
		"""async (txt) => {
		const r = await frappe.call({
			method: 'frappe.desk.search.search_link',
			args: {
				doctype: 'Facility',
				txt,
				filters: { status: 'Active' },
				page_length: 20,
			},
		});
		return r.message || [];
	}""",
		txt,
	)


def main():
	OUT.mkdir(parents=True, exist_ok=True)
	report = {"base": BASE, "checks": [], "screenshots": [], "failed": []}

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True)
		page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
		login(page)
		open_new_repayment(page)

		# Discover an Active facility with numeric loan-like name via API
		active_loan_hits = page.evaluate(
			"""async () => {
			const r = await frappe.call({
				method: 'frappe.desk.search.search_link',
				args: { doctype:'Facility', txt:'662004835', filters:{status:'Active'}, page_length:20 }
			});
			return r.message || [];
		}"""
		)
		report["active_loan_api"] = active_loan_hits

		cases = [
			("by_id_fragment", "13569", "FAC-2026-13569"),
			("by_name_partial", "UI TEST FAC Active", "FAC-2026-13569"),
			("by_loan_partial", "662004835", None),  # Active hit if exists; Draft must not appear
			("by_exact_id", "FAC-2026-13569", "FAC-2026-13569"),
			("nonexistent", "ZZZ-NO-SUCH-FACILITY-999", None),
		]

		for key, txt, expect in cases:
			open_new_repayment(page)
			ui = search_facility(page, txt)
			api = api_search(page, txt)
			shot = OUT / f"{key}.png"
			page.screenshot(path=str(shot), full_page=False)
			report["screenshots"].append(str(shot))
			joined = "\n".join(ui.get("results") or [])
			api_values = [x.get("value") for x in (api or [])]
			entry = {
				"key": key,
				"txt": txt,
				"ui_results": ui.get("results"),
				"filter_line": ui.get("filterLine"),
				"api_values": api_values,
				"expect": expect,
			}
			if expect:
				ok = expect in joined or expect in api_values
				if not ok:
					report["failed"].append(key)
			if key == "by_loan_partial":
				# Draft FAC-2026-13826 must never appear
				if DRAFT_LOAN in joined or DRAFT_LOAN in api_values:
					report["failed"].append("draft_leak")
					entry["draft_leak"] = True
				# If any Active 662004835 exists, UI/API should find it
				if active_loan_hits and not api_values:
					report["failed"].append("loan_miss")
			if key == "nonexistent":
				if api_values or any("FAC-" in (r or "") for r in (ui.get("results") or [])):
					report["failed"].append(key)
			# Filter description should mention Status / Active (not cryptic repayment_select)
			fline = ui.get("filterLine") or ""
			if "Repayment Select" in fline:
				report["failed"].append("cryptic_filter")
				entry["cryptic_filter"] = True
			entry["ok"] = key not in report["failed"] and "draft_leak" not in report["failed"]
			report["checks"].append(entry)

		# Explicit draft ID search — must be empty under Active filter
		open_new_repayment(page)
		ui = search_facility(page, DRAFT_LOAN)
		api = api_search(page, DRAFT_LOAN)
		shot = OUT / "draft_excluded.png"
		page.screenshot(path=str(shot), full_page=False)
		report["screenshots"].append(str(shot))
		if DRAFT_LOAN in "\n".join(ui.get("results") or []) or DRAFT_LOAN in [
			x.get("value") for x in (api or [])
		]:
			report["failed"].append("draft_id_not_excluded")
		report["checks"].append(
			{
				"key": "draft_excluded",
				"txt": DRAFT_LOAN,
				"ui_results": ui.get("results"),
				"api_values": [x.get("value") for x in (api or [])],
				"filter_line": ui.get("filterLine"),
			}
		)

		browser.close()

	report["ok"] = not report["failed"]
	print(json.dumps(report, ensure_ascii=False, indent=2))
	sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
	main()

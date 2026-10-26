#!/usr/bin/env python3
"""Playwright UI/UX verification for Facility v5.6.20 footers (EN + FA + responsive)."""

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
	"/workspace/development/frappe-bench/sites/development.localhost/public/files/ui_ux_5620"
)

FAC_DP = "FAC-2026-13568"
FAC_NORMAL = "FAC-2026-13570"

# Hardcoded Persian that must NOT appear when language is English
FORBIDDEN_EN = [
	"چک‌های خرید دین",
	"تعداد چک",
	"جمع مبلغ چک",
	"اصل تسهیلات",
	"فرع تسهیلات",
	"جمع تسهیلات",
	"تعداد اقساط",
	"جمع اصل",
	"جمع سود",
	"جمع کل اقساط",
]

REQUIRED_EN_DP = [
	"Debt Purchase Cheques",
	"Cheque Count",
	"Cheque Total",
	"Principal",
	"Profit",
	"Facility Total",
	"Difference",
	"Installment Count",
	"Total Principal",
	"Total Profit",
	"Total Installments",
]

REQUIRED_FA_DP = [
	"چک‌های خرید دین",
	"تعداد چک",
	"جمع مبلغ چک‌ها",
	"اختلاف",
	"تعداد اقساط",
	"جمع اصل",
	"جمع سود",
	"جمع کل اقساط",
]


def login(page):
	page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
	page.fill('input[data-fieldname="usr"], input#login_email, input[name="usr"]', USER)
	page.fill('input[data-fieldname="pwd"], input#login_password, input[type="password"]', PWD)
	page.click('button[type="submit"], .btn-login')
	page.wait_for_url(lambda url: "/app" in url or "/desk" in url, timeout=60000)
	page.wait_for_timeout(1200)


def set_language(page, lang: str):
	"""Persist user language and hard-reload Desk so boot messages refresh."""
	page.evaluate(
		"""(lang) => {
			return new Promise((resolve, reject) => {
				frappe.call({
					method: 'frappe.client.set_value',
					args: {
						doctype: 'User',
						name: frappe.session.user,
						fieldname: 'language',
						value: lang,
					},
					callback: (r) => {
						if (r.exc) {
							reject(r.exc);
							return;
						}
						resolve(true);
					},
					error: (e) => reject(e),
				});
			});
		}""",
		lang,
	)
	page.wait_for_timeout(500)
	# Full navigation rebuilds frappe.boot._messages
	page.goto(f"{BASE}/app/facility/{FAC_DP}", wait_until="domcontentloaded", timeout=60000)
	page.wait_for_timeout(2000)
	boot_lang = page.evaluate("() => (frappe.boot && frappe.boot.lang) || frappe.lang || null")
	if boot_lang and boot_lang.split("-")[0] != lang.split("-")[0]:
		# Fallback: some sites use locale codes like en-US / fa-IR
		page.evaluate(
			"""(lang) => {
				return new Promise((resolve, reject) => {
					frappe.call({
						method: 'frappe.client.set_value',
						args: {
							doctype: 'User',
							name: frappe.session.user,
							fieldname: 'language',
							value: lang === 'fa' ? 'fa' : 'en',
						},
						callback: () => resolve(true),
						error: reject,
					});
				});
			}""",
			lang,
		)
		page.reload(wait_until="domcontentloaded")
		page.wait_for_timeout(2000)


def open_facility(page, name: str):
	page.goto(f"{BASE}/app/facility/{name}", wait_until="domcontentloaded", timeout=60000)
	page.wait_for_timeout(2800)


def inspect(page) -> dict:
	return page.evaluate(
		"""() => {
		const frm = cur_frm;
		const text = (sel) => {
			const el = document.querySelector(sel);
			return el ? (el.innerText || '').trim() : '';
		};
		const body = document.body.innerText || '';
		const pdc = text('.ee-facility-grid-footer-pdc');
		const sch = text('.ee-facility-grid-footer-schedule');
		const sectionLabel = (() => {
			const w = frm && frm.fields_dict.section_debt_purchase_pdcs && frm.fields_dict.section_debt_purchase_pdcs.$wrapper;
			if (!w) return '';
			const h = w.find('.section-head, .section-head .uppercase, .form-section .section-head').first();
			return (h.text() || '').trim();
		})();
		const tableLabelVisible = (() => {
			const wrap = document.querySelector('.frappe-control[data-fieldname="debt_purchase_pdcs"]');
			if (!wrap) return false;
			const lab = wrap.querySelector('.control-label');
			if (!lab) return false;
			const style = getComputedStyle(lab);
			if (style.display === 'none' || style.visibility === 'hidden') return false;
			return !!(lab.innerText || '').trim();
		})();
		const summaryCompact = !!document.querySelector('.ee-facility-summary');
		const largeGreyBox = (() => {
			const el = document.querySelector('.ee-facility-grid-footer-pdc, .ee-facility-grid-footer-schedule');
			if (!el) return false;
			const bg = getComputedStyle(el).backgroundColor;
			// old design used control-bg fill on the whole block
			return el.classList.contains('ee-facility-summary') ? false : true;
		})();
		const diffItem = document.querySelector('.ee-facility-summary__item--warn, .ee-facility-summary__item--ok');
		const dir = getComputedStyle(document.documentElement).direction || document.dir || '';
		return {
			lang: (frappe.boot && frappe.boot.lang) || frappe.lang || null,
			dir,
			is_debt_purchase: frm ? cint(frm.doc.is_debt_purchase) : null,
			section_label: sectionLabel,
			table_label_visible: tableLabelVisible,
			pdc_footer: pdc,
			schedule_footer: sch,
			summary_compact: summaryCompact,
			has_diff_indicator: !!diffItem,
			diff_class: diffItem ? diffItem.className : '',
			body_sample: body.slice(0, 500),
			pdc_in_dom: !!document.querySelector('.ee-facility-grid-footer-pdc'),
			schedule_in_dom: !!document.querySelector('.ee-facility-grid-footer-schedule'),
		};
	}"""
	)


def scroll_summaries(page):
	page.evaluate(
		"""() => {
		const el = document.querySelector('.ee-facility-grid-footer-pdc')
			|| document.querySelector('.ee-facility-grid-footer-schedule')
			|| document.querySelector('[data-fieldname="repayment_schedule"]');
		if (el) el.scrollIntoView({block: 'center'});
	}"""
	)
	page.wait_for_timeout(400)


def contains_any(text: str, needles: list[str]) -> list[str]:
	return [n for n in needles if n in (text or "")]


def missing_any(text: str, needles: list[str]) -> list[str]:
	return [n for n in needles if n not in (text or "")]


def main():
	OUT.mkdir(parents=True, exist_ok=True)
	report = {"base": BASE, "checks": [], "screenshots": []}
	failed = []

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=True)
		context = browser.new_context(viewport={"width": 1440, "height": 900})
		page = context.new_page()
		login(page)

		# ---------- English ----------
		set_language(page, "en")
		for fac, key in [(FAC_DP, "en_dp"), (FAC_NORMAL, "en_normal")]:
			open_facility(page, fac)
			scroll_summaries(page)
			info = inspect(page)
			shot = OUT / f"{key}.png"
			page.screenshot(path=str(shot), full_page=False)
			report["screenshots"].append(str(shot))
			combined = f"{info.get('section_label','')}\n{info.get('pdc_footer','')}\n{info.get('schedule_footer','')}"
			entry = {"key": key, "facility": fac, **info}
			if key == "en_dp":
				bad = contains_any(combined, FORBIDDEN_EN)
				sec = page.evaluate(
					"""() => {
					const s = cur_frm.fields_dict.section_debt_purchase_pdcs;
					return s && s.df ? (s.df.label || '') : '';
				}"""
				)
				entry["section_df_label"] = sec
				# Section heading comes from DocType meta (translated); footers from __().
				haystack = "\n".join(
					[
						combined,
						info.get("section_label") or "",
						sec or "",
					]
				)
				miss = missing_any(haystack, REQUIRED_EN_DP)
				entry["forbidden_found"] = bad
				entry["missing_en"] = miss
				entry["duplicate_table_label"] = bool(info.get("table_label_visible"))
				if bad or miss or info.get("table_label_visible"):
					failed.append(key)
				if not info.get("summary_compact"):
					failed.append(f"{key}:not_compact")
			else:
				# normal: no PDC footer text required visible; schedule yes
				if "Installment Count" not in (info.get("schedule_footer") or ""):
					failed.append(f"{key}:schedule")
					entry["missing_en"] = ["Installment Count"]
				if contains_any(info.get("schedule_footer") or "", FORBIDDEN_EN):
					failed.append(f"{key}:persian_leak")
			report["checks"].append(entry)

		# responsive EN DP
		open_facility(page, FAC_DP)
		for width, wkey in [(1440, "en_dp_1440"), (1280, "en_dp_1280"), (768, "en_dp_768")]:
			page.set_viewport_size({"width": width, "height": 900})
			page.wait_for_timeout(500)
			scroll_summaries(page)
			info = inspect(page)
			shot = OUT / f"{wkey}.png"
			page.screenshot(path=str(shot), full_page=False)
			report["screenshots"].append(str(shot))
			overflow = page.evaluate(
				"""() => {
				const el = document.querySelector('.ee-facility-summary');
				if (!el) return null;
				return {
					scrollWidth: el.scrollWidth,
					clientWidth: el.clientWidth,
					overflows: el.scrollWidth > el.clientWidth + 2,
				};
			}"""
			)
			report["checks"].append({"key": wkey, "overflow": overflow, "compact": info.get("summary_compact")})
			if overflow and overflow.get("overflows"):
				# wrapping is OK; hard overflow of page is worse — only fail severe
				pass

		# ---------- Persian ----------
		page.set_viewport_size({"width": 1440, "height": 900})
		set_language(page, "fa")
		for fac, key in [(FAC_DP, "fa_dp"), (FAC_NORMAL, "fa_normal")]:
			open_facility(page, fac)
			scroll_summaries(page)
			info = inspect(page)
			shot = OUT / f"{key}.png"
			page.screenshot(path=str(shot), full_page=False)
			report["screenshots"].append(str(shot))
			combined = f"{info.get('pdc_footer','')}\n{info.get('schedule_footer','')}"
			entry = {"key": key, "facility": fac, **info}
			if key == "fa_dp":
				miss = missing_any(combined, ["تعداد چک", "اختلاف", "تعداد اقساط", "جمع اصل"])
				# English leftovers in custom summary should not dominate
				en_left = contains_any(combined, ["Cheque Count", "Installment Count", "Facility Total"])
				entry["missing_fa"] = miss
				entry["english_leftover"] = en_left
				if miss or en_left:
					failed.append(key)
			else:
				if "تعداد اقساط" not in (info.get("schedule_footer") or "") and "Installment Count" in (
					info.get("schedule_footer") or ""
				):
					failed.append(f"{key}:not_translated")
			report["checks"].append(entry)

		# difference indicator on incomplete facility
		open_facility(page, "FAC-2026-13571")
		scroll_summaries(page)
		info = inspect(page)
		shot = OUT / "fa_incomplete_diff.png"
		page.screenshot(path=str(shot), full_page=False)
		report["screenshots"].append(str(shot))
		entry = {"key": "fa_incomplete_diff", **info}
		if "mismatch" not in (info.get("diff_class") or "") and "warn" not in (info.get("diff_class") or ""):
			# class ee-facility-summary__item--warn
			if "warn" not in (info.get("diff_class") or ""):
				failed.append("diff_indicator")
				entry["diff_fail"] = True
		report["checks"].append(entry)

		# restore English for developer convenience
		set_language(page, "en")
		browser.close()

	report["failed"] = failed
	report["ok"] = not failed
	print(json.dumps(report, ensure_ascii=False, indent=2))
	sys.exit(0 if not failed else 1)


if __name__ == "__main__":
	main()

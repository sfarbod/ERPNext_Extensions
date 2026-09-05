#!/usr/bin/env node
/**
 * Cross-tab UI proof (v5.1.1 reset):
 * Item Group ↔ Item EQUAL (stock).
 * Inventory Account tab ABSENT.
 * Account = REAL voucher-scoped GL under Item Group (not stock peer).
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-cross-tab");
const COMPANY = "اسپاد فارمد دارو";
const FY = "1405";
const ITEM_GROUP = "API";
const EXPECT = { inward: 518930425599, outward: 380007928429, balance: 138922497170 };
const EXPECT_SCOPED_GL = { period_debit: 895626059773, period_credit: 895626059773 };

const checks = [];
const pass = (n, d = null) => checks.push({ name: n, ok: true, detail: d });
const fail = (n, e) =>
	checks.push({ name: n, ok: false, err: e?.message || JSON.stringify(e) });
const eq = (a, b, eps = 0.02) => Math.abs(Number(a) - Number(b)) <= eps;

const BOOT = `
window.ae_e2e_set_ig = function(ae, ig) {
	ae.prepared_mode = "live";
	const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
	let bag = AF.empty();
	bag = AF.set_entry(bag, { key: "item_group", value: ig, origin: "user", lifetime: "session", meta: { display_label: ig } });
	ae.set_analysis_filters_bag(bag, { silent: true });
	ae._sync_scopes_from_analysis_filters();
	ae.sync_filter_controls_from_document_scope();
};
window.ae_e2e_clear_ig = function(ae) {
	ae.clear_all_analysis_filters();
	ae._sync_scopes_from_analysis_filters();
	ae.sync_filter_controls_from_document_scope();
};
`;

async function login(page) {
	const res = await fetch(`${BASE}/api/method/login`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ usr: "Administrator", pwd: process.env.AE_PASS || "admin" }),
	});
	const sid = (res.headers.get("set-cookie") || "").match(/sid=([^;]+)/)?.[1];
	await page.context().addCookies([{ name: "sid", value: sid, domain: new URL(BASE).hostname, path: "/" }]);
}

async function idle(page) {
	await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
		timeout: 300000,
	});
}

async function axis(page, id) {
	await page.evaluate(async (ax) => {
		const ae = window.cur_ae;
		ae.prepared_mode = "live";
		ae.analysis_context.page = 1;
		ae.analysis_context.page_size = 500;
		ae.switch_axis(ax);
		await ae.refresh_summary();
	}, id);
	await idle(page);
	return page.evaluate(() => {
		const t = window.cur_ae.totals || {};
		const hint = document.querySelector(".ae-measure-family-hint");
		const tabs = [...document.querySelectorAll(".ae-nav-tab")].map((el) =>
			el.getAttribute("data-axis")
		);
		return {
			inward: Number(t.inward_value || 0),
			outward: Number(t.outward_value || 0),
			balance: Number(t.balance_value ?? Number(t.inward_value || 0) - Number(t.outward_value || 0)),
			debit: Number(t.debit_balance || 0),
			credit: Number(t.credit_balance || 0),
			period_debit: Number(t.period_debit || 0),
			period_credit: Number(t.period_credit || 0),
			hint: hint ? hint.textContent.trim() : null,
			family: hint?.getAttribute("data-measure-family") || null,
			mode: hint?.getAttribute("data-inventory-filter-mode") || null,
			tabs,
			row_sum_inward: (window.cur_ae.rows || []).reduce((s, r) => s + Number(r.inward_value || 0), 0),
		};
	});
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({
		headless: true,
		args: ["--host-resolver-rules=MAP development.localhost 127.0.0.1"],
	});
	const context = await browser.newContext({ locale: "en-US", viewport: { width: 1700, height: 1000 } });
	const page = await context.newPage();
	try {
		await login(page);
		await page.addInitScript(() => {
			Object.defineProperty(navigator, "language", { get: () => "en-US" });
			Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
		});
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded", timeout: 180000 });
		await page.waitForFunction(() => window.frappe?.boot, null, { timeout: 120000 });
		await page.evaluate(() => {
			if (frappe?.boot) {
				frappe.boot.lang = "en";
				frappe.boot.lang_code = "en";
			}
		});
		await page.waitForSelector(".ae-toolbar", { timeout: 120000 });
		await page.evaluate(() => {
			const e = frappe.pages["account-explorer"];
			window.cur_ae = e?.account_explorer || e?.wrapper?.account_explorer;
		});
		await page.waitForFunction(() => window.cur_ae?.metadata?.enabled, null, { timeout: 120000 });
		await page.evaluate(BOOT);
		await page.evaluate(
			async ({ company, fy }) => {
				const ae = window.cur_ae;
				ae.prepared_mode = "live";
				await ae.company_field.set_value(company);
				if (ae.fiscal_year_field) await ae.fiscal_year_field.set_value(fy);
				await new Promise((r) => setTimeout(r, 400));
			},
			{ company: COMPANY, fy: FY }
		);

		const fullAc = await axis(page, "account_level");
		await page.evaluate((ig) => window.ae_e2e_set_ig(window.cur_ae, ig), ITEM_GROUP);

		const ig = await axis(page, "item_group");
		await page.screenshot({ path: path.join(OUT, "01_item_group.png"), fullPage: true });
		const item = await axis(page, "item");
		await page.screenshot({ path: path.join(OUT, "02_item.png"), fullPage: true });
		const ac = await axis(page, "account_level");
		await page.screenshot({ path: path.join(OUT, "03_account_scoped.png"), fullPage: true });

		if (!ig.tabs.includes("inventory_account") && ig.tabs.filter(Boolean).slice(-1)[0] === "voucher")
			pass("H_inventory_account_absent_voucher_last", { tabs: ig.tabs });
		else fail("H_inventory_account_absent_voucher_last", { tabs: ig.tabs });

		if (eq(ig.inward, EXPECT.inward) && eq(ig.outward, EXPECT.outward) && eq(ig.balance, EXPECT.balance))
			pass("ig_expected", ig);
		else fail("ig_expected", { ig, EXPECT });

		if (
			eq(ig.inward, item.inward) &&
			eq(ig.outward, item.outward) &&
			eq(ig.balance, item.balance) &&
			eq(ig.debit, item.debit) &&
			eq(ig.credit, item.credit)
		)
			pass("ig_eq_item", { ig, item });
		else fail("ig_eq_item", { ig, item });

		if (eq(ig.row_sum_inward, ig.inward)) pass("footer_eq_row_sum_stock", { ig });
		else fail("footer_eq_row_sum_stock", { ig });

		if (
			eq(ac.period_debit, EXPECT_SCOPED_GL.period_debit) &&
			eq(ac.period_credit, EXPECT_SCOPED_GL.period_credit)
		)
			pass("account_scoped_gl_parity", { ac, EXPECT_SCOPED_GL });
		else fail("account_scoped_gl_parity", { ac, EXPECT_SCOPED_GL });

		if (ac.period_debit < fullAc.period_debit - 1)
			pass("B_account_row_set_changes_under_ig", { scoped: ac.period_debit, full: fullAc.period_debit });
		else fail("B_account_row_set_changes_under_ig", { scoped: ac.period_debit, full: fullAc.period_debit });

		if (!eq(ac.period_debit, ig.inward) && !eq(ac.period_credit, ig.outward))
			pass("account_gl_not_stock_peer", { ac, ig, hint: ac.hint, family: ac.family, mode: ac.mode });
		else fail("account_gl_not_stock_peer", { ac, ig, hint: ac.hint, family: ac.family, mode: ac.mode });

		await page.evaluate(() => window.ae_e2e_clear_ig(window.cur_ae));
		const cleared = await axis(page, "account_level");
		if (eq(cleared.period_debit, fullAc.period_debit))
			pass("D_filter_clear_restores_full_gl", { cleared, fullAc });
		else fail("D_filter_clear_restores_full_gl", { cleared, fullAc });

		const report = { failed: checks.filter((c) => !c.ok).length, checks, ig, item, ac, fullAc, cleared };
		fs.writeFileSync(path.join(OUT, "cross_tab_pw.json"), JSON.stringify(report, null, 2));
		console.log(JSON.stringify(report, null, 2));
		if (report.failed) process.exitCode = 1;
	} catch (e) {
		fail("fatal", e);
		console.log(JSON.stringify({ failed: 1, checks }, null, 2));
		process.exitCode = 1;
	} finally {
		await browser.close();
	}
}

main();

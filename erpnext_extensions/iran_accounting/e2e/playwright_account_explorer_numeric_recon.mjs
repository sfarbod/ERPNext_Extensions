#!/usr/bin/env node
/**
 * Playwright numeric assertions for Item Group API ↔ Inventory Account ↔ Account isolation.
 * Uses fixed live company اسپاد / FY1405 / Item Group API.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://127.0.0.1:8000";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const COMPANY = "اسپاد فارمد دارو";
const FY = "1405";
const ITEM_GROUP = "API";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-numeric-recon");

const EXPECT = {
	inward: 518930425599,
	outward: 380007928429,
	balance: 138922497170,
};

const checks = [];
const pass = (name, detail = null) => checks.push({ name, ok: true, detail });
const fail = (name, err) =>
	checks.push({
		name,
		ok: false,
		err: err?.message || (typeof err === "object" ? JSON.stringify(err) : String(err)),
	});

const eq = (a, b, eps = 0.02) => Math.abs(Number(a) - Number(b)) <= eps;

const SET_FILTERS = `
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
	ae.prepared_mode = "live";
	const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
	ae.set_analysis_filters_bag(AF.empty(), { silent: true });
	ae._sync_scopes_from_analysis_filters();
	ae.sync_filter_controls_from_document_scope();
};
`;

async function login(page) {
	const res = await fetch(`${BASE}/api/method/login`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ usr: USER, pwd: PASS }),
	});
	if (!res.ok) throw new Error(`login ${res.status}`);
	const sid = (res.headers.get("set-cookie") || "").match(/sid=([^;]+)/)?.[1];
	await page.context().addCookies([{ name: "sid", value: sid, domain: new URL(BASE).hostname, path: "/" }]);
}

async function idle(page) {
	await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
		timeout: 300000,
	});
}

async function refreshAxis(page, axis) {
	await page.evaluate(async (ax) => {
		const ae = window.cur_ae;
		ae.prepared_mode = "live";
		ae.analysis_context.page = 1;
		ae.analysis_context.page_size = 500;
		ae.switch_axis(ax);
		await ae.refresh_summary();
	}, axis);
	await idle(page);
}

function stockTotals() {
	return {
		inward: Number(window.cur_ae.totals?.inward_value || 0),
		outward: Number(window.cur_ae.totals?.outward_value || 0),
		balance: Number(
			window.cur_ae.totals?.balance_value ??
				Number(window.cur_ae.totals?.inward_value || 0) - Number(window.cur_ae.totals?.outward_value || 0)
		),
		debit: Number(window.cur_ae.totals?.debit_balance || 0),
		credit: Number(window.cur_ae.totals?.credit_balance || 0),
		rows: (window.cur_ae.rows || []).map((r) => ({
			code: r.display_code,
			inward: Number(r.inward_value || 0),
			outward: Number(r.outward_value || 0),
			balance: Number(r.balance_value ?? Number(r.inward_value || 0) - Number(r.outward_value || 0)),
			period_debit: Number(r.period_debit || 0),
			period_credit: Number(r.period_credit || 0),
			debit_balance: Number(r.debit_balance || 0),
			credit_balance: Number(r.credit_balance || 0),
			net_balance: Number(r.net_balance || 0),
		})),
	};
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({
		headless: true,
		args: ["--host-resolver-rules=MAP development.localhost 127.0.0.1"],
	});
	const context = await browser.newContext({
		locale: "en-US",
		viewport: { width: 1700, height: 1000 },
	});
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
		// Force page show if desk routed but wrapper empty
		await page.evaluate(async () => {
			try {
				if (frappe.set_route) await frappe.set_route("account-explorer");
			} catch (e) {
				/* locale shortcut noise */
			}
		});
		await page.waitForSelector(".ae-toolbar", { timeout: 120000 });
		await page.waitForFunction(() => {
			const e = frappe?.pages?.["account-explorer"];
			return !!(e?.account_explorer || e?.wrapper?.account_explorer)?.company_field;
		}, null, { timeout: 60000 });
		await page.evaluate(() => {
			const e = frappe.pages["account-explorer"];
			window.cur_ae = e?.account_explorer || e?.wrapper?.account_explorer;
		});
		await page.waitForFunction(() => window.cur_ae?.metadata?.enabled, null, { timeout: 120000 });
		await page.evaluate(SET_FILTERS);
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
		await page.evaluate((ig) => window.ae_e2e_set_ig(window.cur_ae, ig), ITEM_GROUP);

		await refreshAxis(page, "item_group");
		const ig = await page.evaluate(stockTotals);
		await page.screenshot({ path: path.join(OUT, "ig.png"), fullPage: true });
		if (eq(ig.inward, EXPECT.inward) && eq(ig.outward, EXPECT.outward) && eq(ig.balance, EXPECT.balance))
			pass("item_group_expected_totals", ig);
		else fail("item_group_expected_totals", { ig, EXPECT });

		await refreshAxis(page, "item");
		const item = await page.evaluate(stockTotals);
		await page.screenshot({ path: path.join(OUT, "item.png"), fullPage: true });
		const itemSum = {
			inward: item.rows.reduce((s, r) => s + r.inward, 0),
			outward: item.rows.reduce((s, r) => s + r.outward, 0),
			balance: item.rows.reduce((s, r) => s + r.balance, 0),
		};
		if (
			eq(itemSum.inward, ig.inward) &&
			eq(itemSum.outward, ig.outward) &&
			eq(itemSum.balance, ig.balance)
		)
			pass("item_sum_eq_item_group", { itemSum, ig });
		else fail("item_sum_eq_item_group", { itemSum, ig });

		const tabs = await page.evaluate(() =>
			[...document.querySelectorAll(".ae-nav-tab")].map((el) => el.getAttribute("data-axis"))
		);
		if (!tabs.includes("inventory_account") && tabs.filter(Boolean).slice(-1)[0] === "voucher")
			pass("inventory_account_absent_voucher_last", { tabs });
		else fail("inventory_account_absent_voucher_last", { tabs });

		// Account WITH filter — REAL voucher-scoped GL (must NOT equal stock inward/outward)
		await refreshAxis(page, "account_level");
		const acWith = await page.evaluate(() => ({
			totals: {
				period_debit: Number(window.cur_ae.totals?.period_debit || 0),
				period_credit: Number(window.cur_ae.totals?.period_credit || 0),
				net_balance: Number(window.cur_ae.totals?.net_balance || 0),
				debit_balance: Number(window.cur_ae.totals?.debit_balance || 0),
				credit_balance: Number(window.cur_ae.totals?.credit_balance || 0),
			},
			rows: (window.cur_ae.rows || []).slice(0, 20).map((r) => ({
				code: r.display_code,
				period_debit: Number(r.period_debit || 0),
				period_credit: Number(r.period_credit || 0),
				debit_balance: Number(r.debit_balance || 0),
				credit_balance: Number(r.credit_balance || 0),
				net_balance: Number(r.net_balance || 0),
			})),
			mode: (window.cur_ae.metadata?.axes || []).find((a) => a.id === "account_level")
				?.inventory_filter_mode,
		}));
		await page.screenshot({ path: path.join(OUT, "account_with_ig.png"), fullPage: true });

		if (
			!eq(acWith.totals.period_debit, ig.inward) &&
			!eq(acWith.totals.period_credit, ig.outward)
		)
			pass("account_gl_not_equal_stock_inward_outward", { ac: acWith.totals, ig });
		else fail("account_gl_not_equal_stock_inward_outward", { ac: acWith.totals, ig });

		if (eq(acWith.totals.period_debit, 895626059773) && eq(acWith.totals.period_credit, 895626059773))
			pass("account_scoped_gl_expected", acWith.totals);
		else fail("account_scoped_gl_expected", acWith.totals);

		await page.evaluate(() => window.ae_e2e_clear_ig(window.cur_ae));
		await refreshAxis(page, "account_level");
		const acWithout = await page.evaluate(() => ({
			period_debit: Number(window.cur_ae.totals?.period_debit || 0),
			period_credit: Number(window.cur_ae.totals?.period_credit || 0),
			net_balance: Number(window.cur_ae.totals?.net_balance || 0),
			debit_balance: Number(window.cur_ae.totals?.debit_balance || 0),
			credit_balance: Number(window.cur_ae.totals?.credit_balance || 0),
		}));
		await page.screenshot({ path: path.join(OUT, "account_without_ig.png"), fullPage: true });

		if (acWith.totals.period_debit < acWithout.period_debit - 1)
			pass("account_scoped_smaller_than_full_gl", { acWith: acWith.totals, acWithout });
		else fail("account_scoped_smaller_than_full_gl", { acWith: acWith.totals, acWithout });

		const report = {
			failed: checks.filter((c) => !c.ok).length,
			checks,
			ig,
			itemSum,
			acWith: acWith.totals,
			acWithout,
			account_rows_sample: acWith.rows,
			tabs,
		};
		fs.writeFileSync(path.join(OUT, "playwright_numeric.json"), JSON.stringify(report, null, 2));
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

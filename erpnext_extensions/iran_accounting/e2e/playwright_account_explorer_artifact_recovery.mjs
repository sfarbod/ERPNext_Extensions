#!/usr/bin/env node
/**
 * Desk verification: Account Explorer recovers after missing prepared artifacts
 * (DB-only restore). development.localhost only.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8000";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const COMPANY = "اسپاد فارمد دارو";
const FROM = "2026-03-21";
const TO = "2027-03-20";
const EXPECT_DEBIT = 994353195185;
const OUT = path.resolve(__dirname, "screenshots/account-explorer-artifact-recovery");

const checks = [];
const pass = (name, detail = null) => checks.push({ name, ok: true, detail });
const fail = (name, err) =>
	checks.push({
		name,
		ok: false,
		err: err?.message || (typeof err === "object" ? JSON.stringify(err) : String(err)),
	});

async function login(page) {
	const res = await fetch(`${BASE}/api/method/login`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ usr: USER, pwd: PASS }),
	});
	if (!res.ok) throw new Error(`login ${res.status}`);
	const sid = (res.headers.get("set-cookie") || "").match(/sid=([^;]+)/)?.[1];
	if (!sid) throw new Error("no sid");
	await page.context().addCookies([
		{ name: "sid", value: sid, domain: new URL(BASE).hostname, path: "/" },
	]);
}

async function resolveAe(page) {
	await page.waitForFunction(() => {
		const entry = frappe?.pages?.["account-explorer"];
		const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		return !!(ae && document.querySelector(".ae-shell, .ae-toolbar"));
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
}

async function idle(page, timeout = 300000) {
	await page.waitForFunction(
		() => {
			const ae = window.cur_ae;
			if (!ae) return false;
			const loading = !!ae.store?.get?.("loading")?.summary;
			const banner =
				ae.$summary_loading?.is?.(":visible") &&
				!ae.$summary_loading?.hasClass?.("visually-hidden");
			return !loading && !banner;
		},
		null,
		{ timeout }
	);
}

async function configure(page) {
	await page.evaluate(
		async ({ company, from, to }) => {
			const ae = window.cur_ae;
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = "1405";
			ae.document_scope.from_date = from;
			ae.document_scope.to_date = to;
			ae.document_scope.hide_zero_rows = 1;
			ae.document_scope.status = ae.document_scope.status || {};
			ae.document_scope.status.include_opening_entries = 1;
			ae.document_scope.status.include_cancelled_entries = 0;
			ae.document_scope.status.include_default_finance_book_entries = 1;
			ae.document_scope.status.include_period_closing_vouchers = 0;
			if (ae.company_field?.set_value) await ae.company_field.set_value(company);
			if (ae.from_date_field?.set_value) await ae.from_date_field.set_value(from);
			if (ae.to_date_field?.set_value) await ae.to_date_field.set_value(to);
			ae.analysis_context.page = 1;
			ae.analysis_context.page_size = 200;
		},
		{ company: COMPANY, from: FROM, to: TO }
	);
}

async function refreshAxis(page, axis, levelSequence = null) {
	const run = async () => {
		await page.evaluate(
			async ({ ax, level }) => {
				const ae = window.cur_ae;
				ae.analysis_context.page = 1;
				const axisChanged = ae.analysis_context.view_axis !== ax;
				ae.analysis_context.view_axis = ax;
				if (ax === "voucher") {
					ae.analysis_context.sort_field = "posting_date";
					ae.analysis_context.sort_order = "desc";
				} else if (ax === "account_level") {
					ae.analysis_context.sort_field = "display_code";
					ae.analysis_context.sort_order = "asc";
				} else {
					ae.analysis_context.sort_field = "display_title";
					ae.analysis_context.sort_order = "asc";
				}
				if (level != null) ae.analysis_context.level_sequence = level;
				else if ("level_sequence" in ae.analysis_context) {
					delete ae.analysis_context.level_sequence;
				}
				// Prefer in-place refresh; only call switch_axis when axis actually changes.
				if (axisChanged && typeof ae.switch_axis === "function") {
					ae.switch_axis(ax);
				}
				await ae.refresh_summary();
			},
			{ ax: axis, level: levelSequence }
		);
	};
	try {
		await run();
	} catch (e) {
		const msg = String(e?.message || e);
		if (!msg.includes("Execution context was destroyed")) throw e;
		await page.waitForLoadState("domcontentloaded");
		await resolveAe(page);
		await configure(page);
		await run();
	}
	await resolveAe(page);
	await idle(page);
	// Prepared rebuilds via RQ can return "preparing" with empty rows after loading clears.
	await waitReadyRows(page, { minRows: axis === "account_level" || axis === "voucher" ? 1 : 0 });
}

async function waitReadyRows(page, { minRows = 1, timeout = 300000 } = {}) {
	if (minRows <= 0) return;
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const st = await pageState(page);
		const failed =
			st.bodyText.includes("could not load this view") ||
			st.errorText.includes("FileNotFound");
		if (failed) throw new Error(`view failed while waiting: ${st.errorText || st.bodyText}`);
		if (!st.loading && st.rowCount >= minRows) return st;
		await page.evaluate(async () => {
			if (window.cur_ae?.refresh_summary) await window.cur_ae.refresh_summary();
		});
		await idle(page);
		await page.waitForTimeout(1000);
	}
	throw new Error(`rows did not become ready (minRows=${minRows})`);
}

async function pageState(page) {
	return page.evaluate(() => {
		const ae = window.cur_ae;
		const err =
			document.querySelector(".ae-error, .ae-summary-error, .msgprint")?.innerText ||
			ae?.store?.get?.("error") ||
			"";
		return {
			axis: ae?.analysis_context?.view_axis,
			level: ae?.analysis_context?.level_sequence,
			levelTitle: ae?.level_title || ae?.store?.get?.("level_title"),
			status: ae?.last_summary_status || ae?.store?.get?.("status"),
			loading: !!ae?.store?.get?.("loading")?.summary,
			rowCount: (ae?.rows || []).length,
			errorText: String(err || ""),
			bodyText: document.body?.innerText?.slice(0, 500) || "",
			debit11100409: (() => {
				const row = (ae?.rows || []).find(
					(r) => String(r.display_code) === "11100409" || String(r.selected_account || "").includes("11100409")
				);
				return row ? Number(row.debit_balance || row.closing_debit || 0) : null;
			})(),
		};
	});
}

async function shot(page, name) {
	fs.mkdirSync(OUT, { recursive: true });
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	// Playwright's default navigator.language is "en-US@posix", which crashes
	// Frappe AltShortcutGroup via Intl.Locale. Real browsers use en-US / fa-IR.
	// Fix the harness locale — do not monkey-patch Intl.
	const context = await browser.newContext({ locale: "en-US" });
	const page = await context.newPage();
	page.setDefaultTimeout(120000);

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		const navLang = await page.evaluate(() => navigator.language);
		if (navLang === "en-US") pass("harness_locale_en_US", navLang);
		else fail("harness_locale_en_US", navLang);
		await configure(page);
		pass("ae_page_opens");

		// Root Group (may rebuild via RQ if artifact was deleted pre-run)
		await refreshAxis(page, "account_level", 1);
		let st = await pageState(page);
		await shot(page, "01_group");
		if (st.loading) fail("group_not_loading", "still loading");
		else if (st.errorText.includes("FileNotFound") || st.bodyText.includes("could not load this view"))
			fail("group_load", st.errorText || st.bodyText);
		else if (st.rowCount < 1) fail("group_rows", st);
		else pass("group_loads", { rows: st.rowCount });

		// General Ledger
		await refreshAxis(page, "account_level", 2);
		st = await pageState(page);
		await shot(page, "02_gl");
		if (st.rowCount < 1 || st.bodyText.includes("could not load this view")) fail("gl_loads", st);
		else pass("gl_loads", { rows: st.rowCount });

		// Subsidiary Ledger
		await refreshAxis(page, "account_level", 3);
		st = await pageState(page);
		await shot(page, "03_sl");
		if (st.rowCount < 1 || st.bodyText.includes("could not load this view")) fail("sl_loads", st);
		else pass("sl_loads", { rows: st.rowCount });

		// Level 4 + numerical
		await refreshAxis(page, "account_level", 4);
		st = await pageState(page);
		await shot(page, "04_level4");
		if (st.rowCount < 1) fail("level4_loads", st);
		else pass("level4_loads", { rows: st.rowCount });
		if (st.debit11100409 != null && Math.abs(st.debit11100409 - EXPECT_DEBIT) < 0.5)
			pass("level4_11100409_debit", st.debit11100409);
		else fail("level4_11100409_debit", st.debit11100409);

		// Level 5
		await refreshAxis(page, "account_level", 5);
		st = await pageState(page);
		await shot(page, "05_level5");
		if (st.rowCount < 1 || st.bodyText.includes("could not load this view")) fail("level5_loads", st);
		else pass("level5_loads", { rows: st.rowCount });

		// Cross-axis tabs
		for (const axis of ["party", "currency", "item_group", "item", "voucher"]) {
			await refreshAxis(page, axis, null);
			st = await pageState(page);
			await shot(page, `axis_${axis}`);
			if (st.bodyText.includes("could not load this view") || st.errorText.includes("FileNotFound"))
				fail(`${axis}_loads`, st);
			else pass(`${axis}_loads`, { rows: st.rowCount });
		}

		// Dimension with cost_center
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.analysis_context.view_axis = "dimension";
			ae.analysis_context.dimension_scope = ae.analysis_context.dimension_scope || {};
			ae.analysis_context.dimension_scope.dimension_type = "cost_center";
			ae.analysis_context.page = 1;
			if (typeof ae.switch_axis === "function") ae.switch_axis("dimension");
			await ae.refresh_summary();
		});
		await idle(page);
		st = await pageState(page);
		if (st.bodyText.includes("could not load this view") || st.errorText.includes("FileNotFound"))
			fail("dimension_loads", st);
		else pass("dimension_loads", { rows: st.rowCount });

		await refreshAxis(page, "account_level", 1);
		st = await pageState(page);
		if (st.rowCount < 1) fail("tab_switch_back", st);
		else pass("tab_switch_back", { rows: st.rowCount });

		// Refresh
		await page.evaluate(async () => {
			await window.cur_ae.refresh_summary();
		});
		await idle(page);
		st = await pageState(page);
		if (st.loading || st.bodyText.includes("could not load this view")) fail("refresh", st);
		else pass("refresh_works", { rows: st.rowCount });

		// Soft reload
		await page.reload({ waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await configure(page);
		await refreshAxis(page, "account_level", 1);
		st = await pageState(page);
		await shot(page, "07_reload");
		if (st.rowCount < 1 || st.bodyText.includes("could not load this view")) fail("reload", st);
		else pass("reload_works", { rows: st.rowCount });

		// Hard refresh (cache bypass)
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await configure(page);
		await refreshAxis(page, "account_level", 1);
		st = await pageState(page);
		if (st.rowCount < 1) fail("hard_refresh", st);
		else pass("hard_refresh", { rows: st.rowCount });

		// Concurrent client polls
		const concurrent = await page.evaluate(async () => {
			const ae = window.cur_ae;
			const results = await Promise.all([
				ae.refresh_summary(),
				ae.refresh_summary(),
				ae.refresh_summary(),
			]);
			return {
				loading: !!ae.store?.get?.("loading")?.summary,
				rows: (ae.rows || []).length,
				error: String(ae.store?.get?.("error") || ""),
				results: results?.length || 0,
			};
		});
		await idle(page);
		if (concurrent.error.includes("FileNotFound") || concurrent.rows < 1)
			fail("concurrent_refresh", concurrent);
		else pass("concurrent_refresh", concurrent);

		st = await pageState(page);
		if (st.loading) fail("no_permanent_spinner", st);
		else pass("no_permanent_spinner");
		if (st.errorText.includes("FileNotFound") || st.bodyText.includes("FileNotFoundError"))
			fail("no_filenotfound_exposed", st.errorText);
		else pass("no_filenotfound_exposed");
	} catch (e) {
		fail("suite_crash", e);
		try {
			await shot(page, "zz_crash");
		} catch {
			/* ignore */
		}
	} finally {
		await browser.close();
	}

	const report = {
		ok: checks.every((c) => c.ok),
		base: BASE,
		company: COMPANY,
		checks,
	};
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	process.exit(report.ok ? 0 : 1);
}

main();

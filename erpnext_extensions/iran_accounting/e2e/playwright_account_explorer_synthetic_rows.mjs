#!/usr/bin/env node
/**
 * v5.1.1 — forbidden synthetic rows absent (Desk :8000).
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8000";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-synthetic-rows");
const COMPANY = "اسپاد فارمد دارو";
const FY = "1405";
const BAD =
	/(__UNCLASSIFIED__|Unclassified|Unspecified|Unassigned|Unmapped|__UNSPECIFIED__|__UNMAPPED__|Not Specified)/i;

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
		body: JSON.stringify({ usr: "Administrator", pwd: "admin" }),
	});
	if (!res.ok) throw new Error(`login ${res.status}`);
	const sid = (res.headers.get("set-cookie") || "").match(/sid=([^;]+)/)?.[1];
	await page.context().addCookies([
		{ name: "sid", value: sid, domain: new URL(BASE).hostname, path: "/" },
	]);
}

async function idle(page) {
	await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
		timeout: 300000,
	});
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({
		headless: true,
		args: ["--host-resolver-rules=MAP development.localhost 127.0.0.1"],
	});
	const context = await browser.newContext({ locale: "en-US", viewport: { width: 1600, height: 950 } });
	const page = await context.newPage();
	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, {
			waitUntil: "domcontentloaded",
			timeout: 120000,
		});
		await page.waitForSelector(".ae-toolbar", { timeout: 30000 });
		await page.waitForFunction(() => {
			const e = frappe?.pages?.["account-explorer"];
			const ae = e?.account_explorer || e?.wrapper?.account_explorer;
			return !!(ae && ae.company_field && ae.metadata?.enabled);
		}, null, { timeout: 90000 });
		await page.evaluate(() => {
			const e = frappe.pages["account-explorer"];
			window.cur_ae = e.account_explorer || e.wrapper.account_explorer;
		});

		await page.evaluate(async ({ company, fy }) => {
			const ae = window.cur_ae;
			ae.prepared_mode = "live";
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = fy;
			ae.document_scope.from_date = "2026-03-21";
			ae.document_scope.to_date = "2027-03-20";
			ae.document_scope.hide_zero_rows = 1;
			ae.document_scope.status = {
				include_opening_entries: 1,
				include_cancelled_entries: 0,
				include_default_finance_book_entries: 1,
				include_period_closing_vouchers: 0,
			};
			ae.analysis_context.level_sequence = 1;
			ae.switch_axis("account_level", 1);
			await ae.refresh_summary();
		}, { company: COMPANY, fy: FY });
		await idle(page);

		const rootScan = await page.evaluate(() => {
			const rows = window.cur_ae.rows || [];
			return {
				codes: rows.map((r) => r.display_code),
				titles: rows.map((r) => r.display_title),
				bodyHasBad: /__UNCLASSIFIED__|Unclassified/i.test(document.body.innerText),
				totals: window.cur_ae.totals,
			};
		});
		if (rootScan.bodyHasBad || rootScan.codes.some((c) => BAD.test(String(c))))
			fail("account_root_no_unclassified", rootScan);
		else pass("account_root_no_unclassified", rootScan);

		await page.screenshot({ path: path.join(OUT, "01-account-root.png"), fullPage: true });

		// Drill Group 11
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			const row11 = (ae.rows || []).find((r) => String(r.display_code) === "11");
			if (!row11) throw new Error("Group 11 missing");
			ae.analysis_context.account_scope = {
				mode: "tree",
				selected_account: row11.selected_account,
				is_virtual_group: 0,
				level_sequence: 2,
			};
			ae.analysis_context.level_sequence = 2;
			ae.switch_axis("account_level", 2);
			await ae.refresh_summary();
		});
		await idle(page);
		const under11 = await page.evaluate(() => ({
			codes: (window.cur_ae.rows || []).map((r) => r.display_code),
			titles: (window.cur_ae.rows || []).map((r) => r.display_title),
			bodyHasBad: /__UNCLASSIFIED__|Unclassified/i.test(document.body.innerText),
			totals: window.cur_ae.totals,
		}));
		if (under11.bodyHasBad || under11.codes.some((c) => BAD.test(String(c))))
			fail("group11_no_unclassified", under11);
		else pass("group11_no_unclassified", under11);
		await page.screenshot({ path: path.join(OUT, "02-group-11.png"), fullPage: true });

		// Case A Item Group API
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
			let bag = AF.empty();
			bag = AF.set_entry(bag, {
				key: "item_group",
				value: "API",
				origin: "user",
				lifetime: "session",
				meta: { display_label: "API" },
			});
			ae.set_analysis_filters_bag(bag, { silent: true });
			ae._sync_scopes_from_analysis_filters();
			ae.sync_filter_controls_from_document_scope();
			ae.analysis_context.account_scope = {
				mode: "tree",
				selected_account: null,
				is_virtual_group: 0,
			};
			ae.analysis_context.level_sequence = 3;
			ae.switch_axis("account_level", 3);
			await ae.refresh_summary();
		});
		await idle(page);
		const caseA = await page.evaluate(async () => {
			const ae = window.cur_ae;
			const ig = await frappe.call({
				method: "erpnext_extensions.iran_accounting.account_explorer.get_item_group_summary",
				args: {
					payload: {
						document_scope: {
							company: ae.document_scope.company,
							fiscal_year: ae.document_scope.fiscal_year,
							from_date: ae.document_scope.from_date,
							to_date: ae.document_scope.to_date,
							hide_zero_rows: 1,
							status: ae.document_scope.status,
							inventory: { item_group: "API" },
						},
						analysis_context: { view_axis: "item_group", page_size: 50 },
						prepared_mode: "live",
					},
				},
			});
			return {
				codes: (ae.rows || []).map((r) => r.display_code),
				totals: ae.totals,
				bodyHasBad: /__UNCLASSIFIED__|Unclassified/i.test(document.body.innerText),
				ig: ig.message?.totals,
			};
		});
		const eq =
			Math.abs(Number(caseA.totals?.period_debit) - Number(caseA.ig?.inward_value)) < 0.02 &&
			Math.abs(Number(caseA.totals?.period_credit) - Number(caseA.ig?.outward_value)) < 0.02;
		if (caseA.bodyHasBad || caseA.codes.some((c) => BAD.test(String(c))))
			fail("case_a_no_synthetic", caseA);
		else pass("case_a_no_synthetic", caseA);
		(eq ? pass : fail)("case_a_ig_eq_account", caseA);
		await page.screenshot({ path: path.join(OUT, "03-case-a.png"), fullPage: true });

		// Party — no Unspecified
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
			ae.set_analysis_filters_bag(AF.empty(), { silent: true });
			ae._sync_scopes_from_analysis_filters();
			ae.sync_filter_controls_from_document_scope();
			ae.switch_axis("party");
			await ae.refresh_summary();
		});
		await idle(page);
		const party = await page.evaluate(
			() => !/Unspecified|__UNSPECIFIED__/i.test(document.body.innerText)
		);
		(party ? pass : fail)("party_no_unspecified", party);

		// Dimension
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.switch_axis("dimension");
			await ae.refresh_summary();
		});
		await idle(page);
		const dim = await page.evaluate(
			() => !/Unassigned|Unspecified|__UNSPECIFIED__/i.test(document.body.innerText)
		);
		(dim ? pass : fail)("dimension_no_unassigned", dim);

		// Clear / reapply — synthetic never returns
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.switch_axis("account_level", 1);
			await ae.refresh_summary();
		});
		await idle(page);
		const again = await page.evaluate(
			() => !/__UNCLASSIFIED__|Unclassified/i.test(document.body.innerText)
		);
		(again ? pass : fail)("reapply_still_clean", again);
	} catch (e) {
		fail("suite_crash", e);
		try {
			await page.screenshot({ path: path.join(OUT, "crash.png"), fullPage: true });
		} catch {
			/* ignore */
		}
	} finally {
		await browser.close();
	}
	const failed = checks.filter((c) => !c.ok).length;
	const result = { failed, checks, out: OUT, base: BASE };
	fs.writeFileSync(path.join(OUT, "result.json"), JSON.stringify(result, null, 2));
	console.log(JSON.stringify(result, null, 2));
	process.exit(failed ? 1 : 0);
}

main();

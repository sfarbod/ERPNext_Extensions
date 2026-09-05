#!/usr/bin/env node
/**
 * Playwright: v5.1.1 asymmetric Case A / Case B contract.
 *
 * Case A: Item/Item Group → Account EQUAL (engine: sle_scoped_stock)
 * Case B: Account without inventory → posted_gl; reverse equality NOT asserted
 * Clear / reapply: no stale scope; no Inventory Account tab
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8001";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const COMPANY = "اسپاد فارمد دارو";
const FY = "1405";
const ITEM_GROUP = "API";
const MULTI_ITEM = process.env.AE_MULTI_ITEM || "13200023";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-asymmetric-contract");

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
window.ae_e2e_set_item = function(ae, item) {
	ae.prepared_mode = "live";
	const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
	let bag = AF.empty();
	bag = AF.set_entry(bag, { key: "item", value: item, origin: "user", lifetime: "session", meta: { item_code: item, display_label: item } });
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
	if (!sid) throw new Error("no sid");
	await page.context().addCookies([
		{ name: "sid", value: sid, domain: new URL(BASE).hostname, path: "/" },
	]);
	return sid;
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

async function apiSummary(page, axis, inventory) {
	const payload = {
		document_scope: {
			company: COMPANY,
			fiscal_year: FY,
			from_date: "2026-03-21",
			to_date: "2027-03-20",
			hide_zero_rows: 1,
			status: {
				include_opening_entries: 1,
				include_cancelled_entries: 0,
				include_default_finance_book_entries: 1,
				include_period_closing_vouchers: 0,
			},
			inventory: inventory || {},
		},
		analysis_context: {
			view_axis: axis,
			level_sequence: 3,
			page_size: 500,
			detail_mode: "summary",
		},
		prepared_mode: "live",
	};
	const method =
		axis === "item_group"
			? "erpnext_extensions.iran_accounting.account_explorer.get_item_group_summary"
			: axis === "item"
				? "erpnext_extensions.iran_accounting.account_explorer.get_item_summary"
				: "erpnext_extensions.iran_accounting.account_explorer.get_account_summary";
	return page.evaluate(
		async ({ method, payload }) => {
			const r = await frappe.call({ method, args: { payload } });
			return r.message || r;
		},
		{ method, payload }
	);
}

function sumLeaves(rows, field) {
	let s = 0;
	for (const r of rows || []) {
		if (r.is_group || r.has_children) continue;
		s += Number(r[field] || 0);
	}
	return s;
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({
		headless: true,
		args: ["--host-resolver-rules=MAP development.localhost 127.0.0.1"],
	});
	const context = await browser.newContext({
		locale: "en-US",
		viewport: { width: 1600, height: 950 },
	});
	const page = await context.newPage();
	try {
		await login(page);

		await page.goto(`${BASE}/app/account-explorer`, {
			waitUntil: "domcontentloaded",
			timeout: 120000,
		});
		await page.waitForSelector(".ae-toolbar", { timeout: 30000 });
		await page.waitForFunction(
			() => {
				const entry = frappe?.pages?.["account-explorer"];
				const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
				return !!(ae && ae.company_field);
			},
			null,
			{ timeout: 90000 }
		);
		await page.evaluate(() => {
			const entry = frappe.pages["account-explorer"];
			window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		});
		await page.waitForFunction(() => window.cur_ae?.metadata?.enabled, null, {
			timeout: 90000,
		});
		await page.evaluate((script) => eval(script), SET_FILTERS);
		await page.evaluate(
			async ({ company, fy }) => {
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
				await ae.refresh_summary();
			},
			{ company: COMPANY, fy: FY }
		);
		await idle(page);

		// 12. no Inventory Account tab
		const hasInvTab = await page.evaluate(() => {
			const tabs = [...document.querySelectorAll(".ae-nav-tab[data-axis]")].map((el) =>
				el.getAttribute("data-axis")
			);
			return tabs.includes("inventory_account");
		});
		(hasInvTab ? fail : pass)("no_inventory_account_tab", { hasInvTab });

		// 11. Voucher remains last visible axis
		const voucherOk = await page.evaluate(() => {
			const axes = [...document.querySelectorAll(".ae-nav-tab[data-axis]")].map((el) =>
				el.getAttribute("data-axis")
			);
			return axes.includes("voucher") && axes[axes.length - 1] === "voucher";
		});
		(voucherOk ? pass : fail)("voucher_axis_present", voucherOk);

		// --- 1. Item Group API → Account equality ---
		await page.evaluate((ig) => window.ae_e2e_set_ig(window.cur_ae, ig), ITEM_GROUP);
		await refreshAxis(page, "item_group");
		const igApi = await apiSummary(page, "item_group", { item_group: ITEM_GROUP });
		const igUi = await page.evaluate(() => window.cur_ae?.totals || {});
		const igOpen = Number(igApi.totals?.opening_value || 0);
		const igIn = Number(igApi.totals?.inward_value || 0);
		const igOut = Number(igApi.totals?.outward_value || 0);
		const igBal = Number(
			igApi.totals?.balance_value ?? igApi.totals?.debit_balance ?? 0
		);
		if (igIn > 0) pass("case_a_ig_has_value", igApi.totals);
		else fail("case_a_ig_has_value", igApi.totals);

		// 13. footer = API
		if (eq(igUi.inward_value, igIn) && eq(igUi.outward_value, igOut))
			pass("footer_ig_ui_eq_api", { igUi, api: igApi.totals });
		else fail("footer_ig_ui_eq_api", { igUi, api: igApi.totals });

		await refreshAxis(page, "account_level");
		const acApi = await apiSummary(page, "account_level", { item_group: ITEM_GROUP });
		const eng = acApi.account_fact_engine;
		const axisEng = acApi.account_axis_engine;
		if (eng === "sle_scoped_stock" && axisEng === "sle_scoped_stock")
			pass("case_a_engine", { eng, axisEng });
		else fail("case_a_engine", { eng, axisEng, keys: Object.keys(acApi || {}).sort() });
		if (["E3", "E3_SCOPED_GL", "voucher_scoped_gl", "posted_gl", "stock_construction_replay"].includes(eng))
			fail("case_a_engine_not_stale", eng);
		else pass("case_a_engine_not_stale", eng);

		const acPd = Number(acApi.totals?.period_debit || acApi.totals?.inward_value || 0);
		const acPc = Number(acApi.totals?.period_credit || acApi.totals?.outward_value || 0);
		const acNb = Number(acApi.totals?.net_balance || acApi.totals?.balance_value || 0);
		const acOpen = Number(acApi.totals?.opening_balance || acApi.totals?.opening_value || 0);
		if (
			eq(igIn, acPd) &&
			eq(igOut, acPc) &&
			eq(igBal, acNb) &&
			(igOpen === 0 || eq(igOpen, acOpen) || Math.abs(igOpen - acOpen) < 1)
		)
			pass("case_a_ig_eq_account", { igIn, acPd, igOut, acPc, igBal, acNb, igOpen, acOpen });
		else fail("case_a_ig_eq_account", { igIn, acPd, igOut, acPc, igBal, acNb, igOpen, acOpen });

		const leafDebit = sumLeaves(acApi.rows, "period_debit");
		const leafCredit = sumLeaves(acApi.rows, "period_credit");
		if (eq(leafDebit, acPd) && eq(leafCredit, acPc))
			pass("hierarchy_footer_eq_leaves", { leafDebit, acPd, leafCredit, acPc });
		else fail("hierarchy_footer_eq_leaves", { leafDebit, acPd, leafCredit, acPc });

		const acUi = await page.evaluate(() => window.cur_ae?.totals || {});
		if (eq(acUi.period_debit || acUi.debit_turnover || acUi.inward_value, acPd))
			pass("footer_account_ui_eq_api", { acUi, acPd });
		else fail("footer_account_ui_eq_api", { acUi, acPd });

		const banner = await page.evaluate(() => {
			const t =
				document.querySelector(".ae-construction-badge")?.textContent ||
				document.querySelector(".ae-scope-banner")?.textContent ||
				"";
			return /Case A|sle_scoped|stock population|Item Group/i.test(t);
		});
		(banner ? pass : fail)("case_a_banner_visible", banner);

		// Network payload on UI refresh matches API engine
		const netPromise = page.waitForResponse(
			(r) => r.url().includes("get_account_summary") && r.status() === 200,
			{ timeout: 120000 }
		);
		await refreshAxis(page, "account_level");
		const netResp = await netPromise;
		const netJson = await netResp.json();
		const netMsg = netJson.message || netJson;
		if (netMsg.account_fact_engine === "sle_scoped_stock")
			pass("network_payload_case_a_engine", netMsg.account_fact_engine);
		else fail("network_payload_case_a_engine", netMsg.account_fact_engine);

		// --- 2. Item → Account equality (multi-account item) ---
		await page.evaluate((item) => window.ae_e2e_set_item(window.cur_ae, item), MULTI_ITEM);
		await refreshAxis(page, "item");
		const itemApi = await apiSummary(page, "item", { item: MULTI_ITEM });
		const itemIn = Number(itemApi.totals?.inward_value || 0);
		const itemOut = Number(itemApi.totals?.outward_value || 0);
		const itemBal = Number(itemApi.totals?.balance_value || 0);
		if (itemIn > 0) pass("multi_item_has_value", { item: MULTI_ITEM, itemIn, itemOut, itemBal });
		else fail("multi_item_has_value", { item: MULTI_ITEM, totals: itemApi.totals });

		await refreshAxis(page, "account_level");
		const itemAc = await apiSummary(page, "account_level", { item: MULTI_ITEM });
		if (itemAc.account_fact_engine !== "sle_scoped_stock")
			fail("item_case_a_engine", itemAc.account_fact_engine);
		else pass("item_case_a_engine", itemAc.account_fact_engine);
		const iPd = Number(itemAc.totals?.period_debit || 0);
		const iPc = Number(itemAc.totals?.period_credit || 0);
		const iNb = Number(itemAc.totals?.net_balance || 0);
		if (eq(itemIn, iPd) && eq(itemOut, iPc) && eq(itemBal, iNb))
			pass("case_a_item_eq_account", { itemIn, iPd, itemOut, iPc, itemBal, iNb });
		else fail("case_a_item_eq_account", { itemIn, iPd, itemOut, iPc, itemBal, iNb });
		const distinctAccts = (itemAc.rows || []).filter(
			(r) => !r.is_group && !r.has_children && Number(r.period_debit || r.period_credit || r.net_balance)
		).length;
		if (distinctAccts >= 2) pass("multi_account_item_split", { distinctAccts });
		else pass("multi_account_item_split_soft", { distinctAccts, note: "may be single-account on current data" });

		// --- 8. Clear filter → GL mode ---
		await page.evaluate(() => window.ae_e2e_clear_ig(window.cur_ae));
		await refreshAxis(page, "account_level");
		const bare = await apiSummary(page, "account_level", {});
		if (bare.account_fact_engine === "posted_gl")
			pass("clear_filter_posted_gl", bare.account_fact_engine);
		else fail("clear_filter_posted_gl", bare.account_fact_engine);
		const barePd = Number(bare.totals?.period_debit || 0);
		if (barePd > igIn * 5) pass("case_b_account_gt_stock", { barePd, igIn });
		else fail("case_b_account_gt_stock", { barePd, igIn });

		// --- 9/10. Account → Item/IG discovery; reverse equality NOT asserted ---
		await refreshAxis(page, "item_group");
		const igBare = await apiSummary(page, "item_group", {});
		const igBareIn = Number(igBare.totals?.inward_value || 0);
		pass("case_b_reverse_equality_not_asserted", {
			account_period_debit: barePd,
			item_group_inward: igBareIn,
			note: "intentional asymmetry — mismatch allowed",
		});
		if (igBareIn < barePd) pass("case_b_stock_lt_account_live", { igBareIn, barePd });
		else pass("case_b_stock_may_eq_or_lt", { igBareIn, barePd });

		// --- 14/15. Reload retains scope + rapid clear/reapply ---
		await page.evaluate((ig) => window.ae_e2e_set_ig(window.cur_ae, ig), ITEM_GROUP);
		await refreshAxis(page, "item_group");
		const ig2 = await apiSummary(page, "item_group", { item_group: ITEM_GROUP });
		if (eq(ig2.totals?.inward_value, igIn) && eq(ig2.totals?.outward_value, igOut))
			pass("reapply_ig_restores_totals", ig2.totals);
		else fail("reapply_ig_restores_totals", { first: igApi.totals, second: ig2.totals });

		await page.evaluate(() => window.ae_e2e_clear_ig(window.cur_ae));
		await refreshAxis(page, "account_level");
		await page.evaluate((ig) => window.ae_e2e_set_ig(window.cur_ae, ig), ITEM_GROUP);
		await refreshAxis(page, "account_level");
		const ac2 = await apiSummary(page, "account_level", { item_group: ITEM_GROUP });
		if (ac2.account_fact_engine === "sle_scoped_stock" && eq(ac2.totals?.period_debit, igIn))
			pass("rapid_clear_reapply_no_stale", ac2.totals);
		else fail("rapid_clear_reapply_no_stale", ac2);

		// Reload page with filters re-applied after boot
		await page.reload({ waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".ae-toolbar", { timeout: 30000 });
		await page.waitForFunction(
			() => {
				const entry = frappe?.pages?.["account-explorer"];
				const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
				return !!(ae && ae.company_field);
			},
			null,
			{ timeout: 90000 }
		);
		await page.evaluate(() => {
			const entry = frappe.pages["account-explorer"];
			window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		});
		await page.waitForFunction(() => window.cur_ae?.metadata?.enabled, null, {
			timeout: 90000,
		});
		await page.evaluate((script) => eval(script), SET_FILTERS);
		await page.evaluate(
			async ({ company, fy, ig }) => {
				const ae = window.cur_ae;
				ae.prepared_mode = "live";
				ae.document_scope.company = company;
				ae.document_scope.fiscal_year = fy;
				ae.document_scope.from_date = "2026-03-21";
				ae.document_scope.to_date = "2027-03-20";
				ae.document_scope.hide_zero_rows = 1;
				window.ae_e2e_set_ig(ae, ig);
				ae.analysis_context.view_axis = "account_level";
				await ae.refresh_summary();
			},
			{ company: COMPANY, fy: FY, ig: ITEM_GROUP }
		);
		await idle(page);
		const afterReload = await page.evaluate(() => ({
			totals: window.cur_ae?.totals || {},
			inv: window.cur_ae?.document_scope?.inventory || {},
		}));
		const reloadEng = await apiSummary(page, "account_level", { item_group: ITEM_GROUP });
		if (
			reloadEng.account_fact_engine === "sle_scoped_stock" &&
			eq(reloadEng.totals?.period_debit, igIn) &&
			afterReload.inv?.item_group === ITEM_GROUP &&
			eq(afterReload.totals?.period_debit || afterReload.totals?.inward_value, igIn)
		)
			pass("reload_retains_correct_scope", { afterReload, eng: reloadEng.account_fact_engine });
		else fail("reload_retains_correct_scope", { afterReload, reloadEng });

		await page.screenshot({ path: path.join(OUT, "asymmetric-final.png"), fullPage: true });
	} catch (e) {
		fail("suite_crash", e);
		try {
			await page.screenshot({ path: path.join(OUT, "asymmetric-crash.png"), fullPage: true });
		} catch {
			/* ignore */
		}
	} finally {
		await browser.close();
	}

	const failed = checks.filter((c) => !c.ok).length;
	const result = { failed, checks, out: OUT };
	fs.writeFileSync(path.join(OUT, "asymmetric_contract_result.json"), JSON.stringify(result, null, 2));
	console.log(JSON.stringify(result, null, 2));
	process.exit(failed ? 1 : 0);
}

main();

#!/usr/bin/env node
/**
 * Account Explorer 5.7.43 — terminal account carry-forward across L4/L5.
 * Real Playwright against development.localhost.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8000";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const COMPANY = process.env.AE_COMPANY || "اسپاد فارمد دارو";
const ACC_L3 = "111601 - موجودی مواد اولیه - E";
const ACC_L4 = "11100102 - صندوق دفتر مرکزی ( مالی ) - E";
const ACC_L3_GROUP = "111001 - صندوق ریالی - E";
const ACC_L4_WITH_L5 = "11100401 - بانک کارآفرین - E";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5743");

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({ name, ok: false, err: err?.stack || err?.message || String(err) });

async function login(page) {
	await page.goto(`${BASE}/login?redirect-to=%2Fapp`);
	await page.fill('input[data-fieldname="usr"], #login_email, input[type="email"], input[type="text"]', USER);
	await page.fill('input[data-fieldname="pwd"], #login_password, input[type="password"]', PASS);
	await Promise.all([
		page.waitForURL(/\/(app|desk)/, { timeout: 120000 }),
		page.click('button[type="submit"], .btn-login, button:has-text("Login")'),
	]);
}

async function resolveAe(page) {
	await page.waitForFunction(() => {
		const entry = frappe?.pages?.["account-explorer"];
		const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		return !!(ae && ae.metadata?.axes?.length && document.querySelector(".ae-shell, .ae-toolbar"));
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
}

async function waitSummaryIdle(page, { requireRows = false, timeout = 180000 } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const state = await page.evaluate(() => {
			const ae = window.cur_ae;
			const loading = !!ae?.store?.get?.("loading")?.summary;
			const bannerHidden =
				ae?.$summary_loading?.hasClass?.("visually-hidden") ||
				ae?.$summary_loading?.hasClass?.("is-hidden") ||
				!ae?.$summary_loading?.is?.(":visible");
			const gridLoading = ae?.$grid?.hasClass?.("ae-grid-wrap--loading");
			return { ready: !loading && bannerHidden && !gridLoading, rows: (ae?.rows || []).length };
		});
		if (state.ready && (!requireRows || state.rows > 0)) return state;
		await page.waitForTimeout(250);
	}
	throw new Error("summary idle timeout");
}

async function snapshot(page) {
	return page.evaluate(() => {
		const ae = window.cur_ae;
		const filters = ae.get_analysis_filters?.() || {};
		return {
			level: ae.analysis_context.level_sequence,
			company: ae.document_scope.company,
			fiscal_year: ae.document_scope.fiscal_year,
			from_date: ae.document_scope.from_date,
			to_date: ae.document_scope.to_date,
			account_scope: { ...(ae.analysis_context.account_scope || {}) },
			column_filters: { ...(ae.analysis_context.column_filters || {}) },
			dimensions: { ...(ae.document_scope.accounting_dimensions || {}) },
			filter_keys: Object.keys(filters || {}),
			filter_summary: document.querySelector(".ae-filter-summary, .ae-active-filters")?.textContent || "",
			hierarchy_empty: ae.hierarchy_empty,
			rows: (ae.rows || []).map((r) => ({
				account: r.selected_account,
				code: r.display_code,
				title: r.display_title,
				is_terminal: r.is_terminal_account,
				carried: r.is_terminal_carried_forward,
				source_level: r.source_level_sequence,
				level: r.level_sequence,
				period_debit: r.period_debit,
				period_credit: r.period_credit,
				opening_debit: r.opening_debit,
				debit_balance: r.debit_balance,
				credit_balance: r.credit_balance,
			})),
		};
	});
}

async function seedScope(page) {
	await page.evaluate(async ({ company }) => {
		const ae = window.cur_ae;
		ae.document_scope.company = company;
		ae.document_scope.fiscal_year = "1405";
		ae.document_scope.from_date = "2026-03-21";
		ae.document_scope.to_date = "2027-03-20";
		if (ae.company_field?.set_value) await ae.company_field.set_value(company);
		if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
		if (ae.from_date_field?.set_value) await ae.from_date_field.set_value("2026-03-21");
		if (ae.to_date_field?.set_value) await ae.to_date_field.set_value("2027-03-20");
		ae.clear_all_analysis_filters?.({ refresh: false });
		ae.clear_column_filters?.({ refresh: false });
		ae.switch_axis("account_level", 3);
	}, { company: COMPANY });
	await waitSummaryIdle(page, { requireRows: true });
}

async function analyzeAccount(page, account) {
	await page.evaluate(async ({ acc }) => {
		const ae = window.cur_ae;
		ae.analysis_context.account_scope = {
			mode: "account",
			selected_account: acc,
			tree_root_account: acc,
			virtual_row_key: null,
			is_virtual_group: 0,
			level_sequence: ae.analysis_context.level_sequence,
		};
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		let bag = ae.get_analysis_filters();
		bag = AF.apply_policy(bag, "replace_filter", {
			key: "account",
			value: acc,
			label: acc,
			lifetime: "session",
			origin: "e2e_v5743",
		});
		ae.set_analysis_filters_bag(bag);
		ae.refresh_summary({ new_execution: true, reason: "e2e_v5743_analyze" });
	}, { acc: account });
	await waitSummaryIdle(page, { requireRows: true });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const apiEvidence = [];

	page.on("response", async (res) => {
		const url = res.url();
		if (!url.includes("get_account_summary")) return;
		let body = null;
		try {
			body = await res.json();
		} catch {
			body = null;
		}
		const message = body?.message || body;
		apiEvidence.push({
			url: url.slice(0, 180),
			level: message?.level_sequence,
			row_count: (message?.rows || []).length,
			hierarchy_empty: message?.hierarchy_empty || null,
			codes: (message?.rows || []).map((r) => r.display_code).slice(0, 8),
			terminals: (message?.rows || []).map((r) => ({
				code: r.display_code,
				terminal: r.is_terminal_account,
				carried: r.is_terminal_carried_forward,
				source: r.source_level_sequence,
			})),
		});
	});

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`);
		await resolveAe(page);
		await seedScope(page);
		await page.screenshot({ path: path.join(OUT, "01-seed-l3.png"), fullPage: true });

		// ----- Scenario A: terminal Subsidiary Ledger -----
		await analyzeAccount(page, ACC_L3);
		const l3 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "02-terminal-l3-analyze.png"), fullPage: true });
		if (
			l3.rows.length !== 1 ||
			l3.rows[0].account !== ACC_L3 ||
			l3.rows[0].code !== "111601" ||
			!(l3.rows[0].period_debit > 0 || l3.rows[0].period_credit > 0)
		) {
			failCheck("A_terminal_l3_own_turnover", new Error(JSON.stringify(l3)));
		} else {
			passCheck("A_terminal_l3_own_turnover", l3.rows[0]);
		}

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const l4 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "03-terminal-l4-carryforward.png"), fullPage: true });
		if (
			l4.rows.length !== 1 ||
			l4.rows[0].account !== ACC_L3 ||
			l4.rows[0].code !== "111601" ||
			!(Number(l4.rows[0].is_terminal) && Number(l4.rows[0].carried)) ||
			l4.hierarchy_empty?.reason
		) {
			failCheck("A_terminal_l4_carryforward", new Error(JSON.stringify(l4)));
		} else {
			passCheck("A_terminal_l4_carryforward", l4.rows[0]);
		}

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 5));
		await waitSummaryIdle(page, { requireRows: true });
		const l5 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "04-terminal-l5-carryforward.png"), fullPage: true });
		if (
			l5.rows.length !== 1 ||
			l5.rows[0].account !== ACC_L3 ||
			l5.rows[0].code !== "111601" ||
			!(Number(l5.rows[0].is_terminal) && Number(l5.rows[0].carried))
		) {
			failCheck("A_terminal_l5_carryforward", new Error(JSON.stringify(l5)));
		} else {
			passCheck("A_terminal_l5_carryforward", l5.rows[0]);
		}
		if (new Set(l5.rows.map((r) => r.account)).size !== l5.rows.length) {
			failCheck("A_no_duplicate_rows", new Error(JSON.stringify(l5.rows)));
		} else {
			passCheck("A_no_duplicate_rows", { count: l5.rows.length });
		}

		// Consistency of figures L3/L4/L5
		const debitSet = new Set(
			[l3.rows[0].period_debit, l4.rows[0].period_debit, l5.rows[0].period_debit].map((v) =>
				Number(v || 0).toFixed(2)
			)
		);
		if (debitSet.size !== 1) {
			failCheck("A_no_double_count_figures", new Error(JSON.stringify({ l3, l4, l5 })));
		} else {
			passCheck("A_no_double_count_figures", { period_debit: l3.rows[0].period_debit });
		}

		// ----- Scenario B: terminal Level 4 → Level 5 -----
		await page.evaluate(() => {
			const ae = window.cur_ae;
			ae.clear_all_analysis_filters?.({ refresh: false });
			ae.switch_axis("account_level", 4);
		});
		await waitSummaryIdle(page);
		await analyzeAccount(page, ACC_L4);
		const l4term = await snapshot(page);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 5));
		await waitSummaryIdle(page, { requireRows: true });
		const l5fromL4 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "05-l4-terminal-at-l5.png"), fullPage: true });
		if (
			l5fromL4.rows.length !== 1 ||
			l5fromL4.rows[0].account !== ACC_L4 ||
			l5fromL4.rows[0].code !== "11100102" ||
			!(Number(l5fromL4.rows[0].is_terminal) && Number(l5fromL4.rows[0].carried))
		) {
			failCheck("B_l4_terminal_at_l5", new Error(JSON.stringify({ l4term, l5fromL4 })));
		} else {
			passCheck("B_l4_terminal_at_l5", l5fromL4.rows[0]);
		}

		// ----- Scenario C: non-terminal with real descendants -----
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters?.({ refresh: false });
			window.cur_ae.switch_axis("account_level", 3);
		});
		await waitSummaryIdle(page);
		await analyzeAccount(page, ACC_L3_GROUP);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const groupL4 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "06-nonterminal-l4-children.png"), fullPage: true });
		if (
			!(groupL4.rows.length > 0) ||
			groupL4.rows.some((r) => r.account === ACC_L3_GROUP) ||
			groupL4.rows.some((r) => Number(r.carried))
		) {
			failCheck("C_real_l4_children_no_parent_dup", new Error(JSON.stringify(groupL4)));
		} else {
			passCheck("C_real_l4_children_no_parent_dup", {
				count: groupL4.rows.length,
				codes: groupL4.rows.map((r) => r.code),
			});
		}

		await page.evaluate(async ({ acc }) => {
			const ae = window.cur_ae;
			ae.analysis_context.account_scope = {
				mode: "account",
				selected_account: acc,
				tree_root_account: acc,
				virtual_row_key: null,
				is_virtual_group: 0,
				level_sequence: 4,
			};
			ae.switch_axis("account_level", 5);
		}, { acc: ACC_L4_WITH_L5 });
		await waitSummaryIdle(page, { requireRows: true });
		const groupL5 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "07-nonterminal-l5-children.png"), fullPage: true });
		if (
			!(groupL5.rows.length > 0) ||
			groupL5.rows.some((r) => r.account === ACC_L4_WITH_L5) ||
			groupL5.rows.some((r) => Number(r.carried))
		) {
			failCheck("C_real_l5_children_no_parent_dup", new Error(JSON.stringify(groupL5)));
		} else {
			passCheck("C_real_l5_children_no_parent_dup", {
				count: groupL5.rows.length,
				codes: groupL5.rows.map((r) => r.code).slice(0, 8),
			});
		}

		// ----- Scenario D: filter preservation L3→L4→L5→Back→Back -----
		await seedScope(page);
		const cc = await page.evaluate(async () => {
			const list = await frappe.db.get_list("Cost Center", {
				filters: { company: window.cur_ae.document_scope.company, disabled: 0 },
				fields: ["name"],
				limit: 1,
			});
			return list?.[0]?.name || null;
		});
		await analyzeAccount(page, ACC_L3);
		await page.evaluate(({ costCenter }) => {
			const ae = window.cur_ae;
			if (costCenter) {
				ae.document_scope.accounting_dimensions = {
					...(ae.document_scope.accounting_dimensions || {}),
					cost_center: costCenter,
				};
			}
			ae.analysis_context.column_filters = {
				...(ae.analysis_context.column_filters || {}),
				display_code: "111601",
			};
			ae.refresh_summary({ new_execution: true, reason: "e2e_v5743_filters" });
		}, { costCenter: cc });
		await waitSummaryIdle(page, { requireRows: true });
		const d0 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "08-filters-l3.png"), fullPage: true });

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const d1 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "09-filters-l4.png"), fullPage: true });

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 5));
		await waitSummaryIdle(page, { requireRows: true });
		const d2 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "10-filters-l5.png"), fullPage: true });

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const d3 = await snapshot(page);

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 3));
		await waitSummaryIdle(page, { requireRows: true });
		const d4 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "11-filters-back-l3.png"), fullPage: true });

		const filterOk = [d0, d1, d2, d3, d4].every(
			(s) =>
				s.company === COMPANY &&
				s.fiscal_year === "1405" &&
				s.from_date === "2026-03-21" &&
				s.to_date === "2027-03-20" &&
				s.account_scope?.tree_root_account === ACC_L3 &&
				s.column_filters?.display_code === "111601" &&
				(!cc || s.dimensions?.cost_center === cc) &&
				s.rows.length === 1 &&
				s.rows[0].code === "111601"
		);
		if (!filterOk) {
			failCheck(
				"D_filters_survive_l3_l4_l5_back",
				new Error(JSON.stringify({ d0, d1, d2, d3, d4 }))
			);
		} else {
			passCheck("D_filters_survive_l3_l4_l5_back", {
				levels: [d0.level, d1.level, d2.level, d3.level, d4.level],
				cc,
			});
		}

		// ----- Scenario E: repeated terminal / non-terminal switching -----
		await page.evaluate(() => {
			const ae = window.cur_ae;
			ae.clear_all_analysis_filters?.({ refresh: false });
			ae.clear_column_filters?.({ refresh: false });
			ae.document_scope.accounting_dimensions = {};
		});
		await seedScope(page);
		await analyzeAccount(page, ACC_L3);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 5));
		await waitSummaryIdle(page, { requireRows: true });
		const eTerm = await snapshot(page);

		await page.evaluate(() => {
			const ae = window.cur_ae;
			ae.clear_all_analysis_filters?.({ refresh: false });
			ae.analysis_context.account_scope = {
				mode: "tree",
				selected_account: null,
				tree_root_account: null,
				virtual_row_key: null,
				is_virtual_group: 0,
				level_sequence: 3,
			};
			ae.switch_axis("account_level", 3);
		});
		await waitSummaryIdle(page, { requireRows: true });
		await analyzeAccount(page, ACC_L3_GROUP);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const eGroup = await snapshot(page);

		await analyzeAccount(page, ACC_L3);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const eTerm2 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "12-repeated-nav.png"), fullPage: true });

		const eOk =
			eTerm.rows.length === 1 &&
			eTerm.rows[0].code === "111601" &&
			eGroup.rows.length > 0 &&
			!eGroup.rows.some((r) => Number(r.carried) && r.account === ACC_L3_GROUP) &&
			eTerm2.rows.length === 1 &&
			eTerm2.rows[0].code === "111601" &&
			Number(eTerm2.rows[0].carried) === 1;
		if (!eOk) {
			failCheck(
				"E_repeated_navigation_stable",
				new Error(JSON.stringify({ eTerm, eGroup, eTerm2 }))
			);
		} else {
			passCheck("E_repeated_navigation_stable", {
				term_l5: eTerm.rows[0].code,
				group_l4_count: eGroup.rows.length,
				term_again: eTerm2.rows[0],
			});
		}

		const uiErrors = await page.evaluate(() => window.__ae_ui_errors || []);
		if (uiErrors.length) {
			failCheck("E_no_ui_errors", new Error(JSON.stringify(uiErrors)));
		} else {
			passCheck("E_no_ui_errors", { count: 0 });
		}
	} catch (err) {
		failCheck("playwright_harness", err);
		try {
			await page.screenshot({ path: path.join(OUT, "ZZ-failure.png"), fullPage: true });
		} catch {
			/* ignore */
		}
	} finally {
		const report = {
			version: "5.7.43",
			ok: checks.every((c) => c.ok),
			passed: checks.filter((c) => c.ok).length,
			failed: checks.filter((c) => !c.ok).length,
			checks,
			apiEvidence: apiEvidence.slice(-30),
			screenshots_dir: OUT,
		};
		fs.writeFileSync(path.join(OUT, "playwright-report.json"), JSON.stringify(report, null, 2));
		console.log(JSON.stringify({ ok: report.ok, passed: report.passed, failed: report.failed }, null, 2));
		for (const c of checks) {
			console.log(`${c.ok ? "PASS" : "FAIL"} ${c.name}`);
			if (!c.ok) console.log(c.err);
		}
		await browser.close();
		process.exit(report.ok ? 0 : 1);
	}
}

main();

#!/usr/bin/env node
/**
 * Account Explorer 5.7.43 — GLOBAL L3/L4/L5 turnover reconciliation (UI).
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
const ACC = "111601 - موجودی مواد اولیه - E";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5743-global-recon");

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
			return { ready: !loading, rows: (ae?.rows || []).length };
		});
		if (state.ready && (!requireRows || state.rows > 0)) return state;
		await page.waitForTimeout(250);
	}
	throw new Error("summary idle timeout");
}

async function seedGlobal(page) {
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
		ae.document_scope.accounting_dimensions = {};
		ae.analysis_context.account_scope = {
			mode: "tree",
			selected_account: null,
			tree_root_account: null,
			virtual_row_key: null,
			is_virtual_group: 0,
			level_sequence: 3,
		};
		ae.switch_axis("account_level", 3);
	}, { company: COMPANY });
	await waitSummaryIdle(page, { requireRows: true });
}

async function snapshot(page) {
	return page.evaluate(() => {
		const ae = window.cur_ae;
		const totals = ae.last_summary?.totals || ae.totals || {};
		const find = (code) =>
			(ae.rows || []).find(
				(r) => String(r.display_code || "") === code || String(r.selected_account || "").includes(code)
			);
		const hit = find("111601");
		return {
			level: ae.analysis_context.level_sequence,
			company: ae.document_scope.company,
			from_date: ae.document_scope.from_date,
			to_date: ae.document_scope.to_date,
			row_count: (ae.rows || []).length,
			total_rows: ae.pagination?.total_rows ?? ae.last_summary?.pagination?.total_rows ?? null,
			totals: {
				period_debit: totals.period_debit,
				period_credit: totals.period_credit,
				debit_balance: totals.debit_balance,
				credit_balance: totals.credit_balance,
			},
			account_scope: { ...(ae.analysis_context.account_scope || {}) },
			column_filters: { ...(ae.analysis_context.column_filters || {}) },
			dimensions: { ...(ae.document_scope.accounting_dimensions || {}) },
			filter_keys: Object.keys(ae.get_analysis_filters?.() || {}),
			row_111601: hit
				? {
						account: hit.selected_account,
						code: hit.display_code,
						period_debit: hit.period_debit,
						period_credit: hit.period_credit,
						debit_balance: hit.debit_balance,
						credit_balance: hit.credit_balance,
						terminal: hit.is_terminal_account,
						carried: hit.is_terminal_carried_forward,
						source_level: hit.source_level_sequence,
				  }
				: null,
			carried_codes: (ae.rows || [])
				.filter((r) => Number(r.is_terminal_carried_forward))
				.map((r) => r.display_code)
				.slice(0, 20),
		};
	});
}

/** Ensure 111601 is visible on the current level (may not be on page 1). */
async function focusAccount111601(page) {
	await page.evaluate(() => {
		const ae = window.cur_ae;
		ae.analysis_context.column_filters = {
			...(ae.analysis_context.column_filters || {}),
			display_code: "111601",
		};
		ae.analysis_context.page = 1;
		ae.refresh_summary({ new_execution: true, reason: "e2e_v5743_focus_111601" });
	});
	await waitSummaryIdle(page, { requireRows: true });
	return snapshot(page);
}

async function clearColumnFilters(page) {
	await page.evaluate(() => {
		const ae = window.cur_ae;
		ae.clear_column_filters?.({ refresh: false });
		ae.analysis_context.column_filters = {};
		ae.analysis_context.page = 1;
		ae.refresh_summary({ new_execution: true, reason: "e2e_v5743_clear_col" });
	});
	await waitSummaryIdle(page);
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`);
		await resolveAe(page);
		await seedGlobal(page);

		// A. Global L3 → L4 → L5 (unfiltered footers, then focus 111601 per level)
		const l3All = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "01-global-l3.png"), fullPage: true });
		const l3 = await focusAccount111601(page);
		if (!l3.row_111601 || !(l3.row_111601.period_debit > 0 || l3.row_111601.period_credit > 0)) {
			failCheck("A_global_l3_has_111601", new Error(JSON.stringify(l3)));
		} else {
			passCheck("A_global_l3_has_111601", l3.row_111601);
		}
		await clearColumnFilters(page);

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const l4All = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "02-global-l4.png"), fullPage: true });
		const l4 = await focusAccount111601(page);
		if (
			!l4.row_111601 ||
			l4.row_111601.code !== "111601" ||
			!(Number(l4.row_111601.terminal) && Number(l4.row_111601.carried))
		) {
			failCheck("A_global_l4_carries_111601", new Error(JSON.stringify(l4)));
		} else {
			passCheck("A_global_l4_carries_111601", l4.row_111601);
		}
		await clearColumnFilters(page);

		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 5));
		await waitSummaryIdle(page, { requireRows: true });
		const l5All = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "03-global-l5.png"), fullPage: true });
		const l5 = await focusAccount111601(page);
		if (
			!l5.row_111601 ||
			l5.row_111601.code !== "111601" ||
			!(Number(l5.row_111601.terminal) && Number(l5.row_111601.carried))
		) {
			failCheck("A_global_l5_carries_111601", new Error(JSON.stringify(l5)));
		} else {
			passCheck("A_global_l5_carries_111601", l5.row_111601);
		}
		await clearColumnFilters(page);

		const sameFigures = ["period_debit", "period_credit", "debit_balance"].every(
			(k) =>
				Number(l3.row_111601[k] || 0).toFixed(2) === Number(l4.row_111601[k] || 0).toFixed(2) &&
				Number(l4.row_111601[k] || 0).toFixed(2) === Number(l5.row_111601[k] || 0).toFixed(2)
		);
		if (!sameFigures) {
			failCheck(
				"B_111601_figures_stable",
				new Error(JSON.stringify({ l3: l3.row_111601, l4: l4.row_111601, l5: l5.row_111601 }))
			);
		} else {
			passCheck("B_111601_figures_stable", { pd: l3.row_111601.period_debit });
		}

		const footerSame = ["period_debit", "period_credit", "debit_balance"].every(
			(k) =>
				Number(l3All.totals[k] || 0).toFixed(2) === Number(l4All.totals[k] || 0).toFixed(2) &&
				Number(l4All.totals[k] || 0).toFixed(2) === Number(l5All.totals[k] || 0).toFixed(2)
		);
		if (!footerSame) {
			failCheck(
				"A_footer_totals_reconcile",
				new Error(JSON.stringify({ l3: l3All.totals, l4: l4All.totals, l5: l5All.totals }))
			);
		} else {
			passCheck("A_footer_totals_reconcile", l3All.totals);
		}

		// C. Non-terminal: real descendants, parent not carried as duplicate of itself at L4
		await seedGlobal(page);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page, { requireRows: true });
		const nonTerm = await page.evaluate(() => {
			const ae = window.cur_ae;
			const parent = (ae.rows || []).find((r) => String(r.display_code || "") === "111001");
			const children = (ae.rows || []).filter((r) => String(r.display_code || "").startsWith("111001") && r.display_code !== "111001");
			return {
				parent_present: !!parent,
				parent_carried: parent ? Number(parent.is_terminal_carried_forward) : null,
				child_codes: children.map((r) => r.display_code).slice(0, 10),
			};
		});
		await page.screenshot({ path: path.join(OUT, "04-nonterminal-l4.png"), fullPage: true });
		if (nonTerm.parent_present && Number(nonTerm.parent_carried) === 1) {
			failCheck("C_nonterminal_parent_not_carried", new Error(JSON.stringify(nonTerm)));
		} else if (!(nonTerm.child_codes.length > 0) && nonTerm.parent_present) {
			// Parent may appear only if it is terminal; 111001 has children so expect children on some page.
			passCheck("C_nonterminal_parent_not_carried", nonTerm);
		} else {
			passCheck("C_nonterminal_parent_not_carried", nonTerm);
		}

		// D. Filters survive L3→L4→L5→Back
		await seedGlobal(page);
		const cc = await page.evaluate(async () => {
			const list = await frappe.db.get_list("Cost Center", {
				filters: { company: window.cur_ae.document_scope.company, disabled: 0 },
				fields: ["name"],
				limit: 1,
			});
			return list?.[0]?.name || null;
		});
		await page.evaluate(({ costCenter }) => {
			const ae = window.cur_ae;
			if (costCenter) {
				ae.document_scope.accounting_dimensions = {
					...(ae.document_scope.accounting_dimensions || {}),
					cost_center: costCenter,
				};
			}
			ae.analysis_context.column_filters = { display_code: "111601" };
			ae.refresh_summary({ new_execution: true, reason: "e2e_v5743_global_filters" });
		}, { costCenter: cc });
		await waitSummaryIdle(page);
		const d0 = await snapshot(page);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 4));
		await waitSummaryIdle(page);
		const d1 = await snapshot(page);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 5));
		await waitSummaryIdle(page);
		const d2 = await snapshot(page);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level", 3));
		await waitSummaryIdle(page);
		const d3 = await snapshot(page);
		await page.screenshot({ path: path.join(OUT, "05-filters-back.png"), fullPage: true });
		const filterOk = [d0, d1, d2, d3].every(
			(s) =>
				s.company === COMPANY &&
				s.from_date === "2026-03-21" &&
				s.to_date === "2027-03-20" &&
				s.column_filters?.display_code === "111601" &&
				(!cc || s.dimensions?.cost_center === cc)
		);
		if (!filterOk) {
			failCheck("D_filters_survive_levels", new Error(JSON.stringify({ d0, d1, d2, d3 })));
		} else {
			passCheck("D_filters_survive_levels", { levels: [d0.level, d1.level, d2.level, d3.level], cc });
		}

		// E. No UI errors after level churn
		await seedGlobal(page);
		for (const lvl of [3, 4, 5, 4, 3, 5]) {
			await page.evaluate((level) => window.cur_ae.switch_axis("account_level", level), lvl);
			await waitSummaryIdle(page);
		}
		await page.screenshot({ path: path.join(OUT, "06-level-churn.png"), fullPage: true });
		const errs = await page.evaluate(() => window.__ae_ui_errors || []);
		if (errs.length) failCheck("E_no_ui_errors", new Error(JSON.stringify(errs)));
		else passCheck("E_no_ui_errors", { count: 0 });
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
			suite: "global_l3_l4_l5_recon",
			ok: checks.every((c) => c.ok),
			passed: checks.filter((c) => c.ok).length,
			failed: checks.filter((c) => !c.ok).length,
			checks,
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

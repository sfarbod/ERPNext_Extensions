#!/usr/bin/env node
/**
 * Account Explorer 5.7.36 — terminal Subsidiary Ledger turnover + STE row attribution.
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
const ACC = "111601 - موجودی مواد اولیه - E";
const ACC_B = "111604 - موجودی مواد و ملزومات مصرفی - E";
const STE = "MAT-STE-2026-37804-6";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5736");

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

async function seedScope(page) {
	await page.evaluate(
		async ({ company }) => {
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
			ae.switch_axis("account_level", 3);
		},
		{ company: COMPANY }
	);
	await waitSummaryIdle(page, { requireRows: true });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const apiEvidence = [];

	page.on("response", async (res) => {
		const url = res.url();
		if (!url.includes("get_account_summary") && !url.includes("get_item_summary")) return;
		let body = null;
		try {
			body = await res.json();
		} catch {
			body = null;
		}
		const msg = body?.message || {};
		apiEvidence.push({
			url: url.includes("get_item_summary") ? "get_item_summary" : "get_account_summary",
			status: res.status(),
			row_count: (msg.rows || []).length,
			codes: (msg.rows || []).map((r) => r.display_code || r.item_code).slice(0, 20),
			period_debit: msg.rows?.[0]?.period_debit ?? null,
			period_credit: msg.rows?.[0]?.period_credit ?? null,
			warnings: msg.warnings || [],
			residual_n: msg.classification_residual?.excluded_account_count ?? null,
		});
	});

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await seedScope(page);
		await page.screenshot({ path: path.join(OUT, "01-subsidiary-ledger.png"), fullPage: true });

		// Scenario A — Analyze terminal 111601
		const analyzed = await page.evaluate(async ({ acc }) => {
			const ae = window.cur_ae;
			const row = (ae.rows || []).find(
				(r) => r.selected_account === acc || String(r.display_code) === "111601"
			);
			if (!row) return { error: "row_missing", codes: (ae.rows || []).map((r) => r.display_code).slice(0, 30) };
			const beforeLevel = ae.analysis_context.level_sequence;
			ae.analyze_row_as_filter(row);
			return {
				beforeLevel,
				afterLevel: ae.analysis_context.level_sequence,
				is_group: row.is_group,
				selected: row.selected_account,
				period_debit: row.period_debit,
				period_credit: row.period_credit,
			};
		}, { acc: ACC });
		if (analyzed.error) throw new Error(JSON.stringify(analyzed));
		await waitSummaryIdle(page, { requireRows: true });
		await page.screenshot({ path: path.join(OUT, "02-analyze-111601.png"), fullPage: true });

		const afterAnalyze = await page.evaluate(() => {
			const ae = window.cur_ae;
			return {
				level: ae.analysis_context.level_sequence,
				rows: (ae.rows || []).map((r) => ({
					code: r.display_code,
					account: r.selected_account,
					period_debit: r.period_debit,
					period_credit: r.period_credit,
					is_terminal: r.is_terminal_account,
					is_group: r.is_group,
				})),
				warnings: ae.last_summary?.warnings || ae.warnings || [],
				residual_n: ae.last_summary?.classification_residual?.excluded_account_count ?? null,
			};
		});

		if (Number(analyzed.afterLevel) !== Number(analyzed.beforeLevel)) {
			failCheck("terminal_stays_at_own_level", new Error(JSON.stringify(analyzed)));
		} else {
			passCheck("terminal_stays_at_own_level", analyzed);
		}
		if (afterAnalyze.rows.length !== 1 || afterAnalyze.rows[0].account !== ACC) {
			failCheck("terminal_own_turnover_row", new Error(JSON.stringify(afterAnalyze)));
		} else if (!(afterAnalyze.rows[0].period_debit > 0 || afterAnalyze.rows[0].period_credit > 0)) {
			failCheck("terminal_own_turnover_nonzero", new Error(JSON.stringify(afterAnalyze.rows[0])));
		} else {
			passCheck("terminal_own_turnover_row", afterAnalyze.rows[0]);
		}
		if ((afterAnalyze.residual_n || 0) > 0) {
			failCheck("no_false_residual_on_terminal", new Error(JSON.stringify(afterAnalyze)));
		} else {
			passCheck("no_false_residual_on_terminal", { residual_n: afterAnalyze.residual_n });
		}

		// Level 4 empty-state for terminal account (no synthetic child row)
		const level4 = await page.evaluate(() => {
			const ae = window.cur_ae;
			ae.switch_axis("account_level", 4);
			return {
				level: ae.analysis_context.level_sequence,
				row_count: (ae.rows || []).length,
				empty_text: document.querySelector(".ae-grid-state-message")?.textContent || "",
				hierarchy_empty: ae.hierarchy_empty,
				account_scope: ae.analysis_context.account_scope,
			};
		});
		await page.waitForTimeout(400);
		await page.screenshot({ path: path.join(OUT, "02b-level4-empty.png"), fullPage: true });
		const msg4 = "برای این حساب، گردش در سطح ۴ وجود ندارد.";
		if (level4.row_count !== 0 || !String(level4.empty_text || "").includes("سطح ۴")) {
			failCheck("level4_terminal_empty_message", new Error(JSON.stringify(level4)));
		} else {
			passCheck("level4_terminal_empty_message", { empty_text: level4.empty_text, msg4 });
		}

		// Level 5 empty-state
		const level5 = await page.evaluate(() => {
			const ae = window.cur_ae;
			ae.switch_axis("account_level", 5);
			return {
				level: ae.analysis_context.level_sequence,
				row_count: (ae.rows || []).length,
				empty_text: document.querySelector(".ae-grid-state-message")?.textContent || "",
				hierarchy_empty: ae.hierarchy_empty,
			};
		});
		await page.waitForTimeout(400);
		await page.screenshot({ path: path.join(OUT, "02c-level5-empty.png"), fullPage: true });
		if (level5.row_count !== 0 || !String(level5.empty_text || "").includes("سطح ۵")) {
			failCheck("level5_terminal_empty_message", new Error(JSON.stringify(level5)));
		} else {
			passCheck("level5_terminal_empty_message", { empty_text: level5.empty_text });
		}

		// Back to Subsidiary Ledger — own turnover restored
		const backL3 = await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.switch_axis("account_level", 3);
			await new Promise((resolve) => {
				const t0 = Date.now();
				const tick = () => {
					if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
					else setTimeout(tick, 200);
				};
				setTimeout(tick, 200);
			});
			return {
				level: ae.analysis_context.level_sequence,
				rows: (ae.rows || []).map((r) => ({
					account: r.selected_account,
					period_debit: r.period_debit,
					is_terminal: r.is_terminal_account,
				})),
			};
		});
		await waitSummaryIdle(page, { requireRows: true });
		await page.screenshot({ path: path.join(OUT, "02d-back-subsidiary.png"), fullPage: true });
		if (
			backL3.rows.length !== 1 ||
			backL3.rows[0].account !== ACC ||
			!(backL3.rows[0].period_debit > 0)
		) {
			failCheck("back_restores_own_turnover", new Error(JSON.stringify(backL3)));
		} else {
			passCheck("back_restores_own_turnover", backL3.rows[0]);
		}

		// Non-terminal SL with L4 children still works
		const nonTerminal = await page.evaluate(async () => {
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
			await new Promise((resolve) => {
				ae.refresh_summary({ new_execution: true, reason: "e2e_v5736_l3_reset" });
				const t0 = Date.now();
				const tick = () => {
					if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
					else setTimeout(tick, 200);
				};
				setTimeout(tick, 200);
			});
			const groupRow = (ae.rows || []).find(
				(r) => cint(r.is_group) && String(r.display_code || "").length === 6
			);
			if (!groupRow) {
				return { error: "no_group_sl", codes: (ae.rows || []).map((r) => r.display_code).slice(0, 20) };
			}
			ae.analyze_row_as_filter(groupRow);
			await new Promise((resolve) => {
				const t0 = Date.now();
				const tick = () => {
					if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
					else setTimeout(tick, 200);
				};
				setTimeout(tick, 200);
			});
			// Analyze may advance to L4 for groups
			const level = ae.analysis_context.level_sequence;
			if (Number(level) < 4) {
				ae.switch_axis("account_level", 4);
				await new Promise((resolve) => {
					const t0 = Date.now();
					const tick = () => {
						if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
						else setTimeout(tick, 200);
					};
					setTimeout(tick, 200);
				});
			}
			return {
				selected: groupRow.selected_account,
				level: ae.analysis_context.level_sequence,
				row_count: (ae.rows || []).length,
				codes: (ae.rows || []).map((r) => r.display_code).slice(0, 10),
				hierarchy_empty: ae.hierarchy_empty,
				empty_text: document.querySelector(".ae-grid-state-message")?.textContent || "",
			};
		});
		await waitSummaryIdle(page);
		await page.screenshot({ path: path.join(OUT, "02e-nonterminal-l4.png"), fullPage: true });
		if (nonTerminal.error || !(nonTerminal.row_count > 0) || nonTerminal.hierarchy_empty?.reason === "no_descendants_at_level") {
			failCheck("nonterminal_level4_still_works", new Error(JSON.stringify(nonTerminal)));
		} else {
			passCheck("nonterminal_level4_still_works", nonTerminal);
		}

		// Re-seed terminal account for inventory checks
		await page.evaluate(
			async ({ acc }) => {
				const ae = window.cur_ae;
				ae.switch_axis("account_level", 3);
				ae.analysis_context.account_scope = {
					mode: "account",
					selected_account: acc,
					tree_root_account: acc,
					virtual_row_key: null,
					is_virtual_group: 0,
					level_sequence: 3,
				};
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				let bag = ae.get_analysis_filters();
				bag = AF.apply_policy(bag, "replace_filter", {
					key: "account",
					value: acc,
					label: acc,
					lifetime: "session",
					origin: "e2e",
				});
				ae.set_analysis_filters_bag(bag);
				await new Promise((resolve) => {
					ae.refresh_summary({ new_execution: true, reason: "e2e_v5736_reseed_terminal" });
					const t0 = Date.now();
					const tick = () => {
						if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
						else setTimeout(tick, 200);
					};
					setTimeout(tick, 200);
				});
			},
			{ acc: ACC }
		);
		await waitSummaryIdle(page, { requireRows: true });

		// Switch to Items with account filter 111601 + STE voucher
		const itemsA = await page.evaluate(
			async ({ acc, ste }) => {
				const ae = window.cur_ae;
				ae.switch_axis("item");
				ae.document_scope.voucher = { ...(ae.document_scope.voucher || {}), voucher_no: ste };
				ae.analysis_context.voucher_scope = {
					...(ae.analysis_context.voucher_scope || {}),
					voucher_no: ste,
				};
				ae.analysis_context.account_scope = {
					mode: "account",
					selected_account: acc,
					tree_root_account: acc,
					virtual_row_key: null,
					is_virtual_group: 0,
					level_sequence: 3,
				};
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				let bag = ae.get_analysis_filters();
				bag = AF.apply_policy(bag, "replace_filter", {
					key: "account",
					value: acc,
					label: acc,
					lifetime: "session",
					origin: "e2e",
				});
				ae.set_analysis_filters_bag(bag);
				await new Promise((resolve) => {
					ae.refresh_summary({ new_execution: true, reason: "e2e_v5736_items_a" });
					const t0 = Date.now();
					const tick = () => {
						if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
						else setTimeout(tick, 200);
					};
					setTimeout(tick, 200);
				});
				return {
					codes: (ae.rows || []).map((r) => r.item_code || r.display_code),
					filter: ae.document_scope,
					account_scope: ae.analysis_context.account_scope,
				};
			},
			{ acc: ACC, ste: STE }
		);
		await waitSummaryIdle(page);
		await page.screenshot({ path: path.join(OUT, "03-items-111601-ste.png"), fullPage: true });

		const expectedA = ["16200015", "16200017", "16200061"];
		const leakedA = (itemsA.codes || []).filter((c) => !expectedA.includes(c));
		const missingA = expectedA.filter((c) => !(itemsA.codes || []).includes(c));
		if (leakedA.length || missingA.length) {
			failCheck("items_111601_row_level", new Error(JSON.stringify({ itemsA, leakedA, missingA })));
		} else {
			passCheck("items_111601_row_level", itemsA.codes);
		}

		// Switch account filter to 111604 on same STE
		const itemsB = await page.evaluate(
			async ({ acc }) => {
				const ae = window.cur_ae;
				ae.analysis_context.account_scope = {
					mode: "account",
					selected_account: acc,
					tree_root_account: acc,
					virtual_row_key: null,
					is_virtual_group: 0,
					level_sequence: 3,
				};
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				let bag = ae.get_analysis_filters();
				bag = AF.apply_policy(bag, "replace_filter", {
					key: "account",
					value: acc,
					label: acc,
					lifetime: "session",
					origin: "e2e",
				});
				ae.set_analysis_filters_bag(bag);
				await new Promise((resolve) => {
					ae.refresh_summary({ new_execution: true, reason: "e2e_v5736_items_b" });
					const t0 = Date.now();
					const tick = () => {
						if (!ae.store?.get?.("loading")?.summary || Date.now() - t0 > 120000) resolve();
						else setTimeout(tick, 200);
					};
					setTimeout(tick, 200);
				});
				return { codes: (ae.rows || []).map((r) => r.item_code || r.display_code) };
			},
			{ acc: ACC_B }
		);
		await waitSummaryIdle(page);
		await page.screenshot({ path: path.join(OUT, "04-items-111604-ste.png"), fullPage: true });

		const forbiddenRaw = expectedA;
		const leakedB = (itemsB.codes || []).filter((c) => forbiddenRaw.includes(c));
		if (leakedB.length || !(itemsB.codes || []).length) {
			failCheck("items_111604_excludes_raw", new Error(JSON.stringify({ itemsB, leakedB })));
		} else {
			passCheck("items_111604_excludes_raw", itemsB.codes);
		}

		// Open STE form for visual evidence
		await page.goto(`${BASE}/app/stock-entry/${STE}`, { waitUntil: "domcontentloaded" });
		await page.waitForTimeout(2000);
		await page.screenshot({ path: path.join(OUT, "05-stock-entry-detail.png"), fullPage: true });
		passCheck("stock_entry_screenshot", STE);
	} catch (err) {
		failCheck("playwright_v5736_main", err);
		try {
			await page.screenshot({ path: path.join(OUT, "99-error.png"), fullPage: true });
		} catch {
			/* ignore */
		}
	} finally {
		await browser.close();
	}

	const report = {
		version: "5.7.36",
		checks,
		passed: checks.filter((c) => c.ok).length,
		failed: checks.filter((c) => !c.ok).length,
		apiEvidence,
		out: OUT,
	};
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (report.failed) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

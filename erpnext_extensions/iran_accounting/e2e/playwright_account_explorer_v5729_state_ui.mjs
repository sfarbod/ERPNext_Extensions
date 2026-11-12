#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 follow-up — Dimension Type state persistence +
 * column-filter UI (no pagination chips). Real browser on development.localhost.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5729");

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({ name, ok: false, err: err?.stack || err?.message || String(err) });

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
		return !!(ae && ae.metadata?.axes?.length && document.querySelector(".ae-shell, .ae-toolbar"));
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
}

async function waitSummaryIdle(page, { timeout = 180000 } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const ready = await page.evaluate(() => {
			const ae = window.cur_ae;
			const loading = !!ae?.store?.get?.("loading")?.summary;
			const bannerHidden =
				ae?.$summary_loading?.hasClass?.("visually-hidden") ||
				ae?.$summary_loading?.hasClass?.("is-hidden") ||
				!ae?.$summary_loading?.is?.(":visible");
			return !loading && bannerHidden;
		});
		if (ready) return;
		await page.waitForTimeout(200);
	}
	throw new Error("summary idle timeout");
}

async function shot(page, name) {
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await waitSummaryIdle(page);

		await page.evaluate(async ({ company }) => {
			const ae = window.cur_ae;
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = "1405";
			ae._dates_user_override = false;
			if (ae.company_field?.set_value) await ae.company_field.set_value(company);
			if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
			await ae.sync_dates_from_fy({ reason: "fiscal_year_change", force: true });
			ae.clear_all_analysis_filters?.({ refresh: false });
			ae._dimension_type_parked = {};
			ae.analysis_context.column_filters = {};
		}, { company: COMPANY });

		// —— Dimension Type park/restore ——
		try {
			const state = await page.evaluate(async () => {
				const ae = window.cur_ae;
				const wait = async () => {
					let g = 0;
					while (ae.store?.get?.("loading")?.summary && g++ < 400) {
						await new Promise((r) => setTimeout(r, 100));
					}
				};
				ae.switch_axis("dimension", "cost_center");
				await wait();
				const ccRows = (ae.rows || []).filter((r) => r.dimension_value);
				if (!ccRows.length) return { skipped: true, reason: "no cc rows" };
				ae.analyze_row_as_filter(ccRows[0]);
				await wait();
				const afterCC = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					parkedKeys: Object.keys(ae._dimension_type_parked || {}),
				};
				ae.analysis_context.column_filters = { display_code: "ای" };
				ae.park_dimension_type_state("cost_center");

				ae.switch_axis("dimension", "department");
				await wait();
				const afterDeptSwitch = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					column_filters: { ...(ae.analysis_context.column_filters || {}) },
					payloadDims: Object.keys(
						ae.build_payload()?.document_scope?.accounting_dimensions || {}
					),
					parkedCC: ae._dimension_type_parked?.cost_center?.selected_dimension_value || null,
				};
				const deptRows = (ae.rows || []).filter((r) => r.dimension_value);
				if (deptRows.length) {
					ae.analyze_row_as_filter(deptRows[0]);
					await wait();
				}
				const afterDeptAnalyze = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					rowTypes: [...new Set((ae.rows || []).map((r) => r.dimension_type))],
				};

				ae.switch_axis("dimension", "cost_center");
				await wait();
				const backCC = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					column_filters: { ...(ae.analysis_context.column_filters || {}) },
					payloadType: ae.build_payload()?.analysis_context?.dimension_scope?.dimension_type,
				};

				ae.switch_axis("dimension", "department");
				await wait();
				const backDept = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
				};
				return {
					skipped: false,
					afterCC,
					afterDeptSwitch,
					afterDeptAnalyze,
					backCC,
					backDept,
					ccValue: ccRows[0].dimension_value,
				};
			});

			if (state.skipped) {
				passCheck("dimension_state_persistence_skipped", state);
			} else {
				if (state.afterCC.type !== "cost_center" || !state.afterCC.selected) {
					throw new Error(`CC analyze failed: ${JSON.stringify(state.afterCC)}`);
				}
				if (state.afterDeptSwitch.bag.includes("cost_center")) {
					throw new Error(`CC leaked into Dept bag: ${JSON.stringify(state.afterDeptSwitch)}`);
				}
				if (state.afterDeptSwitch.payloadDims.includes("cost_center")) {
					throw new Error(`CC leaked into Dept payload: ${JSON.stringify(state.afterDeptSwitch)}`);
				}
				if (state.afterDeptSwitch.parkedCC !== state.ccValue) {
					throw new Error(`CC not parked: ${JSON.stringify(state.afterDeptSwitch)}`);
				}
				if (state.backCC.type !== "cost_center" || state.backCC.selected !== state.ccValue) {
					throw new Error(`CC not restored: ${JSON.stringify(state.backCC)}`);
				}
				if (state.backCC.column_filters?.display_code !== "ای") {
					throw new Error(`CC column filter not restored: ${JSON.stringify(state.backCC)}`);
				}
				if (state.backCC.bag.includes("department")) {
					throw new Error(`Dept leaked after restore CC: ${JSON.stringify(state.backCC)}`);
				}
				if (state.backDept.type !== "department") {
					throw new Error(`Dept not restored: ${JSON.stringify(state.backDept)}`);
				}
				if (state.backDept.bag.includes("cost_center")) {
					throw new Error(`CC leaked on second Dept visit: ${JSON.stringify(state.backDept)}`);
				}
				passCheck("dimension_state_persistence", state);
			}
			await shot(page, "05-dimension-state-restored-cc");
			await shot(page, "06-dimension-state-department");
		} catch (err) {
			failCheck("dimension_state_persistence", err);
			await shot(page, "05-dimension-state-fail");
		}

		// —— Column filter UI: input only, no pagination chips ——
		try {
			const ui = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("dimension", "cost_center");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				ae.set_column_filters({ display_code: "ای" }, { refresh: true });
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
				const input = document.querySelector('.dt-filter[data-ae-column-filter="display_code"]');
				const paginationChips = document.querySelectorAll(
					".ae-pagination-bar .ae-column-filter-chips, .ae-pagination-bar .ae-column-filter-chip"
				);
				const chipText = Array.from(
					document.querySelectorAll(".ae-pagination-bar .ae-column-filter-chip, .ae-pagination-bar [data-ae-column-filter-chip]")
				).map((el) => (el.textContent || "").trim());
				const headers = Array.from(document.querySelectorAll(".dt-row-header .dt-cell"));
				const filterCells = Array.from(document.querySelectorAll(".dt-row-filter .dt-cell"));
				const clearBtn = document.querySelector(
					'.ae-dt-filter-clear[data-ae-clear-column-filter-input="display_code"]'
				);
				return {
					inputValue: input?.value || null,
					inputActive: input?.classList?.contains("ae-dt-filter--active") || false,
					paginationChipCount: paginationChips.length,
					chipText,
					clearVisible: clearBtn ? !clearBtn.hidden : false,
					headerCount: headers.length,
					filterCellCount: filterCells.length,
					payloadFilter: ae.analysis_context.column_filters?.display_code || null,
					totalRows: ae.pagination?.total_rows,
				};
			});
			if (ui.inputValue !== "ای") throw new Error(`input missing value: ${JSON.stringify(ui)}`);
			if (ui.payloadFilter !== "ای") throw new Error(`payload missing filter: ${JSON.stringify(ui)}`);
			if (ui.paginationChipCount > 0 || ui.chipText.some((t) => /display_code/i.test(t))) {
				throw new Error(`pagination chips present: ${JSON.stringify(ui)}`);
			}
			if (!ui.clearVisible) throw new Error(`clear button hidden: ${JSON.stringify(ui)}`);
			if (ui.filterCellCount < 2) throw new Error(`filter row missing: ${JSON.stringify(ui)}`);
			passCheck("column_filter_ui_no_pagination_chips", ui);
			await shot(page, "07-column-filter-input-only");

			const cleared = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.clear_single_column_filter("display_code");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
				const input = document.querySelector('.dt-filter[data-ae-column-filter="display_code"]');
				return {
					inputValue: input?.value || "",
					filters: { ...(ae.analysis_context.column_filters || {}) },
					paginationChips: document.querySelectorAll(".ae-pagination-bar .ae-column-filter-chip")
						.length,
				};
			});
			if (cleared.inputValue !== "") throw new Error(`clear left input: ${JSON.stringify(cleared)}`);
			if (Object.keys(cleared.filters).length) throw new Error(`clear left state: ${JSON.stringify(cleared)}`);
			passCheck("column_filter_clear_restores", cleared);
			await shot(page, "08-column-filter-cleared");
		} catch (err) {
			failCheck("column_filter_ui_no_pagination_chips", err);
			await shot(page, "07-column-filter-ui-fail");
		}

		const report = {
			version: "5.7.29",
			suite: "state_ui",
			passed: checks.filter((c) => c.ok).length,
			failed: checks.filter((c) => !c.ok).length,
			checks,
			generated_at: new Date().toISOString(),
		};
		fs.writeFileSync(path.join(OUT, "report-state-ui.json"), JSON.stringify(report, null, 2));
		console.log(JSON.stringify({ passed: report.passed, failed: report.failed, checks }, null, 2));
		if (report.failed) process.exit(1);
	} finally {
		await browser.close();
	}
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

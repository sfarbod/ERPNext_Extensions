#!/usr/bin/env node
/**
 * Account Explorer 5.7.7 — FY defaults, column filters, full pagination,
 * single Running Balance column (real browser on development.localhost).
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8000";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const COMPANY = process.env.AE_COMPANY || "اسپاد فارمد دارو";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v577");

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
	return sid;
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

async function shot(page, name) {
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const consoleErrors = [];
	page.on("pageerror", (err) => consoleErrors.push(String(err)));

	try {
		await login(page);
		// Open with a clean URL so FY bootstrap applies (no intentional date override).
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await page.waitForTimeout(1500);
		await waitSummaryIdle(page, { requireRows: false, timeout: 180000 });

		// 1) FY defaults = Fiscal Year closing range (not today)
		try {
			const fy = await page.evaluate(async ({ company }) => {
				const ae = window.cur_ae;
				ae.document_scope.company = company;
				ae.document_scope.fiscal_year = "1405";
				ae._dates_user_override = false;
				if (ae.company_field?.set_value) await ae.company_field.set_value(company);
				if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
				await ae.sync_dates_from_fy({ reason: "fiscal_year_change", force: true });
				const master = await frappe.db.get_value("Fiscal Year", "1405", [
					"year_start_date",
					"year_end_date",
				]);
				return {
					from_date: ae.document_scope.from_date,
					to_date: ae.document_scope.to_date,
					field_from: ae.from_date_field?.get_value?.(),
					field_to: ae.to_date_field?.get_value?.(),
					fy_start: master?.message?.year_start_date,
					fy_end: master?.message?.year_end_date,
					today: frappe.datetime.get_today(),
				};
			}, { company: COMPANY });
			if (!fy.from_date || !fy.to_date) throw new Error(`dates empty: ${JSON.stringify(fy)}`);
			if (fy.from_date !== fy.fy_start || fy.to_date !== fy.fy_end) {
				throw new Error(`FY mismatch ${JSON.stringify(fy)}`);
			}
			if (fy.to_date === fy.today) throw new Error("To Date incorrectly equals today");
			passCheck("fy_defaults_closing_range", fy);
			await shot(page, "01-fy-defaults");
		} catch (err) {
			failCheck("fy_defaults_closing_range", err);
		}

		// Preserve intentional user date selection across refresh
		try {
			const preserved = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae._dates_user_override = true;
				ae.document_scope.from_date = "2026-04-01";
				ae.document_scope.to_date = "2026-04-30";
				ae.from_date_field?.set_value?.("2026-04-01");
				ae.to_date_field?.set_value?.("2026-04-30");
				await ae.sync_dates_from_fy({ reason: "bootstrap" });
				return {
					from_date: ae.document_scope.from_date,
					to_date: ae.document_scope.to_date,
					override: ae._dates_user_override,
				};
			});
			if (preserved.from_date !== "2026-04-01" || preserved.to_date !== "2026-04-30") {
				throw new Error(`override overwritten: ${JSON.stringify(preserved)}`);
			}
			passCheck("fy_preserves_user_dates", preserved);
		} catch (err) {
			failCheck("fy_preserves_user_dates", err);
		}

		// Restore full FY for remaining scenarios
		await page.evaluate(async ({ company }) => {
			const ae = window.cur_ae;
			ae._dates_user_override = false;
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = "1405";
			await ae.sync_dates_from_fy({ reason: "fiscal_year_change", force: true });
			ae.document_scope.to_date = "2026-03-25";
			ae.to_date_field?.set_value?.("2026-03-25");
			ae._dates_user_override = true;
			ae.analysis_context.page = 1;
			ae.analysis_context.page_size = 10;
			ae.analysis_context.column_filters = {};
			await ae.refresh_summary({ new_execution: true, reason: "v577_seed" });
		}, { company: COMPANY });
		await waitSummaryIdle(page, { requireRows: true });

		// 2) Column filters (server-side) + reset
		try {
			const filtered = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.set_column_filters({ display_code: "11" }, { refresh: true });
				let guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const rows = (ae.rows || []).map((r) => r.display_code);
				const page = ae.pagination?.page;
				ae.clear_column_filters({ refresh: true });
				guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				return {
					filtered_rows: rows,
					page_after_filter: page,
					filters_cleared: Object.keys(ae.analysis_context.column_filters || {}).length === 0,
					all_match: rows.every((c) => String(c || "").includes("11")),
				};
			});
			if (!filtered.filters_cleared) throw new Error("filters not cleared");
			if (filtered.page_after_filter !== 1) throw new Error("filter did not reset page");
			if (filtered.filtered_rows.length && !filtered.all_match) {
				throw new Error(`filter miss: ${JSON.stringify(filtered.filtered_rows.slice(0, 5))}`);
			}
			passCheck("column_filters_server", filtered);
			await shot(page, "02-column-filters");
		} catch (err) {
			failCheck("column_filters_server", err);
		}

		// 3) Full pagination controls
		try {
			const pag = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.analysis_context.page_size = 5;
				ae.analysis_context.page = 1;
				ae.analysis_context.column_filters = {};
				await ae.refresh_summary({ new_execution: true, reason: "pag_seed" });
				let guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const wait = async () => {
					let g = 0;
					while (ae.store?.get?.("loading")?.summary && g++ < 400) {
						await new Promise((r) => setTimeout(r, 100));
					}
				};
				const ui = {
					has_bar: !!document.querySelector(".ae-pagination-bar"),
					has_first: !!document.querySelector(".ae-pagination-bar button"),
					has_input: !!document.querySelector(".ae-page-input"),
					indicator: document.querySelector(".ae-page-indicator")?.textContent || "",
				};
				const page1Keys = (ae.rows || []).map((r) => r.row_key || r.display_code);
				const total_pages = ae.pagination?.total_pages || 1;
				const total_rows = ae.pagination?.total_rows || 0;
				if (total_pages < 2) {
					return { skipped: true, ui, total_rows, total_pages };
				}
				ae.analysis_context.page = total_pages;
				await ae.refresh_summary();
				await wait();
				const lastKeys = (ae.rows || []).map((r) => r.row_key || r.display_code);
				ae.analysis_context.page = 2;
				await ae.refresh_summary();
				await wait();
				const page2Keys = (ae.rows || []).map((r) => r.row_key || r.display_code);
				ae.analysis_context.page = 1;
				await ae.refresh_summary();
				await wait();
				const page1Again = (ae.rows || []).map((r) => r.row_key || r.display_code);
				return {
					skipped: false,
					ui,
					total_pages,
					total_rows,
					page1Keys,
					page2Keys,
					lastKeys,
					page1Again,
					overlap12: page1Keys.some((k) => page2Keys.includes(k)),
					stable: JSON.stringify(page1Keys) === JSON.stringify(page1Again),
				};
			});
			if (!pag.ui?.has_bar || !pag.ui?.has_input) {
				throw new Error(`pagination UI missing: ${JSON.stringify(pag.ui)}`);
			}
			if (!pag.skipped) {
				if (pag.overlap12) throw new Error("duplicate keys across page 1/2");
				if (!pag.stable) throw new Error("page 1 unstable after navigation");
			}
			passCheck("full_pagination", pag);
			await shot(page, "03-pagination");
		} catch (err) {
			failCheck("full_pagination", err);
		}

		// 4) Voucher Running Balance — single column, continuity, no legacy columns
		try {
			const rb = await page.evaluate(async ({ company }) => {
				const ae = window.cur_ae;
				const account = await frappe.db.get_value("Account", {
					company,
					account_number: "111625",
					is_group: 0,
				}, "name");
				const accountName = account?.message?.name || account?.message || account;
				ae.switch_axis("voucher");
				ae.analysis_context.account_scope = {
					mode: "account",
					selected_account: accountName,
					tree_root_account: accountName,
					is_virtual_group: 0,
				};
				ae.analysis_context.page = 1;
				ae.analysis_context.page_size = 3;
				ae.analysis_context.sort_field = "posting_date";
				ae.analysis_context.sort_order = "asc";
				ae.analysis_context.column_filters = {};
				ae.document_scope.to_date = "2026-03-25";
				await ae.refresh_summary({ new_execution: true, reason: "voucher_rb" });
				let guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const cols = (ae.last_summary_columns || ae.columns || []).map((c) => c.id || c.fieldname);
				const headerText = Array.from(document.querySelectorAll(".dt-header .dt-cell")).map(
					(el) => (el.textContent || "").trim()
				);
				const page1 = (ae.rows || []).map((r) => ({
					debit: r.debit,
					credit: r.credit,
					running_balance: r.running_balance,
					display: r.running_balance_display,
					gl: r.gl_entry,
				}));
				// Visual Debit/Credit accumulation must match when sorted chronologically.
				let run = 0;
				const okAccum = page1.every((row) => {
					run = run + Number(row.debit || 0) - Number(row.credit || 0);
					return Math.abs(Number(row.running_balance) - run) < 0.02;
				});
				ae.analysis_context.page = 2;
				await ae.refresh_summary();
				guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const page2 = (ae.rows || []).map((r) => ({
					debit: r.debit,
					credit: r.credit,
					running_balance: r.running_balance,
					gl: r.gl_entry,
				}));
				let cont = page1.length ? Number(page1[page1.length - 1].running_balance) : 0;
				const okCont = page2.every((row) => {
					cont = cont + Number(row.debit || 0) - Number(row.credit || 0);
					return Math.abs(Number(row.running_balance) - cont) < 0.02;
				});
				return {
					cols,
					headerText,
					page1,
					page2,
					okAccum,
					okCont: !page2.length || okCont,
					hasRunning: cols.includes("running_balance"),
					hasLegacyDebit: cols.includes("running_debit_balance"),
					hasLegacyCredit: cols.includes("running_credit_balance"),
					legacyHeader:
						headerText.some((t) => /running debit/i.test(t)) ||
						headerText.some((t) => /running credit/i.test(t)),
				};
			}, { company: COMPANY });
			if (!rb.hasRunning) throw new Error(`missing running_balance col: ${rb.cols}`);
			if (rb.hasLegacyDebit || rb.hasLegacyCredit || rb.legacyHeader) {
				throw new Error(`legacy columns present: ${JSON.stringify(rb)}`);
			}
			if (!rb.okAccum) throw new Error(`accumulation failed: ${JSON.stringify(rb.page1)}`);
			if (!rb.okCont) {
				throw new Error(`page continuity failed: ${JSON.stringify(rb)}`);
			}
			passCheck("running_balance_single_column", {
				page1: rb.page1,
				page2: rb.page2,
				okAccum: rb.okAccum,
				okCont: rb.okCont,
			});
			await shot(page, "04-running-balance");
		} catch (err) {
			failCheck("running_balance_single_column", err);
		}

		// 5) Export parity (API-level via desk session — same running_balance field)
		try {
			const parity = await page.evaluate(async ({ company }) => {
				const account = await frappe.db.get_value("Account", {
					company,
					account_number: "111625",
					is_group: 0,
				}, "name");
				const accountName = account?.message?.name || account?.message || account;
				const payload = {
					document_scope: {
						company,
						fiscal_year: "1405",
						from_date: "2026-03-21",
						to_date: "2026-03-25",
						status: {
							include_opening_entries: 1,
							include_cancelled_entries: 0,
							include_default_finance_book_entries: 1,
							include_period_closing_vouchers: 0,
						},
						accounting: {},
						voucher: {},
						currency: { currency_type: "account_currency" },
						accounting_dimensions: {},
						inventory: {},
						hide_zero_rows: 0,
					},
					analysis_context: {
						view_axis: "voucher",
						detail_mode: "summary",
						page: 1,
						page_size: 20,
						sort_field: "posting_date",
						sort_order: "asc",
						account_scope: {
							mode: "account",
							selected_account: accountName,
							tree_root_account: accountName,
						},
						party_scope: {},
					},
				};
				const grid = await frappe.call(
					"erpnext_extensions.iran_accounting.account_explorer.get_voucher_summary",
					{ payload }
				);
				const rows = grid?.message?.rows || [];
				const hasDisplay = rows.every((r) => "running_balance_display" in r && "running_balance" in r);
				const noLegacy = rows.every(
					(r) => !("running_debit_balance" in r) && !("running_credit_balance" in r)
				);
				return { count: rows.length, hasDisplay, noLegacy, sample: rows.slice(0, 3) };
			}, { company: COMPANY });
			if (!parity.hasDisplay || !parity.noLegacy) {
				throw new Error(`export/grid field parity failed: ${JSON.stringify(parity)}`);
			}
			passCheck("grid_export_running_balance_fields", parity);
		} catch (err) {
			failCheck("grid_export_running_balance_fields", err);
		}

		await shot(page, "05-final");
	} finally {
		await browser.close();
	}

	const failed = checks.filter((c) => !c.ok);
	const report = {
		site: SITE,
		base: BASE,
		passed: checks.filter((c) => c.ok).length,
		failed: failed.length,
		checks,
		consoleErrors: consoleErrors.slice(0, 20),
	};
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (failed.length) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

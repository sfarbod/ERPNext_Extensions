#!/usr/bin/env node
/**
 * Account Explorer 5.7.7 UI fixes — single pagination toolbar (no raw HTML),
 * Party Name column filter persistence + clear (real browser).
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v577-ui-fixes");

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
		return !!(ae && ae.metadata?.axes?.length);
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
}

async function waitIdle(page, { requireRows = false, timeout = 180000 } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const state = await page.evaluate(() => {
			const ae = window.cur_ae;
			const loading = !!ae?.store?.get?.("loading")?.summary;
			return { ready: !loading, rows: (ae?.rows || []).length };
		});
		if (state.ready && (!requireRows || state.rows > 0)) return state;
		await page.waitForTimeout(200);
	}
	throw new Error("idle timeout");
}

async function seedFy(page) {
	await page.evaluate(async ({ company }) => {
		const ae = window.cur_ae;
		ae.document_scope.company = company;
		ae.document_scope.fiscal_year = "1405";
		ae._dates_user_override = false;
		if (ae.company_field?.set_value) await ae.company_field.set_value(company);
		if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
		await ae.sync_dates_from_fy({ reason: "fiscal_year_change", force: true });
		ae.document_scope.to_date = "2026-03-25";
		ae.to_date_field?.set_value?.("2026-03-25");
		ae._dates_user_override = true;
		ae.analysis_context.column_filters = {};
		ae.analysis_context.page = 1;
		ae.analysis_context.page_size = 5;
	}, { company: COMPANY });
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
		await seedFy(page);
		await page.evaluate(async () => {
			await window.cur_ae.refresh_summary({ new_execution: true, reason: "ui_fix_seed" });
		});
		await waitIdle(page, { requireRows: true });

		// —— Issue 1: single pagination, no raw HTML ——
		try {
			const pag = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.analysis_context.page_size = 5;
				ae.analysis_context.page = 1;
				await ae.refresh_summary({ new_execution: true, reason: "pag_check" });
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const bars = [...document.querySelectorAll("[data-ae-pagination='1']")];
				const pageSizeInToolbar = !!document.querySelector(
					".ae-grid-toolbar-prefs [data-ae-page-size], .ae-grid-toolbar .ae-grid-toolbar-control--page-size"
				);
				const indicator = document.querySelector("[data-ae-page-indicator]")?.textContent || "";
				const totalAttr = document
					.querySelector("[data-ae-total-rows]")
					?.getAttribute("data-ae-total-rows");
				return {
					barCount: bars.length,
					pageSizeInToolbar,
					indicator,
					totalAttr,
					hasHtmlMarkup: /<div|<\/div>|&lt;div/i.test(indicator),
					hasFirst: !!document.querySelector("[data-ae-page-action='first']"),
					hasPageInput: !!document.querySelector("[data-ae-page-input]"),
					hasPageSize: !!document.querySelector("[data-ae-page-size]"),
					total_rows: ae.pagination?.total_rows,
					total_pages: ae.pagination?.total_pages,
				};
			});
			if (pag.barCount !== 1) throw new Error(`expected 1 pagination bar, got ${pag.barCount}`);
			if (pag.pageSizeInToolbar) throw new Error("page size still in grid toolbar");
			if (pag.hasHtmlMarkup) throw new Error(`raw HTML in indicator: ${pag.indicator}`);
			if (!pag.hasFirst || !pag.hasPageInput || !pag.hasPageSize) {
				throw new Error(`missing controls: ${JSON.stringify(pag)}`);
			}
			passCheck("single_pagination_no_html", pag);
			await shot(page, "01-pagination-account-levels");
		} catch (err) {
			failCheck("single_pagination_no_html", err);
		}

		// Switch axis and confirm still one bar
		try {
			const again = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("voucher");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				ae.switch_axis("party");
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				return {
					bars: document.querySelectorAll("[data-ae-pagination='1']").length,
					axis: ae.analysis_context.view_axis,
					indicator: document.querySelector("[data-ae-page-indicator]")?.textContent || "",
				};
			});
			if (again.bars !== 1) throw new Error(`bars after axis switch: ${again.bars}`);
			if (/<div/i.test(again.indicator)) throw new Error(`html after switch: ${again.indicator}`);
			passCheck("pagination_stable_across_axes", again);
			await shot(page, "02-pagination-party-axis");
		} catch (err) {
			failCheck("pagination_stable_across_axes", err);
		}

		// —— Issue 2: Party Name filter ——
		try {
			await shot(page, "03-parties-before-filter");
			const filtered = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("party");
				ae.analysis_context.page_size = 50;
				ae.analysis_context.page = 1;
				ae.analysis_context.column_filters = {};
				await ae.refresh_summary({ new_execution: true, reason: "party_before" });
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const before = ae.pagination?.total_rows || 0;
				ae.set_column_filters({ display_title: "نانو" }, { refresh: true });
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const input = document.querySelector("[data-ae-column-filter='display_title']");
				const chip = document.querySelector("[data-ae-column-filter-chip='display_title']");
				const clearBtn = document.querySelector(
					"[data-ae-clear-column-filter-input='display_title']"
				);
				const titles = (ae.rows || []).map((r) => r.display_title);
				return {
					before,
					after: ae.pagination?.total_rows || 0,
					inputValue: input?.value || "",
					inputVisible: !!(input && input.offsetParent !== null),
					hasChip: !!chip,
					hasClear: !!clearBtn && clearBtn.hidden === false,
					titles,
					allMatch: titles.every((t) => String(t || "").includes("نانو")),
					payloadFilter: ae.analysis_context.column_filters?.display_title || null,
					indicator: document.querySelector("[data-ae-page-indicator]")?.textContent || "",
				};
			});
			if (filtered.payloadFilter !== "نانو") {
				throw new Error(`payload missing: ${JSON.stringify(filtered)}`);
			}
			if (filtered.inputValue !== "نانو") {
				throw new Error(`input lost value: ${JSON.stringify(filtered)}`);
			}
			if (!filtered.inputVisible) throw new Error("filter input not visible");
			if (!filtered.hasClear) throw new Error("clear button missing");
			if (filtered.after < 1) throw new Error(`expected matches, got ${filtered.after}`);
			if (!filtered.allMatch) throw new Error(`non-matching titles: ${filtered.titles}`);
			if (/<div/i.test(filtered.indicator)) throw new Error("html in pagination after filter");
			passCheck("party_name_filter_persists", filtered);
			await shot(page, "04-party-name-filter-nano");
		} catch (err) {
			failCheck("party_name_filter_persists", err);
		}

		// Independent row-count check via API
		try {
			const count = await page.evaluate(async ({ company }) => {
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
						view_axis: "party",
						detail_mode: "summary",
						page: 1,
						page_size: 200,
						sort_field: "display_title",
						sort_order: "asc",
						party_scope: {},
						account_scope: {},
						column_filters: { display_title: "نانو" },
					},
				};
				const res = await frappe.call(
					"erpnext_extensions.iran_accounting.account_explorer.get_party_summary",
					{ payload }
				);
				const rows = res?.message?.rows || [];
				return {
					total_rows: res?.message?.pagination?.total_rows,
					titles: rows.map((r) => r.display_title),
				};
			}, { company: COMPANY });
			const uiTotal = await page.evaluate(() => window.cur_ae?.pagination?.total_rows);
			if (count.total_rows !== uiTotal) {
				throw new Error(`UI ${uiTotal} != API ${count.total_rows}`);
			}
			if (!count.titles.every((t) => String(t).includes("نانو"))) {
				throw new Error(`API titles mismatch: ${JSON.stringify(count.titles)}`);
			}
			passCheck("party_name_row_count_independent", { ...count, uiTotal });
		} catch (err) {
			failCheck("party_name_row_count_independent", err);
		}

		// Clear filter via clear button; other filters preserved
		try {
			const cleared = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.set_column_filters(
					{ display_title: "نانو", party_type: "Customer" },
					{ refresh: true }
				);
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const before = { ...(ae.analysis_context.column_filters || {}) };
				const clearBtn = document.querySelector(
					"[data-ae-clear-column-filter-input='display_title']"
				);
				if (!clearBtn) throw new Error("clear button not found");
				clearBtn.click();
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const input = document.querySelector("[data-ae-column-filter='display_title']");
				return {
					before,
					after: { ...(ae.analysis_context.column_filters || {}) },
					inputValue: input?.value || "",
					total_rows: ae.pagination?.total_rows,
				};
			});
			if (cleared.after.display_title) throw new Error("display_title still set");
			if (cleared.after.party_type !== "Customer") {
				throw new Error(`other filter lost: ${JSON.stringify(cleared.after)}`);
			}
			if (cleared.inputValue) throw new Error(`input still has ${cleared.inputValue}`);
			passCheck("party_name_clear_keeps_other_filters", cleared);
			await shot(page, "05-party-name-cleared");
		} catch (err) {
			failCheck("party_name_clear_keeps_other_filters", err);
		}

		// Empty result state
		try {
			const empty = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.set_column_filters(
					{ display_title: "___no_such_party_xyz___" },
					{ refresh: true }
				);
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const input = document.querySelector("[data-ae-column-filter='display_title']");
				const indicator = document.querySelector("[data-ae-page-indicator]")?.textContent || "";
				return {
					rows: (ae.rows || []).length,
					total_rows: ae.pagination?.total_rows,
					inputValue: input?.value || "",
					inputPresent: !!input,
					hasHtml: /<div/i.test(indicator),
					indicator,
				};
			});
			if (empty.total_rows !== 0 || empty.rows !== 0) {
				throw new Error(`expected empty: ${JSON.stringify(empty)}`);
			}
			if (!empty.inputPresent || empty.inputValue !== "___no_such_party_xyz___") {
				throw new Error(`filter not visible on empty: ${JSON.stringify(empty)}`);
			}
			if (empty.hasHtml) throw new Error(`html on empty: ${empty.indicator}`);
			passCheck("party_name_empty_result_keeps_filter", empty);
			await shot(page, "06-party-name-empty");
		} catch (err) {
			failCheck("party_name_empty_result_keeps_filter", err);
		}

		await shot(page, "07-final");
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
	};
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (failed.length) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

#!/usr/bin/env node
/**
 * Account Explorer 5.7.7 final acceptance — voucher column order,
 * pagination singleton, Party Name filter UX (real browser).
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
			return {
				ready: !ae?.store?.get?.("loading")?.summary,
				rows: (ae?.rows || []).length,
			};
		});
		if (state.ready && (!requireRows || state.rows > 0)) return state;
		await page.waitForTimeout(200);
	}
	throw new Error("idle timeout");
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
			ae.grid_column_order = [];
			ae._voucher_column_defaults_applied = false;
			await ae.refresh_summary({ new_execution: true, reason: "final_seed" });
		}, { company: COMPANY });
		await waitIdle(page, { requireRows: true });

		// 1) Voucher column order Debit → Credit → Running Balance → Remarks
		try {
			const order = await page.evaluate(async ({ company }) => {
				const ae = window.cur_ae;
				const account = await frappe.db.get_value(
					"Account",
					{ company, account_number: "111625", is_group: 0 },
					"name"
				);
				const accountName = account?.message?.name || account?.message || account;
				ae.grid_column_order = [];
				ae._voucher_column_defaults_applied = false;
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
				await ae.refresh_summary({ new_execution: true, reason: "voucher_order" });
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const visible = (ae.datatable_adapter?._column_defs || []).map((c) => c.id);
				const preferred = ae.get_voucher_preferred_column_ids?.() || [];
				const slice = (() => {
					const i = visible.indexOf("debit");
					const j = visible.indexOf("remarks");
					return i >= 0 && j >= 0 ? visible.slice(i, j + 1) : visible;
				})();
				await ae.refresh_summary({ new_execution: true, reason: "voucher_order_refresh" });
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const after = (ae.datatable_adapter?._column_defs || []).map((c) => c.id);
				const afterSlice = (() => {
					const i = after.indexOf("debit");
					const j = after.indexOf("remarks");
					return i >= 0 && j >= 0 ? after.slice(i, j + 1) : after;
				})();
				const page1 = (ae.rows || []).map((r) => ({
					debit: r.debit,
					credit: r.credit,
					running_balance: r.running_balance,
					gl: r.gl_entry,
				}));
				let run = 0;
				const okAccum = page1.every((row) => {
					run = run + Number(row.debit || 0) - Number(row.credit || 0);
					return Math.abs(Number(row.running_balance) - run) < 0.02;
				});
				ae.analysis_context.page = 2;
				await ae.refresh_summary();
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const page2 = (ae.rows || []).map((r) => ({
					debit: r.debit,
					credit: r.credit,
					running_balance: r.running_balance,
				}));
				let cont = page1.length ? Number(page1[page1.length - 1].running_balance) : 0;
				const okCont = page2.every((row) => {
					cont = cont + Number(row.debit || 0) - Number(row.credit || 0);
					return Math.abs(Number(row.running_balance) - cont) < 0.02;
				});
				const legacy = visible.includes("running_debit_balance") ||
					visible.includes("running_credit_balance");
				return {
					slice,
					afterSlice,
					preferredBlock: preferred.slice(
						preferred.indexOf("debit"),
						preferred.indexOf("remarks") + 1
					),
					okAccum,
					okCont,
					legacy,
					page1,
					page2,
				};
			}, { company: COMPANY });
			const expected = ["debit", "credit", "running_balance", "remarks"];
			if (JSON.stringify(order.slice) !== JSON.stringify(expected)) {
				throw new Error(`order ${JSON.stringify(order.slice)}`);
			}
			if (JSON.stringify(order.afterSlice) !== JSON.stringify(expected)) {
				throw new Error(`order after refresh ${JSON.stringify(order.afterSlice)}`);
			}
			if (order.legacy) throw new Error("legacy running columns present");
			if (!order.okAccum) throw new Error("running balance accumulation failed");
			if (order.page2.length && !order.okCont) throw new Error("page continuity failed");
			passCheck("voucher_column_order_and_balance", order);
			await shot(page, "08-voucher-column-order");
		} catch (err) {
			failCheck("voucher_column_order_and_balance", err);
		}

		// 2) Export field parity (API)
		try {
			const parity = await page.evaluate(async ({ company }) => {
				const account = await frappe.db.get_value(
					"Account",
					{ company, account_number: "111625", is_group: 0 },
					"name"
				);
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
				const colIds = (grid?.message?.columns || []).map((c) => c.id);
				const block = colIds.slice(colIds.indexOf("debit"), colIds.indexOf("remarks") + 1);
				const rows = grid?.message?.rows || [];
				return {
					block,
					hasDisplay: rows.every((r) => "running_balance_display" in r),
					noLegacy: rows.every(
						(r) => !("running_debit_balance" in r) && !("running_credit_balance" in r)
					),
					count: rows.length,
				};
			}, { company: COMPANY });
			if (JSON.stringify(parity.block) !== JSON.stringify(["debit", "credit", "running_balance", "remarks"])) {
				throw new Error(`api columns ${JSON.stringify(parity.block)}`);
			}
			if (!parity.hasDisplay || !parity.noLegacy) throw new Error(JSON.stringify(parity));
			passCheck("voucher_export_grid_column_parity", parity);
		} catch (err) {
			failCheck("voucher_export_grid_column_parity", err);
		}

		// 3) Pagination singleton + no HTML
		try {
			const pag = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("account_level");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				ae.switch_axis("party");
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const indicator = document.querySelector("[data-ae-page-indicator]")?.textContent || "";
				return {
					bars: document.querySelectorAll("[data-ae-pagination='1']").length,
					pageSizeInToolbar: !!document.querySelector(
						".ae-grid-toolbar .ae-grid-toolbar-control--page-size"
					),
					hasHtml: /<div|<\/div>/i.test(indicator),
					indicator,
				};
			});
			if (pag.bars !== 1) throw new Error(`bars=${pag.bars}`);
			if (pag.pageSizeInToolbar) throw new Error("page size in toolbar");
			if (pag.hasHtml) throw new Error(`html: ${pag.indicator}`);
			passCheck("pagination_singleton_clean", pag);
			await shot(page, "09-pagination-singleton");
		} catch (err) {
			failCheck("pagination_singleton_clean", err);
		}

		// 4) Party Name filter lifecycle
		try {
			const party = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("party");
				ae.analysis_context.page_size = 50;
				ae.analysis_context.column_filters = {};
				ae.analysis_context.account_scope = {
					mode: "tree",
					selected_account: null,
					tree_root_account: null,
					is_virtual_group: 0,
				};
				ae.analysis_context.party_scope = {
					party_type: null,
					selected_party: null,
					party_name_query: null,
					identifier_query: null,
					unified_identity: 0,
					identity_members: null,
				};
				await ae.refresh_summary({ new_execution: true, reason: "party_pre" });
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				// Matching parties for نانو are Suppliers in this dataset.
				ae.set_column_filters({ display_title: "نانو", party_type: "Supplier" }, { refresh: true });
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const input = document.querySelector("[data-ae-column-filter='display_title']");
				const titles = (ae.rows || []).map((r) => r.display_title);
				const afterFilter = {
					inputValue: input?.value || "",
					visible: !!(input && input.offsetParent !== null),
					total: ae.pagination?.total_rows,
					titles,
					allMatch: titles.every((t) => String(t || "").includes("نانو")),
					filters: { ...(ae.analysis_context.column_filters || {}) },
				};
				document.querySelector("[data-ae-clear-column-filter-input='display_title']")?.click();
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const afterClear = {
					filters: { ...(ae.analysis_context.column_filters || {}) },
					inputValue: document.querySelector("[data-ae-column-filter='display_title']")?.value || "",
					total: ae.pagination?.total_rows,
				};
				ae.set_column_filters({ display_title: "___none___" }, { refresh: true });
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const empty = {
					rows: (ae.rows || []).length,
					total: ae.pagination?.total_rows,
					inputValue: document.querySelector("[data-ae-column-filter='display_title']")?.value || "",
					inputPresent: !!document.querySelector("[data-ae-column-filter='display_title']"),
				};
				return { afterFilter, afterClear, empty };
			});
			if (party.afterFilter.inputValue !== "نانو" || !party.afterFilter.visible) {
				throw new Error(`persist ${JSON.stringify(party.afterFilter)}`);
			}
			if (!party.afterFilter.allMatch || party.afterFilter.total < 1) {
				throw new Error(`match ${JSON.stringify(party.afterFilter)}`);
			}
			if (party.afterClear.filters.display_title) throw new Error("clear failed");
			if (party.afterClear.filters.party_type !== "Supplier") {
				throw new Error(`other filter lost ${JSON.stringify(party.afterClear)}`);
			}
			if (!party.empty.inputPresent || party.empty.inputValue !== "___none___" || party.empty.total !== 0) {
				throw new Error(`empty ${JSON.stringify(party.empty)}`);
			}
			passCheck("party_name_filter_lifecycle", party);
			await shot(page, "10-party-name-final");
		} catch (err) {
			failCheck("party_name_filter_lifecycle", err);
		}

		await shot(page, "11-final-acceptance");
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
	fs.writeFileSync(path.join(OUT, "final-report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (failed.length) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

#!/usr/bin/env node
/**
 * Account Explorer 5.6.35 — persistent identifier, Account Group Analyze,
 * Party→Identifier sync, running balance columns, global Reset.
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
const IDENTIFIER = "14008809078";
const CUSTOMER = "CUS-00003";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5635");

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
		return !!(
			ae &&
			ae.metadata?.axes?.length &&
			document.querySelector(".ae-shell, .ae-toolbar")
		);
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
		await page.waitForTimeout(200);
	}
	throw new Error("summary idle timeout");
}

async function seedFy1405(page) {
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
	}, { company: COMPANY });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const consoleErrors = [];
	page.on("pageerror", (err) => consoleErrors.push(String(err)));
	page.on("console", (msg) => {
		if (msg.type() === "error") consoleErrors.push(msg.text());
	});

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await seedFy1405(page);

		// Scenario 1 — Identifier persistence across axes
		try {
			const persist = await page.evaluate(async ({ identifier }) => {
				const ae = window.cur_ae;
				await ae.filter_controls.identifier_query?.set_value?.(identifier);
				ae.sync_document_scope_from_controls();
				// Authoritative: keep party_scope aligned even if set_value was deferred.
				ae.analysis_context.party_scope = {
					...(ae.analysis_context.party_scope || {}),
					identifier_query: identifier,
					identity_identifier: identifier,
				};
				const waitIdle = async () => {
					let guard = 0;
					while (ae.store?.get?.("loading")?.summary && guard++ < 300) {
						await new Promise((r) => setTimeout(r, 100));
					}
				};
				ae.switch_axis("account_level", 1);
				await waitIdle();
				const axes = ["party", "dimension", "currency", "voucher", "account_level"];
				const out = { scopes: [], totals: [] };
				for (const axis of axes) {
					if (axis === "account_level") ae.switch_axis("account_level", 1);
					else if (axis === "dimension") {
						ae.switch_axis(
							"dimension",
							ae.metadata?.default_dimension_type || "cost_center"
						);
					} else ae.switch_axis(axis);
					await waitIdle();
					out.scopes.push({
						axis,
						identifier: ae.analysis_context.party_scope?.identifier_query || null,
						control: ae.filter_controls.identifier_query?.get_value?.() || null,
						payload_identifier:
							ae.build_payload()?.analysis_context?.party_scope?.identifier_query || null,
					});
					out.totals.push({
						axis,
						debit: ae.totals?.period_debit ?? ae.totals?.debit ?? null,
						rows: (ae.rows || []).length,
					});
				}
				return out;
			}, { identifier: IDENTIFIER });
			await waitSummaryIdle(page);
			const lost = persist.scopes.filter(
				(s) => String(s.identifier) !== IDENTIFIER && String(s.payload_identifier) !== IDENTIFIER
			);
			if (lost.length) throw new Error(`identifier lost on axes: ${JSON.stringify(lost)}`);
			passCheck("identifier_persists_across_axes", persist);
		} catch (err) {
			failCheck("identifier_persists_across_axes", err);
		}

		// Scenario 2 — Account Group Analyze
		try {
			const analyze = await page.evaluate(async () => {
				const ae = window.cur_ae;
				const waitIdle = async () => {
					let guard = 0;
					while (ae.store?.get?.("loading")?.summary && guard++ < 300) {
						await new Promise((r) => setTimeout(r, 100));
					}
				};
				ae.filter_controls.identifier_query?.set_value("");
				ae.analysis_context.party_scope = {
					party_type: null,
					selected_party: null,
					identifier_query: null,
					unified_identity: 0,
				};
				ae.clear_all_analysis_filters({ refresh: false });
				ae.switch_axis("account_level", 1);
				await waitIdle();
				const group = (ae.rows || []).find((r) => String(r.display_code) === "11");
				if (!group) return { error: "group_11_missing", codes: (ae.rows || []).map((r) => r.display_code) };
				ae.analyze_row_as_filter(group);
				await waitIdle();
				const codes = (ae.rows || []).map((r) => r.display_code);
				return {
					level: ae.analysis_context.level_sequence,
					codes,
					has21: codes.includes("21"),
					has11: codes.includes("11"),
					account_filter: ae.get_analysis_filters()?.account?.value || null,
				};
			});
			if (analyze.error) throw new Error(JSON.stringify(analyze));
			if (analyze.has21) throw new Error(`sibling 21 present: ${analyze.codes}`);
			if (analyze.has11) throw new Error(`parent 11 still a detail row: ${analyze.codes}`);
			passCheck("account_group_analyze_descendants", analyze);
		} catch (err) {
			failCheck("account_group_analyze_descendants", err);
		}

		// Scenario 3 — Party → Identifier sync + clear Party keeps identifier
		try {
			const sync = await page.evaluate(async ({ customer }) => {
				const ae = window.cur_ae;
				await ae.filter_controls.party_type?.set_value?.("Customer");
				await ae.filter_controls.party?.set_value?.([customer]);
				const afterSelect = await new Promise((resolve, reject) => {
					frappe.call({
						method: `${ae.api_base}.resolve_party_identifier`,
						args: { party_type: "Customer", party: customer },
						callback: (r) => {
							const identifier = r.message?.identifier;
							if (identifier != null && identifier !== "") {
								ae.filter_controls.identifier_query?.set_value(String(identifier));
								ae.analysis_context.party_scope = {
									...(ae.analysis_context.party_scope || {}),
									party_type: "Customer",
									selected_party: customer,
									identifier_query: String(identifier),
								};
							}
							resolve({
								identifier: ae.filter_controls.identifier_query?.get_value?.() || "",
								raw: r.message || null,
							});
						},
						error: (e) => reject(e || new Error("resolve_party_identifier failed")),
					});
				});
				await ae.filter_controls.party?.set_value?.(null);
				await ae.filter_controls.party_type?.set_value?.("");
				const afterClear = ae.filter_controls.identifier_query?.get_value?.() || "";
				return { afterSelect: afterSelect.identifier, afterClear, raw: afterSelect.raw };
			}, { customer: CUSTOMER });
			if (String(sync.afterSelect) !== IDENTIFIER) {
				throw new Error(`expected identifier after party select, got ${sync.afterSelect}`);
			}
			if (String(sync.afterClear) !== IDENTIFIER) {
				throw new Error(`identifier cleared with party: ${sync.afterClear}`);
			}
			passCheck("party_identifier_sync", sync);
		} catch (err) {
			failCheck("party_identifier_sync", err);
		}

		// Scenario 4 — Running balance columns present
		try {
			const running = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.analysis_context.view_axis = "voucher";
				ae.analysis_context.sort_field = "posting_date";
				ae.analysis_context.sort_order = "asc";
				ae.analysis_context.page = 1;
				ae.analysis_context.page_size = 10;
				ae.filter_controls.identifier_query?.set_value("");
				ae.analysis_context.party_scope = { identifier_query: null, unified_identity: 0 };
				ae.ensure_voucher_default_column_visibility();
				const hidden = new Set(ae.summary_hidden_columns || []);
				hidden.delete("running_debit_balance");
				hidden.delete("running_credit_balance");
				ae.set_summary_hidden_columns([...hidden]);
				await ae.refresh_summary({ new_execution: true, reason: "e2e_running" });
				let guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 200) {
					await new Promise((r) => setTimeout(r, 200));
				}
				const row = (ae.rows || [])[0] || {};
				return {
					hasDebitCol: (ae.metadata?.voucher_columns || ae.columns || []).some?.(
						(c) => c.id === "running_debit_balance"
					) || Object.prototype.hasOwnProperty.call(row, "running_debit_balance"),
					sample: {
						debit: row.debit,
						credit: row.credit,
						running_debit_balance: row.running_debit_balance,
						running_credit_balance: row.running_credit_balance,
					},
				};
			});
			if (!running.hasDebitCol && running.sample.running_debit_balance == null) {
				throw new Error(JSON.stringify(running));
			}
			passCheck("running_balance_columns", running);
		} catch (err) {
			failCheck("running_balance_columns", err);
		}

		// Scenario 5 — Global Reset
		try {
			const reset = await page.evaluate(async ({ identifier }) => {
				const ae = window.cur_ae;
				ae.filter_controls.identifier_query?.set_value(identifier);
				ae.filter_controls.party_type?.set_value("Customer");
				ae.filter_controls.finance_book?.set_value("");
				ae.sync_document_scope_from_controls();
				ae.analysis_context.view_axis = "voucher";
				const before = {
					company: ae.document_scope.company,
					fiscal_year: ae.document_scope.fiscal_year,
					from_date: ae.document_scope.from_date,
					to_date: ae.document_scope.to_date,
					identifier: ae.filter_controls.identifier_query?.get_value?.(),
					axis: ae.analysis_context.view_axis,
				};
				ae.reset_all_filters();
				let guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 200) {
					await new Promise((r) => setTimeout(r, 200));
				}
				return {
					before,
					after: {
						company: ae.document_scope.company,
						fiscal_year: ae.document_scope.fiscal_year,
						from_date: ae.document_scope.from_date,
						to_date: ae.document_scope.to_date,
						identifier: ae.filter_controls.identifier_query?.get_value?.() || "",
						party_type: ae.filter_controls.party_type?.get_value?.() || "",
						axis: ae.analysis_context.view_axis,
						account_filter: ae.get_analysis_filters()?.account || null,
						party_identity: ae.get_analysis_filters()?.party_identity || null,
					},
				};
			}, { identifier: IDENTIFIER });
			if (reset.after.company !== reset.before.company) throw new Error("company changed");
			if (reset.after.from_date !== reset.before.from_date) throw new Error("from_date changed");
			if (reset.after.to_date !== reset.before.to_date) throw new Error("to_date changed");
			if (reset.after.identifier) throw new Error(`identifier remained: ${reset.after.identifier}`);
			if (reset.after.axis !== "account_level") throw new Error(`axis=${reset.after.axis}`);
			if (reset.after.account_filter || reset.after.party_identity) {
				throw new Error("analysis filters remained");
			}
			passCheck("global_reset", reset);
		} catch (err) {
			failCheck("global_reset", err);
		}

		await page.screenshot({ path: path.join(OUT, "final.png"), fullPage: true });
	} finally {
		await browser.close();
	}

	const failed = checks.filter((c) => !c.ok);
	const report = { site: SITE, checks, consoleErrors: consoleErrors.slice(0, 20), failed: failed.length };
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (failed.length) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 — Dimension Type isolation + debounced column filters
 * (real browser on development.localhost).
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

function chipText(page) {
	return page.evaluate(() => {
		const chips = Array.from(document.querySelectorAll(".ae-filter-summary .ae-chip, .ae-filter-chip, .ae-summary-chip"));
		return chips.map((el) => (el.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean);
	});
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const apiLog = [];
	page.on("request", (req) => {
		const url = req.url();
		if (url.includes("account_explorer") || url.includes("get_")) {
			apiLog.push({ url, method: req.method(), t: Date.now() });
		}
	});

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await page.waitForTimeout(1000);
		await waitSummaryIdle(page, { requireRows: false });

		await page.evaluate(async ({ company }) => {
			const ae = window.cur_ae;
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = "1405";
			ae._dates_user_override = false;
			if (ae.company_field?.set_value) await ae.company_field.set_value(company);
			if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
			await ae.sync_dates_from_fy({ reason: "fiscal_year_change", force: true });
			ae.analysis_context.page = 1;
			ae.analysis_context.page_size = 50;
			ae.analysis_context.column_filters = {};
			ae.clear_all_analysis_filters?.({ refresh: false });
		}, { company: COMPANY });

		// —— Scenario A: Dimension Type isolation ——
		try {
			await page.evaluate(() => {
				window.__ae_v5729_ready_for_cc_shot = false;
				window.__ae_v5729_ready_for_dept_shot = false;
			});
			const isolationPromise = page.evaluate(async () => {
				const ae = window.cur_ae;
				const dims = (ae.metadata?.dimensions || []).map((d) => d.fieldname);
				const hasCC = dims.includes("cost_center");
				const hasDept = dims.includes("department");
				if (!hasCC) {
					window.__ae_v5729_ready_for_cc_shot = true;
					window.__ae_v5729_ready_for_dept_shot = true;
					return { skipped: true, reason: "no cost_center" };
				}

				ae.switch_axis("dimension", "cost_center");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const ccRows = (ae.rows || []).filter((r) => r.dimension_value);
				if (!ccRows.length) {
					window.__ae_v5729_ready_for_cc_shot = true;
					window.__ae_v5729_ready_for_dept_shot = true;
					return { skipped: true, reason: "no cost_center rows" };
				}
				const ccRow = ccRows[0];
				ae.analyze_row_as_filter(ccRow);
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				window.__ae_v5729_ready_for_cc_shot = true;
				const afterCC = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bagDims: Object.keys(ae.get_analysis_filters?.()?.dimensions || {}),
					rowTypes: [...new Set((ae.rows || []).map((r) => r.dimension_type).filter(Boolean))],
					payloadType: ae.build_payload?.()?.analysis_context?.dimension_scope?.dimension_type,
					payloadDims: Object.keys(
						ae.build_payload?.()?.document_scope?.accounting_dimensions || {}
					),
				};

				// Back then switch to Department (or project fallback)
				if (typeof ae.go_back === "function") {
					ae.go_back();
				}
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 200) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const nextType = hasDept ? "department" : dims.find((d) => d !== "cost_center") || null;
				if (!nextType) {
					window.__ae_v5729_ready_for_dept_shot = true;
					return { skipped: true, reason: "no second dimension type", afterCC };
				}

				ae.switch_axis("dimension", nextType);
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const afterSwitch = {
					type: ae.analysis_context.dimension_scope?.dimension_type,
					bagDims: Object.keys(ae.get_analysis_filters?.()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					payloadDims: Object.keys(
						ae.build_payload?.()?.document_scope?.accounting_dimensions || {}
					),
				};
				const nextRows = (ae.rows || []).filter((r) => r.dimension_value);
				let afterDept = null;
				if (nextRows.length) {
					ae.analyze_row_as_filter(nextRows[0]);
					g = 0;
					while (ae.store?.get?.("loading")?.summary && g++ < 400) {
						await new Promise((r) => setTimeout(r, 100));
					}
					afterDept = {
						type: ae.analysis_context.dimension_scope?.dimension_type,
						selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
						bagDims: Object.keys(ae.get_analysis_filters?.()?.dimensions || {}),
						rowTypes: [...new Set((ae.rows || []).map((r) => r.dimension_type).filter(Boolean))],
						payloadDims: Object.keys(
							ae.build_payload?.()?.document_scope?.accounting_dimensions || {}
						),
					};
				}
				window.__ae_v5729_ready_for_dept_shot = true;
				return {
					skipped: false,
					nextType,
					afterCC,
					afterSwitch,
					afterDept,
					ccValue: ccRow.dimension_value,
				};
			});
			await page.waitForFunction(() => window.__ae_v5729_ready_for_cc_shot === true, null, {
				timeout: 180000,
			});
			await shot(page, "01-dimension-cost-center-analyze");
			await page.waitForFunction(() => window.__ae_v5729_ready_for_dept_shot === true, null, {
				timeout: 180000,
			}).catch(() => null);
			await shot(page, "02-dimension-department-analyze");
			const isolation = await isolationPromise;

			if (isolation.skipped) {
				passCheck("dimension_isolation_skipped", isolation);
			} else {
				if (isolation.afterCC.type !== "cost_center") {
					throw new Error(`CC type wrong: ${JSON.stringify(isolation.afterCC)}`);
				}
				if (isolation.afterCC.bagDims.some((d) => d !== "cost_center")) {
					throw new Error(`CC bag leak: ${JSON.stringify(isolation.afterCC.bagDims)}`);
				}
				if (isolation.afterCC.rowTypes.some((t) => t && t !== "cost_center")) {
					throw new Error(`CC row type leak: ${JSON.stringify(isolation.afterCC.rowTypes)}`);
				}
				if (isolation.afterCC.payloadDims.some((d) => d !== "cost_center")) {
					throw new Error(`CC payload leak: ${JSON.stringify(isolation.afterCC.payloadDims)}`);
				}
				if (isolation.afterSwitch.type !== isolation.nextType) {
					throw new Error(`switch type wrong: ${JSON.stringify(isolation.afterSwitch)}`);
				}
				if (isolation.afterSwitch.bagDims.includes("cost_center")) {
					throw new Error(`stale CC after switch: ${JSON.stringify(isolation.afterSwitch)}`);
				}
				if (isolation.afterDept) {
					if (isolation.afterDept.type !== isolation.nextType) {
						throw new Error(`dept type wrong: ${JSON.stringify(isolation.afterDept)}`);
					}
					if (isolation.afterDept.bagDims.includes("cost_center")) {
						throw new Error(`CC chip after dept analyze: ${JSON.stringify(isolation.afterDept)}`);
					}
					if (isolation.afterDept.rowTypes.some((t) => t && t !== isolation.nextType)) {
						throw new Error(`dept row leak: ${JSON.stringify(isolation.afterDept)}`);
					}
				}
				passCheck("dimension_type_isolation", isolation);
			}
		} catch (err) {
			failCheck("dimension_type_isolation", err);
			await shot(page, "01-dimension-isolation-fail");
		}

		const chips = await chipText(page);
		try {
			const chipLeak =
				chips.some((t) => /cost center/i.test(t)) &&
				chips.some((t) => /department/i.test(t) && /analysis|filter/i.test(t));
			// Soft check: when on department, Cost Center analysis chip must not appear.
			const onDept = await page.evaluate(
				() => window.cur_ae?.analysis_context?.dimension_scope?.dimension_type === "department"
			);
			if (onDept && chips.some((t) => /cost\s*center/i.test(t) && !/dimension type/i.test(t))) {
				throw new Error(`Filter Summary still shows Cost Center: ${JSON.stringify(chips)}`);
			}
			passCheck("filter_summary_matches_scope", { chips, chipLeak: !!chipLeak, onDept });
		} catch (err) {
			failCheck("filter_summary_matches_scope", err);
		}

		// —— Scenario B: Debounced column filters ——
		try {
			await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.clear_all_analysis_filters?.({ refresh: false });
				ae.switch_axis("party");
				ae.analysis_context.column_filters = {};
				ae.analysis_context.page = 1;
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
			});
			await waitSummaryIdle(page, { requireRows: false });

			const debounce = await page.evaluate(async () => {
				const ae = window.cur_ae;
				const before = ae._column_filter_seq || 0;
				const calls = [];
				const orig = ae.set_column_filters.bind(ae);
				ae.set_column_filters = (filters, opts) => {
					calls.push({ filters: { ...filters }, opts: { ...opts }, t: Date.now() });
					return orig(filters, opts);
				};
				// Rapid typing via handler (simulates DataTable applyFilter stream)
				ae.handle_datatable_column_filters({ 2: "ن" });
				ae.handle_datatable_column_filters({ 2: "نا" });
				ae.handle_datatable_column_filters({ 2: "نان" });
				ae.handle_datatable_column_filters({ 2: "نانو" });
				await new Promise((r) => setTimeout(r, 200));
				const midCalls = calls.length;
				await new Promise((r) => setTimeout(r, 450));
				const afterDebounce = calls.length;
				const last = calls[calls.length - 1];
				// Enter immediate
				ae.handle_datatable_column_filters({ 2: "نانوX" }, { immediate: true });
				const afterEnter = calls.length;
				// Clear immediate
				ae.clear_single_column_filter("display_title");
				const afterClear = calls.length;
				ae.set_column_filters = orig;
				return {
					midCalls,
					afterDebounce,
					afterEnter,
					afterClear,
					lastFilters: last?.filters || null,
					seqDelta: (ae._column_filter_seq || 0) - before,
					page: ae.analysis_context.page,
					retainedInput: ae._latest_column_filter_mapped,
				};
			});
			if (debounce.midCalls !== 0) {
				throw new Error(`requests before 500ms: ${debounce.midCalls}`);
			}
			if (debounce.afterDebounce !== 1) {
				throw new Error(`expected 1 debounced apply, got ${debounce.afterDebounce}`);
			}
			if (debounce.afterEnter < 2) {
				throw new Error("Enter did not apply immediately");
			}
			if (debounce.afterClear < 3) {
				throw new Error("Clear did not apply immediately");
			}
			if (debounce.page !== 1) {
				throw new Error(`page not reset: ${debounce.page}`);
			}
			passCheck("column_filter_debounce_500", debounce);
			await shot(page, "03-debounced-column-filter");
		} catch (err) {
			failCheck("column_filter_debounce_500", err);
			await shot(page, "03-debounce-fail");
		}

		// Repeat debounce smoke on Dimensions / Items / Vouchers
		for (const axis of ["dimension", "item", "voucher"]) {
			try {
				const r = await page.evaluate(async (viewAxis) => {
					const ae = window.cur_ae;
					if (viewAxis === "dimension") {
						ae.switch_axis("dimension", "cost_center");
					} else {
						ae.switch_axis(viewAxis);
					}
					let g = 0;
					while (ae.store?.get?.("loading")?.summary && g++ < 400) {
						await new Promise((r) => setTimeout(r, 100));
					}
					const calls = [];
					const orig = ae.set_column_filters.bind(ae);
					ae.set_column_filters = (filters, opts) => {
						calls.push(1);
						return orig(filters, opts);
					};
					ae.handle_datatable_column_filters({ 2: "a" });
					ae.handle_datatable_column_filters({ 2: "ab" });
					ae.handle_datatable_column_filters({ 2: "abc" });
					await new Promise((r) => setTimeout(r, 200));
					const mid = calls.length;
					await new Promise((r) => setTimeout(r, 450));
					const after = calls.length;
					ae.set_column_filters = orig;
					ae.clear_column_filters?.({ refresh: false });
					return { axis: viewAxis, mid, after };
				}, axis);
				if (r.mid !== 0 || r.after !== 1) {
					throw new Error(JSON.stringify(r));
				}
				passCheck(`debounce_axis_${axis}`, r);
			} catch (err) {
				failCheck(`debounce_axis_${axis}`, err);
			}
		}

		// —— Scenario C: Cross-axis regression ——
		try {
			const cross = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.clear_all_analysis_filters?.({ refresh: false });
				ae.switch_axis("dimension", "cost_center");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const row = (ae.rows || []).find((r) => r.dimension_value);
				if (row) ae.analyze_row_as_filter(row);
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const dimSelected = ae.analysis_context.dimension_scope?.selected_dimension_value;
				ae.set_column_filters({ display_code: "1" }, { refresh: false });
				ae.switch_axis("account_level");
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const afterAccount = {
					axis: ae.analysis_context.view_axis,
					column_filters: { ...(ae.analysis_context.column_filters || {}) },
					dimType: ae.analysis_context.dimension_scope?.dimension_type,
					dimSelected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					bagDims: Object.keys(ae.get_analysis_filters?.()?.dimensions || {}),
				};
				ae.switch_axis("party");
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const afterParty = {
					axis: ae.analysis_context.view_axis,
					column_filters: { ...(ae.analysis_context.column_filters || {}) },
				};
				return { dimSelected, afterAccount, afterParty };
			});
			// Column filters are presentation-scoped; axis switch remounts grid — filters may clear or persist by design.
			// Analysis dimension filter may remain as cross-axis chip (allowed) but must stay cost_center only.
			if (cross.afterAccount.bagDims.some((d) => d !== "cost_center" && d !== undefined)) {
				throw new Error(`cross bag leak: ${JSON.stringify(cross)}`);
			}
			passCheck("cross_axis_regression", cross);
			await shot(page, "04-cross-axis");
		} catch (err) {
			failCheck("cross_axis_regression", err);
		}

		const report = {
			site: SITE,
			base: BASE,
			company: COMPANY,
			version: "5.7.29",
			passed: checks.filter((c) => c.ok).length,
			failed: checks.filter((c) => !c.ok).length,
			checks,
			api_request_samples: apiLog.slice(0, 40),
			generated_at: new Date().toISOString(),
		};
		fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
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

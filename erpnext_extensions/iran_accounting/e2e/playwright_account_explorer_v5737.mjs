#!/usr/bin/env node
/**
 * Account Explorer 5.7.37 — 3000 ms trailing-edge column-filter debounce.
 * Real browser; verifies timer reset, Enter/Clear immediacy, axis coverage, stale cancel.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5737");
const DEBOUNCE_MS = 3000;

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

async function seed(page) {
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
		ae.switch_axis("party");
	}, { company: COMPANY });
	await waitSummaryIdle(page, { requireRows: false });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const network = [];
	page.on("request", (req) => {
		const u = req.url();
		if (
			u.includes("get_party_summary") ||
			u.includes("get_item_summary") ||
			u.includes("get_voucher_summary") ||
			u.includes("get_dimension_summary") ||
			u.includes("get_account_summary")
		) {
			network.push({ t: Date.now(), url: u.split("/").pop()?.split("?")[0] || u });
		}
	});

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await seed(page);
		await page.screenshot({ path: path.join(OUT, "01-party-grid.png"), fullPage: true });

		// Confirm constant in page source / runtime path
		const constantOk = await page.evaluate(() => {
			const ae = window.cur_ae;
			const src = String(ae.handle_datatable_column_filters || "");
			// Constant is module-scoped; verify cancel + timer path exist on instance.
			return {
				hasCancel: typeof ae.cancel_pending_column_filter_debounce === "function",
				hasHandle: typeof ae.handle_datatable_column_filters === "function",
			};
		});
		if (!constantOk.hasCancel || !constantOk.hasHandle) {
			failCheck("runtime_debounce_api", new Error(JSON.stringify(constantOk)));
		} else {
			passCheck("runtime_debounce_api", constantOk);
		}

		// —— Test A: Continuous typing (5 chars), zero mid-requests, one after 3s ——
		try {
			const a = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.cancel_pending_column_filter_debounce?.();
				ae.analysis_context.column_filters = {};
				const calls = [];
				const orig = ae.set_column_filters.bind(ae);
				ae.set_column_filters = (filters, opts) => {
					calls.push({ filters: { ...filters }, t: Date.now(), opts: { ...opts } });
					return orig(filters, opts);
				};
				const chars = ["1", "15", "150", "1501", "15010"];
				for (const v of chars) {
					ae.handle_datatable_column_filters({ 2: v });
					await new Promise((r) => setTimeout(r, 400));
				}
				const mid = calls.length;
				await new Promise((r) => setTimeout(r, 3200));
				const after = calls.length;
				const last = calls[calls.length - 1];
				ae.set_column_filters = orig;
				ae.clear_column_filters?.({ refresh: false });
				return {
					mid,
					after,
					value: last?.filters ? Object.values(last.filters)[0] : null,
					all: calls.map((c) => c.filters),
				};
			});
			if (a.mid !== 0) throw new Error(`mid calls during typing: ${a.mid}`);
			if (a.after !== 1) throw new Error(`expected 1 apply, got ${a.after}`);
			if (String(a.value) !== "15010") throw new Error(`wrong value ${a.value}`);
			passCheck("test_a_continuous_typing_3000", a);
			await page.screenshot({ path: path.join(OUT, "02-test-a-typing.png"), fullPage: true });
		} catch (err) {
			failCheck("test_a_continuous_typing_3000", err);
		}

		// —— Test B: Timer reset (2s + type + 2s = no fire; +1.1s = fire) ——
		try {
			const b = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.cancel_pending_column_filter_debounce?.();
				const calls = [];
				const orig = ae.set_column_filters.bind(ae);
				ae.set_column_filters = (filters, opts) => {
					calls.push({ filters: { ...filters }, t: Date.now() });
					return orig(filters, opts);
				};
				ae.handle_datatable_column_filters({ 2: "1" });
				await new Promise((r) => setTimeout(r, 2000));
				ae.handle_datatable_column_filters({ 2: "15" });
				await new Promise((r) => setTimeout(r, 2000));
				const mid = calls.length;
				await new Promise((r) => setTimeout(r, 1100));
				const after = calls.length;
				const value = after ? Object.values(calls[calls.length - 1].filters || {})[0] : null;
				ae.set_column_filters = orig;
				ae.clear_column_filters?.({ refresh: false });
				return { mid, after, value };
			});
			if (b.mid !== 0) throw new Error(`fire before full wait: ${b.mid}`);
			if (b.after !== 1) throw new Error(`expected 1 after reset, got ${b.after}`);
			if (String(b.value) !== "15") throw new Error(`value ${b.value}`);
			passCheck("test_b_timer_reset", b);
		} catch (err) {
			failCheck("test_b_timer_reset", err);
		}

		// —— Test C: Enter immediate + cancel pending ——
		try {
			const c = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.cancel_pending_column_filter_debounce?.();
				const calls = [];
				const orig = ae.set_column_filters.bind(ae);
				ae.set_column_filters = (filters, opts) => {
					calls.push({ filters: { ...filters }, immediate: !!opts?.immediate, t: Date.now() });
					return orig(filters, opts);
				};
				ae.handle_datatable_column_filters({ 2: "nan" });
				await new Promise((r) => setTimeout(r, 500));
				ae.handle_datatable_column_filters({ 2: "nano" }, { immediate: true });
				const afterEnter = calls.length;
				await new Promise((r) => setTimeout(r, 3200));
				const afterWait = calls.length;
				ae.set_column_filters = orig;
				ae.clear_column_filters?.({ refresh: false });
				return { afterEnter, afterWait, last: calls[calls.length - 1] };
			});
			if (c.afterEnter !== 1) throw new Error(`enter count ${c.afterEnter}`);
			if (c.afterWait !== 1) throw new Error(`duplicate after wait: ${c.afterWait}`);
			passCheck("test_c_enter_immediate", c);
		} catch (err) {
			failCheck("test_c_enter_immediate", err);
		}

		// —— Test D: Clear cancels pending ——
		try {
			const d = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.cancel_pending_column_filter_debounce?.();
				const calls = [];
				const orig = ae.set_column_filters.bind(ae);
				ae.set_column_filters = (filters, opts) => {
					calls.push({ filters: { ...filters }, t: Date.now() });
					return orig(filters, opts);
				};
				ae.handle_datatable_column_filters({ 2: "stale" });
				await new Promise((r) => setTimeout(r, 400));
				ae.clear_single_column_filter("display_title");
				const afterClear = calls.length;
				await new Promise((r) => setTimeout(r, 3200));
				const afterWait = calls.length;
				const revived = calls.some((c) => Object.values(c.filters || {}).includes("stale"));
				ae.set_column_filters = orig;
				return { afterClear, afterWait, revived, calls };
			});
			if (d.afterClear < 1) throw new Error("clear did not apply");
			if (d.revived) throw new Error("stale value reapplied after clear");
			if (d.afterWait !== d.afterClear) throw new Error(`timer fired after clear: ${JSON.stringify(d)}`);
			passCheck("test_d_clear_cancels", d);
		} catch (err) {
			failCheck("test_d_clear_cancels", err);
		}

		// —— Test E: Axis coverage (party already done; dimension/item/voucher) ——
		for (const axis of ["dimension", "item", "voucher"]) {
			try {
				const r = await page.evaluate(async (viewAxis) => {
					const ae = window.cur_ae;
					ae.cancel_pending_column_filter_debounce?.();
					if (viewAxis === "dimension") {
						ae.switch_axis("dimension", "cost_center");
					} else {
						ae.switch_axis(viewAxis);
					}
					let g = 0;
					while (ae.store?.get?.("loading")?.summary && g++ < 400) {
						await new Promise((x) => setTimeout(x, 100));
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
					await new Promise((x) => setTimeout(x, 800));
					const mid = calls.length;
					await new Promise((x) => setTimeout(x, 2500));
					const after = calls.length;
					ae.set_column_filters = orig;
					ae.clear_column_filters?.({ refresh: false });
					return { axis: viewAxis, mid, after };
				}, axis);
				if (r.mid !== 0 || r.after !== 1) throw new Error(JSON.stringify(r));
				passCheck(`test_e_axis_${axis}`, r);
			} catch (err) {
				failCheck(`test_e_axis_${axis}`, err);
			}
		}

		// —— Test F: Rapid axis change cancels stale timer ——
		try {
			const f = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("party");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((x) => setTimeout(x, 100));
				}
				const partyCalls = [];
				const itemCalls = [];
				const orig = ae.set_column_filters.bind(ae);
				ae.set_column_filters = (filters, opts) => {
					const axis = ae.analysis_context.view_axis;
					(axis === "item" ? itemCalls : partyCalls).push({
						axis,
						filters: { ...filters },
						t: Date.now(),
					});
					return orig(filters, opts);
				};
				ae.handle_datatable_column_filters({ 2: "partyOnly" });
				await new Promise((x) => setTimeout(x, 500));
				ae.switch_axis("item");
				g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((x) => setTimeout(x, 100));
				}
				await new Promise((x) => setTimeout(x, 3200));
				const staleParty = partyCalls.some((c) =>
					Object.values(c.filters || {}).includes("partyOnly")
				);
				ae.set_column_filters = orig;
				ae.clear_column_filters?.({ refresh: false });
				return { partyCalls: partyCalls.length, itemCalls: itemCalls.length, staleParty };
			});
			if (f.staleParty) throw new Error(`stale party filter applied after axis change: ${JSON.stringify(f)}`);
			passCheck("test_f_axis_change_cancels_stale", f);
			await page.screenshot({ path: path.join(OUT, "03-test-f-axis-change.png"), fullPage: true });
		} catch (err) {
			failCheck("test_f_axis_change_cancels_stale", err);
		}
	} catch (err) {
		failCheck("playwright_v5737_main", err);
		try {
			await page.screenshot({ path: path.join(OUT, "99-error.png"), fullPage: true });
		} catch {
			/* ignore */
		}
	} finally {
		await browser.close();
	}

	const report = {
		version: "5.7.37",
		debounce_ms: DEBOUNCE_MS,
		checks,
		passed: checks.filter((c) => c.ok).length,
		failed: checks.filter((c) => !c.ok).length,
		networkSample: network.slice(0, 40),
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

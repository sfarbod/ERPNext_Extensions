#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 — Dimension data accuracy (browser).
 * Proves Department filters constrain GL results after axis navigation.
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
const DEPT = "ایمنی و بهداشت - E";
const EXPECTED_PD = 5592288032;
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5729-data");

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err, detail = null) =>
	checks.push({ name, ok: false, err: err?.stack || err?.message || String(err), detail });

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

async function waitIdle(page) {
	await page.evaluate(async () => {
		const ae = window.cur_ae;
		let g = 0;
		while (ae.store?.get?.("loading")?.summary && g++ < 400) {
			await new Promise((x) => setTimeout(x, 100));
		}
	});
}

async function shot(page, name) {
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function reset(page) {
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
		ae._dimension_type_cleared = {};
	}, { company: COMPANY });
	await waitIdle(page);
}

async function analyzeDept(page) {
	await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
	await waitIdle(page);
	await page.evaluate((dept) => {
		const ae = window.cur_ae;
		const row = (ae.rows || []).find((r) => String(r.dimension_value || "") === dept);
		if (!row) throw new Error(`department row missing: ${dept}`);
		ae.analyze_row_as_filter(row);
	}, DEPT);
	await waitIdle(page);
}

async function accountSnapshot(page) {
	return page.evaluate(() => {
		const ae = window.cur_ae;
		const p = ae.build_payload();
		const rows = ae.rows || [];
		return {
			payloadDocDims: p.document_scope.accounting_dimensions,
			payloadSelected: p.analysis_context.dimension_scope,
			sumPD: rows.reduce((s, r) => s + Number(r.period_debit || 0), 0),
			sumPC: rows.reduce((s, r) => s + Number(r.period_credit || 0), 0),
			codes: rows.map((r) => r.display_code),
			loanish: rows
				.filter((r) => /وام|تسهیلات|loan/i.test(`${r.display_title || ""}${r.display_code || ""}`))
				.map((r) => r.display_code),
			chips: Array.from(document.querySelectorAll(".ae-filter-chip")).map((el) => ({
				group: el.getAttribute("data-group"),
				key: el.getAttribute("data-key"),
				text: (el.innerText || "").replace(/\s+/g, " ").trim(),
			})),
		};
	});
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await waitIdle(page);

		// T1: Analyze department → dimension summary is filtered + payload correct
		try {
			await reset(page);
			await analyzeDept(page);
			const s = await page.evaluate(() => {
				const ae = window.cur_ae;
				const p = ae.build_payload();
				const rows = ae.rows || [];
				return {
					payloadDocDims: p.document_scope.accounting_dimensions,
					payloadSelected: p.analysis_context.dimension_scope,
					values: rows.map((r) => r.dimension_value),
					period_debit: rows[0]?.period_debit,
					period_credit: rows[0]?.period_credit,
				};
			});
			if (s.payloadDocDims.department !== DEPT) throw new Error("payload missing department");
			if (s.payloadSelected.selected_dimension_value !== DEPT) {
				throw new Error("selected_dimension_value missing");
			}
			if (JSON.stringify(s.values) !== JSON.stringify([DEPT])) {
				throw new Error(`unexpected rows ${JSON.stringify(s.values)}`);
			}
			if (Math.abs(Number(s.period_debit) - EXPECTED_PD) > 1) {
				throw new Error(`period_debit ${s.period_debit} != ${EXPECTED_PD}`);
			}
			passCheck("T1 analyze department payload+row", s);
			await shot(page, "T1-dept-analyze");
		} catch (e) {
			failCheck("T1 analyze department payload+row", e);
		}

		// T2: Department → Account Levels keeps filter + no loan groups
		try {
			await reset(page);
			await analyzeDept(page);
			await page.evaluate(() => window.cur_ae.switch_axis("account_level", 1));
			await waitIdle(page);
			const s = await accountSnapshot(page);
			if (Math.abs(s.sumPD - EXPECTED_PD) > 1) throw new Error(`sumPD ${s.sumPD}`);
			if (s.codes.includes("21") || s.codes.includes("22")) {
				throw new Error(`loan liability groups present: ${s.codes}`);
			}
			if (s.loanish.length) throw new Error(`loanish ${s.loanish}`);
			if (s.payloadDocDims.department !== DEPT) throw new Error("dept lost on accounts");
			passCheck("T2 dept → accounts filtered", s);
			await shot(page, "T2-dept-accounts");
		} catch (e) {
			failCheck("T2 dept → accounts filtered", e);
		}

		// T3: CRITICAL — Department → Cost Center → Account Levels must KEEP department filter
		try {
			await reset(page);
			await analyzeDept(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			const onCc = await page.evaluate(() => {
				const ae = window.cur_ae;
				const p = ae.build_payload();
				return {
					payloadDocDims: p.document_scope.accounting_dimensions,
					selected: p.analysis_context.dimension_scope,
					bagDept: ae.get_analysis_filters()?.dimensions?.department?.value || null,
				};
			});
			// Global filters: department stays active on Cost Center tab.
			if (onCc.payloadDocDims.department !== DEPT) {
				throw new Error("department missing from Cost Center payload");
			}
			if (onCc.bagDept !== DEPT) throw new Error("department missing from bag on CC");
			await page.evaluate(() => window.cur_ae.switch_axis("account_level", 1));
			await waitIdle(page);
			const s = await accountSnapshot(page);
			if (s.payloadDocDims.department !== DEPT) {
				throw new Error(`department lost on accounts: ${JSON.stringify(s.payloadDocDims)}`);
			}
			if (Math.abs(s.sumPD - EXPECTED_PD) > 1) {
				throw new Error(`unfiltered after CC hop: sumPD=${s.sumPD}`);
			}
			if (s.codes.includes("21") || s.loanish.length) {
				throw new Error("loan groups after CC hop");
			}
			// Chip should be active analysis (not merely parked) once restored into bag.
			const activeChip = s.chips.find(
				(c) => c.key === "dimensions.department" && c.group === "analysis_filters"
			);
			if (!activeChip) throw new Error(`expected active dept chip; chips=${JSON.stringify(s.chips)}`);
			passCheck("T3 dept → CC → accounts keeps filter", { onCc, s });
			await shot(page, "T3-dept-cc-accounts-filtered");
		} catch (e) {
			failCheck("T3 dept → CC → accounts keeps filter", e);
			await shot(page, "T3-fail");
		}

		// T4: Explicit delete then CC → accounts must NOT restore
		try {
			await reset(page);
			await analyzeDept(page);
			await page.evaluate(() =>
				window.cur_ae.remove_analysis_filter_chip("dimensions.department")
			);
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("account_level", 1));
			await waitIdle(page);
			const s = await accountSnapshot(page);
			if (s.payloadDocDims.department) throw new Error("deleted dept resurrected in payload");
			if (Math.abs(s.sumPD - EXPECTED_PD) < 1) {
				throw new Error("still filtered after explicit delete");
			}
			passCheck("T4 deleted dept does not restore on accounts", s);
			await shot(page, "T4-deleted-not-restored");
		} catch (e) {
			failCheck("T4 deleted dept does not restore on accounts", e);
		}

		// T5: Parties / Vouchers after CC hop stay department-scoped
		try {
			await reset(page);
			await analyzeDept(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("voucher"));
			await waitIdle(page);
			const v = await page.evaluate(() => {
				const ae = window.cur_ae;
				const p = ae.build_payload();
				return {
					payloadDocDims: p.document_scope.accounting_dimensions,
					n: (ae.rows || []).length,
				};
			});
			if (v.payloadDocDims.department !== DEPT) throw new Error("voucher payload missing dept");
			await page.evaluate(() => window.cur_ae.switch_axis("party"));
			await waitIdle(page);
			const party = await page.evaluate(() => {
				const p = window.cur_ae.build_payload();
				return p.document_scope.accounting_dimensions;
			});
			if (party.department !== DEPT) throw new Error("party payload missing dept");
			passCheck("T5 vouchers/parties keep dept after CC hop", { v, party });
			await shot(page, "T5-voucher-party");
		} catch (e) {
			failCheck("T5 vouchers/parties keep dept after CC hop", e);
		}

		// T6: Cost Center tab keeps department as global AND (CC list may narrow)
		try {
			await reset(page);
			await analyzeDept(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			const s = await page.evaluate(() => {
				const ae = window.cur_ae;
				const p = ae.build_payload();
				return {
					payloadDocDims: p.document_scope.accounting_dimensions,
					rowCount: (ae.rows || []).length,
					selected: p.analysis_context.dimension_scope,
				};
			});
			if (s.payloadDocDims.department !== DEPT) throw new Error("dept not global on CC");
			passCheck("T6 CC keeps global department filter", s);
			await shot(page, "T6-cc-global-dept");
		} catch (e) {
			failCheck("T6 CC keeps global department filter", e);
		}
	} catch (err) {
		failCheck("fatal", err);
		try {
			await shot(page, "fatal");
		} catch (_) {}
	} finally {
		const passed = checks.filter((c) => c.ok).length;
		const failed = checks.filter((c) => !c.ok).length;
		const report = {
			version: "5.7.29",
			suite: "dimension-data-accuracy",
			company: COMPANY,
			department: DEPT,
			expected_period_debit: EXPECTED_PD,
			passed,
			failed,
			checks,
			generated_at: new Date().toISOString(),
		};
		fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
		console.log(JSON.stringify({ passed, failed, names: checks.map((c) => [c.name, c.ok]) }, null, 2));
		await browser.close();
		process.exit(failed ? 1 : 0);
	}
}

main();

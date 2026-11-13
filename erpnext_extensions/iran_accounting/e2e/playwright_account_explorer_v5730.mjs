#!/usr/bin/env node
/**
 * Account Explorer 5.7.30 — deleted Dimension filters stay deleted;
 * DataTable Code/Title filter inputs align with headers.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5730");

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

async function waitIdle(page) {
	const started = Date.now();
	while (Date.now() - started < 180000) {
		const ready = await page.evaluate(() => !window.cur_ae?.store?.get?.("loading")?.summary);
		if (ready) return;
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
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await waitIdle(page);
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

		try {
			const r = await page.evaluate(async () => {
				const ae = window.cur_ae;
				const wait = async () => {
					let g = 0;
					while (ae.store?.get?.("loading")?.summary && g++ < 400) {
						await new Promise((x) => setTimeout(x, 100));
					}
				};
				ae.switch_axis("dimension", "cost_center");
				await wait();
				const row = (ae.rows || []).find((x) => x.dimension_value);
				if (!row) return { skipped: true };
				ae.analyze_row_as_filter(row);
				await wait();
				ae.remove_analysis_filter_chip("dimensions.cost_center");
				await wait();
				const afterRemove = {
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					parked: ae._dimension_type_parked?.cost_center?.analysis_entry || null,
					cleared: !!ae._dimension_type_cleared?.cost_center,
					payloadSelected:
						ae.build_payload()?.analysis_context?.dimension_scope?.selected_dimension_value ??
						null,
					payloadDims: { ...(ae.build_payload()?.document_scope?.accounting_dimensions || {}) },
				};
				ae.switch_axis("dimension", "department");
				await wait();
				const deptRow = (ae.rows || []).find((x) => x.dimension_value);
				if (deptRow) {
					ae.analyze_row_as_filter(deptRow);
					await wait();
				}
				ae.switch_axis("dimension", "cost_center");
				await wait();
				const afterBack = {
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					parked: ae._dimension_type_parked?.cost_center?.analysis_entry || null,
					payloadSelected:
						ae.build_payload()?.analysis_context?.dimension_scope?.selected_dimension_value ??
						null,
					payloadDims: { ...(ae.build_payload()?.document_scope?.accounting_dimensions || {}) },
					analysisChips: Array.from(document.querySelectorAll(".ae-filter-chip"))
						.map((el) => (el.textContent || "").replace(/\s+/g, " ").trim())
						.filter((t) => /Cost Center|مرکز هزینه/i.test(t) && !/Dimension Type|نوع بُعد/i.test(t)),
				};
				// Re-analyze should work after clear
				const again = (ae.rows || []).find((x) => x.dimension_value);
				if (again) {
					ae.analyze_row_as_filter(again);
					await wait();
				}
				const afterReanalyze = {
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					cleared: !!ae._dimension_type_cleared?.cost_center,
				};
				return { skipped: false, value: row.dimension_value, afterRemove, afterBack, afterReanalyze };
			});
			await shot(page, "01-after-delete-no-resurrect");
			if (r.skipped) {
				passCheck("deleted_dimension_filter_stays_gone", { skipped: true });
			} else {
				if (r.afterRemove.parked || r.afterRemove.selected || r.afterRemove.payloadSelected) {
					throw new Error(`afterRemove dirty: ${JSON.stringify(r.afterRemove)}`);
				}
				if (!r.afterRemove.cleared) throw new Error("cleared flag missing");
				if (r.afterBack.bag.includes("cost_center") || r.afterBack.selected || r.afterBack.parked) {
					throw new Error(`resurrected: ${JSON.stringify(r.afterBack)}`);
				}
				if (r.afterBack.analysisChips.length) {
					throw new Error(`analysis chip returned: ${JSON.stringify(r.afterBack.analysisChips)}`);
				}
				if (!r.afterReanalyze.bag.includes("cost_center") || !r.afterReanalyze.selected) {
					throw new Error(`re-analyze failed: ${JSON.stringify(r.afterReanalyze)}`);
				}
				if (r.afterReanalyze.cleared) throw new Error("cleared flag not reset after re-analyze");
				passCheck("deleted_dimension_filter_stays_gone", r);
			}
			await shot(page, "02-reanalyze-after-clear");
		} catch (err) {
			failCheck("deleted_dimension_filter_stays_gone", err);
			await shot(page, "01-delete-fail");
		}

		try {
			const align = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("dimension", "cost_center");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((x) => setTimeout(x, 100));
				}
				await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
				const host =
					document.querySelector(".ae-grid-container--datatable") ||
					document.querySelector(".ae-datatable-host");
				const measure = (colId) => {
					const filterInput = host?.querySelector(`.dt-filter[data-ae-column-filter="${colId}"]`);
					const filterCell = filterInput?.closest(".dt-cell");
					const colClass = [...(filterCell?.classList || [])].find((c) =>
						c.startsWith("dt-cell--col-")
					);
					const headerCell = colClass ? host.querySelector(`.dt-row-header .${colClass}`) : null;
					const bodyCell = colClass
						? host.querySelector(
								`.dt-row:not(.dt-row-header):not(.dt-row-filter) .${colClass}`
							)
						: null;
					const hb = headerCell?.getBoundingClientRect();
					const fb = filterCell?.getBoundingClientRect();
					const bb = bodyCell?.getBoundingClientRect();
					const cs = filterCell ? getComputedStyle(filterCell) : null;
					return {
						colId,
						position: cs?.position || null,
						deltaLeft: hb && fb ? Math.abs(hb.left - fb.left) : null,
						deltaWidth: hb && fb ? Math.abs(hb.width - fb.width) : null,
						deltaBodyLeft: hb && bb ? Math.abs(hb.left - bb.left) : null,
						headerLeft: hb?.left,
						filterLeft: fb?.left,
					};
				};
				return { display_code: measure("display_code"), display_title: measure("display_title") };
			});
			await shot(page, "03-filter-alignment");
			for (const id of ["display_code", "display_title"]) {
				const m = align[id];
				if (!m || m.deltaLeft == null) throw new Error(`missing ${id}`);
				if (m.deltaLeft > 1 || m.deltaWidth > 2) {
					throw new Error(`misaligned ${id}: ${JSON.stringify(m)}`);
				}
				if (m.position !== "sticky" && id === "display_code") {
					// first data col is sticky when checkbox present
					if (m.position !== "relative" && m.position !== "sticky") {
						throw new Error(`bad position ${id}: ${m.position}`);
					}
				}
			}
			if (align.display_code.position !== "sticky") {
				throw new Error(`Code filter lost sticky: ${align.display_code.position}`);
			}
			passCheck("filter_header_alignment", align);
		} catch (err) {
			failCheck("filter_header_alignment", err);
			await shot(page, "03-alignment-fail");
		}

		const report = {
			version: "5.7.30",
			passed: checks.filter((c) => c.ok).length,
			failed: checks.filter((c) => !c.ok).length,
			checks,
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

#!/usr/bin/env node
/**
 * Reproduce v5.7.30 bugs before fix:
 * 1) Explicitly deleted Dimension analysis filter restored via park/activate
 * 2) Code/Title filter input misalignment vs headers
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
		const ready = await page.evaluate(() => {
			const ae = window.cur_ae;
			return !ae?.store?.get?.("loading")?.summary;
		});
		if (ready) return;
		await page.waitForTimeout(200);
	}
	throw new Error("idle timeout");
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
		}, { company: COMPANY });

		// Issue 1 repro
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
				const afterAnalyze = {
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					parked: ae._dimension_type_parked?.cost_center?.analysis_entry?.value || null,
					chips: Array.from(document.querySelectorAll(".ae-filter-chip")).map((el) =>
						(el.textContent || "").replace(/\s+/g, " ").trim()
					),
				};
				ae.remove_analysis_filter_chip("dimensions.cost_center");
				await wait();
				const afterRemove = {
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					parked: ae._dimension_type_parked?.cost_center?.analysis_entry?.value || null,
					payloadSelected:
						ae.build_payload()?.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
					payloadDims: {
						...(ae.build_payload()?.document_scope?.accounting_dimensions || {}),
					},
					analysisChips: Array.from(
						document.querySelectorAll(".ae-filter-chip[data-ae-filter-group='analysis_filters'], .ae-filter-summary-group--analysis .ae-filter-chip")
					).map((el) => (el.textContent || "").replace(/\s+/g, " ").trim()),
					allChips: Array.from(document.querySelectorAll(".ae-filter-chip")).map((el) =>
						(el.textContent || "").replace(/\s+/g, " ").trim()
					),
				};
				ae.switch_axis("dimension", "department");
				await wait();
				ae.switch_axis("dimension", "cost_center");
				await wait();
				const afterSwitchBack = {
					bag: Object.keys(ae.get_analysis_filters()?.dimensions || {}),
					selected: ae.analysis_context.dimension_scope?.selected_dimension_value,
					parked: ae._dimension_type_parked?.cost_center?.analysis_entry?.value || null,
					payloadSelected:
						ae.build_payload()?.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
					payloadDims: {
						...(ae.build_payload()?.document_scope?.accounting_dimensions || {}),
					},
					analysisChipHasValue: Array.from(document.querySelectorAll(".ae-filter-chip")).some((el) => {
						const t = (el.textContent || "").replace(/\s+/g, " ");
						return /Cost Center|مرکز هزینه/i.test(t) && !/Dimension Type|نوع بُعد/i.test(t) && row.dimension_value && t.includes(String(row.dimension_value).slice(0, 8));
					}),
					bagHasCC: !!ae.get_analysis_filters()?.dimensions?.cost_center,
				};
				return { skipped: false, value: row.dimension_value, afterAnalyze, afterRemove, afterSwitchBack };
			});
			await page.screenshot({ path: path.join(OUT, "repro-01-after-delete-switch.png"), fullPage: true });
			if (r.skipped) {
				passCheck("issue1_skipped", r);
			} else {
				const resurrected =
					r.afterSwitchBack.bagHasCC ||
					r.afterSwitchBack.selected ||
					r.afterSwitchBack.payloadSelected ||
					r.afterSwitchBack.parked;
				if (resurrected) {
					failCheck(
						"issue1_deleted_filter_persists",
						new Error(`deleted filter resurrected: ${JSON.stringify(r.afterSwitchBack)}`)
					);
				} else {
					passCheck("issue1_deleted_filter_persists", { note: "already fixed?", ...r });
				}
				// Also check parked retained immediately after remove
				if (r.afterRemove.parked) {
					failCheck(
						"issue1_parked_after_remove",
						new Error(`park retained after remove: ${r.afterRemove.parked}`)
					);
				} else {
					passCheck("issue1_parked_after_remove", r.afterRemove);
				}
			}
		} catch (err) {
			failCheck("issue1_deleted_filter_persists", err);
		}

		// Issue 2 repro — measure header vs filter cell widths for code/title
		try {
			const align = await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.switch_axis("dimension", "cost_center");
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((x) => setTimeout(x, 100));
				}
				await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
				const host = document.querySelector(".ae-grid-container--datatable") || document.querySelector(".ae-datatable-host");
				const measure = (colId) => {
					const header = host?.querySelector(`.dt-row-header .dt-cell[data-col-index], .dt-row-header .dt-cell`);
					const headers = Array.from(host?.querySelectorAll(".dt-row-header .dt-cell") || []);
					const filters = Array.from(host?.querySelectorAll(".dt-row-filter .dt-cell") || []);
					const bodies = Array.from(host?.querySelectorAll(".dt-row:not(.dt-row-header):not(.dt-row-filter) .dt-cell") || []);
					const idx = headers.findIndex((el) => {
						const input = filters[headers.indexOf(el)]?.querySelector?.(`[data-ae-column-filter="${colId}"]`);
						return !!input || el.className.includes("ae-dt-first-col") && colId === "display_code";
					});
					// Prefer data-ae-column-filter mapping
					let filterInput = host?.querySelector(`.dt-filter[data-ae-column-filter="${colId}"]`);
					let filterCell = filterInput?.closest(".dt-cell");
					let colClass = [...(filterCell?.classList || [])].find((c) => c.startsWith("dt-cell--col-"));
					let headerCell = colClass ? host.querySelector(`.dt-row-header .${colClass}`) : null;
					let bodyCell = colClass
						? host.querySelector(`.dt-row:not(.dt-row-header):not(.dt-row-filter) .${colClass}`)
						: null;
					const hb = headerCell?.getBoundingClientRect();
					const fb = filterCell?.getBoundingClientRect();
					const bb = bodyCell?.getBoundingClientRect();
					const ib = filterInput?.getBoundingClientRect();
					return {
						colId,
						colClass,
						header: hb ? { left: hb.left, width: hb.width, right: hb.right } : null,
						filterCell: fb ? { left: fb.left, width: fb.width, right: fb.right } : null,
						body: bb ? { left: bb.left, width: bb.width, right: bb.right } : null,
						input: ib ? { left: ib.left, width: ib.width, right: ib.right } : null,
						deltaHeaderFilterLeft: hb && fb ? Math.abs(hb.left - fb.left) : null,
						deltaHeaderFilterWidth: hb && fb ? Math.abs(hb.width - fb.width) : null,
						deltaHeaderBodyLeft: hb && bb ? Math.abs(hb.left - bb.left) : null,
						deltaHeaderBodyWidth: hb && bb ? Math.abs(hb.width - bb.width) : null,
						inputOverflowsCell: ib && fb ? ib.right - fb.right > 1 : null,
					};
				};
				return {
					display_code: measure("display_code"),
					display_title: measure("display_title"),
				};
			});
			await page.screenshot({ path: path.join(OUT, "repro-02-filter-alignment.png"), fullPage: true });
			const bad = ["display_code", "display_title"].some((id) => {
				const m = align[id];
				if (!m?.header || !m?.filterCell) return true;
				return m.deltaHeaderFilterLeft > 1 || m.deltaHeaderFilterWidth > 2 || m.inputOverflowsCell;
			});
			if (bad) {
				failCheck("issue2_filter_alignment", new Error(JSON.stringify(align)));
			} else {
				passCheck("issue2_filter_alignment", align);
			}
		} catch (err) {
			failCheck("issue2_filter_alignment", err);
		}

		const report = {
			phase: "repro",
			passed: checks.filter((c) => c.ok).length,
			failed: checks.filter((c) => !c.ok).length,
			checks,
			generated_at: new Date().toISOString(),
		};
		fs.writeFileSync(path.join(OUT, "repro-report.json"), JSON.stringify(report, null, 2));
		console.log(JSON.stringify(report, null, 2));
		process.exit(0); // repro script always exits 0; failures are in report
	} finally {
		await browser.close();
	}
}

main().catch((e) => {
	console.error(e);
	process.exit(1);
});

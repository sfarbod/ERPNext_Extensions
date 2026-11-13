#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 — Global Cross-Axis Filtering E2E (Playwright).
 * Park/Activate isolation is obsolete; filters are global AND constraints.
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
const EXPECTED_PC = 1868092078;
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5729-global");

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err, detail = null) =>
	checks.push({
		name,
		ok: false,
		err: err?.stack || err?.message || String(err),
		detail,
	});

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

async function ensureAe(page) {
	const ok = await page.evaluate(() => {
		const entry = frappe?.pages?.["account-explorer"];
		const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		if (ae) {
			window.cur_ae = ae;
			return true;
		}
		return !!window.cur_ae;
	}).catch(() => false);
	if (!ok) {
		await resolveAe(page);
	}
}

async function waitIdle(page) {
	await ensureAe(page);
	try {
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			if (!ae) return;
			let g = 0;
			while (ae.store?.get?.("loading")?.summary && g++ < 500) {
				await new Promise((x) => setTimeout(x, 100));
			}
		});
	} catch (err) {
		// Soft navigation / remount — rebind and continue.
		if (/Execution context was destroyed|Target closed/i.test(String(err))) {
			await page.waitForLoadState("domcontentloaded").catch(() => {});
			await resolveAe(page);
			await page.evaluate(async () => {
				const ae = window.cur_ae;
				if (!ae) return;
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 200) {
					await new Promise((x) => setTimeout(x, 100));
				}
			});
			return;
		}
		throw err;
	}
}

async function shot(page, name) {
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function reset(page) {
	await ensureAe(page);
	await page.evaluate(async ({ company }) => {
		const ae = window.cur_ae;
		if (!ae) throw new Error("cur_ae missing in reset");
		ae.document_scope.company = company;
		ae.document_scope.fiscal_year = "1405";
		ae._dates_user_override = false;
		if (ae.company_field?.set_value) await ae.company_field.set_value(company);
		if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
		await ae.sync_dates_from_fy({ reason: "fiscal_year_change", force: true });
		ae.clear_all_analysis_filters?.({ refresh: false });
		ae._dimension_type_ui = {};
		ae._dimension_type_parked = {};
		ae._dimension_type_cleared = {};
		ae.analysis_context.dimension_scope = {
			...(ae.analysis_context.dimension_scope || {}),
			selected_dimension_value: null,
		};
		ae.document_scope.accounting_dimensions = {};
	}, { company: COMPANY });
	await waitIdle(page);
}

async function analyzeDimensionValue(page, field, exact = null) {
	await page.evaluate((f) => window.cur_ae.switch_axis("dimension", f), field);
	await waitIdle(page);
	// Temporarily clear THIS type's filter to list candidates; keep sibling globals.
	await page.evaluate(() => {
		const ae = window.cur_ae;
		const field = ae.analysis_context.dimension_scope?.dimension_type;
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		if (!field) return;
		let bag = ae.get_analysis_filters();
		if (!bag.dimensions?.[field]) return;
		bag = AF.remove_entry(bag, `dimensions.${field}`);
		ae.set_analysis_filters_bag(bag);
		ae.analysis_context.dimension_scope = {
			dimension_type: field,
			selected_dimension_value: null,
		};
		const dims = { ...(ae.document_scope.accounting_dimensions || {}) };
		delete dims[field];
		ae.document_scope.accounting_dimensions = dims;
		ae._sync_scopes_from_analysis_filters?.();
		ae.refresh_summary({ new_execution: true, reason: "global_list_reload" });
	});
	await waitIdle(page);
	const found = await page.evaluate(({ exact }) => {
		const ae = window.cur_ae;
		const rows = ae.rows || [];
		let row = rows.find((r) => {
			const v = String(r.dimension_value || "");
			if (exact != null) return v === exact;
			return !!v;
		});
		if (!row && exact) {
			row = rows.find((r) => String(r.dimension_value || "").includes(String(exact).slice(0, 8)));
		}
		if (!row) {
			return { found: null, available: rows.slice(0, 8).map((r) => r.dimension_value) };
		}
		ae.analyze_row_as_filter(row);
		return { found: row.dimension_value };
	}, { exact });
	await waitIdle(page);
	if (!found?.found) {
		throw new Error(
			`no row for ${field} (${exact || "any"}); sample=${JSON.stringify(found?.available || [])}`
		);
	}
	return found.found;
}

async function snap(page, label) {
	return page.evaluate((lbl) => {
		const ae = window.cur_ae;
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		const bag = ae.get_analysis_filters();
		const payload = ae.build_payload();
		const rows = ae.rows || [];
		const summaryRows = AF.build_summary_rows(bag, {
			document_scope: ae.document_scope,
			analysis_context: ae.analysis_context,
			metadata: ae.metadata,
		});
		const totals = ae.store?.get?.("totals") || ae.totals || {};
		return {
			label: lbl,
			view_axis: ae.analysis_context.view_axis,
			dimension_type: ae.analysis_context.dimension_scope?.dimension_type || null,
			selected: ae.analysis_context.dimension_scope?.selected_dimension_value ?? null,
			bag_dims: Object.fromEntries(
				Object.entries(bag.dimensions || {}).map(([k, v]) => [k, v?.value ?? v])
			),
			bag_keys: Object.keys(bag).filter((k) => k !== "dimensions" && bag[k]),
			payloadDocDims: { ...(payload.document_scope?.accounting_dimensions || {}) },
			payloadSelected:
				payload.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
			payloadDimType: payload.analysis_context?.dimension_scope?.dimension_type ?? null,
			rowCount: rows.length,
			sumPD: rows.reduce((s, r) => s + Number(r.period_debit || 0), 0),
			sumPC: rows.reduce((s, r) => s + Number(r.period_credit || 0), 0),
			totalsPD: Number(totals.period_debit || 0),
			totalsPC: Number(totals.period_credit || 0),
			chips: summaryRows
				.filter((r) => r.group === "analysis_filters")
				.map((r) => ({ key: r.key, value: r.value })),
			parkedChipCount: summaryRows.filter((r) => r.group === "parked_dimension_filters").length,
		};
	}, label);
}

function assertDeptGlobal(s, where) {
	if (s.payloadDocDims.department !== DEPT) {
		throw new Error(`${where}: payload missing department (${JSON.stringify(s.payloadDocDims)})`);
	}
	if (s.bag_dims.department !== DEPT) {
		throw new Error(`${where}: bag missing department`);
	}
	if (!s.chips.some((c) => c.key === "dimensions.department")) {
		throw new Error(`${where}: department chip missing`);
	}
	if (s.parkedChipCount) {
		throw new Error(`${where}: parked chips must not appear`);
	}
}

async function hopDim(page, field) {
	await ensureAe(page);
	await page.evaluate((f) => window.cur_ae.switch_axis("dimension", f), field);
	await waitIdle(page);
	await ensureAe(page);
}

async function hopAxis(page, axis, arg = null) {
	await ensureAe(page);
	await page.evaluate(({ axis, arg }) => window.cur_ae.switch_axis(axis, arg), { axis, arg });
	await waitIdle(page);
	await ensureAe(page);
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
		await reset(page);

		// E2E 1 — Cross-axis persistence of Department
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			const tour = [
				["dimension", "cost_center"],
				["account_level", 1],
				["party", null],
				["voucher", null],
				["item", null],
				["currency", null],
				["item_group", null],
				["dimension", "department"],
			];
			const snaps = [];
			for (const [axis, arg] of tour) {
				if (axis === "dimension") await hopDim(page, arg);
				else await hopAxis(page, axis, arg);
				const s = await snap(page, `E2E1 ${axis}:${arg}`);
				assertDeptGlobal(s, `E2E1 ${axis}`);
				snaps.push(s);
			}
			passCheck("E2E1 cross-axis department persistence", { hops: snaps.length });
			await shot(page, "E2E1-persistence");
		} catch (e) {
			failCheck("E2E1 cross-axis department persistence", e);
			await shot(page, "E2E1-fail");
		}

		// E2E 2 — Multiple filters from different axes
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			await hopAxis(page, "account_level", 1);
			const account = await page.evaluate(() => {
				const ae = window.cur_ae;
				const row =
					(ae.rows || []).find((r) => r.selected_account && !cint(r.is_group)) ||
					(ae.rows || []).find((r) => r.selected_account || r.is_virtual_group);
				if (!row) return null;
				ae.analyze_row_as_filter(row);
				return row.selected_account || row.row_key || true;
			});
			await waitIdle(page);
			if (!account) throw new Error("no account row to analyze under department");
			await hopAxis(page, "party");
			await page.evaluate(() => {
				const ae = window.cur_ae;
				const row = (ae.rows || []).find((r) => r.party);
				if (row) ae.analyze_row_as_filter(row);
			});
			await waitIdle(page);
			const axes = ["dimension", "account_level", "party", "voucher", "item", "currency"];
			for (const axis of axes) {
				if (axis === "dimension") await hopDim(page, "department");
				else await hopAxis(page, axis, axis === "account_level" ? 1 : null);
				const s = await snap(page, `E2E2 ${axis}`);
				assertDeptGlobal(s, `E2E2 ${axis}`);
				if (!s.bag_keys.includes("account") && !s.chips.some((c) => c.key === "account")) {
					throw new Error(`E2E2 account filter lost on ${axis}`);
				}
			}
			passCheck("E2E2 multi-axis filters remain global");
			await shot(page, "E2E2-multi");
		} catch (e) {
			failCheck("E2E2 multi-axis filters remain global", e);
			await shot(page, "E2E2-fail");
		}

		// E2E 3 — Empty intersection
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			await page.evaluate(() => {
				const ae = window.cur_ae;
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				const entry = {
					key: "dimensions.project",
					value: "__AE_GLOBAL_EMPTY_PROJECT__",
					lifetime: "session",
					origin: "analyze",
					removable: true,
					meta: { dimension_type: "project" },
				};
				ae.set_analysis_filters_bag(AF.set_entry(ae.get_analysis_filters(), entry));
				ae._sync_scopes_from_analysis_filters?.();
			});
			for (const [axis, arg] of [
				["account_level", 1],
				["party", null],
				["voucher", null],
				["item", null],
				["dimension", "department"],
			]) {
				if (axis === "dimension") await hopDim(page, arg);
				else await hopAxis(page, axis, arg);
				await page.evaluate(() =>
					window.cur_ae.refresh_summary({ new_execution: true, reason: "empty" })
				);
				await waitIdle(page);
				const s = await snap(page, `E2E3 ${axis}`);
				assertDeptGlobal(s, `E2E3 ${axis}`);
				if (s.payloadDocDims.project !== "__AE_GLOBAL_EMPTY_PROJECT__") {
					throw new Error(`project filter dropped on ${axis}`);
				}
				if (s.rowCount !== 0) {
					throw new Error(`expected 0 rows on ${axis}, got ${s.rowCount}`);
				}
				if (Math.abs(s.sumPD - EXPECTED_PD) < 1) {
					throw new Error(`fell back to dept-only totals on ${axis}`);
				}
			}
			passCheck("E2E3 empty intersection stays empty");
			await shot(page, "E2E3-empty");
		} catch (e) {
			failCheck("E2E3 empty intersection stays empty", e);
			await shot(page, "E2E3-fail");
		}

		// E2E 4 — Individual filter deletion
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			const cc = await analyzeDimensionValue(page, "cost_center");
			await hopDim(page, "project");
			await page.evaluate(() => {
				const ae = window.cur_ae;
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				ae.set_analysis_filters_bag(
					AF.set_entry(ae.get_analysis_filters(), {
						key: "dimensions.project",
						value: "PRJ-KEEP",
						lifetime: "session",
						origin: "analyze",
						removable: true,
						meta: { dimension_type: "project" },
					})
				);
				ae._sync_scopes_from_analysis_filters?.();
			});
			await hopAxis(page, "account_level", 1);
			await page.evaluate(() =>
				window.cur_ae.remove_analysis_filter_chip("dimensions.project")
			);
			await waitIdle(page);
			const after = await snap(page, "E2E4 after delete project");
			if (after.bag_dims.project || after.payloadDocDims.project) {
				throw new Error("project not removed");
			}
			if (after.bag_dims.department !== DEPT || after.bag_dims.cost_center !== cc) {
				throw new Error(`siblings lost: ${JSON.stringify(after.bag_dims)}`);
			}
			await hopDim(page, "department");
			await hopDim(page, "cost_center");
			await hopAxis(page, "party");
			const tour = await snap(page, "E2E4 after tour");
			if (tour.bag_dims.project) throw new Error("project resurrected");
			if (tour.bag_dims.department !== DEPT) throw new Error("dept lost");
			passCheck("E2E4 individual filter deletion", { cc, after });
			await shot(page, "E2E4-delete");
		} catch (e) {
			failCheck("E2E4 individual filter deletion", e);
			await shot(page, "E2E4-fail");
		}

		// E2E 5 — Clear All Filters
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			await analyzeDimensionValue(page, "cost_center");
			await hopAxis(page, "account_level", 1);
			await page.evaluate(() => window.cur_ae.clear_all_analysis_filters({ refresh: true }));
			await waitIdle(page);
			const s = await snap(page, "E2E5 cleared");
			if (Object.keys(s.bag_dims).length) throw new Error("bag dims remain");
			if (Object.keys(s.payloadDocDims).length) throw new Error("payload dims remain");
			if (s.chips.length) throw new Error("analysis chips remain");
			await hopDim(page, "department");
			await hopAxis(page, "party");
			const back = await snap(page, "E2E5 after switch");
			if (Object.keys(back.bag_dims).length) throw new Error("filters resurrected");
			passCheck("E2E5 clear all filters");
			await shot(page, "E2E5-clear");
		} catch (e) {
			failCheck("E2E5 clear all filters", e);
			await shot(page, "E2E5-fail");
		}

		// E2E 6 — Multiple Dimension Types simultaneous
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			const cc = await analyzeDimensionValue(page, "cost_center");
			await page.evaluate(() => {
				const ae = window.cur_ae;
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				let bag = ae.get_analysis_filters();
				bag = AF.set_entry(bag, {
					key: "dimensions.project",
					value: "PRJ-AND",
					lifetime: "session",
					origin: "analyze",
					removable: true,
					meta: { dimension_type: "project" },
				});
				bag = AF.set_entry(bag, {
					key: "dimensions.facility",
					value: "FAC-AND",
					lifetime: "session",
					origin: "analyze",
					removable: true,
					meta: { dimension_type: "facility" },
				});
				ae.set_analysis_filters_bag(bag);
				ae._sync_scopes_from_analysis_filters?.();
			});
			for (const f of ["department", "cost_center", "project", "facility"]) {
				await hopDim(page, f);
				const s = await snap(page, `E2E6 ${f}`);
				for (const key of ["department", "cost_center", "project", "facility"]) {
					if (!s.payloadDocDims[key] && !s.bag_dims[key]) {
						throw new Error(`${key} missing while on ${f}`);
					}
				}
				if (s.bag_dims.department !== DEPT) throw new Error("dept lost");
				if (s.bag_dims.cost_center !== cc) throw new Error("cc lost");
			}
			passCheck("E2E6 multi dimension types simultaneous", { cc });
			await shot(page, "E2E6-multi-dim");
		} catch (e) {
			failCheck("E2E6 multi dimension types simultaneous", e);
			await shot(page, "E2E6-fail");
		}

		// E2E 7 — 30 consecutive transitions
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			await analyzeDimensionValue(page, "cost_center");
			const sequence = [];
			const dims = ["department", "cost_center", "project", "facility"];
			const axes = ["account_level", "party", "voucher", "item", "currency", "item_group"];
			for (let i = 0; i < 30; i++) {
				if (i % 2 === 0) {
					const f = dims[i % dims.length];
					await hopDim(page, f);
					sequence.push(`dim:${f}`);
				} else {
					const a = axes[i % axes.length];
					await hopAxis(page, a, a === "account_level" ? 1 : null);
					sequence.push(`axis:${a}`);
				}
				const s = await snap(page, `E2E7 ${i}`);
				assertDeptGlobal(s, `E2E7#${i}`);
				if (!s.bag_dims.cost_center) throw new Error(`cc lost at step ${i}`);
				const chipKeys = s.chips.map((c) => c.key).sort().join(",");
				const uniq = new Set(s.chips.map((c) => c.key));
				if (uniq.size !== s.chips.length) throw new Error(`duplicate chips at ${i}: ${chipKeys}`);
			}
			passCheck("E2E7 thirty transitions", { sequence });
			await shot(page, "E2E7-transitions");
		} catch (e) {
			failCheck("E2E7 thirty transitions", e);
			await shot(page, "E2E7-fail");
		}

		// SQL recon smoke: department-only account totals
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", DEPT);
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "SQL recon");
			assertDeptGlobal(s, "SQL");
			if (Math.abs(s.sumPD - EXPECTED_PD) > 1) {
				throw new Error(`sumPD ${s.sumPD} != ${EXPECTED_PD}`);
			}
			if (Math.abs(s.sumPC - EXPECTED_PC) > 1) {
				throw new Error(`sumPC ${s.sumPC} != ${EXPECTED_PC}`);
			}
			passCheck("SQL recon department account totals", s);
			await shot(page, "SQL-recon");
		} catch (e) {
			failCheck("SQL recon department account totals", e);
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
			suite: "global-cross-axis-filters",
			company: COMPANY,
			department: DEPT,
			expected_period_debit: EXPECTED_PD,
			expected_period_credit: EXPECTED_PC,
			passed,
			failed,
			checks,
			semantics: {
				global_filters: "Every Analyze filter constrains all axes (AND)",
				dimension_type: "Presentation only; never parks/drops query filters",
				empty_intersection: "Returns zero rows; never unfiltered fallback",
				clear_all: "Removes every global analytical filter",
			},
			generated_at: new Date().toISOString(),
		};
		fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
		console.log(
			JSON.stringify({ passed, failed, names: checks.map((c) => [c.name, c.ok]) }, null, 2)
		);
		await browser.close();
		process.exit(failed ? 1 : 0);
	}
}

main();

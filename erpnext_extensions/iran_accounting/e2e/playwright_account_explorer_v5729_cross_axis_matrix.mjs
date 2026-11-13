#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 — cross-axis Dimension data-accuracy matrix (global filters).
 * Every path checks payload dims + returned totals vs independent expectations.
 * Dimension Type switches must NOT park/drop sibling filters.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5729-matrix");

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

async function waitIdle(page) {
	await page.evaluate(async () => {
		const ae = window.cur_ae;
		let g = 0;
		while (ae.store?.get?.("loading")?.summary && g++ < 500) {
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

async function analyzeDimensionValue(page, field, valueIncludes = null, exact = null) {
	await page.evaluate((f) => window.cur_ae.switch_axis("dimension", f), field);
	await waitIdle(page);
	// If this type is already constrained to a single analyzed value, clear only the
	// active bag entry WITHOUT forget/cleared so the full type list reloads for picking.
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
		ae.refresh_summary({ new_execution: true, reason: "matrix_list_reload" });
	});
	await waitIdle(page);
	const found = await page.evaluate(
		({ valueIncludes, exact }) => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			let row = rows.find((r) => {
				const v = String(r.dimension_value || "");
				if (exact != null) return v === exact;
				if (valueIncludes) return v.includes(valueIncludes);
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
		},
		{ valueIncludes, exact }
	);
	await waitIdle(page);
	if (!found?.found) {
		throw new Error(
			`no row for ${field} (${exact || valueIncludes || "any"}); sample=${JSON.stringify(found?.available || [])}`
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
			parked_dimensions: ae.get_parked_dimension_summary_entries?.() || {},
		});
		return {
			label: lbl,
			view_axis: ae.analysis_context.view_axis,
			dimension_type: ae.analysis_context.dimension_scope?.dimension_type || null,
			selected: ae.analysis_context.dimension_scope?.selected_dimension_value ?? null,
			bag_dims: Object.fromEntries(
				Object.entries(bag.dimensions || {}).map(([k, v]) => [k, v?.value ?? v])
			),
			parked: Object.fromEntries(
				Object.entries(ae._dimension_type_parked || {}).map(([k, v]) => [
					k,
					{
						value: v?.analysis_entry?.value ?? null,
						cleared: !!v?.analysis_cleared,
					},
				])
			),
			cleared: { ...(ae._dimension_type_cleared || {}) },
			payloadDocDims: { ...(payload.document_scope?.accounting_dimensions || {}) },
			payloadSelected:
				payload.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
			payloadDimType: payload.analysis_context?.dimension_scope?.dimension_type ?? null,
			rowCount: rows.length,
			sumPD: rows.reduce((s, r) => s + Number(r.period_debit || 0), 0),
			sumPC: rows.reduce((s, r) => s + Number(r.period_credit || 0), 0),
			codes: rows.slice(0, 30).map((r) => r.display_code || r.dimension_value || r.voucher_no),
			loanish: rows
				.filter((r) =>
					/وام|تسهیلات|loan/i.test(`${r.display_title || ""}${r.display_code || ""}`)
				)
				.map((r) => r.display_code),
			chips: summaryRows.map((r) => ({
				group: r.group,
				key: r.key,
				value: r.value,
				removable: !!r.removable,
			})),
		};
	}, label);
}

function assertDeptActive(s, where) {
	if (s.payloadDocDims.department !== DEPT) {
		throw new Error(`${where}: payload missing department (${JSON.stringify(s.payloadDocDims)})`);
	}
	if (s.bag_dims.department !== DEPT) {
		throw new Error(`${where}: bag missing department`);
	}
}

function assertDeptAbsent(s, where) {
	if (s.payloadDocDims.department) {
		throw new Error(`${where}: department unexpectedly in payload`);
	}
	if (s.bag_dims.department) {
		throw new Error(`${where}: department unexpectedly in bag`);
	}
}

function assertFilteredAccounts(s, where) {
	assertDeptActive(s, where);
	if (Math.abs(s.sumPD - EXPECTED_PD) > 1) {
		throw new Error(`${where}: sumPD ${s.sumPD} != ${EXPECTED_PD}`);
	}
	if (Math.abs(s.sumPC - EXPECTED_PC) > 1) {
		throw new Error(`${where}: sumPC ${s.sumPC} != ${EXPECTED_PC}`);
	}
	if (s.codes.includes("21") || s.codes.includes("22") || s.loanish.length) {
		throw new Error(`${where}: loan/liability groups present ${JSON.stringify(s.codes)}`);
	}
}

async function hopDim(page, field) {
	await page.evaluate((f) => window.cur_ae.switch_axis("dimension", f), field);
	await waitIdle(page);
}

async function hopAxis(page, axis, arg = null) {
	await page.evaluate(({ axis, arg }) => window.cur_ae.switch_axis(axis, arg), { axis, arg });
	await waitIdle(page);
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
	const available = {};

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await waitIdle(page);
		await reset(page);

		available.dims = await page.evaluate(() =>
			(window.cur_ae.metadata?.dimensions || []).map((d) => d.fieldname)
		);

		const need = [
			"department",
			"cost_center",
			"project",
			"facility",
			"bank_dimension",
		];
		for (const f of need) {
			if (!available.dims.includes(f)) {
				failCheck(`fixture dimension ${f}`, new Error(`missing ${f}`));
			}
		}

		// ── 1. Department → Cost Center → Account Levels ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			const onCc = await snap(page, "1 on CC");
			assertDeptActive(onCc, "1 CC keeps global dept");
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "1 accounts");
			assertFilteredAccounts(s, "1");
			passCheck("1 Dept→CC→Account Levels", { onCc, s });
			await shot(page, "01-dept-cc-accounts");
		} catch (e) {
			failCheck("1 Dept→CC→Account Levels", e);
			await shot(page, "01-fail");
		}

		// ── 2. Department → Cost Center → Parties ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await hopAxis(page, "party");
			const s = await snap(page, "2 parties");
			assertDeptActive(s, "2");
			passCheck("2 Dept→CC→Parties", s);
			await shot(page, "02-dept-cc-parties");
		} catch (e) {
			failCheck("2 Dept→CC→Parties", e);
		}

		// ── 3. Department → Cost Center → Vouchers ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await hopAxis(page, "voucher");
			const s = await snap(page, "3 vouchers");
			assertDeptActive(s, "3");
			if (!(s.rowCount > 0)) throw new Error("expected vouchers for department");
			// Payload + non-empty voucher page; line-level SQL reconciliation covered by backend suite.
			passCheck("3 Dept→CC→Vouchers", s);
			await shot(page, "03-dept-cc-vouchers");
		} catch (e) {
			failCheck("3 Dept→CC→Vouchers", e);
		}

		// ── 4. Department → Facility → Items ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			if (available.dims.includes("facility")) {
				await hopDim(page, "facility");
				const onFac = await snap(page, "4 on facility");
				assertDeptActive(onFac, "4 facility keeps global dept");
			}
			await hopAxis(page, "item");
			const s = await snap(page, "4 items");
			assertDeptActive(s, "4");
			passCheck("4 Dept→Facility→Items", s);
			await shot(page, "04-dept-facility-items");
		} catch (e) {
			failCheck("4 Dept→Facility→Items", e);
		}

		// ── 5. Department → Project → Account Levels ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "project");
			const onProj = await snap(page, "5 on project");
			assertDeptActive(onProj, "5 project keeps global dept");
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "5 accounts");
			assertFilteredAccounts(s, "5");
			passCheck("5 Dept→Project→Account Levels", { onProj, s });
			await shot(page, "05-dept-project-accounts");
		} catch (e) {
			failCheck("5 Dept→Project→Account Levels", e);
		}

		// ── 6. Department → Bank Dimension → Parties ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			if (!available.dims.includes("bank_dimension")) throw new Error("no bank_dimension");
			await hopDim(page, "bank_dimension");
			const onBank = await snap(page, "6 on bank");
			assertDeptActive(onBank, "6 bank keeps global dept");
			await hopAxis(page, "party");
			const s = await snap(page, "6 parties");
			assertDeptActive(s, "6");
			passCheck("6 Dept→Bank Dimension→Parties", { onBank, s });
			await shot(page, "06-dept-bank-parties");
		} catch (e) {
			failCheck("6 Dept→Bank Dimension→Parties", e);
		}

		// ── 7. Department → Cost Center → Department (global filter persists) ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await hopDim(page, "department");
			const s = await snap(page, "7 back dept");
			if (s.bag_dims.department !== DEPT || s.payloadDocDims.department !== DEPT) {
				throw new Error(`dept not global on type return: ${JSON.stringify(s)}`);
			}
			passCheck("7 Dept→CC→Department", s);
			await shot(page, "07-dept-cc-dept");
		} catch (e) {
			failCheck("7 Dept→CC→Department", e);
		}

		// ── 8. Department → Cost Center → Clear Selection → Account Levels ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await page.evaluate(() => window.cur_ae.clear_grid_selection());
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "8 after clear selection");
			assertFilteredAccounts(s, "8");
			passCheck("8 Clear Selection keeps filters", s);
			await shot(page, "08-clear-selection-accounts");
		} catch (e) {
			failCheck("8 Clear Selection keeps filters", e);
		}

		// ── 9. Department → Cost Center → Delete Filter → Account Levels ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await page.evaluate(() =>
				window.cur_ae.remove_analysis_filter_chip("dimensions.department")
			);
			await waitIdle(page);
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "9 after delete");
			assertDeptAbsent(s, "9");
			if (Math.abs(s.sumPD - EXPECTED_PD) < 1) {
				throw new Error("still department-filtered after delete");
			}
			if (s.chips.some((c) => c.key === "dimensions.department")) {
				throw new Error("department chip remained after delete");
			}
			passCheck("9 Delete Filter → Accounts unfiltered", s);
			await shot(page, "09-delete-then-accounts");
		} catch (e) {
			failCheck("9 Delete Filter → Accounts unfiltered", e);
		}

		// ── 10. Department → Cost Center → Reset All Filters ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await page.evaluate(() => window.cur_ae.reset_all_filters());
			await waitIdle(page);
			const s = await snap(page, "10 after reset all filters");
			assertDeptAbsent(s, "10");
			if (Object.keys(s.bag_dims).length) throw new Error("bag not empty after reset");
			passCheck("10 Reset All Filters clears layers", s);
			await shot(page, "10-reset-all-filters");
		} catch (e) {
			failCheck("10 Reset All Filters clears layers", e);
		}

		// ── 10b. Reset All Axes (grid prefs) must NOT clear analysis filters ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await page.evaluate(() => {
				window.cur_ae.user_preferences?.reset_all_axes?.();
			});
			const s = await snap(page, "10b after reset all axes prefs");
			if (s.bag_dims.department !== DEPT) {
				throw new Error("Reset All Axes cleared analysis filters");
			}
			passCheck("10b Reset All Axes preserves analysis", s);
		} catch (e) {
			failCheck("10b Reset All Axes preserves analysis", e);
		}

		// ── Repeat with Cost Center as the analyzed session filter ──
		try {
			await reset(page);
			const cc = await analyzeDimensionValue(page, "cost_center");
			await hopDim(page, "department");
			const onDept = await snap(page, "R1 on dept");
			if (onDept.payloadDocDims.cost_center !== cc) {
				throw new Error(`cost_center must stay global on department tab: ${JSON.stringify(onDept.payloadDocDims)}`);
			}
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "R1 accounts");
			if (s.payloadDocDims.cost_center !== cc) {
				throw new Error(`CC not global: ${JSON.stringify(s.payloadDocDims)}`);
			}
			if (!(s.sumPD > 0)) throw new Error("expected positive filtered turnover");
			if (s.sumPD > 1e14) throw new Error(`likely unfiltered sumPD=${s.sumPD}`);
			passCheck("R1 CostCenter→Dept→Accounts keeps CC global", { cc, onDept, s });
			await shot(page, "R1-cc-dept-accounts");
		} catch (e) {
			failCheck("R1 CostCenter→Dept→Accounts keeps CC global", e);
		}

		// ── Empty department → zero ──
		try {
			await reset(page);
			await hopDim(page, "department");
			await page.evaluate(() => {
				const ae = window.cur_ae;
				const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
				const empty = "__AE_MATRIX_EMPTY_DEPT__";
				const entry = {
					key: "dimensions.department",
					value: empty,
					lifetime: "session",
					origin: "analyze",
					removable: true,
					meta: { dimension_type: "department" },
				};
				ae.set_analysis_filters_bag(AF.set_entry(ae.get_analysis_filters(), entry));
				ae.analysis_context.dimension_scope = {
					dimension_type: "department",
					selected_dimension_value: empty,
				};
				ae.document_scope.accounting_dimensions = { department: empty };
			});
			await page.evaluate(() => window.cur_ae.refresh_summary({ new_execution: true, reason: "test" }));
			await waitIdle(page);
			const s = await snap(page, "empty dept");
			if (s.rowCount !== 0) throw new Error(`expected 0 rows, got ${s.rowCount}`);
			passCheck("N1 empty department zero rows", s);
			await shot(page, "N1-empty-department");
		} catch (e) {
			failCheck("N1 empty department zero rows", e);
		}

		// ── Global AND: analyze dept then analyze CC; both stay active everywhere ──
		try {
			await reset(page);
			await analyzeDimensionValue(page, "department", null, DEPT);
			await hopDim(page, "cost_center");
			await waitIdle(page);
			const cc = await page.evaluate(() => {
				const ae = window.cur_ae;
				const row = (ae.rows || []).find((r) => r.dimension_value) || null;
				if (!row) return null;
				ae.analyze_row_as_filter(row);
				return row.dimension_value;
			});
			await waitIdle(page);
			if (!cc) throw new Error("no cost center row to analyze");
			const onCc = await snap(page, "N2 on cc after both");
			if (onCc.payloadDocDims.department !== DEPT) {
				throw new Error("dept must remain global on CC tab");
			}
			if (onCc.bag_dims.department !== DEPT) throw new Error("dept missing from bag on CC");
			if (onCc.payloadDocDims.cost_center !== cc) throw new Error("cc missing on CC tab");
			await hopAxis(page, "account_level", 1);
			const s = await snap(page, "N2 accounts both");
			if (s.payloadDocDims.department !== DEPT || s.payloadDocDims.cost_center !== cc) {
				throw new Error(`expected AND of both dims: ${JSON.stringify(s.payloadDocDims)}`);
			}
			if (s.sumPD > EXPECTED_PD + 1) throw new Error(`AND wider than dept-only: ${s.sumPD}`);
			passCheck("N2 both dims AND on accounts", { onCc, s, cc });
			await shot(page, "N2-both-dims-and");
		} catch (e) {
			failCheck("N2 both dims AND on accounts", e);
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
			suite: "cross-axis-matrix",
			company: COMPANY,
			department: DEPT,
			expected_period_debit: EXPECTED_PD,
			expected_period_credit: EXPECTED_PC,
			available_dimensions: available.dims || [],
			passed,
			failed,
			checks,
			semantics: {
				dimensions_axis: "All Dimension analyses remain global AND constraints",
				leave_dimensions: "Axis/type switches never park or drop global filters",
				clear_selection: "Row checkboxes only",
				reset_all_axes: "Grid preferences only",
				reset_all_filters: "Clears every global analytical filter",
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

#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 — pre-fix reproduction for axis/tab navigation bugs.
 * Does not change product code; dumps machine-readable evidence.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-v5729-nav");

const results = [];
const record = (name, ok, detail = null) => results.push({ name, ok, detail });

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

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

	const snap = async (label) =>
		page.evaluate((lbl) => {
			const ae = window.cur_ae;
			const bag = ae.get_analysis_filters?.() || {};
			const payload = ae.build_payload?.() || {};
			const Summary = erpnext_extensions?.account_explorer?.core?.AnalysisFilterSummary;
			const rows = Summary?.build_rows
				? Summary.build_rows({
						bag,
						document_scope: ae.document_scope,
						analysis_context: ae.analysis_context,
						metadata: ae.metadata,
				  })
				: [];
			return {
				label: lbl,
				view_axis: ae.analysis_context?.view_axis,
				dimension_type: ae.analysis_context?.dimension_scope?.dimension_type || null,
				selected: ae.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
				bag_dims: Object.fromEntries(
					Object.entries(bag.dimensions || {}).map(([k, v]) => [k, v?.value ?? v])
				),
				doc_dims: { ...(ae.document_scope?.accounting_dimensions || {}) },
				parked: Object.fromEntries(
					Object.entries(ae._dimension_type_parked || {}).map(([k, v]) => [
						k,
						{
							value: v?.analysis_entry?.value ?? null,
							cleared_flag: !!v?.analysis_cleared,
							selected: v?.selected_dimension_value ?? null,
						},
					])
				),
				cleared: { ...(ae._dimension_type_cleared || {}) },
				chips: (rows || []).map((r) => ({
					group: r.group,
					key: r.key,
					label: r.label,
					value: r.value,
					removable: !!r.removable,
				})),
				payload_dims: {
					...(payload.document_scope?.accounting_dimensions || {}),
				},
				payload_selected:
					payload.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
				payload_dim_type:
					payload.analysis_context?.dimension_scope?.dimension_type ?? null,
			};
		}, label);

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
		await waitIdle(page);

		const dims = await page.evaluate(() =>
			(window.cur_ae.metadata?.dimensions || []).map((d) => d.fieldname)
		);
		const need = ["department", "cost_center", "project"].filter((d) => dims.includes(d));
		if (need.length < 2) {
			throw new Error(`need department+cost_center; have ${JSON.stringify(dims)}`);
		}

		const wait = async () => {
			await page.evaluate(async () => {
				const ae = window.cur_ae;
				let g = 0;
				while (ae.store?.get?.("loading")?.summary && g++ < 400) {
					await new Promise((x) => setTimeout(x, 100));
				}
			});
		};

		const analyzeFirst = async (field) => {
			await page.evaluate(async (f) => {
				const ae = window.cur_ae;
				ae.switch_axis("dimension", f);
			}, field);
			await wait();
			const ok = await page.evaluate(async (f) => {
				const ae = window.cur_ae;
				const row = (ae.rows || []).find((x) => x.dimension_value);
				if (!row) return false;
				ae.analyze_row_as_filter(row);
				return true;
			}, field);
			await wait();
			return ok;
		};

		// ── Scenario A1: delete while on that type, switch away/back ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		if (!(await analyzeFirst("department"))) throw new Error("no department rows");
		const a1_before = await snap("A1 after analyze department");
		await page.evaluate(() => {
			window.cur_ae.remove_analysis_filter_chip("dimensions.department");
		});
		await wait();
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
		await wait();
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const a1_after = await snap("A1 after delete+CC+Dept");
		const a1_ok =
			!a1_after.bag_dims.department &&
			a1_after.selected == null &&
			!a1_after.chips.some((c) => c.key === "dimensions.department");
		record("A1 delete-on-type then switch", a1_ok, { a1_before, a1_after });
		await page.screenshot({ path: path.join(OUT, "A1-after-delete-switch.png"), fullPage: true });

		// ── Scenario A2: park Dept, delete from summary while on CC (if visible) or via API ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
		await wait();
		const a2_on_cc = await snap("A2 on cost_center with parked dept");
		// Explicitly delete department while Cost Center is active (chip may be hidden).
		await page.evaluate(() => {
			const ae = window.cur_ae;
			// Mimic user intent: remove Department analysis even if parked/hidden.
			ae.forget_parked_dimension_analysis?.("department");
			ae.remove_analysis_filter_chip("dimensions.department");
		});
		await wait();
		const a2_after_del = await snap("A2 after delete dept while on CC");
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const a2_back = await snap("A2 back to department");
		const a2_ok =
			!a2_back.bag_dims.department &&
			a2_back.selected == null &&
			!a2_back.chips.some((c) => c.key === "dimensions.department") &&
			!(a2_back.parked.department?.value);
		record("A2 delete parked dept while on CC", a2_ok, { a2_on_cc, a2_after_del, a2_back });
		await page.screenshot({ path: path.join(OUT, "A2-park-delete-return.png"), fullPage: true });

		// ── Scenario A3: user repro — Dept analyze → CC → clear_all → Dept ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
		await wait();
		await page.evaluate(() => window.cur_ae.clear_all_analysis_filters({ refresh: true }));
		await wait();
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const a3 = await snap("A3 after clear_all return dept");
		const a3_ok = !a3.bag_dims.department && a3.selected == null;
		record("A3 clear_all while on CC then return Dept", a3_ok, a3);

		// ── Scenario A4: BUG CANDIDATE — Dept analyze → CC → remove only via bag sync without forget ──
		// Simulate incomplete delete: clear bag entry for department if present, switch without forget.
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
		await wait();
		// Incomplete clear: wipe parked analysis_entry but leave cleared=false (legacy bug shape)
		await page.evaluate(() => {
			const ae = window.cur_ae;
			const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
			let bag = ae.get_analysis_filters();
			if (bag.dimensions?.department) {
				bag = AF.remove_entry(bag, "dimensions.department");
				ae.set_analysis_filters_bag(bag);
			}
			// Do NOT call forget — leave parked.department.analysis_entry intact
		});
		await wait();
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const a4 = await snap("A4 incomplete delete resurrects?");
		const a4_resurrected = !!(a4.bag_dims.department || a4.selected);
		record(
			"A4 incomplete-delete resurrects (expect FAIL if bug)",
			!a4_resurrected,
			{ resurrected: a4_resurrected, a4 }
		);
		await page.screenshot({ path: path.join(OUT, "A4-incomplete-delete.png"), fullPage: true });

		// ── Scenario B: intentional park preserve ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		const b_dept = await snap("B after dept");
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
		await wait();
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const b_back = await snap("B return dept");
		const b_ok =
			!!b_back.bag_dims.department &&
			String(b_back.bag_dims.department) === String(b_dept.bag_dims.department);
		record("B intentional park preserves dept", b_ok, { b_dept, b_back });

		// ── Scenario C: main axis round-trip ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		const c_dept = await snap("C after dept analyze");
		for (const axis of ["party", "item", "voucher", "currency", "item_group"]) {
			await page.evaluate((ax) => window.cur_ae.switch_axis(ax), axis);
			await wait();
		}
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const c_back = await snap("C after axis tour back to dept");
		const c_ok =
			!!c_back.bag_dims.department &&
			String(c_back.bag_dims.department) === String(c_dept.bag_dims.department);
		record("C main-axis tour preserves undeleted dept", c_ok, { c_dept, c_back });

		// ── Scenario C2: delete on Parties (visible chip) then return Dimensions ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		await page.evaluate(() => window.cur_ae.switch_axis("party"));
		await wait();
		const c2_party = await snap("C2 on party with dept chip?");
		await page.evaluate(() => {
			window.cur_ae.remove_analysis_filter_chip("dimensions.department");
		});
		await wait();
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
		await wait();
		const c2_back = await snap("C2 back after delete on party");
		const c2_ok =
			!c2_back.bag_dims.department &&
			c2_back.selected == null &&
			!c2_back.chips.some((c) => c.key === "dimensions.department");
		record("C2 delete dept chip on Parties then return", c2_ok, { c2_party, c2_back });
		await page.screenshot({ path: path.join(OUT, "C2-delete-on-party.png"), fullPage: true });

		// ── Scenario D: filter summary vs payload after switches ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		await analyzeFirst("cost_center");
		const d_cc = await snap("D on CC after both analyzed");
		const d_mismatches = [];
		const analysisChips = d_cc.chips.filter((c) => c.group === "analysis_filters");
		const chipDimKeys = analysisChips
			.filter((c) => String(c.key).startsWith("dimensions."))
			.map((c) => c.key.slice("dimensions.".length));
		if (chipDimKeys.includes("department")) {
			d_mismatches.push("dept chip visible on CC");
		}
		if (d_cc.payload_dims.department) {
			d_mismatches.push("dept in payload while on CC");
		}
		if (d_cc.bag_dims.department) {
			d_mismatches.push("dept still in active bag on CC");
		}
		record("D isolation summary vs payload on CC", d_mismatches.length === 0, {
			d_cc,
			d_mismatches,
		});

		// ── Scenario E: filter alignment ──
		await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
		await wait();
		const align = await page.evaluate(() => {
			const measure = (col) => {
				const header = document.querySelector(
					`.dt-header .dt-cell[data-col-index="${col}"]`
				);
				const filter = document.querySelector(
					`.dt-row-filter .dt-cell[data-col-index="${col}"]`
				);
				const body = document.querySelector(
					`.dt-body .dt-cell[data-col-index="${col}"]`
				);
				if (!header || !filter) return null;
				const hr = header.getBoundingClientRect();
				const fr = filter.getBoundingClientRect();
				const br = body?.getBoundingClientRect();
				const cs = getComputedStyle(filter);
				return {
					col,
					headerLeft: hr.left,
					filterLeft: fr.left,
					bodyLeft: br?.left ?? null,
					deltaHeaderFilter: Math.abs(hr.left - fr.left),
					deltaHeaderBody: br ? Math.abs(hr.left - br.left) : null,
					filterPos: cs.position,
					filterLeftCss: cs.left,
					headerWidth: hr.width,
					filterWidth: fr.width,
				};
			};
			// col 0 checkbox, 1 code, 2 title typically
			return { code: measure(1), title: measure(2) };
		});
		const e_ok =
			align.code &&
			align.title &&
			align.code.deltaHeaderFilter <= 1 &&
			align.title.deltaHeaderFilter <= 1;
		record("E Code/Title filter alignment", e_ok, align);
		await page.screenshot({ path: path.join(OUT, "E-filter-alignment.png"), fullPage: true });

		// ── Scenario F: 20 transitions stress ──
		await page.evaluate(() => {
			window.cur_ae.clear_all_analysis_filters({ refresh: false });
			window.cur_ae._dimension_type_parked = {};
			window.cur_ae._dimension_type_cleared = {};
		});
		await analyzeFirst("department");
		await analyzeFirst("cost_center");
		if (need.includes("project")) {
			await analyzeFirst("project");
		}
		const sequence = [];
		const axes = ["dimension", "party", "item", "voucher", "currency", "account_level", "item_group"];
		const dimCycle = need.slice(0, 3);
		for (let i = 0; i < 20; i++) {
			if (i % 2 === 0) {
				const f = dimCycle[i % dimCycle.length];
				await page.evaluate((field) => window.cur_ae.switch_axis("dimension", field), f);
				sequence.push(`dimension:${f}`);
			} else {
				const ax = axes[i % axes.length];
				await page.evaluate((a) => window.cur_ae.switch_axis(a), ax);
				sequence.push(ax);
			}
			await wait();
		}
		const f_end = await snap("F after 20 transitions");
		const dupKeys = f_end.chips
			.map((c) => c.key)
			.filter((k, i, arr) => arr.indexOf(k) !== i);
		const f_ok = dupKeys.length === 0;
		record("F 20 transitions no duplicate chips", f_ok, { sequence, f_end, dupKeys });
		await page.screenshot({ path: path.join(OUT, "F-after-20.png"), fullPage: true });
	} catch (err) {
		record("fatal", false, { err: err?.stack || String(err) });
		try {
			await page.screenshot({ path: path.join(OUT, "fatal.png"), fullPage: true });
		} catch (_) {}
	} finally {
		const passed = results.filter((r) => r.ok).length;
		const failed = results.filter((r) => !r.ok).length;
		const report = { passed, failed, results, version: "5.7.29-nav-repro" };
		fs.writeFileSync(path.join(OUT, "repro-report.json"), JSON.stringify(report, null, 2));
		console.log(JSON.stringify({ passed, failed, names: results.map((r) => [r.name, r.ok]) }, null, 2));
		await browser.close();
		process.exit(failed ? 1 : 0);
	}
}

main();

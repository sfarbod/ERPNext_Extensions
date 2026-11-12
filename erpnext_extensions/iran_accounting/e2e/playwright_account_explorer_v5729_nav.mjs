#!/usr/bin/env node
/**
 * Account Explorer 5.7.29 — comprehensive axis / Dimension Type navigation suite.
 * Browser evidence for delete-vs-park, isolation, main-axis tour, summary/payload, alignment.
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
		while (ae.store?.get?.("loading")?.summary && g++ < 400) {
			await new Promise((x) => setTimeout(x, 100));
		}
	});
}

async function shot(page, name) {
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function snap(page, label) {
	return page.evaluate((lbl) => {
		const ae = window.cur_ae;
		const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
		const bag = ae.get_analysis_filters();
		const payload = ae.build_payload?.() || {};
		const rows = AF.build_summary_rows(bag, {
			document_scope: ae.document_scope,
			analysis_context: ae.analysis_context,
			metadata: ae.metadata,
			parked_dimensions: ae.get_parked_dimension_summary_entries?.() || {},
		});
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
			payload_dims: { ...(payload.document_scope?.accounting_dimensions || {}) },
			payload_selected:
				payload.analysis_context?.dimension_scope?.selected_dimension_value ?? null,
			payload_dim_type: payload.analysis_context?.dimension_scope?.dimension_type ?? null,
		};
	}, label);
}

async function resetSession(page) {
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

async function analyzeFirst(page, field) {
	await page.evaluate((f) => window.cur_ae.switch_axis("dimension", f), field);
	await waitIdle(page);
	const ok = await page.evaluate(() => {
		const ae = window.cur_ae;
		const row = (ae.rows || []).find((x) => x.dimension_value);
		if (!row) return false;
		ae.analyze_row_as_filter(row);
		return true;
	});
	await waitIdle(page);
	if (!ok) throw new Error(`no rows for ${field}`);
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
		await resetSession(page);

		const dims = await page.evaluate(() =>
			(window.cur_ae.metadata?.dimensions || []).map((d) => d.fieldname)
		);
		const need = ["department", "cost_center", "project"].filter((d) => dims.includes(d));
		if (!need.includes("department") || !need.includes("cost_center")) {
			throw new Error(`need department+cost_center; have ${JSON.stringify(dims)}`);
		}

		// ── Test 1 — Explicit deletion (on-type) ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			await page.evaluate(() =>
				window.cur_ae.remove_analysis_filter_chip("dimensions.department")
			);
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
			await waitIdle(page);
			const s = await snap(page, "T1");
			if (s.bag_dims.department || s.selected != null) {
				throw new Error(`resurrected: ${JSON.stringify(s.bag_dims)} sel=${s.selected}`);
			}
			if (s.chips.some((c) => c.key === "dimensions.department")) {
				throw new Error("department chip still present after delete");
			}
			passCheck("T1 explicit delete on-type", s);
			await shot(page, "T1-delete-no-resurrect");
		} catch (e) {
			failCheck("T1 explicit delete on-type", e);
			await shot(page, "T1-fail");
		}

		// ── Test 1b — Delete parked chip while on Cost Center ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			const onCc = await snap(page, "T1b on CC");
			const parkedChip = onCc.chips.find(
				(c) => c.key === "dimensions.department" && c.group === "parked_dimension_filters"
			);
			if (!parkedChip) {
				throw new Error(`expected parked department chip; chips=${JSON.stringify(onCc.chips)}`);
			}
			if (onCc.payload_dims.department) {
				throw new Error("parked department must not be in payload");
			}
			await page.evaluate(() =>
				window.cur_ae.remove_analysis_filter_chip("dimensions.department")
			);
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
			await waitIdle(page);
			const back = await snap(page, "T1b back");
			if (back.bag_dims.department || back.selected != null) {
				throw new Error("parked delete resurrected on return");
			}
			passCheck("T1b delete parked chip on CC", { onCc, back });
			await shot(page, "T1b-parked-chip-delete");
		} catch (e) {
			failCheck("T1b delete parked chip on CC", e);
			await shot(page, "T1b-fail");
		}

		// ── Test 2 — Intentional preservation ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			const before = await snap(page, "T2 before");
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
			await waitIdle(page);
			const after = await snap(page, "T2 after");
			if (String(after.bag_dims.department) !== String(before.bag_dims.department)) {
				throw new Error("parked department not restored");
			}
			passCheck("T2 intentional park preserve", { before, after });
			await shot(page, "T2-park-preserve");
		} catch (e) {
			failCheck("T2 intentional park preserve", e);
		}

		// ── Test 3 — Independent Dimension Types ──
		try {
			await resetSession(page);
			const values = {};
			for (const f of need) {
				await analyzeFirst(page, f);
				values[f] = (await snap(page, `T3 ${f}`)).bag_dims[f];
			}
			const cycle = [...need, ...need, need[0]];
			for (const f of cycle) {
				await page.evaluate((field) => window.cur_ae.switch_axis("dimension", field), f);
				await waitIdle(page);
				const s = await snap(page, `T3 at ${f}`);
				if (String(s.bag_dims[f] || "") !== String(values[f] || "")) {
					throw new Error(`isolation broke for ${f}: ${JSON.stringify(s.bag_dims)}`);
				}
				for (const other of need) {
					if (other === f) continue;
					if (s.bag_dims[other]) {
						throw new Error(`${other} leaked into bag while on ${f}`);
					}
					if (s.payload_dims[other]) {
						throw new Error(`${other} leaked into payload while on ${f}`);
					}
					if (
						s.chips.some(
							(c) =>
								c.key === `dimensions.${other}` && c.group === "analysis_filters"
						)
					) {
						throw new Error(`${other} active chip while on ${f}`);
					}
				}
			}
			passCheck("T3 independent dimension types", { values });
			await shot(page, "T3-independent-types");
		} catch (e) {
			failCheck("T3 independent dimension types", e);
		}

		// ── Test 4 — Main axis switching ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			const before = await snap(page, "T4 before");
			for (const axis of ["party", "item", "voucher", "currency", "item_group", "account_level"]) {
				await page.evaluate((ax) => window.cur_ae.switch_axis(ax), axis);
				await waitIdle(page);
			}
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
			await waitIdle(page);
			const after = await snap(page, "T4 after");
			if (String(after.bag_dims.department) !== String(before.bag_dims.department)) {
				throw new Error("department lost after main-axis tour");
			}
			passCheck("T4 main axis tour", { before, after });
			await shot(page, "T4-main-axis");
		} catch (e) {
			failCheck("T4 main axis tour", e);
		}

		// ── Test 5 — 20 transitions ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			await analyzeFirst(page, "cost_center");
			if (need.includes("project")) await analyzeFirst(page, "project");
			const axes = [
				"dimension",
				"party",
				"item",
				"voucher",
				"currency",
				"account_level",
				"item_group",
			];
			const sequence = [];
			for (let i = 0; i < 20; i++) {
				if (i % 2 === 0) {
					const f = need[i % need.length];
					await page.evaluate((field) => window.cur_ae.switch_axis("dimension", field), f);
					sequence.push(`dimension:${f}`);
				} else {
					const ax = axes[i % axes.length];
					await page.evaluate((a) => window.cur_ae.switch_axis(a), ax);
					sequence.push(ax);
				}
				await waitIdle(page);
			}
			const end = await snap(page, "T5 end");
			const keys = end.chips.map((c) => `${c.group}:${c.key}`);
			const dups = keys.filter((k, i, arr) => arr.indexOf(k) !== i);
			if (dups.length) throw new Error(`duplicate chips: ${dups.join(",")}`);
			passCheck("T5 twenty transitions", { sequence, end });
			await shot(page, "T5-twenty-transitions");
		} catch (e) {
			failCheck("T5 twenty transitions", e);
		}

		// ── Test 6 — Filter Summary vs payload ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			await analyzeFirst(page, "cost_center");
			const s = await snap(page, "T6 on CC");
			const activeDimChips = s.chips.filter(
				(c) => c.group === "analysis_filters" && String(c.key).startsWith("dimensions.")
			);
			if (activeDimChips.some((c) => c.key === "dimensions.department")) {
				throw new Error("department active chip on cost_center");
			}
			if (s.payload_dims.department) {
				throw new Error("department in payload on cost_center");
			}
			if (!s.chips.some((c) => c.key === "dimensions.department" && c.group === "parked_dimension_filters")) {
				throw new Error("expected parked department chip");
			}
			if (s.payload_dims.cost_center && String(s.payload_dims.cost_center) !== String(s.bag_dims.cost_center)) {
				throw new Error("payload/bag cost_center mismatch");
			}
			passCheck("T6 summary vs payload", s);
			await shot(page, "T6-summary-payload");
		} catch (e) {
			failCheck("T6 summary vs payload", e);
		}

		// ── Test 7 — DataTable layout ──
		try {
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
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
						deltaHeaderFilter: Math.abs(hr.left - fr.left),
						deltaWidth: Math.abs(hr.width - fr.width),
						filterPos: cs.position,
						bodyDelta: br ? Math.abs(hr.left - br.left) : null,
					};
				};
				return { code: measure(1), title: measure(2) };
			});
			if (!align.code || align.code.deltaHeaderFilter > 1) {
				throw new Error(`code misaligned: ${JSON.stringify(align.code)}`);
			}
			if (!align.title || align.title.deltaHeaderFilter > 1) {
				throw new Error(`title misaligned: ${JSON.stringify(align.title)}`);
			}
			passCheck("T7 filter alignment", align);
			await shot(page, "T7-filter-alignment");
		} catch (e) {
			failCheck("T7 filter alignment", e);
		}

		// ── Test 8 — Clear Analysis Filters clears parked ──
		try {
			await resetSession(page);
			await analyzeFirst(page, "department");
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "cost_center"));
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.clear_all_analysis_filters({ refresh: true }));
			await waitIdle(page);
			await page.evaluate(() => window.cur_ae.switch_axis("dimension", "department"));
			await waitIdle(page);
			const s = await snap(page, "T8");
			if (s.bag_dims.department || s.selected != null) {
				throw new Error("clear_all left department restored");
			}
			if (s.chips.some((c) => c.key === "dimensions.department")) {
				throw new Error("department chip after clear_all");
			}
			passCheck("T8 clear_all clears parked", s);
			await shot(page, "T8-clear-all");
		} catch (e) {
			failCheck("T8 clear_all clears parked", e);
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

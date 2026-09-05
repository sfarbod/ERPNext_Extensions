#!/usr/bin/env node
/**
 * Account Explorer v5.1.1 — Inventory axes + initial render (Playwright).
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8001";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-inventory");
const PREP =
	"erpnext_extensions.iran_accounting.e2e.account_explorer_inventory_prep.prepare_inventory_e2e";

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({
		name,
		ok: false,
		err: err?.stack || err?.message || (typeof err === "object" ? JSON.stringify(err) : String(err)),
	});

const BROWSER_SET_INVENTORY_FILTERS = `
window.ae_e2e_set_inventory_filters = function(ae, opts = {}) {
	ae.prepared_mode = "live";
	const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
	let bag = AF.empty();
	if (opts.item_group) {
		bag = AF.set_entry(bag, {
			key: "item_group",
			value: opts.item_group,
			origin: "user",
			lifetime: "session",
			meta: { display_label: opts.item_group },
		});
	}
	if (opts.item) {
		bag = AF.set_entry(bag, {
			key: "item",
			value: opts.item,
			origin: "user",
			lifetime: "session",
			meta: { item_code: opts.item, display_label: opts.item },
		});
	}
	if (opts.warehouse) {
		bag = AF.set_entry(bag, {
			key: "warehouse",
			value: opts.warehouse,
			origin: "user",
			lifetime: "session",
			meta: { display_label: opts.warehouse },
		});
	}
	ae.set_analysis_filters_bag(bag, { silent: true });
	ae._sync_scopes_from_analysis_filters();
	ae.sync_filter_controls_from_document_scope();
};
`;

function benchExecute(method) {
	const cmd = `cd ${BENCH} && bench --site ${SITE} execute ${method}`;
	const out = execSync(cmd, { encoding: "utf8", maxBuffer: 50 * 1024 * 1024 });
	const lines = out.trim().split("\n").filter(Boolean);
	const last = lines[lines.length - 1];
	try {
		return JSON.parse(last);
	} catch {
		throw new Error(`bench execute failed: ${last}\n${out.slice(-1500)}`);
	}
}

async function login(page) {
	const res = await fetch(`${BASE}/api/method/login`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ usr: USER, pwd: PASS }),
	});
	if (!res.ok) throw new Error(`login failed ${res.status}`);
	const sid = (res.headers.get("set-cookie") || "").match(/sid=([^;]+)/)?.[1];
	if (!sid) throw new Error("no sid");
	await page.context().addCookies([
		{ name: "sid", value: sid, domain: new URL(BASE).hostname, path: "/" },
	]);
}

async function main() {
	let prep;
	try {
		prep = benchExecute(PREP);
		passCheck("prep", prep);
	} catch (e) {
		failCheck("prep", e);
		console.log(JSON.stringify({ checks }, null, 2));
		process.exit(1);
	}

	const browser = await chromium.launch({
		headless: true,
		args: ["--host-resolver-rules=MAP development.localhost 127.0.0.1"],
	});
	const context = await browser.newContext({
		locale: "en-US",
		viewport: { width: 1600, height: 950 },
	});
	const page = await context.newPage();
	fs.mkdirSync(OUT, { recursive: true });

	try {
		await login(page);
		const t0 = Date.now();
		await page.goto(`${BASE}/app/account-explorer`, {
			waitUntil: "domcontentloaded",
			timeout: 120000,
		});

		// Scenario A — shell / controls appear without waiting for heavy metadata
		await page.waitForSelector(".ae-toolbar", { timeout: 15000 });
		const shellMs = Date.now() - t0;
		await page.waitForFunction(
			() => {
				const entry = frappe?.pages?.["account-explorer"];
				const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
				return !!(ae && ae.company_field && ae.from_date_field && ae.to_date_field);
			},
			null,
			{ timeout: 30000 }
		);
		const controlsMs = Date.now() - t0;
		await page.evaluate(() => {
			const entry = frappe.pages["account-explorer"];
			window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		});
		if (shellMs < 3000 && controlsMs < 5000) {
			passCheck("A_initial_shell_controls", { shellMs, controlsMs });
		} else {
			failCheck("A_initial_shell_controls", { shellMs, controlsMs });
		}
		await page.screenshot({ path: path.join(OUT, "shell.png"), fullPage: true });

		// Wait for metadata init
		await page.waitForFunction(() => window.cur_ae?.metadata?.enabled, null, {
			timeout: 90000,
		});
		await page.evaluate(BROWSER_SET_INVENTORY_FILTERS);

		const scopeResult = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.document_scope.company = prep.company;
			ae.document_scope.fiscal_year = prep.fiscal_year || null;
			ae.document_scope.from_date = prep.from_date;
			ae.document_scope.to_date = prep.to_date;
			ae.document_scope.hide_zero_rows = 0;
			ae.prepared_mode = "live";
			await ae.company_field.set_value(prep.company);
			if (ae.fiscal_year_field && prep.fiscal_year) {
				await ae.fiscal_year_field.set_value(prep.fiscal_year);
			}
			if (ae.from_date_field) await ae.from_date_field.set_value(prep.from_date);
			if (ae.to_date_field) await ae.to_date_field.set_value(prep.to_date);
			return { company: ae.document_scope.company, fiscal_year: ae.document_scope.fiscal_year };
		}, prep);

		// Scenario B — Item Group tab
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.switch_axis("item_group");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
		});
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		const groupState = await page.evaluate((prep) => {
			const rows = window.cur_ae.rows || [];
			return {
				count: rows.length,
				hasParent: rows.some(
					(r) => r.item_group === prep.parent_group || r.display_code === prep.parent_group
				),
				hasChild: rows.some((r) => r.item_group === prep.child_group || r.display_code === prep.child_group),
				sample: rows.slice(0, 3).map((r) => ({
					code: r.display_code,
					balance: r.balance_value,
				})),
			};
		}, prep);
		if (groupState.count > 0 && !groupState.hasParent) passCheck("B_item_group_leaf_only", groupState);
		else failCheck("B_item_group_leaf_only", groupState);

		// Scenario B2 — Item Group footer: Inward/Outward + Debit/Credit Balance (side-netted)
		const groupFooter = await page.evaluate(() => {
			const ae = window.cur_ae;
			const labels = Array.from(document.querySelectorAll(".ae-total-item-label")).map((el) =>
				(el.textContent || "").trim()
			);
			const totals = ae.totals || {};
			const signed = (ae.rows || []).reduce(
				(s, r) => s + (Number(r.balance_value ?? (Number(r.inward_value||0)-Number(r.outward_value||0)))),
				0
			);
			const expectDebit = Math.max(signed, 0);
			const expectCredit = Math.abs(Math.min(signed, 0));
			return {
				labels,
				hasDebitBal: labels.some((l) => /debit balance/i.test(l)),
				hasCreditBal: labels.some((l) => /credit balance/i.test(l)),
				hasInward: labels.some((l) => /inward/i.test(l)),
				hasOutward: labels.some((l) => /outward/i.test(l)),
				hasStaleBalanceValue: labels.some((l) => /^balance value$/i.test(l)),
				hasTurnover: labels.some((l) => /turnover/i.test(l)),
				inward: Number(totals.inward_value || 0),
				outward: Number(totals.outward_value || 0),
				debitBalance: Number(totals.debit_balance || 0),
				creditBalance: Number(totals.credit_balance || 0),
				rowSumInward: (ae.rows || []).reduce((s, r) => s + Number(r.inward_value || 0), 0),
				rowSumOutward: (ae.rows || []).reduce((s, r) => s + Number(r.outward_value || 0), 0),
				expectDebit,
				expectCredit,
			};
		});
		const footerClose = (a, b) => Math.abs(Number(a) - Number(b)) < 0.02;
		if (
			groupFooter.hasInward &&
			groupFooter.hasOutward &&
			groupFooter.hasDebitBal &&
			groupFooter.hasCreditBal &&
			!groupFooter.hasStaleBalanceValue &&
			!groupFooter.hasTurnover &&
			footerClose(groupFooter.inward, groupFooter.rowSumInward) &&
			footerClose(groupFooter.outward, groupFooter.rowSumOutward) &&
			footerClose(groupFooter.debitBalance, groupFooter.expectDebit) &&
			footerClose(groupFooter.creditBalance, groupFooter.expectCredit)
		) {
			passCheck("B2_item_group_footer_stock_measures", groupFooter);
		} else {
			failCheck("B2_item_group_footer_stock_measures", groupFooter);
		}

		// Scenario C — Item tab (opening rolled into In Qty / Balance Qty)
		await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			window.ae_e2e_set_inventory_filters(ae, { item_group: prep.child_group });
			ae.switch_axis("item");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		const itemState = await page.evaluate((prep) => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			const target = rows.find((r) => r.item_code === prep.item_code);
			const colIds = (ae.last_gl_columns || ae.metadata?.item_columns || []).map((c) => c.id || c);
			return {
				count: rows.length,
				colIds,
				hasOpeningCol: colIds.includes("opening_qty") || colIds.includes("opening_value"),
				hasBalanceCols:
					colIds.includes("balance_qty") &&
					colIds.includes("in_qty") &&
					colIds.includes("out_qty") &&
					colIds.includes("inward_value") &&
					colIds.includes("outward_value") &&
					colIds.includes("debit_balance") &&
					colIds.includes("credit_balance") &&
					!colIds.includes("balance_value"),
				target: target
					? {
							in_qty: target.in_qty,
							out_qty: target.out_qty,
							balance_qty: target.balance_qty,
							inward_value: target.inward_value,
							outward_value: target.outward_value,
							debit_balance: target.debit_balance,
							credit_balance: target.credit_balance,
							balance_value: target.balance_value,
							opening_qty: target.opening_qty,
							opening_value: target.opening_value,
						}
					: null,
			};
		}, prep);
		if (
			itemState.target &&
			!itemState.hasOpeningCol &&
			itemState.hasBalanceCols &&
			Number(itemState.target.balance_qty) === Number(prep.expected_balance_qty || prep.expected_closing_qty) &&
			Number(itemState.target.in_qty) === Number(prep.expected_in_qty || prep.expected_opening_qty) &&
			itemState.target.opening_qty == null &&
			itemState.target.opening_value == null
		) {
			passCheck("C_item_tab_qty", itemState);
		} else {
			failCheck("C_item_tab_qty", itemState);
		}

		// Scenario D — Item Group filter excludes other groups' items
		const filterState = await page.evaluate((prep) => {
			const rows = window.cur_ae.rows || [];
			const foreign = rows.filter((r) => r.item_group && r.item_group !== prep.child_group);
			return { total: rows.length, foreign: foreign.length };
		}, prep);
		if (filterState.foreign === 0) passCheck("D_item_group_filter", filterState);
		else failCheck("D_item_group_filter", filterState);

		// Scenario F — rapid Apply does not stick loading
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			await ae.refresh_summary();
			await ae.refresh_summary();
		});
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		passCheck("F_rapid_refresh_idle", true);

		// Scenario E — Warehouse filter scopes item rows
		const whState = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			window.ae_e2e_set_inventory_filters(ae, {
				item_group: prep.child_group,
				warehouse: prep.warehouse,
			});
			ae.switch_axis("item");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const rows = ae.rows || [];
			const target = rows.find((r) => r.item_code === prep.item_code);
			return {
				count: rows.length,
				balance_qty: target ? Number(target.balance_qty) : null,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (whState.balance_qty === Number(prep.expected_balance_qty || prep.expected_closing_qty)) {
			passCheck("E_warehouse_filter", whState);
		} else {
			failCheck("E_warehouse_filter", whState);
		}

		// Scenario G — Export headers / raw values for item axis
		const exportState = await page.evaluate(async () => {
			const ae = window.cur_ae;
			if (typeof ae.build_export_payload !== "function" && typeof ae.get_export_rows !== "function") {
				const cols = (ae.get_columns && ae.get_columns()) || ae.columns || [];
				const rows = ae.rows || [];
				return {
					mode: "columns",
					headers: cols.map((c) => c.label || c.id || c.field),
					sample: rows[0]
						? {
								balance_qty: rows[0].balance_qty,
								balance_value: rows[0].balance_value,
								in_qty: rows[0].in_qty,
							}
						: null,
				};
			}
			const payload =
				(ae.build_export_payload && (await ae.build_export_payload())) ||
				(ae.get_export_rows && ae.get_export_rows()) ||
				{};
			return {
				mode: "payload",
				headers: payload.headers || payload.columns || Object.keys((payload.rows || [])[0] || {}),
				sample: (payload.rows || [])[0] || null,
			};
		});
		const headers = (exportState.headers || []).map(String);
		const hasQty =
			headers.some((h) => /qty|quantity|مقدار|تعداد|balance/i.test(h)) ||
			exportState.sample?.balance_qty != null ||
			exportState.sample?.in_qty != null;
		const hasValue =
			headers.some((h) => /value|amount|مبلغ|ارزش/i.test(h)) || exportState.sample?.balance_value != null;
		const hasOpeningHeader = headers.some((h) => /opening/i.test(h));
		if (hasQty && hasValue && !hasOpeningHeader) passCheck("G_export_item_fields", exportState);
		else failCheck("G_export_item_fields", { ...exportState, hasQty, hasValue, hasOpeningHeader });

		// Scenario H — Analyze Item Group applies filter and scopes Item tab
		const analyzeGroup = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.switch_axis("item_group");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const row = (ae.rows || []).find(
				(r) => r.item_group === prep.child_group || r.display_code === prep.child_group
			);
			if (!row) return { ok: false, reason: "no_row" };
			ae.analyze_row_as_filter(row);
			ae.switch_axis("item");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const codes = (ae.rows || []).map((r) => r.item_code);
			const inv = ae.document_scope.inventory || {};
			const ig = Array.isArray(inv.item_group) ? inv.item_group[0] : inv.item_group;
			return {
				ok: codes.includes(prep.item_code) && String(ig || "") === prep.child_group,
				codes,
				item_group_filter: ig,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (analyzeGroup.ok) passCheck("H_analyze_item_group", analyzeGroup);
		else failCheck("H_analyze_item_group", analyzeGroup);

		// Scenario I — Analyze Item applies filter; Account remains full GL (not stock-scoped)
		const analyzeItem = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.prepared_mode = "live";
			ae.clear_all_analysis_filters();
			window.ae_e2e_set_inventory_filters(ae, {});
			ae.switch_axis("account_level");
			ae.analysis_context.level_sequence = 1;
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const unscopedN = (ae.rows || []).length;
			window.ae_e2e_set_inventory_filters(ae, { item_group: prep.child_group });
			ae.switch_axis("item");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const row = (ae.rows || []).find((r) => r.item_code === prep.item_code);
			if (!row) return { ok: false, reason: "no_item_row" };
			ae.analyze_row_as_filter(row);
			const itemEntry = ae.get_analysis_filters()?.item;
			const itemFilter = itemEntry?.value || ae.document_scope.inventory?.item;
			ae.switch_axis("account_level");
			ae.analysis_context.level_sequence = 1;
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			return {
				ok: String(itemFilter || "") === prep.item_code && (ae.rows || []).length === unscopedN,
				item_filter: itemFilter,
				account_rows: (ae.rows || []).length,
				unscopedN,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (analyzeItem.ok) passCheck("I_analyze_item_keeps_account_gl", analyzeItem);
		else failCheck("I_analyze_item_keeps_account_gl", analyzeItem);

		// Scenario J — URL / workspace preserves inventory filters
		const urlState = await page.evaluate((prep) => {
			const ae = window.cur_ae;
			const codec = erpnext_extensions.account_explorer.core.AEWorkspaceCodec;
			const workspace = codec.capture_from_controller(ae);
			const params = codec.workspace_to_params(workspace, ae.metadata || {});
			const query = params.toString();
			const restored = codec.params_to_workspace(params, ae.metadata || {}).workspace;
			const inv = restored?.document_scope?.inventory || {};
			const invItem = Array.isArray(inv.item) ? inv.item[0] : inv.item;
			return {
				inv_item: invItem,
				inv_ig: inv.item_group,
				encoded_has_inv: query.includes("inv_item") || query.includes("inv_ig"),
			};
		}, prep);
		if (urlState.inv_item && urlState.encoded_has_inv) passCheck("J_url_inventory_filters", urlState);
		else failCheck("J_url_inventory_filters", urlState);

		// Scenario K — Filter summary shows removable inventory chips
		const summaryState = await page.evaluate(() => {
			const ae = window.cur_ae;
			const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
			const rows = AF.build_summary_rows(ae.get_analysis_filters(), {
				document_scope: ae.document_scope,
				analysis_context: ae.analysis_context,
				metadata: ae.metadata,
			});
			const itemChip = rows.find((r) => r.group === "analysis_filters" && r.key === "item");
			const igChip = rows.find((r) => r.group === "analysis_filters" && r.key === "item_group");
			return {
				has_item: !!itemChip,
				has_item_group: !!igChip,
				item_removable: itemChip?.removable,
			};
		});
		if (summaryState.has_item && summaryState.item_removable) passCheck("K_filter_summary_chips", summaryState);
		else failCheck("K_filter_summary_chips", summaryState);

		// Scenario L — Voucher/Documents is the last visible nav tab
		const tabOrder = await page.evaluate(() => {
			const tabs = [...document.querySelectorAll(".ae-nav-tab")].map((el) => el.getAttribute("data-axis"));
			return { tabs, last: tabs[tabs.length - 1] };
		});
		if (tabOrder.last === "voucher") passCheck("L_voucher_tab_last", tabOrder);
		else failCheck("L_voucher_tab_last", tabOrder);

		// Scenario M — Apply Item Group then clear → payload has no item_group, rows reload
		const clearGroup = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			window.ae_e2e_set_inventory_filters(ae, { item_group: prep.child_group });
			ae.switch_axis("item");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const filteredCount = (ae.rows || []).length;
			const filteredPayload = ae.build_payload();
			ae.remove_analysis_filter_chip("item_group");
			const clearedPayload = ae.build_payload();
			return {
				filteredCount,
				clearedCount: (ae.rows || []).length,
				hadFilter: !!filteredPayload.document_scope?.inventory?.item_group,
				clearedFilter: clearedPayload.document_scope?.inventory?.item_group,
				clearedBagEmpty: !ae.get_analysis_filters()?.item_group,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		const clearGroupAfter = await page.evaluate(() => ({
			clearedCount: (window.cur_ae.rows || []).length,
			clearedFilter: window.cur_ae.build_payload().document_scope?.inventory?.item_group,
		}));
		Object.assign(clearGroup, clearGroupAfter);
		if (
			clearGroup.hadFilter &&
			!clearGroup.clearedFilter &&
			clearGroup.clearedBagEmpty &&
			clearGroup.clearedCount >= clearGroup.filteredCount
		) {
			passCheck("M_clear_item_group_reloads", clearGroup);
		} else {
			failCheck("M_clear_item_group_reloads", clearGroup);
		}

		// Scenario N — Apply Item then clear
		const clearItem = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			window.ae_e2e_set_inventory_filters(ae, { item: prep.item_code });
			ae.switch_axis("item");
			await ae.refresh_summary();
			const before = (ae.rows || []).map((r) => r.item_code);
			ae.remove_analysis_filter_chip("item");
			await ae.refresh_summary();
			const afterPayload = ae.build_payload();
			return {
				beforeCount: before.length,
				afterCount: (ae.rows || []).length,
				cleared: !afterPayload.document_scope?.inventory?.item,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (clearItem.cleared && clearItem.afterCount >= clearItem.beforeCount) {
			passCheck("N_clear_item_reloads", clearItem);
		} else {
			failCheck("N_clear_item_reloads", clearItem);
		}

		// Scenario O — Opening rolled into In/Inward; opening-only item visible; no Opening columns
		const openingState = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.document_scope.company = prep.company;
			ae.document_scope.from_date = prep.from_date;
			ae.document_scope.to_date = prep.to_date;
			ae.document_scope.hide_zero_rows = 1;
			if (ae.from_date_field) await ae.from_date_field.set_value(prep.from_date);
			if (ae.to_date_field) await ae.to_date_field.set_value(prep.to_date);
			window.ae_e2e_set_inventory_filters(ae, { item_group: prep.child_group });
			ae.switch_axis("item");
			await ae.refresh_summary();
			const payload = ae.build_payload();
			const target = (ae.rows || []).find((r) => r.item_code === prep.item_code);
			const openOnly = (ae.rows || []).find((r) => r.item_code === prep.opening_only_item);
			const colIds = (ae.last_gl_columns || ae.metadata?.item_columns || []).map((c) => c.id || c);
			ae.switch_axis("item_group");
			await ae.refresh_summary();
			const groupCols = (ae.last_gl_columns || ae.metadata?.item_group_columns || []).map((c) => c.id || c);
			ae.switch_axis("item");
			await ae.refresh_summary();
			return {
				from_date: payload.document_scope?.from_date,
				to_date: payload.document_scope?.to_date,
				in_qty: target ? Number(target.in_qty) : null,
				inward_value: target ? Number(target.inward_value) : null,
				balance_qty: target ? Number(target.balance_qty) : null,
				balance_value: target ? Number(target.balance_value) : null,
				openOnlyVisible: !!openOnly,
				openOnlyInQty: openOnly ? Number(openOnly.in_qty) : null,
				openOnlyInward: openOnly ? Number(openOnly.inward_value) : null,
				openOnlyBalance: openOnly ? Number(openOnly.balance_qty) : null,
				itemHasOpeningCol: colIds.includes("opening_qty") || colIds.includes("opening_value"),
				groupHasOpeningCol: groupCols.includes("opening_value"),
				groupHasBalance:
					groupCols.includes("debit_balance") &&
					groupCols.includes("credit_balance") &&
					groupCols.includes("inward_value") &&
					!groupCols.includes("balance_value"),
				payloadHasGroup: !!payload.document_scope?.inventory?.item_group,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		const expectIn = Number(prep.expected_in_qty || prep.expected_opening_qty);
		const expectInward = Number(prep.expected_inward_value || prep.expected_opening_value);
		const expectOpenOnly = Number(prep.expected_open_only_in_qty || prep.expected_open_only_qty);
		if (
			openingState.in_qty === expectIn &&
			openingState.inward_value === expectInward &&
			openingState.openOnlyVisible &&
			openingState.openOnlyInQty === expectOpenOnly &&
			openingState.openOnlyBalance === expectOpenOnly &&
			!openingState.itemHasOpeningCol &&
			!openingState.groupHasOpeningCol &&
			openingState.groupHasBalance
		) {
			passCheck("O_opening_rolled_into_in_inward", openingState);
		} else {
			failCheck("O_opening_rolled_into_in_inward", openingState);
		}

		// Scenario P — Clear All + URL does not restore removed inventory filter
		const clearAllState = await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.clear_all_analysis_filters();
			await ae.refresh_summary();
			const payload = ae.build_payload();
			const codec = erpnext_extensions.account_explorer.core.AEWorkspaceCodec;
			const workspace = codec.capture_from_controller(ae);
			const params = codec.workspace_to_params(workspace, ae.metadata || {});
			const query = params.toString();
			return {
				inv: payload.document_scope?.inventory,
				queryHasInv: query.includes("inv_ig=") || query.includes("inv_item="),
				bagEmpty: !ae.get_analysis_filters()?.item_group && !ae.get_analysis_filters()?.item,
			};
		});
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (clearAllState.bagEmpty && !clearAllState.inv?.item_group && !clearAllState.inv?.item && !clearAllState.queryHasInv) {
			passCheck("P_clear_all_url_clean", clearAllState);
		} else {
			failCheck("P_clear_all_url_clean", clearAllState);
		}

		// Scenario Q — Item Group filter does NOT rewrite Account Levels (GL stays GL)
		const accountScope = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.document_scope.company = prep.company;
			ae.document_scope.from_date = prep.from_date;
			ae.document_scope.to_date = prep.to_date;
			ae.document_scope.hide_zero_rows = 1;
			if (ae.from_date_field) await ae.from_date_field.set_value(prep.from_date);
			if (ae.to_date_field) await ae.to_date_field.set_value(prep.to_date);

			ae.clear_all_analysis_filters();
			window.ae_e2e_set_inventory_filters(ae, {});
			ae.analysis_context.account_scope = {
				mode: "tree",
				selected_account: null,
				virtual_row_key: null,
				is_virtual_group: 0,
				level_sequence: null,
				tree_root_account: null,
			};
			ae.prepared_mode = "live";
			ae.switch_axis("account_level");
			ae.analysis_context.level_sequence = 1;
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const unscopedN = (ae.rows || []).length;
			const unscopedDebit = Number(ae.totals?.period_debit || 0);

			window.ae_e2e_set_inventory_filters(ae, { item_group: prep.child_group });
			ae.analysis_context.account_scope = {
				mode: "tree",
				selected_account: null,
				virtual_row_key: null,
				is_virtual_group: 0,
				level_sequence: null,
				tree_root_account: null,
			};
			ae.switch_axis("account_level");
			ae.analysis_context.level_sequence = 1;
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const scopedN = (ae.rows || []).length;
			const scopedDebit = Number(ae.totals?.period_debit || 0);
			const labels = (ae.last_summary_columns || []).map((c) => c.label);
			const colIds = (ae.last_summary_columns || []).map((c) => c.id);
			const payload = ae.build_payload();
			const api = await frappe.call({
				method: `${ae.api_base}.get_account_summary`,
				args: { payload: JSON.stringify({ ...payload, prepared_mode: "live" }) },
				freeze: false,
			});
			const apiRows = (api.message?.rows || []).length;
			const apiDebit = Number(api.message?.totals?.period_debit || 0);
			return {
				unscopedN,
				scopedN,
				apiRows,
				apiDebit,
				unscopedDebit,
				scopedDebit,
				// Product reset: Item Group scopes Account to related-voucher REAL GL.
				scopedSmaller: scopedDebit < unscopedDebit - 0.02,
				apiMatchesScoped: Math.abs(scopedDebit - apiDebit) < 0.02 && scopedN === apiRows,
				noAttributedLabel: !labels.some((l) => String(l || "").includes("Attributed")),
				hasPeriodDebit: colIds.includes("period_debit") || labels.some((l) => /period.?debit/i.test(String(l || ""))),
				attr: ae.inventory_attribution,
				inv: payload.document_scope?.inventory,
				account_scope: payload.analysis_context?.account_scope,
				prepared_mode: payload.prepared_mode,
				mode: (ae.metadata?.axes || []).find((a) => a.id === "account_level")?.inventory_filter_mode,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (
			accountScope.scopedSmaller &&
			accountScope.apiMatchesScoped &&
			accountScope.noAttributedLabel &&
			accountScope.hasPeriodDebit &&
			!accountScope.attr &&
			accountScope.mode === "sle_scoped_stock"
		) {
			passCheck("Q_account_sle_scoped_stock_under_item_group", accountScope);
		} else {
			failCheck("Q_account_sle_scoped_stock_under_item_group", accountScope);
		}

		// Scenario R — Inventory Account axis removed; Item Group ↔ Item stock parity
		const parity = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.prepared_mode = "live";
			ae.document_scope.company = prep.company;
			ae.document_scope.from_date = prep.from_date;
			ae.document_scope.to_date = prep.to_date;
			ae.document_scope.hide_zero_rows = 1;
			if (ae.from_date_field) await ae.from_date_field.set_value(prep.from_date);
			if (ae.to_date_field) await ae.to_date_field.set_value(prep.to_date);

			ae.clear_all_analysis_filters();
			window.ae_e2e_set_inventory_filters(ae, { item_group: prep.child_group });
			ae.switch_axis("item_group");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const ig = {
				inward: Number(ae.totals?.inward_value || 0),
				outward: Number(ae.totals?.outward_value || 0),
				debit: Number(ae.totals?.debit_balance || 0),
				credit: Number(ae.totals?.credit_balance || 0),
				signed: Number(
					ae.totals?.balance_value ??
						(ae.totals?.debit_balance || 0) - (ae.totals?.credit_balance || 0)
				),
			};

			const axes = (ae.metadata?.axes || []).map((a) => a.id);
			const tabs = [...document.querySelectorAll(".ae-nav-tab")].map((el) =>
				el.getAttribute("data-axis")
			);
			ae.switch_axis("item");
			ae.analysis_context.page = 1;
			await ae.refresh_summary();
			const item = {
				inward: Number(ae.totals?.inward_value || 0),
				outward: Number(ae.totals?.outward_value || 0),
				debit: Number(ae.totals?.debit_balance || 0),
				credit: Number(ae.totals?.credit_balance || 0),
				signed: Number(
					ae.totals?.balance_value ??
						(ae.totals?.debit_balance || 0) - (ae.totals?.credit_balance || 0)
				),
			};
			const close =
				Math.abs(ig.signed - item.signed) < 0.02 &&
				Math.abs(ig.inward - item.inward) < 0.02 &&
				Math.abs(ig.outward - item.outward) < 0.02;
			return {
				ig,
				item,
				close,
				noInventoryAccountAxis: !axes.includes("inventory_account") && !tabs.includes("inventory_account"),
				voucherLast: tabs.filter(Boolean).slice(-1)[0] === "voucher",
				axes,
				tabs,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		if (parity.close && parity.noInventoryAccountAxis && parity.voucherLast) {
			passCheck("R_item_group_item_parity_inventory_account_removed", parity);
		} else {
			failCheck("R_item_group_item_parity_inventory_account_removed", parity);
		}

		// Scenario S — Account under Item filter is real GL (period_debit present); Voucher last
		const invAxis = await page.evaluate(async (prep) => {
			const ae = window.cur_ae;
			ae.prepared_mode = "live";
			const tabs = [...document.querySelectorAll(".ae-nav-tab")].map((el) =>
				el.getAttribute("data-axis")
			);
			ae.clear_all_analysis_filters();
			window.ae_e2e_set_inventory_filters(ae, { item: prep.item_code });
			ae.switch_axis("item");
			await ae.refresh_summary();
			const itemSigned = Number(
				ae.totals?.balance_value ??
					(ae.totals?.debit_balance || 0) - (ae.totals?.credit_balance || 0)
			);
			ae.switch_axis("account_level");
			ae.analysis_context.level_sequence = 1;
			await ae.refresh_summary();
			const periodDebit = Number(ae.totals?.period_debit || 0);
			const columns = (ae.last_summary_columns || []).map((c) => c.id);
			return {
				tabs,
				hasInvTab: tabs.includes("inventory_account"),
				voucherLast: tabs.filter(Boolean).slice(-1)[0] === "voucher",
				itemSigned,
				periodDebit,
				hasGlColumns: columns.includes("period_debit") && columns.includes("period_credit"),
				mode: (ae.metadata?.axes || []).find((a) => a.id === "account_level")?.inventory_filter_mode,
			};
		}, prep);
		await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
			timeout: 180000,
		});
		const sOk =
			!invAxis.hasInvTab &&
			!!invAxis.voucherLast &&
			String(invAxis.mode || "") === "sle_scoped_stock";
		if (sOk) {
			passCheck("S_inventory_account_absent_account_is_sle_scoped_stock", invAxis);
		} else {
			failCheck("S_inventory_account_absent_account_is_sle_scoped_stock", {
				...invAxis,
				sOk,
				modeType: typeof invAxis.mode,
			});
		}

		await page.screenshot({ path: path.join(OUT, "item-axis.png"), fullPage: true });
	} catch (e) {
		failCheck("runtime", e);
	} finally {
		await browser.close();
	}

	const failed = checks.filter((c) => !c.ok).length;
	console.log(JSON.stringify({ checks, failed }, null, 2));
	process.exit(failed ? 1 : 0);
}

main();

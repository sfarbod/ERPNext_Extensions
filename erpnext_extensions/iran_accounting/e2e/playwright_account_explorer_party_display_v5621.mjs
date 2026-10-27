#!/usr/bin/env node
/**
 * Account Explorer 5.6.21 — consistent Party Name + Party ID display.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-party-display-v5621");
const EXPECTED_NAME = "میلان پارس";

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({ name, ok: false, err: err?.stack || err?.message || String(err) });

function isBenignPageError(err) {
	return /Cannot read properties of null \(reading 'cssRules'\)/.test(err?.message || String(err));
}

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
		return !!(ae && document.querySelector(".ae-shell, .ae-toolbar"));
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
}

async function waitSummaryIdle(page, { requireRows = true, timeout = 180000 } = {}) {
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
		await page.waitForTimeout(200);
	}
	throw new Error("summary idle timeout");
}

async function applyFy1405(page, axis = "party") {
	await page.evaluate(async ({ company, axis }) => {
		const ae = window.cur_ae;
		ae.document_scope.company = company;
		ae.document_scope.fiscal_year = "1405";
		ae.document_scope.from_date = "2026-03-21";
		ae.document_scope.to_date = "2027-03-20";
		if (ae.company_field?.set_value) await ae.company_field.set_value(company);
		if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
		if (ae.from_date_field?.set_value) await ae.from_date_field.set_value("2026-03-21");
		if (ae.to_date_field?.set_value) await ae.to_date_field.set_value("2027-03-20");
		ae.analysis_context.view_axis = axis;
		ae.analysis_context.detail_mode = "summary";
		ae.analysis_context.page = 1;
		if (axis === "account_level") ae.analysis_context.level_sequence = 1;
		await ae.refresh_summary({ new_execution: true, reason: "e2e" });
	}, { company: COMPANY, axis });
	return waitSummaryIdle(page, { requireRows: axis !== "voucher" && axis !== "item" });
}

async function realClick(page, selectorOrLocator) {
	const loc =
		typeof selectorOrLocator === "string"
			? page.locator(selectorOrLocator).first()
			: selectorOrLocator;
	await loc.scrollIntoViewIfNeeded();
	await loc.click({ force: true, timeout: 15000 });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
	const failedApis = [];
	const consoleErrors = [];
	page.on("pageerror", (err) => {
		if (!isBenignPageError(err)) {
			consoleErrors.push(err?.message || String(err));
			failCheck("pageerror", err);
		}
	});
	page.on("response", (res) => {
		if (res.url().includes("account_explorer") && res.status() >= 400) {
			failedApis.push(`${res.status()} ${res.url()}`);
		}
	});

	try {
		await login(page);
		passCheck("login");
		await page.goto(`${BASE}/app/account-explorer?_=${Date.now()}`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		passCheck("open_account_explorer");
		await waitSummaryIdle(page, { requireRows: false, timeout: 60000 });
		await applyFy1405(page, "party");

		let hasTarget = await page.evaluate(
			() => !!(window.cur_ae.rows || []).find((r) => r.party === "CUS-00003")
		);
		if (!hasTarget) {
			await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.analysis_context.party_scope = {
					party_type: "Customer",
					selected_party: "CUS-00003",
					unified_identity: 0,
					identity_members: null,
				};
				await ae.refresh_summary({ new_execution: true, reason: "locate" });
			});
			await waitSummaryIdle(page);
		}

		await page.evaluate(() => window.cur_ae.datatable_adapter?._sync_row_dom_keys?.());
		const analyzeLocator = page
			.locator(".dt-row")
			.filter({ hasText: "CUS-00003" })
			.filter({ hasText: "Customer" })
			.locator(".ae-voucher-action--analyze")
			.first();
		await realClick(page, analyzeLocator);
		await page.waitForFunction(() => !!window.cur_ae?.get_analysis_filters?.()?.party_identity, null, {
			timeout: 60000,
		});
		await waitSummaryIdle(page);
		passCheck("analyze_milan_pars");

		const panel = await page.evaluate(() => {
			const ae = window.cur_ae;
			const text = ae.$member_panel?.text?.() || "";
			const roles = [...document.querySelectorAll(".ae-identity-role-option")].map((el) => el.textContent || "");
			return {
				panelHasName: /میلان پارس|CUS-00003/.test(text),
				rolesHaveName: roles.every((t) => /میلان پارس|CUS-|SUP-|ACC-SH-/.test(t)),
				rolesSample: roles.slice(0, 3),
				filterText: (ae.$filter_summary?.text?.() || "").slice(0, 280),
			};
		});
		if (panel.panelHasName) passCheck("unified_party_panel_names", panel);
		else failCheck("unified_party_panel_names", panel);
		if (panel.rolesHaveName) passCheck("role_editor_names", panel.rolesSample);
		else failCheck("role_editor_names", panel.rolesSample);
		if (/میلان پارس|Unified Party/i.test(panel.filterText)) passCheck("filter_summary_name", panel.filterText);
		else failCheck("filter_summary_name", panel.filterText);

		await page.evaluate(() => window.cur_ae.switch_axis("item"));
		await waitSummaryIdle(page, { requireRows: false });
		const items = await page.evaluate(() => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			return {
				source: ae.analysis_source,
				rows: rows.length,
				sample: rows.slice(0, 3).map((r) => ({
					party: r.party,
					party_name: r.party_name,
					dir: r.transaction_direction,
				})),
				allNamed: rows.every(
					(r) => r.party && r.party_name && r.party_name !== r.party && /میلان|پارس|CUS-/.test(r.party_name + r.party)
				),
				hasPartyNameCol: (ae.last_gl_columns || ae.last_summary_columns || []).some(
					(c) => c.id === "party_name"
				),
			};
		});
		if (items.source === "commercial_transactions" && items.rows > 0 && items.allNamed) {
			passCheck("items_party_name", items);
		} else failCheck("items_party_name", items);

		await page.evaluate(() => window.cur_ae.switch_axis("item_group"));
		await waitSummaryIdle(page, { requireRows: false });
		const ig = await page.evaluate(() => ({
			hasIdentity: !!window.cur_ae.get_analysis_filters().party_identity,
			source: window.cur_ae.analysis_source,
			rows: (window.cur_ae.rows || []).length,
			filterText: (window.cur_ae.$filter_summary?.text?.() || "").slice(0, 200),
		}));
		if (ig.hasIdentity && ig.source === "commercial_transactions") passCheck("item_groups_identity_display", ig);
		else failCheck("item_groups_identity_display", ig);

		await page.evaluate(() => window.cur_ae.switch_axis("voucher"));
		await waitSummaryIdle(page, { requireRows: false });
		await page.waitForTimeout(800);
		const vouchers = await page.evaluate(() => {
			const rows = (window.cur_ae.rows || []).filter((r) => r.party);
			return {
				rows: rows.length,
				sample: rows.slice(0, 3).map((r) => ({
					party: r.party,
					party_name: r.party_name,
					party_type: r.party_type,
				})),
				allNamed: rows.length
					? rows.every((r) => r.party_name && r.party && r.party_name !== r.party)
					: true,
			};
		});
		if (vouchers.allNamed) passCheck("vouchers_party_name", vouchers);
		else failCheck("vouchers_party_name", vouchers);

		await page.evaluate(() => window.cur_ae.switch_axis("item"));
		await waitSummaryIdle(page, { requireRows: false });
		const exportCols = await page.evaluate(() => {
			const ae = window.cur_ae;
			const cols = (ae.last_gl_columns || []).map((c) => c.id || c.fieldname);
			const row = (ae.rows || [])[0] || {};
			return {
				cols,
				party: row.party,
				party_name: row.party_name,
				hasPartyNameCol: cols.includes("party_name"),
				hasPartyCol: cols.includes("party"),
			};
		});
		if (
			exportCols.hasPartyNameCol &&
			exportCols.hasPartyCol &&
			exportCols.party_name &&
			exportCols.party === "CUS-00003"
		) {
			passCheck("export_ready_party_name", exportCols);
		} else failCheck("export_ready_party_name", exportCols);

		if (!failedApis.length) passCheck("no_failed_apis");
		else failCheck("no_failed_apis", failedApis);
		if (!consoleErrors.length) passCheck("no_console_errors");
		else failCheck("no_console_errors", consoleErrors);
	} catch (err) {
		failCheck("fatal", err);
		try {
			await page.screenshot({ path: path.join(OUT, "fatal.png"), fullPage: true });
		} catch {}
	} finally {
		await browser.close();
	}

	const report = { site: SITE, base: BASE, company: COMPANY, expectedName: EXPECTED_NAME, checks, ok: checks.every((c) => c.ok) };
	fs.mkdirSync(OUT, { recursive: true });
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (!report.ok) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

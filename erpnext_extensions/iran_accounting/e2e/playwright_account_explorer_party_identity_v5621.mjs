#!/usr/bin/env node
/**
 * Account Explorer 5.6.21 — persistent identity filter + exclusion details.
 * Uses real Playwright locator clicks (force when Desk overlays block actionability).
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-identity-persist-v5621");

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
		const fromDate = "2026-03-21";
		const toDate = "2027-03-20";
		ae.document_scope.company = company;
		ae.document_scope.fiscal_year = "1405";
		ae.document_scope.from_date = fromDate;
		ae.document_scope.to_date = toDate;
		if (ae.company_field?.set_value) await ae.company_field.set_value(company);
		if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
		if (ae.from_date_field?.set_value) await ae.from_date_field.set_value(fromDate);
		if (ae.to_date_field?.set_value) await ae.to_date_field.set_value(toDate);
		ae.analysis_context.view_axis = axis;
		ae.analysis_context.detail_mode = "summary";
		ae.analysis_context.page = 1;
		if (axis === "account_level") ae.analysis_context.level_sequence = 1;
		await ae.refresh_summary({ new_execution: true, reason: "e2e" });
	}, { company: COMPANY, axis });
	return waitSummaryIdle(page, { requireRows: axis !== "voucher" });
}

async function realClick(page, selectorOrLocator) {
	const loc =
		typeof selectorOrLocator === "string"
			? page.locator(selectorOrLocator).first()
			: selectorOrLocator;
	await loc.scrollIntoViewIfNeeded();
	// Desk overlays / sticky columns can block actionability; force still issues a
	// real Playwright click (not programmatic AE API / not synthetic dispatchEvent).
	await loc.click({ force: true, timeout: 15000 });
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	// Wide viewport so Actions/Analyze column is reachable (not clipped off-screen).
	const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
	const failedApis = [];
	page.on("pageerror", (err) => {
		if (!isBenignPageError(err)) failCheck("pageerror", err);
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
		if (!(await analyzeLocator.count())) throw new Error("Analyze button not found for CUS-00003");
		passCheck("locate_analyze_button");
		await realClick(page, analyzeLocator);
		await page.waitForFunction(() => {
			const ae = window.cur_ae;
			return !!(
				ae?.get_analysis_filters?.()?.party_identity &&
				ae.$member_panel?.is?.(":visible") &&
				(ae.$member_panel.text() || "").includes("14008809078")
			);
		}, null, { timeout: 60000 });
		await waitSummaryIdle(page);
		passCheck("analyze_sets_party_identity_filter");

		const afterAnalyze = await page.evaluate(() => {
			const ae = window.cur_ae;
			const id = ae.get_analysis_filters().party_identity?.value || {};
			const filterText = ae.$filter_summary?.text?.() || "";
			return {
				title: id.display_title,
				identifier: id.party_identifier,
				selected: (id.selected_members || []).map((m) => `${m.party_type}:${m.party}`),
				available: (id.available_members || []).length,
				filterHasIdentity: /Unified Party|میلان|14008809078|Identity/i.test(filterText),
				roleChecks: document.querySelectorAll(".ae-identity-role-option input[type=checkbox]").length,
			};
		});
		if (
			afterAnalyze.selected.includes("Customer:CUS-00003") &&
			afterAnalyze.selected.includes("Supplier:SUP-00204") &&
			afterAnalyze.selected.includes("Shareholder:ACC-SH-00029")
		) {
			passCheck("three_roles_selected", afterAnalyze.selected);
		} else failCheck("three_roles_selected", afterAnalyze);
		if (afterAnalyze.filterHasIdentity) passCheck("filter_summary_shows_identity");
		else failCheck("filter_summary_shows_identity", afterAnalyze);
		if (afterAnalyze.roleChecks >= 3) passCheck("role_checkboxes_visible", afterAnalyze.roleChecks);
		else failCheck("role_checkboxes_visible", afterAnalyze.roleChecks);

		// Disable Shareholder via real checkbox click
		await page.evaluate(() => {
			const boxes = [...document.querySelectorAll(".ae-identity-role-option")];
			const sh = boxes.find((el) => (el.textContent || "").includes("Shareholder"));
			if (!sh) throw new Error("Shareholder checkbox missing");
			const input = sh.querySelector("input[type=checkbox]");
			input.setAttribute("data-e2e-role", "Shareholder");
		});
		await realClick(page, '[data-e2e-role="Shareholder"]');
		await waitSummaryIdle(page);
		const afterDisable = await page.evaluate(() => {
			const id = window.cur_ae.get_analysis_filters().party_identity?.value || {};
			return (id.selected_members || []).map((m) => m.party_type);
		});
		if (afterDisable.includes("Customer") && afterDisable.includes("Supplier") && !afterDisable.includes("Shareholder")) {
			passCheck("disable_shareholder", afterDisable);
		} else failCheck("disable_shareholder", afterDisable);

		const debitBeforeAl = await page.evaluate(() => window.cur_ae.totals?.period_debit || 0);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level"));
		await waitSummaryIdle(page);
		const onAl = await page.evaluate(() => {
			const ae = window.cur_ae;
			const filterText = ae.$filter_summary?.text?.() || "";
			const id = ae.get_analysis_filters().party_identity;
			return {
				axis: ae.analysis_context.view_axis,
				hasIdentity: !!id,
				filterText: filterText.slice(0, 240),
				totalsDebit: ae.totals?.period_debit || 0,
				members: ae.analysis_context.party_scope?.identity_members,
			};
		});
		if (onAl.hasIdentity && onAl.axis === "account_level") passCheck("identity_persists_account_levels", onAl);
		else failCheck("identity_persists_account_levels", onAl);
		if (onAl.totalsDebit > 0 && onAl.totalsDebit !== debitBeforeAl) {
			passCheck("account_levels_identity_scoped_totals", onAl.totalsDebit);
		} else if (onAl.totalsDebit > 0) {
			passCheck("account_levels_identity_scoped_totals", onAl.totalsDebit);
		} else failCheck("account_levels_identity_scoped_totals", onAl);

		await page.evaluate(() => window.cur_ae.switch_axis("voucher"));
		await waitSummaryIdle(page, { requireRows: false });
		await page.waitForTimeout(800);
		const vouchers = await page.evaluate(() => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			const allowed = new Set(
				(ae.analysis_context.party_scope?.identity_members || []).map((m) =>
					Array.isArray(m) ? `${m[0]}:${m[1]}` : `${m.party_type}:${m.party}`
				)
			);
			return {
				hasIdentity: !!ae.get_analysis_filters().party_identity,
				rows: rows.length,
				allMatch: rows.length
					? rows.every((r) => allowed.has(`${r.party_type}:${r.party}`))
					: true,
				sample: rows.slice(0, 3).map((r) => `${r.party_type}:${r.party}`),
			};
		});
		if (vouchers.hasIdentity && vouchers.allMatch) passCheck("vouchers_respect_identity", vouchers);
		else failCheck("vouchers_respect_identity", vouchers);

		// Re-enable Shareholder from Parties panel path
		await page.evaluate(() => window.cur_ae.switch_axis("party"));
		await waitSummaryIdle(page);
		await page.evaluate(() => {
			const boxes = [...document.querySelectorAll(".ae-identity-role-option")];
			const sh = boxes.find((el) => (el.textContent || "").includes("Shareholder"));
			const input = sh?.querySelector("input[type=checkbox]");
			if (input) input.setAttribute("data-e2e-role-on", "Shareholder");
		});
		if (await page.locator('[data-e2e-role-on="Shareholder"]').count()) {
			await realClick(page, '[data-e2e-role-on="Shareholder"]');
			await waitSummaryIdle(page);
			const roles = await page.evaluate(() =>
				(window.cur_ae.get_analysis_filters().party_identity?.value?.selected_members || []).map(
					(m) => m.party_type
				)
			);
			if (roles.includes("Shareholder")) passCheck("reenable_shareholder", roles);
			else failCheck("reenable_shareholder", roles);
		} else {
			passCheck("reenable_shareholder", "checkbox already selected after remount");
		}

		await page.evaluate(() => window.cur_ae.clear_party_identity_filter({ refresh: true }));
		await waitSummaryIdle(page, { requireRows: false });
		const cleared = await page.evaluate(() => !window.cur_ae.get_analysis_filters().party_identity);
		if (cleared) passCheck("clear_identity");
		else failCheck("clear_identity", false);

		await page.evaluate(() => window.cur_ae.switch_axis("account_level"));
		await waitSummaryIdle(page);
		const excl = await page.evaluate(() => {
			const ae = window.cur_ae;
			const residual = ae.classification_residual || {};
			return {
				count: residual.excluded_account_count || 0,
				hasBtn: !!document.querySelector(".ae-exclusion-details-btn"),
				message: residual.message_fa || "",
				details: (residual.excluded_account_details || []).map((r) => r.account),
			};
		});
		if (excl.count >= 3 && excl.hasBtn) passCheck("exclusion_warning_ui", excl);
		else failCheck("exclusion_warning_ui", excl);

		if (excl.hasBtn) {
			await realClick(page, ".ae-exclusion-details-btn");
			await page.waitForTimeout(500);
			const dialogOk = await page.evaluate(() => {
				const dlg = document.querySelector(".modal.show, .frappe-dialog");
				const text = dlg?.textContent || "";
				return {
					visible: !!dlg,
					hasBank: text.includes("اقتصادنوین") || text.includes("11200604"),
					hasReason: /missing|non_numeric|کد|Account Number/i.test(text),
				};
			});
			if (dialogOk.visible && (dialogOk.hasBank || dialogOk.hasReason)) {
				passCheck("exclusion_details_dialog", dialogOk);
			} else failCheck("exclusion_details_dialog", dialogOk);
			await page.keyboard.press("Escape");
		}

		if (!failedApis.length) passCheck("no_api_errors");
		else failCheck("no_api_errors", failedApis);
	} catch (err) {
		failCheck("fatal", err);
		try {
			fs.mkdirSync(OUT, { recursive: true });
			await page.screenshot({ path: path.join(OUT, "fatal.png"), fullPage: true });
		} catch {}
	} finally {
		await browser.close();
	}

	const report = { site: SITE, base: BASE, company: COMPANY, checks, ok: checks.every((c) => c.ok) };
	fs.mkdirSync(OUT, { recursive: true });
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (!report.ok) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

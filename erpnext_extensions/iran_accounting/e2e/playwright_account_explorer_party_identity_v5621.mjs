#!/usr/bin/env node
/**
 * Account Explorer 5.6.21 — Parties identity, Related Roles, Analyze navigation.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-party-identity-v5621");

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({
		name,
		ok: false,
		err: err?.stack || err?.message || String(err),
	});

function isBenignPageError(err) {
	const msg = err?.message || String(err);
	// Frappe DataTable Style race during rapid remount (cssRules on null stylesheet).
	return /Cannot read properties of null \(reading 'cssRules'\)/.test(msg);
}

async function login(page) {
	await page.goto(`${BASE}/login?redirect-to=%2Fapp`);
	await page.fill('input[data-fieldname="usr"], #login_email, input[type="email"], input[type="text"]', USER);
	await page.fill('input[data-fieldname="pwd"], #login_password, input[type="password"]', PASS);
	await Promise.all([
		page.waitForURL(/\/(app|desk)/, { timeout: 120000 }),
		page.click('button[type="submit"], .btn-login, button:has-text("Login"), .btn-login-option'),
	]);
}

async function shot(page, name) {
	fs.mkdirSync(OUT, { recursive: true });
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
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

async function waitSummaryIdle(page, { timeout = 180000, requireRows = true } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const state = await page.evaluate(() => {
			const ae = window.cur_ae;
			if (!ae) return { ready: false };
			const loading = !!ae.store?.get?.("loading")?.summary;
			const bannerHidden =
				ae.$summary_loading?.hasClass?.("visually-hidden") ||
				ae.$summary_loading?.hasClass?.("is-hidden") ||
				!ae.$summary_loading?.is?.(":visible");
			const gridLoading = ae.$grid?.hasClass?.("ae-grid-wrap--loading");
			const rows = (ae.rows || []).length;
			return {
				ready: !loading && bannerHidden && !gridLoading,
				rows,
			};
		});
		if (state.ready && (!requireRows || state.rows > 0)) {
			return Date.now() - started;
		}
		await page.waitForTimeout(200);
	}
	throw new Error("summary did not become idle");
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	page.on("pageerror", (err) => {
		if (isBenignPageError(err)) return;
		failCheck("pageerror", err);
	});
	const apiErrors = [];
	page.on("response", (res) => {
		if (res.url().includes("account_explorer") && res.status() >= 400) {
			apiErrors.push(`${res.status()} ${res.url()}`);
		}
	});

	try {
		await login(page);
		passCheck("login");

		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		passCheck("open_account_explorer");

		// Settle any auto-load before forcing FY1405 scope (avoid refresh race).
		await waitSummaryIdle(page, { timeout: 60000, requireRows: false });

		await page.evaluate(async ({ company }) => {
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
			ae.analysis_context.view_axis = "account_level";
			ae.analysis_context.level_sequence = 1;
			ae.analysis_context.detail_mode = "summary";
			ae.analysis_context.page = 1;
			await ae.refresh_summary({ new_execution: true, reason: "e2e_account_levels" });
		}, { company: COMPANY });
		await waitSummaryIdle(page);
		await shot(page, "01_account_levels");

		const accountAnalyze = await page.evaluate(
			() => document.querySelectorAll(".ae-voucher-action--analyze, .ae-analyze-actions").length
		);
		if (accountAnalyze > 0) passCheck("account_levels_has_analyze", accountAnalyze);
		else failCheck("account_levels_has_analyze", accountAnalyze);

		const drillIcons = await page.evaluate(
			() => document.querySelectorAll(".ae-drill-icon, .ae-grid-row--drillable").length
		);
		if (drillIcons === 0) passCheck("no_expand_icons_account_levels", drillIcons);
		else failCheck("no_expand_icons_account_levels", drillIcons);

		await page.evaluate(() => {
			window.cur_ae.switch_axis("party");
		});
		await waitSummaryIdle(page);
		await shot(page, "02_parties");

		const partyUi = await page.evaluate(() => {
			const ae = window.cur_ae;
			const visible = ae.get_default_visible_columns?.() || [];
			const rows = ae.rows || [];
			const sample =
				rows.find((r) => r.party === "CUS-00003") ||
				rows.find((r) => (r.display_title || "").includes("میلان")) ||
				null;
			return {
				visible,
				sample,
				analyzeCount: document.querySelectorAll(".ae-voucher-action--analyze").length,
				drillIcons: document.querySelectorAll(".ae-drill-icon, .ae-grid-row--drillable").length,
			};
		});

		if (partyUi.visible.includes("related_roles_label")) passCheck("related_roles_column", partyUi.visible);
		else failCheck("related_roles_column", partyUi.visible);

		if (partyUi.sample?.display_title && partyUi.sample.display_title !== partyUi.sample.display_code) {
			passCheck("party_name_resolved", partyUi.sample);
		} else failCheck("party_name_resolved", partyUi.sample);

		if ((partyUi.sample?.related_roles_label || "").includes("Supplier")) {
			passCheck("related_roles_discovered", partyUi.sample.related_roles_label);
		} else failCheck("related_roles_discovered", partyUi.sample);

		if (partyUi.analyzeCount > 0) passCheck("parties_has_analyze", partyUi.analyzeCount);
		else failCheck("parties_has_analyze", partyUi.analyzeCount);

		if (partyUi.drillIcons === 0) passCheck("no_expand_icons_parties", partyUi.drillIcons);
		else failCheck("no_expand_icons_parties", partyUi.drillIcons);

		await page.evaluate(() => {
			const ae = window.cur_ae;
			const row =
				(ae.rows || []).find((r) => r.party === "CUS-00003") ||
				(ae.rows || []).find((r) => (r.display_title || "").includes("میلان"));
			if (!row) throw new Error("Milan party row not found");
			ae.analyze_party_identity(row);
		});
		await page.waitForTimeout(1500);
		const panel = await page.evaluate(() => {
			const ae = window.cur_ae;
			const text = ae.$member_panel?.text?.() || "";
			return {
				visible: ae.$member_panel?.is?.(":visible"),
				text,
				hasCombined: text.includes("Combined") || text.includes("تحلیلی"),
				hasSupplier: text.includes("Supplier") || text.includes("تامین"),
			};
		});
		if (panel.visible) passCheck("identity_panel_visible", panel);
		else failCheck("identity_panel_visible", panel);
		if (panel.hasSupplier) passCheck("identity_panel_roles", panel);
		else failCheck("identity_panel_roles", panel);
		await shot(page, "03_identity_panel");

		await page.evaluate(() => window.cur_ae.switch_axis("voucher"));
		await waitSummaryIdle(page, { requireRows: false });
		await page.waitForTimeout(500);
		const voucherAnalyze = await page.evaluate(
			() => document.querySelectorAll(".ae-voucher-action--analyze").length
		);
		if (voucherAnalyze === 0) passCheck("vouchers_no_analyze", voucherAnalyze);
		else failCheck("vouchers_no_analyze", voucherAnalyze);

		if (!apiErrors.length) passCheck("no_api_errors");
		else failCheck("no_api_errors", apiErrors);
	} catch (err) {
		failCheck("fatal", err);
		try {
			await shot(page, "fatal");
		} catch {}
	} finally {
		await browser.close();
	}

	const report = { site: SITE, base: BASE, company: COMPANY, checks, ok: checks.every((c) => c.ok) };
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (!report.ok) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

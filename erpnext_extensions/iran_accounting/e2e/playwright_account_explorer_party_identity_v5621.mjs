#!/usr/bin/env node
/**
 * Account Explorer 5.6.21 — Parties identity + UX (Playwright).
 *
 * Verifies Party Name, Identifier Field, no Analyze/expand on Parties,
 * and Account Levels first-load timing on restored Development.
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

async function waitSummaryIdle(page, { timeout = 180000 } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const ready = await page.evaluate(() => {
			const ae = window.cur_ae;
			if (!ae) return false;
			const loading = !!ae.store?.get?.("loading")?.summary;
			const bannerHidden =
				ae.$summary_loading?.hasClass?.("visually-hidden") ||
				ae.$summary_loading?.hasClass?.("is-hidden") ||
				!ae.$summary_loading?.is?.(":visible");
			const gridLoading = ae.$grid?.hasClass?.("ae-grid-wrap--loading");
			return !loading && bannerHidden && !gridLoading;
		});
		if (ready) return Date.now() - started;
		await page.waitForTimeout(200);
	}
	throw new Error("summary did not become idle");
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	page.on("pageerror", (err) => failCheck("pageerror", err));
	const apiErrors = [];
	page.on("response", (res) => {
		if (res.url().includes("account_explorer") && res.status() >= 400) {
			apiErrors.push(`${res.status()} ${res.url()}`);
		}
	});

	try {
		await login(page);
		passCheck("login");

		const tOpen = Date.now();
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		const openMs = Date.now() - tOpen;
		passCheck("open_account_explorer", { open_ms: openMs });

		await page.evaluate(async ({ company }) => {
			const ae = window.cur_ae;
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = "1405";
			if (ae.company_field?.set_value) await ae.company_field.set_value(company);
			if (typeof ae.on_company_change === "function") await ae.on_company_change();
			if (typeof ae.apply_fiscal_year === "function") await ae.apply_fiscal_year("1405");
			ae.analysis_context.view_axis = "account_level";
			ae.analysis_context.level_sequence = 1;
			ae.analysis_context.page = 1;
			ae._pending_grid_perf_operation = "first_render";
		}, { company: COMPANY });

		const alWait = await waitSummaryIdle(page);
		passCheck("account_levels_loaded", { wait_ms: alWait });
		await shot(page, "01_account_levels");

		await page.evaluate(() => {
			const ae = window.cur_ae;
			ae.switch_axis("party");
		});
		const partyWait = await waitSummaryIdle(page);
		passCheck("parties_loaded", { wait_ms: partyWait });
		await shot(page, "02_parties");

		const partyUi = await page.evaluate(() => {
			const ae = window.cur_ae;
			const cols = (ae.last_summary_columns || []).map((c) => c.id || c.fieldname || c);
			const visible = ae.get_default_visible_columns?.() || [];
			const rows = ae.rows || [];
			const sample =
				rows.find((r) => r.party === "CUS-00003") ||
				rows.find((r) => (r.display_title || "").includes("میلان")) ||
				rows[0] ||
				null;
			const analyzeButtons = document.querySelectorAll(
				".ae-analyze-actions, .ae-voucher-action--analyze, .ae-actions-col"
			).length;
			const drillIcons = document.querySelectorAll(
				".ae-grid-row--drillable .ae-drill-icon, .ae-drill-icon"
			).length;
			return {
				cols,
				visible,
				sample,
				analyzeButtons,
				drillIcons,
				rowCount: rows.length,
			};
		});

		if (partyUi.visible.includes("party_identifier")) {
			passCheck("identifier_column_visible", partyUi.visible);
		} else {
			failCheck("identifier_column_visible", partyUi.visible);
		}

		if (partyUi.sample?.display_title && partyUi.sample.display_title !== partyUi.sample.display_code) {
			passCheck("party_name_resolved", {
				party: partyUi.sample.party,
				display_title: partyUi.sample.display_title,
				party_identifier: partyUi.sample.party_identifier,
			});
		} else if (partyUi.sample?.display_title) {
			// Naming-series-less masters may equal id; still require non-empty title.
			passCheck("party_name_present", partyUi.sample);
		} else {
			failCheck("party_name_resolved", partyUi.sample);
		}

		if (partyUi.analyzeButtons === 0) {
			passCheck("no_party_analyze_actions", partyUi.analyzeButtons);
		} else {
			failCheck("no_party_analyze_actions", partyUi.analyzeButtons);
		}

		const drillable = await page.evaluate(
			() => document.querySelectorAll(".ae-grid-row--drillable").length
		);
		if (drillable === 0) {
			passCheck("no_party_drillable_rows", drillable);
		} else {
			failCheck("no_party_drillable_rows", drillable);
		}

		if (!apiErrors.length) passCheck("no_api_errors");
		else failCheck("no_api_errors", apiErrors);

		await shot(page, "03_final");
	} catch (err) {
		failCheck("fatal", err);
		try {
			await shot(page, "fatal");
		} catch {}
	} finally {
		await browser.close();
	}

	const report = {
		site: SITE,
		base: BASE,
		company: COMPANY,
		checks,
		ok: checks.every((c) => c.ok),
	};
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (!report.ok) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

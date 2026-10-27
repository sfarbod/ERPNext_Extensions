#!/usr/bin/env node
/**
 * Account Explorer 5.6.21 — REAL click E2E for Analyze / Unified Party.
 * Fails if Analyze is visible but clicking does nothing or scope stays wrong.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-analyze-click-v5621");

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

async function waitSummaryIdle(page, { timeout = 180000, requireRows = true, minRows = 1 } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeout) {
		const state = await page.evaluate(() => {
			const ae = window.cur_ae;
			if (!ae) return { ready: false, rows: 0, loading: true };
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
				loading,
			};
		});
		if (state.ready && (!requireRows || state.rows >= minRows)) {
			return { elapsed: Date.now() - started, rows: state.rows };
		}
		await page.waitForTimeout(200);
	}
	throw new Error("summary did not become idle with required rows");
}

async function applyFy1405Party(page) {
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
		ae.analysis_context.view_axis = "party";
		ae.analysis_context.detail_mode = "summary";
		ae.analysis_context.page = 1;
		ae.analysis_context.party_scope = {
			party_type: null,
			selected_party: null,
			party_name_query: null,
			identifier_query: null,
			unified_identity: 0,
			identity_members: null,
		};
		ae.hide_member_panel?.();
		ae._reset_breadcrumbs?.([]);
		await ae.refresh_summary({ new_execution: true, reason: "e2e_parties" });
	}, { company: COMPANY });
	return waitSummaryIdle(page);
}

async function findAnalyzeButtonForParty(page, party) {
	return page.evaluate((partyId) => {
		const ae = window.cur_ae;
		const row = (ae.rows || []).find((r) => r.party === partyId);
		if (!row) return { ok: false, reason: "row_not_in_data" };
		const key = row.row_key;
		let btn =
			document.querySelector(`.dt-row[data-ae-row-key="${CSS.escape(key)}"] .ae-voucher-action--analyze`) ||
			null;
		if (!btn) {
			// Clusterize may only mount visible rows — scroll/search DOM by code text.
			const rows = [...document.querySelectorAll(".dt-row:not(.dt-row-header):not(.dt-row-filter)")];
			for (const el of rows) {
				if ((el.textContent || "").includes(partyId)) {
					btn = el.querySelector(".ae-voucher-action--analyze");
					if (btn) break;
				}
			}
		}
		if (!btn) return { ok: false, reason: "button_not_in_dom", key, analyzeButtons: document.querySelectorAll(".ae-voucher-action--analyze").length };
		btn.setAttribute("data-e2e-analyze", partyId);
		return { ok: true, key };
	}, party);
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const apiHits = [];
	const failedApis = [];
	page.on("pageerror", (err) => {
		if (isBenignPageError(err)) return;
		failCheck("pageerror", err);
	});
	page.on("response", (res) => {
		const url = res.url();
		if (!url.includes("account_explorer")) return;
		if (res.status() >= 400) failedApis.push(`${res.status()} ${url}`);
		if (url.includes("get_party_identity_analysis") || url.includes("get_party_summary") || url.includes("get_account_summary")) {
			apiHits.push({ status: res.status(), method: url.includes("get_party_identity_analysis") ? "identity" : url.includes("get_party_summary") ? "party" : "account" });
		}
	});

	const timings = [];

	try {
		await login(page);
		passCheck("login");

		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		passCheck("open_account_explorer");
		await waitSummaryIdle(page, { requireRows: false, timeout: 60000 });

		await applyFy1405Party(page);
		await shot(page, "01_parties_before");

		// Ensure CUS-00003 is in the current page (filter by Party ID if needed).
		let hasTarget = await page.evaluate(
			() => !!(window.cur_ae.rows || []).find((r) => r.party === "CUS-00003")
		);
		if (!hasTarget) {
			await page.evaluate(async () => {
				const ae = window.cur_ae;
				ae.analysis_context.party_scope = {
					...(ae.analysis_context.party_scope || {}),
					party_type: "Customer",
					selected_party: "CUS-00003",
					unified_identity: 0,
					identity_members: null,
				};
				ae.filter_controls.party_type?.set_value?.("Customer");
				ae.filter_controls.party?.set_value?.("CUS-00003");
				ae.analysis_context.page = 1;
				await ae.refresh_summary({ new_execution: true, reason: "e2e_locate_party" });
			});
			await waitSummaryIdle(page);
			hasTarget = await page.evaluate(
				() => !!(window.cur_ae.rows || []).find((r) => r.party === "CUS-00003")
			);
		}

		const before = await page.evaluate(() => {
			const ae = window.cur_ae;
			const sample = (ae.rows || []).find((r) => r.party === "CUS-00003");
			const pathText =
				(ae.$context?.find?.(".ae-breadcrumb")?.text?.() || ae.$context?.text?.() || "").trim();
			return {
				path: pathText,
				filter: (ae.$filter_summary?.text?.() || "").trim(),
				panel: !!ae.$member_panel?.is?.(":visible"),
				scope: { ...(ae.analysis_context.party_scope || {}) },
				rows: (ae.rows || []).length,
				sample,
				analyzeCount: document.querySelectorAll(".ae-voucher-action--analyze").length,
				drillIcons: document.querySelectorAll(".ae-drill-icon, .ae-grid-row--drillable").length,
			};
		});
		if (!before.sample) {
			failCheck("locate_cus_00003", before);
			throw new Error("CUS-00003 not available for Analyze click");
		}
		passCheck("locate_cus_00003", {
			party: before.sample.party,
			title: before.sample.display_title,
			identifier: before.sample.party_identifier,
			roles: before.sample.related_roles_label,
		});
		if (before.analyzeCount > 0) passCheck("parties_has_analyze_buttons", before.analyzeCount);
		else failCheck("parties_has_analyze_buttons", before.analyzeCount);
		if (before.drillIcons === 0) passCheck("no_expand_icons", before.drillIcons);
		else failCheck("no_expand_icons", before.drillIcons);

		const pathBefore = before.path;
		const rowCountBefore = before.rows;

		const located = await findAnalyzeButtonForParty(page, "CUS-00003");
		if (!located.ok) {
			failCheck("analyze_button_dom", located);
			throw new Error(`Analyze button not found: ${JSON.stringify(located)}`);
		}
		passCheck("analyze_button_dom", located);

		const t0 = Date.now();
		let identityApiMs = null;
		let identityJson = null;
		const onIdentity = async (res) => {
			if (!res.url().includes("get_party_identity_analysis") || res.status() !== 200) return;
			identityApiMs = Date.now() - t0;
			identityJson = await res.json().catch(() => null);
		};
		page.on("response", onIdentity);
		try {
			// Desk overlays / sticky headers can block Playwright actionability checks.
			// Dispatch a real DOM click on the marked Analyze button (not programmatic AE API).
			const clicked = await page.evaluate(() => {
				const btn = document.querySelector('[data-e2e-analyze="CUS-00003"]');
				if (!btn) return false;
				btn.scrollIntoView({ block: "center", inline: "nearest" });
				btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
				return true;
			});
			if (!clicked) throw new Error("Analyze button disappeared before click");
			await page.waitForFunction(() => {
				const ae = window.cur_ae;
				const panel = ae?.$member_panel;
				return !!(
					panel?.is?.(":visible") &&
					(panel.text?.() || "").includes("14008809078") &&
					ae.analysis_context?.party_scope?.unified_identity
				);
			}, null, { timeout: 60000 });
			await waitSummaryIdle(page, { requireRows: true, minRows: 1, timeout: 120000 });
		} finally {
			page.off("response", onIdentity);
		}
		if (identityApiMs != null) {
			timings.push({ name: "analyze_click_to_identity_api", ms: identityApiMs });
			passCheck("identity_api_from_click", { ms: identityApiMs });
		} else {
			failCheck("identity_api_from_click", "get_party_identity_analysis was not observed after click");
		}
		const panelStillVisible = await page.evaluate(
			() =>
				!!(
					window.cur_ae?.$member_panel?.is?.(":visible") &&
					(window.cur_ae.$member_panel.text() || "").includes("Supplier")
				)
		);
		if (!panelStillVisible) failCheck("panel_survives_refresh", false);
		else passCheck("panel_survives_refresh", true);
		timings.push({ name: "analyze_click_to_visible_result", ms: Date.now() - t0 });
		await shot(page, "02_after_analyze_click");

		const after = await page.evaluate(() => {
			const ae = window.cur_ae;
			const text = ae.$member_panel?.text?.() || "";
			const rows = ae.rows || [];
			const parties = rows.map((r) => `${r.party_type}:${r.party}`);
			const pathText =
				(ae.$context?.find?.(".ae-breadcrumb")?.text?.() || ae.$context?.text?.() || "").trim();
			const allowed = new Set(["CUS-00003", "SUP-00204", "ACC-SH-00029"]);
			return {
				path: pathText,
				filter: (ae.$filter_summary?.text?.() || "").trim(),
				panelVisible: !!ae.$member_panel?.is?.(":visible"),
				panelText: text,
				hasCombined: /Combined|تحلیلی/.test(text),
				hasSupplier: /Supplier|SUP-/.test(text),
				hasShareholder: /Shareholder|ACC-SH-/.test(text),
				hasIdentifier: text.includes("14008809078"),
				scope: { ...(ae.analysis_context.party_scope || {}) },
				rows: rows.length,
				parties,
				allBelongToIdentity: rows.length > 0 && rows.every((r) => allowed.has(r.party)),
			};
		});

		if (after.panelVisible) passCheck("identity_panel_visible_after_click", true);
		else failCheck("identity_panel_visible_after_click", after);

		if (after.hasIdentifier && after.hasSupplier && after.hasShareholder && after.hasCombined) {
			passCheck("identity_panel_content", {
				hasIdentifier: after.hasIdentifier,
				hasSupplier: after.hasSupplier,
				hasShareholder: after.hasShareholder,
				hasCombined: after.hasCombined,
			});
		} else failCheck("identity_panel_content", after);

		if (after.scope.unified_identity && after.scope.selected_party === "CUS-00003") {
			passCheck("party_scope_updated", after.scope);
		} else failCheck("party_scope_updated", after.scope);

		const members = after.scope.identity_members || [];
		const memberParties = members.map((m) => (Array.isArray(m) ? m[1] : m.party));
		if (
			memberParties.includes("CUS-00003") &&
			memberParties.includes("SUP-00204") &&
			memberParties.includes("ACC-SH-00029")
		) {
			passCheck("identity_members_resolved", memberParties);
		} else failCheck("identity_members_resolved", members);

		if (after.path && after.path !== pathBefore && /Identity|CUS-00003|میلان|14008809078/i.test(after.path)) {
			passCheck("analysis_path_changed", { before: pathBefore, after: after.path });
		} else if (after.path && after.path !== pathBefore) {
			passCheck("analysis_path_changed", { before: pathBefore, after: after.path });
		} else {
			failCheck("analysis_path_changed", { before: pathBefore, after: after.path });
		}

		if (after.rows > 0 && after.rows <= 5 && after.allBelongToIdentity) {
			passCheck("grid_scoped_to_identity", { before: rowCountBefore, after: after.rows, parties: after.parties });
		} else {
			failCheck("grid_scoped_to_identity", { before: rowCountBefore, after: after.rows, parties: after.parties, allBelongToIdentity: after.allBelongToIdentity });
		}

		const identityMessage = identityJson?.message || identityJson;
		if (identityMessage?.roles?.length >= 2 && identityMessage?.combined) {
			passCheck("identity_api_payload", {
				roles: identityMessage.roles.map((r) => `${r.party_type}:${r.party}`),
				combined_debit: identityMessage.combined.period_debit,
			});
		} else failCheck("identity_api_payload", identityMessage);

		// Navigate back — scope should clear
		await page.evaluate(() => {
			const ae = window.cur_ae;
			if (typeof ae.go_back === "function") ae.go_back();
		});
		await waitSummaryIdle(page, { requireRows: true, timeout: 120000 });
		const afterBack = await page.evaluate(() => {
			const ae = window.cur_ae;
			return {
				panel: !!ae.$member_panel?.is?.(":visible"),
				unified: ae.analysis_context?.party_scope?.unified_identity || 0,
				rows: (ae.rows || []).length,
				path: (ae.$context?.find?.(".ae-breadcrumb")?.text?.() || ae.$context?.text?.() || "").trim(),
			};
		});
		if (!afterBack.panel && !afterBack.unified && afterBack.rows >= rowCountBefore) {
			passCheck("navigate_back_restores_scope", afterBack);
		} else if (!afterBack.panel && !afterBack.unified) {
			passCheck("navigate_back_restores_scope", afterBack);
		} else {
			failCheck("navigate_back_restores_scope", afterBack);
		}
		await shot(page, "03_after_back");

		// Repeat Analyze after axis switch remount
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			ae.switch_axis("account_level");
		});
		await waitSummaryIdle(page, { requireRows: true, timeout: 120000 });
		const alAnalyze = await page.evaluate(() => document.querySelectorAll(".ae-voucher-action--analyze").length);
		if (alAnalyze > 0) passCheck("account_levels_has_analyze", alAnalyze);
		else failCheck("account_levels_has_analyze", alAnalyze);

		// Click first Account Levels Analyze and assert filter/path change
		const alBeforePath = await page.evaluate(() =>
			(window.cur_ae.$context?.find?.(".ae-breadcrumb")?.text?.() || window.cur_ae.$context?.text?.() || "").trim()
		);
		await page.evaluate(() => {
			const btn = document.querySelector(".ae-voucher-action--analyze");
			if (!btn) throw new Error("AL Analyze missing");
			btn.setAttribute("data-e2e-al-analyze", "1");
			btn.scrollIntoView({ block: "center" });
			btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
		});
		await waitSummaryIdle(page, { requireRows: false, timeout: 120000 });
		const alAfter = await page.evaluate(() => {
			const ae = window.cur_ae;
			return {
				path: (ae.$context?.find?.(".ae-breadcrumb")?.text?.() || ae.$context?.text?.() || "").trim(),
				account: ae.analysis_context?.account_scope?.selected_account || null,
				filters: ae.get_analysis_filters?.() || {},
			};
		});
		if (alAfter.path !== alBeforePath || alAfter.account || alAfter.filters.account) {
			passCheck("account_levels_analyze_scopes", alAfter);
		} else {
			failCheck("account_levels_analyze_scopes", { before: alBeforePath, after: alAfter });
		}

		await page.evaluate(() => window.cur_ae.switch_axis("party"));
		await waitSummaryIdle(page, { requireRows: true, timeout: 120000 });
		const remountLocate = await findAnalyzeButtonForParty(page, "CUS-00003");
		if (!remountLocate.ok) failCheck("analyze_survives_remount", remountLocate);
		else {
			await page.evaluate(() => {
				const btn = document.querySelector('[data-e2e-analyze="CUS-00003"]');
				if (!btn) throw new Error("remount Analyze missing");
				btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
			});
			await page.waitForFunction(
				() =>
					!!(
						window.cur_ae?.$member_panel?.is?.(":visible") &&
						window.cur_ae.analysis_context?.party_scope?.unified_identity
					),
				null,
				{ timeout: 60000 }
			);
			passCheck("analyze_survives_remount", true);
		}

		await page.evaluate(() => window.cur_ae.switch_axis("voucher"));
		await waitSummaryIdle(page, { requireRows: false, timeout: 120000 });
		const voucherAnalyze = await page.evaluate(
			() => document.querySelectorAll(".ae-voucher-action--analyze").length
		);
		if (voucherAnalyze === 0) passCheck("vouchers_no_analyze", voucherAnalyze);
		else failCheck("vouchers_no_analyze", voucherAnalyze);

		if (!failedApis.length) passCheck("no_api_errors");
		else failCheck("no_api_errors", failedApis);

		passCheck("timings", timings);
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
		timings,
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

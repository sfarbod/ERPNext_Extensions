#!/usr/bin/env node
/**
 * Account Explorer 5.6.21 — Party-to-Item commercial analysis + cross-axis.
 * Real Playwright clicks; identity persists across axes.
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
const OUT = path.resolve(__dirname, "screenshots/account-explorer-commercial-item-v5621");

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

async function setRole(page, roleLabel, enabled) {
	await page.evaluate(({ roleLabel, enabled }) => {
		const boxes = [...document.querySelectorAll(".ae-identity-role-option")];
		const el = boxes.find((node) => (node.textContent || "").includes(roleLabel));
		if (!el) throw new Error(`Role ${roleLabel} missing`);
		const input = el.querySelector("input[type=checkbox]");
		input.setAttribute("data-e2e-role-toggle", roleLabel);
		input.dataset.desired = enabled ? "1" : "0";
	}, { roleLabel, enabled });
	const checked = await page.evaluate((roleLabel) => {
		const boxes = [...document.querySelectorAll(".ae-identity-role-option")];
		const el = boxes.find((node) => (node.textContent || "").includes(roleLabel));
		return !!el?.querySelector("input[type=checkbox]")?.checked;
	}, roleLabel);
	if (checked !== enabled) {
		await realClick(page, `[data-e2e-role-toggle="${roleLabel}"]`);
		await waitSummaryIdle(page, { requireRows: false });
	}
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
		passCheck("select_fy1405");

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
		await realClick(page, analyzeLocator);
		await page.waitForFunction(() => {
			const ae = window.cur_ae;
			return !!(
				ae?.get_analysis_filters?.()?.party_identity &&
				ae.$member_panel?.is?.(":visible")
			);
		}, null, { timeout: 60000 });
		await waitSummaryIdle(page);
		passCheck("analyze_milan_pars");

		const roles = await page.evaluate(() => {
			const id = window.cur_ae.get_analysis_filters().party_identity?.value || {};
			return (id.selected_members || []).map((m) => m.party_type);
		});
		if (roles.includes("Customer") && roles.includes("Supplier") && roles.includes("Shareholder")) {
			passCheck("related_roles_selected", roles);
		} else failCheck("related_roles_selected", roles);

		// Keep Customer + Supplier for commercial item analysis
		await setRole(page, "Shareholder", false);
		passCheck("customer_supplier_selected");

		await page.evaluate(() => window.cur_ae.switch_axis("item_group"));
		await waitSummaryIdle(page, { requireRows: false });
		const ig = await page.evaluate(() => {
			const ae = window.cur_ae;
			return {
				source: ae.analysis_source,
				rows: (ae.rows || []).length,
				hasIdentity: !!ae.get_analysis_filters().party_identity,
				filterText: (ae.$filter_summary?.text?.() || "").slice(0, 300),
				sample: (ae.rows || []).slice(0, 3).map((r) => ({
					group: r.item_group || r.display_code,
					sales: r.sales_base_net_amount,
				})),
				warnings: ae.warnings || [],
			};
		});
		if (ig.hasIdentity && ig.source === "commercial_transactions") {
			passCheck("item_groups_commercial_scoped", ig);
		} else failCheck("item_groups_commercial_scoped", ig);
		if (ig.rows > 0) passCheck("item_groups_have_rows", ig.rows);
		else failCheck("item_groups_have_rows", ig);

		await page.evaluate(() => window.cur_ae.switch_axis("item"));
		await waitSummaryIdle(page, { requireRows: false });
		const items = await page.evaluate(() => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			return {
				source: ae.analysis_source,
				rows: rows.length,
				allSalesOrPurchase: rows.every((r) =>
					["Sales", "Purchase"].includes(r.transaction_direction)
				),
				parties: [...new Set(rows.map((r) => `${r.party_type}:${r.party}`))],
				filterHasSource: /Commercial|تجاری|Analysis Source|منبع/i.test(
					ae.$filter_summary?.text?.() || ""
				),
				sample: rows.slice(0, 5).map((r) => ({
					item: r.item_code,
					dir: r.transaction_direction,
					party: r.party,
					base: r.base_net_amount,
				})),
			};
		});
		if (items.source === "commercial_transactions" && items.rows > 0 && items.allSalesOrPurchase) {
			passCheck("items_commercial_scoped", items);
		} else failCheck("items_commercial_scoped", items);

		// Disable Supplier → only Customer sales
		await setRole(page, "Supplier", false);
		await page.evaluate(() => window.cur_ae.switch_axis("item"));
		await waitSummaryIdle(page, { requireRows: false });
		const custOnly = await page.evaluate(() => {
			const rows = window.cur_ae.rows || [];
			return {
				rows: rows.length,
				allCustomer: rows.every((r) => r.party_type === "Customer" && r.transaction_direction === "Sales"),
				parties: [...new Set(rows.map((r) => r.party))],
			};
		});
		if (custOnly.rows > 0 && custOnly.allCustomer) passCheck("customer_only_sales", custOnly);
		else failCheck("customer_only_sales", custOnly);

		// Enable Supplier, disable Customer → purchase only (may be empty for SUP-00204)
		await setRole(page, "Supplier", true);
		await setRole(page, "Customer", false);
		await page.evaluate(() => window.cur_ae.switch_axis("item"));
		await waitSummaryIdle(page, { requireRows: false });
		const suppOnly = await page.evaluate(() => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			return {
				rows: rows.length,
				allPurchase: rows.every((r) => r.transaction_direction === "Purchase"),
				noLeakage: rows.every((r) => r.party === "SUP-00204"),
				source: ae.analysis_source,
			};
		});
		if (suppOnly.source === "commercial_transactions" && (suppOnly.rows === 0 || (suppOnly.allPurchase && suppOnly.noLeakage))) {
			passCheck("supplier_only_purchases", suppOnly);
		} else failCheck("supplier_only_purchases", suppOnly);

		// Shareholder only → empty state.
		// Enable Shareholder before clearing the last commercial role so the identity
		// filter is not wiped when selected_members becomes empty.
		await setRole(page, "Shareholder", true);
		await setRole(page, "Customer", false);
		await setRole(page, "Supplier", false);
		await page.evaluate(() => window.cur_ae.switch_axis("item"));
		await waitSummaryIdle(page, { requireRows: false });
		const shOnly = await page.evaluate(() => {
			const ae = window.cur_ae;
			const warn = (ae.warnings || []).join(" ");
			const combo = ae.unsupported_filter_combination || {};
			return {
				rows: (ae.rows || []).length,
				reason: combo.reason,
				hasFa: /سهامدار/.test(warn) || /سهامدار/.test(combo.message_fa || ""),
				hasEn: /Shareholder/i.test(warn) || /Shareholder/i.test(combo.message || ""),
				hasIdentity: !!ae.get_analysis_filters().party_identity,
			};
		});
		if (shOnly.rows === 0 && shOnly.reason === "shareholder_has_no_item_transactions" && (shOnly.hasFa || shOnly.hasEn)) {
			passCheck("shareholder_empty_state", shOnly);
		} else failCheck("shareholder_empty_state", shOnly);

		// Restore Customer+Supplier and verify Account Levels still scoped
		await setRole(page, "Customer", true);
		await setRole(page, "Supplier", true);
		await setRole(page, "Shareholder", false);
		await page.evaluate(() => window.cur_ae.switch_axis("account_level"));
		await waitSummaryIdle(page, { requireRows: false });
		const al = await page.evaluate(() => {
			const ae = window.cur_ae;
			return {
				hasIdentity: !!ae.get_analysis_filters().party_identity,
				axis: ae.analysis_context.view_axis,
				debit: ae.totals?.period_debit || 0,
			};
		});
		if (al.hasIdentity && al.axis === "account_level") passCheck("account_levels_preserve_identity", al);
		else failCheck("account_levels_preserve_identity", al);

		await page.evaluate(() => window.cur_ae.switch_axis("dimension"));
		await waitSummaryIdle(page, { requireRows: false });
		const dim = await page.evaluate(() => ({
			hasIdentity: !!window.cur_ae.get_analysis_filters().party_identity,
			rows: (window.cur_ae.rows || []).length,
		}));
		if (dim.hasIdentity) passCheck("dimensions_identity_scope", dim);
		else failCheck("dimensions_identity_scope", dim);

		await page.evaluate(() => window.cur_ae.switch_axis("currency"));
		await waitSummaryIdle(page, { requireRows: false });
		const cur = await page.evaluate(() => ({
			hasIdentity: !!window.cur_ae.get_analysis_filters().party_identity,
			rows: (window.cur_ae.rows || []).length,
		}));
		if (cur.hasIdentity) passCheck("currencies_identity_scope", cur);
		else failCheck("currencies_identity_scope", cur);

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
				allMatch: rows.length ? rows.every((r) => allowed.has(`${r.party_type}:${r.party}`)) : true,
			};
		});
		if (vouchers.hasIdentity && vouchers.allMatch) passCheck("vouchers_identity_scope", vouchers);
		else failCheck("vouchers_identity_scope", vouchers);

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

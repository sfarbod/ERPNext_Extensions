#!/usr/bin/env node
/**
 * Account Explorer 5.6.35 — real Playwright click for Account Group Analyze.
 * Proves NestedSet descendant scoping + identity preservation + Back.
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
const IDENTIFIER = "14008809078";
const SUPPLIER = "SUP-00204";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-analyze-v5635");

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({ name, ok: false, err: err?.stack || err?.message || String(err) });

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
		return !!(ae && ae.metadata?.axes?.length && document.querySelector(".ae-shell, .ae-toolbar"));
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
}

async function waitSummaryIdle(page, { requireRows = false, timeout = 180000 } = {}) {
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
		await page.waitForTimeout(250);
	}
	throw new Error("summary idle timeout");
}

async function seedIdentityAndGroupLevel(page) {
	await page.evaluate(
		async ({ company, supplier, identifier }) => {
			const ae = window.cur_ae;
			ae.document_scope.company = company;
			ae.document_scope.fiscal_year = "1405";
			ae.document_scope.from_date = "2026-03-21";
			ae.document_scope.to_date = "2027-03-20";
			if (ae.company_field?.set_value) await ae.company_field.set_value(company);
			if (ae.fy_field?.set_value) await ae.fy_field.set_value("1405");
			if (ae.from_date_field?.set_value) await ae.from_date_field.set_value("2026-03-21");
			if (ae.to_date_field?.set_value) await ae.to_date_field.set_value("2027-03-20");
			await ae.filter_controls.party_type?.set_value?.("Supplier");
			await ae.filter_controls.party?.set_value?.([supplier]);
			await ae.filter_controls.identifier_query?.set_value?.(identifier);
			if (ae.filter_controls.unified_identity?.set_value) {
				await ae.filter_controls.unified_identity.set_value(1);
			}
			ae.sync_document_scope_from_controls?.();
			ae.analysis_context.party_scope = {
				party_type: "Supplier",
				selected_party: supplier,
				identifier_query: identifier,
				unified_identity: 1,
			};
			ae.clear_all_analysis_filters?.({ refresh: false });
			ae.switch_axis("account_level", 1);
		},
		{ company: COMPANY, supplier: SUPPLIER, identifier: IDENTIFIER }
	);
	await waitSummaryIdle(page, { requireRows: true });
}

function hasSiblingLeak(codes, forbidden) {
	return codes.some((c) => String(c) === forbidden);
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage();
	const consoleErrors = [];
	const apiTrace = [];
	page.on("pageerror", (err) => consoleErrors.push(String(err)));
	page.on("console", (msg) => {
		if (msg.type() === "error") consoleErrors.push(msg.text());
	});
	page.on("request", (req) => {
		if (req.url().includes("get_account_summary")) {
			apiTrace.push({ type: "request", url: req.url(), post: req.postData()?.slice?.(0, 4000) || null });
		}
	});
	page.on("response", async (res) => {
		if (res.url().includes("get_account_summary")) {
			let body = null;
			try {
				body = await res.json();
			} catch {
				body = null;
			}
			const msg = body?.message || {};
			apiTrace.push({
				type: "response",
				status: res.status(),
				codes: (msg.rows || []).map((r) => r.display_code).slice(0, 30),
				tree_root: msg.diagnostics?.tree_root_account || msg.account_scope?.tree_root_account || null,
			});
		}
	});

	try {
		await login(page);
		await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded" });
		await resolveAe(page);
		await seedIdentityAndGroupLevel(page);

		const baseline = await page.evaluate(() => {
			const ae = window.cur_ae;
			return {
				codes: (ae.rows || []).map((r) => r.display_code),
				level: ae.analysis_context.level_sequence,
				path: (ae.$context?.text?.() || ae.$nav?.text?.() || "").slice(0, 300),
				party: ae.analysis_context.party_scope,
			};
		});
		await page.screenshot({ path: path.join(OUT, "01-baseline-group.png"), fullPage: true });
		if (!baseline.codes.includes("11") || !baseline.codes.includes("21")) {
			failCheck("baseline_has_11_and_21", new Error(JSON.stringify(baseline)));
		} else {
			passCheck("baseline_has_11_and_21", baseline);
		}

		// Real locator click on Analyze for Group 21
		try {
			const rowLocator = page.locator(".ae-grid-wrap tr, .dt-row, .ae-summary-row").filter({
				hasText: /21/,
			});
			// Prefer button in the row that shows code 21 as its own cell.
			const analyzeBtn = page
				.locator('button.ae-voucher-action--analyze[data-action="analyze"]')
				.filter({
					has: page.locator("xpath=ancestor::*[contains(., '21') and not(contains(., '211'))]"),
				})
				.first();

			// Fallback: find via evaluate then click by selector tied to row index.
			const target = await page.evaluate(() => {
				const ae = window.cur_ae;
				const idx = (ae.rows || []).findIndex((r) => String(r.display_code) === "21");
				if (idx < 0) return { error: "row_21_missing", codes: (ae.rows || []).map((r) => r.display_code) };
				const row = ae.rows[idx];
				const buttons = Array.from(
					document.querySelectorAll('button.ae-voucher-action--analyze[data-action="analyze"]')
				);
				const btn = buttons[idx] || buttons.find((b) => b.closest("tr, .dt-row")?.innerText?.includes("بدهیهای جاری"));
				if (!btn) return { error: "analyze_btn_missing", idx, selected: row.selected_account };
				btn.setAttribute("data-ae-e2e-analyze", "group-21");
				return {
					idx,
					selected: row.selected_account,
					display_code: row.display_code,
					btnCount: buttons.length,
				};
			});
			if (target.error) throw new Error(JSON.stringify(target));

			const clickTarget = page.locator('button[data-ae-e2e-analyze="group-21"]');
			await clickTarget.scrollIntoViewIfNeeded();
			const reqPromise = page.waitForRequest(
				(req) => req.url().includes("get_account_summary"),
				{ timeout: 120000 }
			);
			const resPromise = page.waitForResponse(
				(res) => res.url().includes("get_account_summary") && res.status() === 200,
				{ timeout: 120000 }
			);
			await clickTarget.click({ force: false, timeout: 30000 });
			const req = await reqPromise;
			const res = await resPromise;
			await waitSummaryIdle(page);

			let payloadPreview = null;
			try {
				const post = req.postData() || "";
				const parsed = JSON.parse(
					decodeURIComponent(post.replace(/^.*?payload=/, "").split("&")[0] || "{}")
				);
				// frappe sends args as form; try evaluate from ae instead
				payloadPreview = null;
			} catch {
				payloadPreview = null;
			}

			const after = await page.evaluate(() => {
				const ae = window.cur_ae;
				const payload = ae.build_payload();
				const pathText =
					ae.$context?.text?.() ||
					document.querySelector(".ae-context-bar, .ae-analysis-path, .ae-breadcrumbs")?.innerText ||
					"";
				return {
					codes: (ae.rows || []).map((r) => r.display_code),
					names: (ae.rows || []).map((r) => r.selected_account),
					level: ae.analysis_context.level_sequence,
					tree_root: ae.analysis_context.account_scope?.tree_root_account || null,
					selected: ae.analysis_context.account_scope?.selected_account || null,
					account_filter: ae.get_analysis_filters()?.account?.value || null,
					pathText: String(pathText).slice(0, 500),
					breadcrumbs: (ae.breadcrumbs || []).map((b) => ({
						label: b.label,
						selected_account: b.selected_account,
					})),
					party: {
						party_type: ae.analysis_context.party_scope?.party_type,
						selected_party: ae.analysis_context.party_scope?.selected_party,
						identifier: ae.analysis_context.party_scope?.identifier_query,
						unified: ae.analysis_context.party_scope?.unified_identity,
					},
					payload_tree_root: payload?.analysis_context?.account_scope?.tree_root_account || null,
					payload_identifier: payload?.analysis_context?.party_scope?.identifier_query || null,
				};
			});
			await page.screenshot({ path: path.join(OUT, "02-after-analyze-21.png"), fullPage: true });

			const evidence = {
				click: target,
				after,
				apiLast: apiTrace.slice(-3),
				responseStatus: res.status(),
			};
			if (hasSiblingLeak(after.codes, "11")) {
				throw new Error(`sibling Group 11 present after Analyze 21: ${JSON.stringify(evidence)}`);
			}
			if (hasSiblingLeak(after.codes, "21")) {
				throw new Error(`parent Group 21 still a detail row: ${JSON.stringify(evidence)}`);
			}
			if (!after.tree_root && !after.payload_tree_root) {
				throw new Error(`tree_root missing: ${JSON.stringify(evidence)}`);
			}
			if (String(after.party.identifier) !== IDENTIFIER) {
				throw new Error(`identifier lost: ${JSON.stringify(after.party)}`);
			}
			const pathOk =
				(after.breadcrumbs || []).some((b) => String(b.selected_account || "").includes("21")) ||
				String(after.pathText).includes("21") ||
				String(after.pathText).includes("بدهی");
			if (!pathOk) {
				throw new Error(`Analysis Path missing Group 21: ${JSON.stringify(after)}`);
			}
			passCheck("real_click_analyze_group_21", evidence);
		} catch (err) {
			failCheck("real_click_analyze_group_21", err);
			await page.screenshot({ path: path.join(OUT, "02-analyze-21-FAILED.png"), fullPage: true });
		}

		// Back restores Group grid
		try {
			await page.evaluate(() => {
				window.cur_ae.go_back();
			});
			await waitSummaryIdle(page, { requireRows: true });
			const restored = await page.evaluate(() => {
				const ae = window.cur_ae;
				return {
					codes: (ae.rows || []).map((r) => r.display_code),
					level: ae.analysis_context.level_sequence,
					tree_root: ae.analysis_context.account_scope?.tree_root_account || null,
					account_filter: ae.get_analysis_filters?.()?.account?.value || null,
					payload_tree: ae.build_payload()?.analysis_context?.account_scope?.tree_root_account || null,
				};
			});
			await page.screenshot({ path: path.join(OUT, "03-after-back.png"), fullPage: true });
			if (!(restored.codes.includes("11") && restored.codes.includes("21"))) {
				throw new Error(`Back did not restore Group grid: ${JSON.stringify(restored)}`);
			}
			if (restored.tree_root || restored.account_filter || restored.payload_tree) {
				throw new Error(`hierarchy scope remained after Back: ${JSON.stringify(restored)}`);
			}
			passCheck("back_restores_group_grid", restored);
		} catch (err) {
			failCheck("back_restores_group_grid", err);
		}

		// Repeat Analyze Group 11 via real click
		try {
			await seedIdentityAndGroupLevel(page);
			const target = await page.evaluate(() => {
				const ae = window.cur_ae;
				const idx = (ae.rows || []).findIndex((r) => String(r.display_code) === "11");
				if (idx < 0) return { error: "row_11_missing" };
				const buttons = Array.from(
					document.querySelectorAll('button.ae-voucher-action--analyze[data-action="analyze"]')
				);
				const btn = buttons[idx];
				if (!btn) return { error: "analyze_btn_missing", idx };
				btn.setAttribute("data-ae-e2e-analyze", "group-11");
				return { idx, selected: ae.rows[idx].selected_account };
			});
			if (target.error) throw new Error(JSON.stringify(target));
			await page.locator('button[data-ae-e2e-analyze="group-11"]').click({ timeout: 30000 });
			await waitSummaryIdle(page);
			const after = await page.evaluate(() => ({
				codes: (window.cur_ae.rows || []).map((r) => r.display_code),
				tree_root: window.cur_ae.analysis_context.account_scope?.tree_root_account,
			}));
			await page.screenshot({ path: path.join(OUT, "04-after-analyze-11.png"), fullPage: true });
			if (hasSiblingLeak(after.codes, "21") || hasSiblingLeak(after.codes, "11")) {
				throw new Error(JSON.stringify(after));
			}
			passCheck("real_click_analyze_group_11", after);
		} catch (err) {
			failCheck("real_click_analyze_group_11", err);
		}

		// Axis switch preserves hierarchy + identity
		try {
			const switched = await page.evaluate(async () => {
				const ae = window.cur_ae;
				const before = {
					tree_root: ae.analysis_context.account_scope?.tree_root_account,
					identifier: ae.analysis_context.party_scope?.identifier_query,
					account_filter: ae.get_analysis_filters()?.account?.value || null,
				};
				ae.switch_axis("party");
				let guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 300) {
					await new Promise((r) => setTimeout(r, 100));
				}
				ae.switch_axis("voucher");
				guard = 0;
				while (ae.store?.get?.("loading")?.summary && guard++ < 300) {
					await new Promise((r) => setTimeout(r, 100));
				}
				const payload = ae.build_payload();
				return {
					before,
					after: {
						tree_root: ae.analysis_context.account_scope?.tree_root_account,
						identifier: ae.analysis_context.party_scope?.identifier_query,
						account_filter: ae.get_analysis_filters()?.account?.value || null,
						payload_tree: payload.analysis_context?.account_scope?.tree_root_account,
						payload_id: payload.analysis_context?.party_scope?.identifier_query,
					},
				};
			});
			if (String(switched.after.identifier || switched.after.payload_id) !== IDENTIFIER) {
				throw new Error(`identity lost on axis switch: ${JSON.stringify(switched)}`);
			}
			if (
				switched.before.account_filter &&
				String(switched.after.account_filter || switched.after.payload_tree) !==
					String(switched.before.account_filter || switched.before.tree_root)
			) {
				throw new Error(`hierarchy lost on axis switch: ${JSON.stringify(switched)}`);
			}
			passCheck("axis_switch_preserves_filters", switched);
		} catch (err) {
			failCheck("axis_switch_preserves_filters", err);
		}
	} finally {
		await browser.close();
	}

	const failed = checks.filter((c) => !c.ok);
	const report = {
		site: SITE,
		checks,
		consoleErrors: consoleErrors.slice(0, 30),
		apiTrace: apiTrace.slice(-8),
		failed: failed.length,
	};
	fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	if (failed.length) process.exit(1);
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});

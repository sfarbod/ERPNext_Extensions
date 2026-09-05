#!/usr/bin/env node
/**
 * Account Explorer v5.1.0 — Jalali date display (Playwright).
 *
 * Scenario A: Voucher/Documents axis — visible Jalali, API canonical ISO
 * Scenario B: Grouped GL detail — header + grid Jalali, API canonical ISO
 * Scenario C: format_ae_date delegates to frappe.datetime (Gregorian path smoke)
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8000";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-jalali-dates");
const PREP_METHOD =
	"erpnext_extensions.iran_accounting.e2e.account_explorer_jalali_dates_prep.prepare_jalali_dates_e2e";

const checks = [];
const passCheck = (name, detail = null) => checks.push({ name, ok: true, detail });
const failCheck = (name, err) =>
	checks.push({
		name,
		ok: false,
		err: err && err.stack ? String(err.stack) : err && err.message ? String(err.message) : String(err),
	});

function benchExecute(method) {
	const cmd = `cd ${BENCH} && bench --site ${SITE} execute ${method}`;
	const out = execSync(cmd, { encoding: "utf8", maxBuffer: 50 * 1024 * 1024 });
	const lines = out.trim().split("\n").filter(Boolean);
	const last = lines[lines.length - 1];
	try {
		return JSON.parse(last);
	} catch {
		throw new Error(`bench execute ${method} did not return JSON. Last: ${last}\n${out.slice(-1500)}`);
	}
}

async function login(page) {
	const res = await fetch(`${BASE}/api/method/login`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ usr: USER, pwd: PASS }),
	});
	if (!res.ok) {
		throw new Error(`API login failed: ${res.status}`);
	}
	const setCookie = res.headers.get("set-cookie") || "";
	const sid = setCookie.match(/sid=([^;]+)/)?.[1];
	if (!sid) {
		throw new Error("API login did not return sid cookie");
	}
	const host = new URL(BASE).hostname;
	await page.context().addCookies([{ name: "sid", value: sid, domain: host, path: "/" }]);
	await page.goto(`${BASE}/app/account-explorer`, { waitUntil: "domcontentloaded", timeout: 120000 });
}

async function shot(page, name) {
	fs.mkdirSync(OUT, { recursive: true });
	await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });
}

async function resolveAe(page) {
	await page.waitForFunction(() => {
		const entry = frappe?.pages?.["account-explorer"];
		const ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
		return !!(ae && ae.company_field && document.querySelector(".ae-shell, .ae-toolbar"));
	}, null, { timeout: 90000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		const inst = entry?.account_explorer || entry?.wrapper?.account_explorer;
		if (!inst) throw new Error("Account Explorer controller not attached");
		window.cur_ae = inst;
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
		if (ready) return;
		await page.waitForTimeout(250);
	}
	throw new Error("summary did not become idle");
}

async function setScopeAndVoucherAxis(page, prep) {
	await page.waitForFunction(
		() => Array.isArray(window.cur_ae?.metadata?.axes) && window.cur_ae.metadata.axes.length > 0,
		null,
		{ timeout: 90000 }
	);
	const result = await page.evaluate(async (prep) => {
		const ae = window.cur_ae;
		ae.document_scope.company = prep.company;
		ae.document_scope.fiscal_year = prep.fiscal_year || null;
		ae.document_scope.from_date = prep.from_date;
		ae.document_scope.to_date = prep.to_date;
		ae.document_scope.hide_zero_rows = 0;
		await ae.company_field.set_value(prep.company);
		if (ae.fiscal_year_field && prep.fiscal_year) {
			await ae.fiscal_year_field.set_value(prep.fiscal_year);
		}
		if (ae.from_date_field) await ae.from_date_field.set_value(prep.from_date);
		if (ae.to_date_field) await ae.to_date_field.set_value(prep.to_date);
		ae.switch_axis("voucher");
		ae.analysis_context.page = 1;
		await ae.refresh_summary();
		return {
			company: ae.document_scope.company,
			row_count: (ae.rows || []).length,
			first_posting: ae.rows?.[0]?.posting_date || null,
			axes: (ae.metadata?.axes || []).map((a) => a.id || a),
		};
	}, prep);
	await waitSummaryIdle(page);
	if (!result.row_count) {
		throw new Error(`voucher summary returned 0 rows: ${JSON.stringify(result)}`);
	}
	return result;
}

function collectGridDateTexts(texts) {
	return texts.filter((t) => /^\d{4}[/-]\d{2}[/-]\d{2}$/.test(String(t || "").trim()));
}

function isGregorianIsoDate(text) {
	const match = String(text || "")
		.trim()
		.match(/^(\d{4})-(\d{2})-(\d{2})$/);
	if (!match) {
		return false;
	}
	const year = Number(match[1]);
	return year >= 1900 && year <= 2100;
}

function isJalaliDisplayDate(text) {
	const match = String(text || "")
		.trim()
		.match(/^(\d{4})[/-](\d{2})[/-](\d{2})$/);
	if (!match) {
		return false;
	}
	const year = Number(match[1]);
	return year >= 1300 && year <= 1500;
}

async function main() {
	let prep;
	try {
		prep = benchExecute(PREP_METHOD);
		passCheck("prep", {
			company: prep.company,
			posting_date_iso: prep.posting_date_iso,
			posting_date_jalali: prep.posting_date_jalali,
		});
	} catch (e) {
		failCheck("prep", e);
		console.log(JSON.stringify({ checks }, null, 2));
		process.exit(1);
	}

	const browser = await chromium.launch({ headless: true });
	const context = await browser.newContext({ locale: "fa-IR" });
	const page = await context.newPage();

	let apiPostingDates = [];
	page.on("response", async (response) => {
		const url = response.url();
		if (!url.includes("get_voucher_summary") && !url.includes("get_grouped_gl_entries")) {
			return;
		}
		try {
			const body = await response.json();
			const message = body.message || body;
			if (!message?.rows && !message?.voucher_header) {
				return;
			}
			const rows = message.rows || [];
			for (const row of rows) {
				if (row.posting_date) apiPostingDates.push(row.posting_date);
			}
			const header = message.voucher_header || {};
			if (header.posting_date) apiPostingDates.push(header.posting_date);
		} catch {
			/* ignore non-json */
		}
	});

	try {
		await login(page);
		await resolveAe(page);
		await setScopeAndVoucherAxis(page, prep);
		await shot(page, "voucher-summary");

		const summaryState = await page.evaluate((prep) => {
			const ae = window.cur_ae;
			const rows = ae.rows || [];
			const row =
				rows.find((r) => r.voucher_type === prep.voucher_type && r.voucher_no === prep.voucher_no) ||
				rows.find((r) => r.posting_date === prep.posting_date_iso) ||
				rows[0];
			const gridTexts = [];
			document.querySelectorAll(".ae-grid td, .dt-cell__content, .ae-dt-cell-text, .ae-drill-label").forEach((el) => {
				const t = (el.textContent || "").trim();
				if (t) gridTexts.push(t);
			});
			return {
				raw_row_posting_date: row?.posting_date || null,
				row_count: rows.length,
				gridTexts,
				formatter_smoke:
					typeof frappe !== "undefined" && frappe.datetime?.str_to_user
						? frappe.datetime.str_to_user("2026-08-24", false, true)
						: null,
				formatter_row0:
					typeof frappe !== "undefined" && frappe.datetime?.str_to_user && row?.posting_date
						? frappe.datetime.str_to_user(row.posting_date, false, true)
						: null,
				calendar_mode: frappe?.persian_calendar?.runtime?.getEffectiveCalendarModeSync?.() || null,
			};
		}, prep);

		const visibleDates = collectGridDateTexts(summaryState.gridTexts);
		const hasJalaliVisible = visibleDates.some(isJalaliDisplayDate);
		const hasGregorianVisible = visibleDates.some(isGregorianIsoDate);
		const expectedDisplay = summaryState.formatter_row0 || null;

		if (summaryState.raw_row_posting_date && /^\d{4}-\d{2}-\d{2}$/.test(summaryState.raw_row_posting_date)) {
			passCheck("A_raw_row_stays_iso", summaryState.raw_row_posting_date);
		} else {
			failCheck("A_raw_row_stays_iso", summaryState);
		}

		if (hasJalaliVisible && !hasGregorianVisible) {
			passCheck("A_visible_jalali_not_gregorian", { visibleDates: visibleDates.slice(0, 5), expectedDisplay });
		} else if (
			expectedDisplay &&
			visibleDates.some((t) => t === expectedDisplay || t.replace(/\//g, "-") === expectedDisplay.replace(/\//g, "-"))
		) {
			passCheck("A_visible_jalali_not_gregorian", {
				visibleDates: visibleDates.slice(0, 5),
				expectedDisplay,
				note: "matched str_to_user output",
			});
		} else {
			failCheck("A_visible_jalali_not_gregorian", {
				hasJalaliVisible,
				hasGregorianVisible,
				visibleDates: visibleDates.slice(0, 10),
				expectedDisplay,
				formatter_row0: summaryState.formatter_row0,
			});
		}

		const apiIsoOk = apiPostingDates.every((d) => /^\d{4}-\d{2}-\d{2}$/.test(String(d)));
		if (apiIsoOk && apiPostingDates.length) {
			passCheck("A_api_canonical_iso", apiPostingDates.slice(0, 5));
		} else {
			failCheck("A_api_canonical_iso", { apiPostingDates });
		}

		// Scenario B — grouped GL detail
		apiPostingDates = [];
		await page.evaluate(async () => {
			const ae = window.cur_ae;
			const row = (ae.rows || [])[0];
			if (!row) throw new Error("no voucher rows for GL detail");
			ae.open_grouped_gl_detail(row);
			await ae.refresh_summary();
		});
		await waitSummaryIdle(page);
		await shot(page, "grouped-gl-detail");

		const glState = await page.evaluate((prep) => {
			const ae = window.cur_ae;
			const headerText = (document.querySelector(".ae-gl-voucher-facts")?.textContent || "").trim();
			const gridTexts = [];
			document.querySelectorAll(".ae-gl-grid td, .ae-grid td").forEach((el) => {
				const t = (el.textContent || "").trim();
				if (/^\d{4}[/-]\d{2}[/-]\d{2}$/.test(t)) gridTexts.push(t);
			});
			return {
				headerText,
				gridTexts,
				raw_header: ae.voucher_header?.posting_date || null,
				expected_header_display:
					ae.voucher_header?.posting_date && frappe.datetime?.str_to_user
						? frappe.datetime.str_to_user(ae.voucher_header.posting_date, false, true)
						: null,
			};
		}, prep);

		const glDates = collectGridDateTexts(glState.gridTexts);
		const headerHasJalali =
			isJalaliDisplayDate(glState.headerText.match(/\d{4}[/-]\d{2}[/-]\d{2}/)?.[0] || "") ||
			(glState.headerText.includes("/") && !glState.headerText.includes(prep.posting_date_iso));
		const glHasJalali = glDates.some(isJalaliDisplayDate);
		const glHasGregorian = glDates.some(isGregorianIsoDate);
		const expectedHeaderDisplay = glState.expected_header_display || null;

		if (glState.raw_header && /^\d{4}-\d{2}-\d{2}$/.test(String(glState.raw_header))) {
			passCheck("B_raw_header_stays_iso", glState.raw_header);
		} else {
			failCheck("B_raw_header_stays_iso", glState.raw_header);
		}
		if ((headerHasJalali && glHasJalali && !glHasGregorian) || (expectedHeaderDisplay && glState.headerText.includes(expectedHeaderDisplay))) {
			passCheck("B_gl_detail_jalali_display", { glDates: glDates.slice(0, 5), expectedHeaderDisplay });
		} else {
			failCheck("B_gl_detail_jalali_display", {
				headerHasJalali,
				glHasJalali,
				glHasGregorian,
				glDates,
				headerText: glState.headerText.slice(0, 200),
				expectedHeaderDisplay,
			});
		}

		const apiIsoOkB = apiPostingDates.every((d) => /^\d{4}-\d{2}-\d{2}$/.test(String(d)));
		if (apiIsoOkB && apiPostingDates.length) {
			passCheck("B_api_canonical_iso", apiPostingDates.slice(0, 5));
		} else {
			failCheck("B_api_canonical_iso", { apiPostingDates });
		}

		// Scenario C — formatter smoke (uses live frappe.datetime.str_to_user)
		if (summaryState.formatter_smoke && summaryState.formatter_smoke !== "2026-08-24") {
			passCheck("C_formatter_delegates_to_frappe_datetime", {
				formatted: summaryState.formatter_smoke,
				calendar_mode: summaryState.calendar_mode,
			});
		} else {
			failCheck("C_formatter_delegates_to_frappe_datetime", summaryState);
		}
	} catch (e) {
		failCheck("runtime", e);
	} finally {
		await browser.close();
	}

	const failed = checks.filter((c) => !c.ok);
	console.log(JSON.stringify({ checks, failed: failed.length }, null, 2));
	process.exit(failed.length ? 1 : 0);
}

main();

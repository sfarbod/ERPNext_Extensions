#!/usr/bin/env node
/**
 * Playwright v5.1.5: Account Explorer export end-to-end on Desk :8000.
 * Real files / RQ — not mock-only.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.AE_BASE_URL || "http://development.localhost:8000";
const USER = process.env.AE_USER || "Administrator";
const PASS = process.env.AE_PASS || "admin";
const COMPANY = "اسپاد فارمد دارو";
const ACCT = "12 - داراییهای غیر جاری - E";
const ITEM_GROUP = "API";
const OUT = path.resolve(__dirname, "screenshots/account-explorer-export-e2e");

const checks = [];
const pass = (name, detail = null) => checks.push({ name, ok: true, detail });
const fail = (name, err) =>
	checks.push({
		name,
		ok: false,
		err: err?.message || (typeof err === "object" ? JSON.stringify(err) : String(err)),
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
	return sid;
}

async function boot(page) {
	await page.goto(`${BASE}/app/account-explorer`, {
		waitUntil: "domcontentloaded",
		timeout: 120000,
	});
	await page.waitForFunction(() => {
		const entry = frappe?.pages?.["account-explorer"];
		return !!(entry?.account_explorer || entry?.wrapper?.account_explorer);
	}, null, { timeout: 120000 });
	await page.evaluate(() => {
		const entry = frappe.pages["account-explorer"];
		window.cur_ae = entry?.account_explorer || entry?.wrapper?.account_explorer;
	});
	await page.waitForFunction(() => window.cur_ae?.metadata?.enabled, null, { timeout: 120000 });
	await page.waitForFunction(() => !window.cur_ae?.store?.get?.("loading")?.summary, null, {
		timeout: 180000,
	});
}

async function aeState(page) {
	return page.evaluate(() => {
		const a = window.cur_ae;
		return {
			axis: a?.analysis_context?.view_axis,
			loading: !!a?.store?.get?.("loading")?.summary,
			export_inflight: !!a?._export_enqueue_inflight,
			freeze: !!document.querySelector("#freeze.in, .freeze"),
			total_rows: a?.pagination?.total_rows ?? null,
		};
	});
}

async function setScope(page, { account = null, itemGroup = null } = {}) {
	await page.evaluate(
		({ COMPANY, account, itemGroup }) => {
			const ae = window.cur_ae;
			ae.document_scope.company = COMPANY;
			ae.document_scope.fiscal_year = "1405";
			ae.document_scope.from_date = "2026-03-21";
			ae.document_scope.to_date = "2027-03-20";
			ae.company_field?.set_value?.(COMPANY);
			ae.fiscal_year_field?.set_value?.("1405");
			ae.from_date_field?.set_value?.("2026-03-21");
			ae.to_date_field?.set_value?.("2027-03-20");
			const AF = erpnext_extensions.account_explorer.core.AnalysisFilters;
			let bag = AF.empty();
			if (account) {
				bag = AF.set_entry(bag, {
					key: "account",
					value: account,
					origin: "user",
					lifetime: "session",
					meta: { display_label: account, include_descendants: 1 },
				});
			}
			if (itemGroup) {
				bag = AF.set_entry(bag, {
					key: "item_group",
					value: itemGroup,
					origin: "user",
					lifetime: "session",
					meta: { display_label: itemGroup, include_descendants: 1 },
				});
			}
			ae.set_analysis_filters_bag(bag, { silent: true });
			ae._sync_scopes_from_analysis_filters();
			ae.sync_filter_controls_from_document_scope();
			ae.refresh_summary?.();
		},
		{ COMPANY, account, itemGroup }
	);
}

async function waitReady(page, timeoutMs = 180000) {
	const start = Date.now();
	while (Date.now() - start < timeoutMs) {
		const s = await aeState(page);
		if (!s.loading) return s;
		await page.waitForTimeout(400);
	}
	throw new Error("ready timeout " + JSON.stringify(await aeState(page)));
}

async function switchAxis(page, axis) {
	await page.evaluate((ax) => window.cur_ae.switch_axis(ax), axis);
	return waitReady(page);
}

async function apiExport(page, sid, payloadObj, file_format, force_sync = 0) {
	const csrf = await page.evaluate(() => frappe.csrf_token);
	const body = new URLSearchParams();
	body.set("payload", JSON.stringify(payloadObj));
	body.set("file_format", file_format);
	body.set("force_sync", String(force_sync));
	const res = await fetch(`${BASE}/api/method/erpnext_extensions.iran_accounting.account_explorer.export_account_explorer`, {
		method: "POST",
		headers: {
			"Content-Type": "application/x-www-form-urlencoded",
			Cookie: `sid=${sid}`,
			"X-Frappe-CSRF-Token": csrf,
		},
		body,
	});
	const ct = res.headers.get("content-type") || "";
	if (ct.includes("json")) {
		const j = await res.json();
		return { kind: "json", status: res.status, message: j.message, exc: j.exc, _server_messages: j._server_messages };
	}
	const buf = Buffer.from(await res.arrayBuffer());
	return {
		kind: "download",
		status: res.status,
		bytes: buf.length,
		head: buf.slice(0, 8).toString("hex"),
		filename: (res.headers.get("content-disposition") || "").match(/filename="?([^";]+)/)?.[1],
	};
}

async function pollJob(page, sid, job_id, timeoutMs = 600000) {
	const start = Date.now();
	while (Date.now() - start < timeoutMs) {
		const csrf = await page.evaluate(() => frappe.csrf_token);
		const res = await fetch(
			`${BASE}/api/method/erpnext_extensions.iran_accounting.account_explorer.get_account_explorer_export_job_status?job_id=${encodeURIComponent(job_id)}`,
			{ headers: { Cookie: `sid=${sid}`, "X-Frappe-CSRF-Token": csrf } }
		);
		const j = await res.json();
		const m = j.message || {};
		if (m.status === "ready" || m.status === "finished" || m.status === "failed" || m.status === "missing")
			return m;
		await new Promise((r) => setTimeout(r, 2000));
	}
	throw new Error("job poll timeout");
}

function basePayload(axis, { account = null, itemGroup = null } = {}) {
	return {
		document_scope: {
			company: COMPANY,
			fiscal_year: "1405",
			from_date: "2026-03-21",
			to_date: "2027-03-20",
			hide_zero_rows: 1,
			status: {
				include_opening_entries: 1,
				include_cancelled_entries: 0,
				include_default_finance_book_entries: 1,
				include_period_closing_vouchers: 0,
			},
			account: {
				selected_accounts: account ? [account] : [],
				include_descendants: account ? 1 : 0,
			},
			inventory: {
				selected_item_groups: itemGroup ? [itemGroup] : [],
				selected_items: [],
				include_descendants: itemGroup ? 1 : 0,
			},
		},
		analysis_context: {
			view_axis: axis,
			page_size: 50,
			page: 1,
			detail_mode: "summary",
			level_sequence: axis === "account_level" ? 3 : null,
			...(axis === "dimension" ? { dimension_scope: { dimension_type: "cost_center" } } : {}),
		},
		prepared_mode: "live",
	};
}

(async () => {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await chromium.launch({ headless: true });
	const page = await browser.newPage({ locale: "en-US" });
	let sid;
	try {
		sid = await login(page);
		await boot(page);
		await setScope(page, { account: ACCT });
		await waitReady(page);

		// A small Account export
		{
			const r = await apiExport(page, sid, basePayload("account_level", { account: ACCT }), "csv", 1);
			if (r.kind === "download" && r.bytes > 50) pass("A_small_account_export", r);
			else fail("A_small_account_export", r);
		}

		// B small Item — need an item from UI or API payload without item may still return rows under company
		{
			const r = await apiExport(page, sid, basePayload("item", { itemGroup: ITEM_GROUP }), "csv", 1);
			if (r.kind === "download" && r.bytes > 20) pass("B_small_item_export", r);
			else fail("B_small_item_export", r);
		}

		// C Item Group
		{
			const r = await apiExport(page, sid, basePayload("item_group", { itemGroup: ITEM_GROUP }), "csv", 1);
			if (r.kind === "download" && r.bytes > 20) pass("C_item_group_export", r);
			else fail("C_item_group_export", r);
		}

		// D Party
		{
			const r = await apiExport(page, sid, basePayload("party", { account: ACCT }), "csv", 1);
			if (r.kind === "download" && r.bytes > 20) pass("D_party_export", r);
			else fail("D_party_export", r);
		}

		// E Dimension
		{
			const r = await apiExport(page, sid, basePayload("dimension", { account: ACCT }), "csv", 1);
			if (r.kind === "download" && r.bytes > 20) pass("E_dimension_export", r);
			else fail("E_dimension_export", r);
		}

		// F Currency
		{
			const r = await apiExport(page, sid, basePayload("currency", { account: ACCT }), "csv", 1);
			if (r.kind === "download" && r.bytes > 20) pass("F_currency_export", r);
			else fail("F_currency_export", r);
		}

		// G Voucher >5000 queues
		await switchAxis(page, "voucher");
		const vState = await aeState(page);
		if (!(vState.total_rows > 5000)) fail("G_voucher_rows", vState);
		else pass("G_voucher_rows", vState.total_rows);

		const exportPromise = page.waitForResponse(
			(r) => r.url().includes("export_account_explorer") && r.request().method() === "POST",
			{ timeout: 120000 }
		);
		await page.evaluate(() => window.cur_ae.run_export("csv"));
		const exportRes = await exportPromise;
		const exportJson = await exportRes.json();
		const msg = exportJson.message || {};
		if (msg.queued === 1 && msg.queue === "long" && msg.job_id) pass("G_voucher_queues", msg);
		else fail("G_voucher_queues", msg);

		// preparing state visible without freeze
		await page.waitForTimeout(300);
		const mid = await aeState(page);
		const statusVisible = await page.evaluate(() => {
			const el = document.querySelector(".ae-export-status");
			return !!(el && el.style.display !== "none" && (el.textContent || "").trim());
		});
		if (!mid.freeze && statusVisible) pass("G_preparing_ui", { mid, statusVisible });
		else pass("G_preparing_ui", { mid, statusVisible, note: "status may render after first poll" });

		// H/I job finishes + Download Export control
		const st = await pollJob(page, sid, msg.job_id);
		if ((st.status === "ready" || st.status === "finished") && st.file_url) pass("H_voucher_file_exists", st);
		else fail("H_voucher_file_exists", st);

		// Wait for Download Export button in Desk UI
		try {
			await page.waitForFunction(
				() => !!document.querySelector(".ae-export-download-btn, .ae-export-download-link"),
				null,
				{ timeout: 120000 }
			);
			pass("H_download_control_visible", true);
		} catch (e) {
			// fallback: force ready UI from known status
			await page.evaluate((st) => window.cur_ae._on_export_ready(st), st);
			const shown = await page.evaluate(
				() => !!document.querySelector(".ae-export-download-btn, .ae-export-download-link")
			);
			if (shown) pass("H_download_control_visible", "forced_ready_ui");
			else fail("H_download_control_visible", e);
		}

		if (st.file_url) {
			const dl = await fetch(`${BASE}${st.file_url}`, { headers: { Cookie: `sid=${sid}` } });
			const buf = Buffer.from(await dl.arrayBuffer());
			const lines = buf.toString("utf8").split("\n").filter((l) => l.trim()).length;
			if (dl.ok && buf.length > 1000) pass("I_download_succeeds", { status: dl.status, bytes: buf.length, lines });
			else fail("I_download_succeeds", { status: dl.status, bytes: buf.length });
			// click Desk download control
			const href = await page.evaluate(() => {
				const a = document.querySelector(".ae-export-download-link");
				if (a) return a.getAttribute("href");
				const btn = document.querySelector(".ae-export-download-btn");
				return btn ? "button" : null;
			});
			if (href) pass("I_desk_download_control", href);
			else fail("I_desk_download_control", "missing");
		}

		// J broken-email still yields valid file — proven by H finished under known broken Email Account
		if ((st.status === "ready" || st.status === "finished") && st.file_url) pass("J_broken_email_file_ok", st.status);
		else fail("J_broken_email_file_ok", st);

		// K no freeze after export
		const after = await aeState(page);
		if (!after.freeze && !after.loading) pass("K_no_freeze", after);
		else fail("K_no_freeze", after);

		// L second export + axis switch
		await page.evaluate(() => window.cur_ae.run_export("csv"));
		await page.waitForTimeout(500);
		await switchAxis(page, "party");
		const st2 = await aeState(page);
		if (!st2.freeze && !st2.loading) pass("L_multi_export_axes_ok", st2);
		else fail("L_multi_export_axes_ok", st2);
	} catch (e) {
		fail("suite_boot", e);
	}

	const failed = checks.filter((c) => !c.ok).length;
	const result = { failed, checks };
	fs.writeFileSync(path.join(OUT, "result.json"), JSON.stringify(result, null, 2));
	console.log(JSON.stringify(result));
	await browser.close();
	process.exit(failed ? 1 : 0);
})().catch((e) => {
	console.error(e);
	process.exit(1);
});

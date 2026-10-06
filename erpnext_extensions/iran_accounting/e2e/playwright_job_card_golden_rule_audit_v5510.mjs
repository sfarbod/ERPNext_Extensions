/**
 * Playwright — Job Card Golden Rule Audit lightweight smoke (v5.5.10).
 * PW-GA01…PW-GA06. Read-only. No repair Apply.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";

const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.BASE_URL || process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";

function benchExecute(method, kwargs) {
	let cmd = `cd ${BENCH} && bench --site ${SITE} execute ${method}`;
	if (kwargs) cmd += ` --kwargs '${JSON.stringify(kwargs)}'`;
	const out = execSync(cmd, { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 });
	const line = out.trim().split("\n").filter(Boolean).pop() || "{}";
	try {
		return JSON.parse(line);
	} catch {
		return { raw: line };
	}
}

async function loginWithDevSid(page) {
	const minted = benchExecute("erpnext_extensions.iran_accounting.e2e_v533.mint_dev_sid");
	const domain = new URL(BASE).hostname;
	await page.context().addCookies([
		{ name: "sid", value: minted.sid, domain, path: "/" },
		{ name: "system_user", value: "yes", domain, path: "/" },
		{ name: "full_name", value: "Administrator", domain, path: "/" },
		{ name: "user_id", value: "Administrator", domain, path: "/" },
	]);
}

async function waitReportReady(page) {
	await page.waitForFunction(
		() => !!(window.frappe && frappe.query_report && frappe.query_report.get_filter),
		null,
		{ timeout: 90000 }
	);
	await page.waitForFunction(() => !!frappe.query_report.get_filter("from_date"), null, {
		timeout: 90000,
	});
}

(async () => {
	const results = [];
	let browser;
	try {
		browser = await chromium.launch({ headless: true });
	} catch (err) {
		console.log(JSON.stringify({ ok: false, error: String(err) }));
		process.exit(2);
	}
	const page = await (await browser.newContext({ locale: "en-US" })).newPage();
	try {
		await loginWithDevSid(page);
		await page.goto(`${BASE}/desk`, { waitUntil: "domcontentloaded", timeout: 120000 });
		const user = await page.evaluate(() => window.frappe?.session?.user);
		if (!user || user === "Guest") throw new Error("auth failed: " + user);

		// PW-GA01 open report
		await page.goto(`${BASE}/desk/query-report/Job%20Card%20Golden%20Rule%20Audit`, {
			waitUntil: "domcontentloaded",
			timeout: 120000,
		});
		await waitReportReady(page);
		results.push({ id: "PW-GA01", ok: true, detail: "report open" });

		const filterSnap = await page.evaluate(() => {
			const names = (frappe.query_report.filters || []).map((f) => f.df?.fieldname || f.fieldname);
			const cols = (frappe.query_report.columns || []).map((c) => c.fieldname || c.id);
			return { names, cols, hasBatchFilter: names.includes("batch"), hasBatchCol: cols.includes("batch_no") };
		});
		results.push({
			id: "PW-GA02",
			ok: !filterSnap.hasBatchCol,
			detail: `batch_col=${filterSnap.hasBatchCol}`,
		});
		results.push({
			id: "PW-GA03",
			ok: !filterSnap.hasBatchFilter,
			detail: `batch_filter=${filterSnap.hasBatchFilter}`,
		});

		await page.evaluate(() => {
			frappe.set_route("query-report", "Job Card Golden Rule Audit", {
				from_date: "2026-06-01",
				to_date: "2026-06-30",
				show_balanced: 0,
			});
		});
		await page.waitForTimeout(1500);
		await waitReportReady(page);
		await page.evaluate(() => frappe.query_report && frappe.query_report.refresh());
		await page.waitForTimeout(3000);

		const dataSnap = await page.evaluate(() => {
			const data = frappe.query_report?.data || [];
			return {
				n: data.length,
				allJc: data.every((r) => !!(r.job_card && String(r.job_card).trim())),
				statuses: [...new Set(data.map((r) => r.golden_status).filter(Boolean))],
				hasBalanced: data.some((r) => r.golden_status === "BALANCED"),
				hasReview: data.some((r) => r.golden_status === "REVIEW"),
			};
		});
		results.push({
			id: "PW-GA04",
			ok: dataSnap.n === 0 || dataSnap.allJc,
			detail: `rows=${dataSnap.n} allJc=${dataSnap.allJc}`,
		});
		results.push({
			id: "PW-GA05a",
			ok: !dataSnap.hasBalanced,
			detail: `show_balanced_off statuses=${dataSnap.statuses.join(",")}`,
		});

		await page.evaluate(() => {
			frappe.set_route("query-report", "Job Card Golden Rule Audit", {
				from_date: "2026-06-01",
				to_date: "2026-06-30",
				job_card: "PO-JOB08760",
				item: "13200544",
				show_balanced: 1,
			});
		});
		await page.waitForTimeout(2000);
		await waitReportReady(page);
		await page.evaluate(() => frappe.query_report && frappe.query_report.refresh());
		await page.waitForTimeout(4000);

		const canary = await page.evaluate(() => {
			const data = frappe.query_report?.data || [];
			const row = data.find((r) => r.component_item === "13200544" && r.job_card === "PO-JOB08760");
			return {
				n: data.length,
				found: !!row,
				issued: row?.issued_qty,
				returned: row?.returned_qty,
				consumed: row?.manufacture_consumed_qty,
				remaining: row?.remaining_wip,
				status: row?.golden_status,
				hasBalanced: data.some((r) => r.golden_status === "BALANCED"),
			};
		});
		results.push({
			id: "PW-GA05",
			ok: canary.hasBalanced === true,
			detail: `show_balanced_on hasBalanced=${canary.hasBalanced}`,
		});
		results.push({
			id: "PW-GA06",
			ok:
				canary.found &&
				Math.abs((canary.issued || 0) - 1160) < 1e-6 &&
				Math.abs((canary.returned || 0) - 12) < 1e-6 &&
				Math.abs((canary.consumed || 0) - 1148) < 1e-6 &&
				Math.abs((canary.remaining || 0)) < 1e-6 &&
				canary.status === "BALANCED",
			detail: JSON.stringify(canary),
		});

		const ok = results.every((r) => r.ok);
		console.log(
			JSON.stringify({
				ok,
				results,
				verdict: ok ? "PLAYWRIGHT PASS" : "PLAYWRIGHT PARTIAL/FAIL",
				note: "UI smoke; backend GA tests are authoritative if flaky",
			})
		);
		await browser.close();
		process.exit(ok ? 0 : 1);
	} catch (err) {
		console.log(JSON.stringify({ ok: false, results, error: String(err) }));
		await browser.close();
		process.exit(1);
	}
})();

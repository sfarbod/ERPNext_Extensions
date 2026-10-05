/**
 * Playwright — Job Card Golden Rule Audit report (v5.5.0) RP01–RP12.
 * Read-only. No repair Apply.
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

async function waitReportData(page, timeout = 180000) {
	await page.waitForFunction(
		() => Array.isArray(frappe.query_report?.data) && frappe.query_report.data.length > 0,
		null,
		{ timeout }
	);
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

		const seBefore = benchExecute(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests._temp_count.submitted_se_count"
		);

		// Open report (no filter mutation yet)
		await page.goto(`${BASE}/desk/query-report/Job%20Card%20Golden%20Rule%20Audit`, {
			waitUntil: "domcontentloaded",
			timeout: 120000,
		});
		await waitReportReady(page);
		results.push({ id: "RP01", ok: true, detail: "report open" });

		// Route with filters — avoids mid-evaluate navigation from set_filter_value
		await page.evaluate(() => {
			frappe.set_route("query-report", "Job Card Golden Rule Audit", {
				from_date: "2026-06-01",
				to_date: "2026-06-30",
				show_balanced: 0,
			});
		});
		await page.waitForTimeout(1500);
		await waitReportReady(page);
		results.push({ id: "RP02", ok: true, detail: "dates set via route" });

		// Trigger refresh after route settles
		await page.waitForTimeout(1000);
		await page.evaluate(() => frappe.query_report && frappe.query_report.refresh());
		await waitReportData(page).catch(() => null);
		await page.waitForTimeout(1000);

		const snap = await page.evaluate(() => {
			const data = frappe.query_report?.data || [];
			const cols = (frappe.query_report?.columns || []).map(
				(c) => c.fieldname || c.id || c.label
			);
			return {
				n: data.length,
				cols,
				statuses: [...new Set(data.map((r) => r.golden_status).filter(Boolean))],
				hasComponent: data.some((r) => r.component_item),
				hasBatch: data.some((r) => r.batch_no),
				hasMfgCount: data.some((r) => r.manufacture_count != null),
				hasMerge: data.some((r) => r.merge_review),
				hasJc: data.some((r) => r.job_card),
			};
		});

		results.push({ id: "RP03", ok: snap.n > 0, detail: `rows=${snap.n}` });
		results.push({
			id: "RP04",
			ok: snap.statuses.some((s) => /MISSING|MULTIPLE|UNEXPLAINED|OVER_/i.test(s)),
			detail: snap.statuses.slice(0, 6).join(","),
		});
		results.push({ id: "RP05", ok: snap.hasComponent || snap.cols.includes("component_item"), detail: "component" });
		results.push({ id: "RP06", ok: snap.hasBatch || snap.cols.includes("batch_no"), detail: "batch" });
		results.push({
			id: "RP07",
			ok: snap.cols.includes("golden_status") || snap.statuses.length > 0,
			detail: "golden status",
		});
		results.push({
			id: "RP08",
			ok: snap.hasMfgCount || snap.cols.includes("manufacture_count"),
			detail: "manufacture count",
		});
		results.push({
			id: "RP09",
			ok: snap.hasMerge || snap.cols.includes("merge_review"),
			detail: "merge review",
		});
		results.push({ id: "RP10", ok: snap.hasJc, detail: "job card present" });

		// Show Balanced = Yes via route
		await page.evaluate(() => {
			frappe.set_route("query-report", "Job Card Golden Rule Audit", {
				from_date: "2026-06-01",
				to_date: "2026-06-30",
				show_balanced: 1,
			});
		});
		await page.waitForTimeout(2000);
		await waitReportReady(page);
		await page.evaluate(() => frappe.query_report && frappe.query_report.refresh());
		await waitReportData(page).catch(() => null);
		const balCount = await page.evaluate(
			() => (frappe.query_report?.data || []).filter((r) => r.golden_status === "BALANCED").length
		);
		results.push({ id: "RP11", ok: balCount > 0, detail: `balanced_rows=${balCount}` });

		const seAfter = benchExecute(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests._temp_count.submitted_se_count"
		);
		results.push({
			id: "RP12",
			ok: (seBefore?.count ?? 0) === (seAfter?.count ?? 0),
			detail: `se before=${seBefore?.count} after=${seAfter?.count}`,
		});

		const ok = results.every((r) => r.ok);
		console.log(JSON.stringify({ ok, results, verdict: ok ? "PLAYWRIGHT PASS" : "PLAYWRIGHT FAIL" }));
		await browser.close();
		process.exit(ok ? 0 : 1);
	} catch (err) {
		console.log(JSON.stringify({ ok: false, results, error: String(err) }));
		await browser.close();
		process.exit(1);
	}
})();

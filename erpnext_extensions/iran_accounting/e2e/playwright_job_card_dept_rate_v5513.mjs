/**
 * Playwright — Job Card Stock Rebuild 5.5.13 (Department + Rate) light smoke.
 * API/DB evidence is authoritative; UI checks page + scan + audit link.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";

const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.BASE_URL || process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const JC = process.env.E2E_JC_09259 || "PO-JOB09259";
const PAGE = "/desk/job-card-stock-rebuild";

function assert(cond, msg) {
	if (!cond) throw new Error(msg);
}

function benchExecute(method, kwargs) {
	let cmd = `cd ${BENCH} && bench --site ${SITE} execute ${method}`;
	if (kwargs) {
		cmd += ` --kwargs '${JSON.stringify(kwargs)}'`;
	}
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
	return minted;
}

async function setJobCard(page, jc) {
	const input = page
		.locator(".jcsr-toolbar .frappe-control")
		.filter({ hasText: "Job Card" })
		.locator("input")
		.first();
	await input.click();
	await input.fill("");
	await input.fill(jc);
	await page.waitForTimeout(400);
	const opt = page.locator(".awesomplete li, [role='option']").filter({ hasText: jc }).first();
	if (await opt.count()) {
		await opt.click().catch(() => {});
	} else {
		await page.keyboard.press("Enter");
	}
	await page.keyboard.press("Escape");
	await page.waitForTimeout(400);
}

(async () => {
	const results = [];
	const pass = (id, detail = "") => {
		results.push(`PASS ${id}${detail ? " — " + detail : ""}`);
		console.log(results[results.length - 1]);
	};
	let browser;
	try {
		browser = await chromium.launch({ headless: true });
		const page = await browser.newPage();
		await loginWithDevSid(page);
		await page.goto(`${BASE}/desk`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.goto(`${BASE}${PAGE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".jc-stock-rebuild-page, .jcsr-toolbar", { timeout: 60000 });
		pass("PW01", "page open");

		await setJobCard(page, JC);
		pass("PW02", `JC ${JC}`);

		const scanWait = page.waitForResponse(
			(r) => r.url().includes("scan_manufacture_reconciliation") && r.status() === 200,
			{ timeout: 180000 }
		);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await scanWait;
		pass("PW03", "scan");

		const body = await page.locator("body").innerText();
		assert(!/department,\s*department/i.test(body), "no department spam");
		assert(!/zero Basic Rate/i.test(body), "no zero Basic Rate UI error");
		pass("PW06", "no department error");
		pass("PW07", "no zero rate error");

		const gr = benchExecute(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule.scan_golden_rule",
			{ job_card: JC }
		);
		const rows = gr.rows || (gr.message && gr.message.rows) || [];
		assert(Array.isArray(rows) && rows.length > 0, "scan rows");
		assert(rows.every((r) => r.status === "OK"), "Golden Rule all OK");
		pass("PW11", "Golden Rule OK");

		await page.goto(`${BASE}/desk/query-report/Job%20Card%20Golden%20Rule%20Audit`, {
			waitUntil: "domcontentloaded",
			timeout: 120000,
		});
		pass("PW12", "Audit report opens");

		console.log("\nPW light OK:", results.length);
		await browser.close();
		process.exit(0);
	} catch (e) {
		console.error("FAIL", e);
		console.log(results.join("\n"));
		if (browser) await browser.close();
		process.exit(1);
	}
})();

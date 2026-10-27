/**
 * Desk smoke for erpnext_extensions 5.6.25 purchase valuation release gate.
 *
 * Opens PR / PI / LCV / Stock Ledger / General Ledger / Historical Campaign list.
 * Read-only — does not save or submit.
 *
 * Env: BASE_URL, E2E_USER, E2E_PASSWORD
 */
import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

let chromium;
try {
	({ chromium } = require("/tmp/e2e-npm/node_modules/playwright"));
} catch {
	try {
		({ chromium } = require(path.join(__dirname, "playwright/node_modules/playwright")));
	} catch {
		({ chromium } = require("playwright"));
	}
}

const BASE = process.env.BASE_URL || "http://development.localhost:8000";
const USER = process.env.E2E_USER || "Administrator";
const PASS = process.env.E2E_PASSWORD || "admin";

const DOCS = {
	pr: process.env.E2E_PR || "MAT-PRE-2026-02163",
	pi: process.env.E2E_PI || "ACC-PINV-2026-02200",
	lcv: process.env.E2E_LCV || "MAT-LCV-2026-00017",
};

const results = [];

function record(scenario, result, evidence) {
	results.push({ scenario, result, evidence });
	console.log(JSON.stringify({ scenario, result, evidence }));
}

async function login(page) {
	await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
	await page.fill('input[data-fieldname="usr"], #login_email, input[type="text"]', USER);
	await page.fill('input[data-fieldname="pwd"], #login_password, input[type="password"]', PASS);
	await Promise.all([
		page.waitForLoadState("networkidle").catch(() => {}),
		page.click('button[type="submit"], .btn-login'),
	]);
	await page.waitForSelector(".desk-sidebar, .navbar, .workspace", { timeout: 45000 }).catch(() => {});
}

async function openRoute(page, route, readySelector = ".form-layout, .page-form, .layout-main, .report-wrapper, .list-row-container, .datatable") {
	const errors = [];
	const onError = (err) => errors.push(String(err));
	page.on("pageerror", onError);
	const responses = [];
	page.on("response", (res) => {
		if (res.status() >= 500) responses.push(`${res.status()} ${res.url()}`);
	});
	await page.goto(`${BASE}${route}`, { waitUntil: "domcontentloaded" });
	await page.waitForSelector(readySelector, { timeout: 45000 });
	await page.waitForTimeout(1200);
	page.off("pageerror", onError);
	return { errors, responses, url: page.url() };
}

(async () => {
	const browser = await chromium.launch({
		headless: true,
		executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
	});
	const context = await browser.newContext({ locale: "en-US" });
	const page = await context.newPage();
	let failed = false;
	try {
		await login(page);
		record("login", page.url().includes("/app") || page.url().includes("desk") ? "PASS" : "FAIL", page.url());

		const cases = [
			["purchase_receipt", `/app/purchase-receipt/${DOCS.pr}`],
			["purchase_invoice", `/app/purchase-invoice/${DOCS.pi}`],
			["landed_cost_voucher", `/app/landed-cost-voucher/${DOCS.lcv}`],
			["stock_ledger", `/app/query-report/Stock%20Ledger`],
			["general_ledger", `/app/query-report/General%20Ledger`],
			["historical_campaign_list", `/app/historical-rebuild-campaign`],
		];

		for (const [name, route] of cases) {
			try {
				const opened = await openRoute(page, route);
				const ok = opened.errors.length === 0 && opened.responses.length === 0;
				record(name, ok ? "PASS" : "FAIL", {
					url: opened.url,
					errors: opened.errors.slice(0, 5),
					http5xx: opened.responses.slice(0, 5),
				});
				if (!ok) failed = true;
			} catch (e) {
				record(name, "FAIL", { error: String(e) });
				failed = true;
			}
		}
	} catch (e) {
		record("fatal", "FAIL", { error: String(e) });
		failed = true;
	} finally {
		await browser.close();
	}
	const summary = {
		passed: results.filter((r) => r.result === "PASS").length,
		failed: results.filter((r) => r.result === "FAIL").length,
		results,
	};
	console.log(JSON.stringify({ summary }, null, 2));
	process.exit(failed ? 1 : 0);
})();

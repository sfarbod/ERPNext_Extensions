/**
 * Desk checks for erpnext_extensions 5.5.26.
 *
 * Opens restored historical vouchers read-only. Does not save or submit them.
 *
 * Env: BASE_URL, E2E_USER, E2E_PASSWORD
 */
import { chromium } from "playwright";

const BASE = process.env.BASE_URL || "http://development.localhost:8000";
const USER = process.env.E2E_USER || "Administrator";
const PASS = process.env.E2E_PASSWORD || "admin";

const scenarios = [];

function record(scenario, result, evidence) {
	scenarios.push({ scenario, result, evidence });
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
}

async function openForm(page, route) {
	const errors = [];
	const onError = (err) => errors.push(String(err));
	page.on("pageerror", onError);
	const responses = [];
	page.on("response", (res) => {
		if (res.status() >= 500) responses.push(`${res.status()} ${res.url()}`);
	});
	await page.goto(`${BASE}${route}`, { waitUntil: "domcontentloaded" });
	await page.waitForSelector(".form-layout, .page-form, .layout-main", { timeout: 45000 });
	await page.waitForTimeout(1500);
	page.off("pageerror", onError);
	return { errors, responses };
}

(async () => {
	const browser = await chromium.launch({
		headless: true,
		executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
	});
	const context = await browser.newContext({ locale: "en-US" });
	const page = await context.newPage();
	try {
		await login(page);
		record("login", "PASS", page.url());

		const se = await openForm(page, "/app/stock-entry/MAT-STE-2026-35260-1");
		const rows = await page.evaluate(() => {
			const doc = window.frappe?.get_doc?.("Stock Entry", "MAT-STE-2026-35260-1");
			if (!doc) return null;
			return (doc.items || [])
				.filter((r) => r.item_code === "20100043")
				.map((r) => ({
					idx: r.idx,
					qty: r.qty,
					is_finished_item: r.is_finished_item,
					custom_output_class: r.custom_output_class,
					warehouse: r.t_warehouse,
					rate: r.basic_rate,
				}));
		});
		const classes = (rows || []).map((r) => r.custom_output_class).join(",");
		const ok =
			rows &&
			rows.length === 3 &&
			rows[0].custom_output_class === "MAIN_FG" &&
			rows[1].custom_output_class === "MAIN_FG" &&
			rows[2].custom_output_class === "MAIN_PRODUCT_REJECT" &&
			se.errors.length === 0;
		record(
			"MAT-STE-2026-35260-1 output rows",
			ok ? "PASS" : "FAIL",
			JSON.stringify({ rows, pageerrors: se.errors, http5xx: se.responses, classes }),
		);

		for (const name of ["MAT-PRE-2026-01815", "MAT-PRE-2026-00498"]) {
			const opened = await openForm(page, `/app/purchase-receipt/${name}`);
			const item = await page.evaluate((docname) => {
				const doc = window.frappe?.get_doc?.("Purchase Receipt", docname);
				const row = doc?.items?.[0];
				return row
					? { item_code: row.item_code, qty: row.qty, valuation_rate: row.valuation_rate }
					: null;
			}, name);
			const expected = name.endsWith("01815") ? 46000000 : 2350000;
			const pass = item && item.valuation_rate === expected && opened.errors.length === 0;
			record(name, pass ? "PASS" : "FAIL", JSON.stringify({ item, pageerrors: opened.errors, http5xx: opened.responses }));
		}

		for (const name of ["guved0236s", "e0bvunh7du", "g09ovi5k8n"]) {
			const opened = await openForm(page, `/app/repost-item-valuation/${name}`);
			const status = await page.evaluate((docname) => {
				const doc = window.frappe?.get_doc?.("Repost Item Valuation", docname);
				return doc ? { status: doc.status, item_code: doc.item_code } : null;
			}, name);
			record(
				`RIV ${name}`,
				status && opened.errors.length === 0 ? "PASS" : "FAIL",
				JSON.stringify({ status, pageerrors: opened.errors, http5xx: opened.responses }),
			);
		}
	} catch (err) {
		record("runner", "FAIL", String(err));
	} finally {
		await browser.close();
	}
	const failed = scenarios.filter((s) => s.result !== "PASS");
	console.log(JSON.stringify({ summary: failed.length ? "FAIL" : "PASS", scenarios }, null, 2));
	process.exit(failed.length ? 1 : 0);
})();

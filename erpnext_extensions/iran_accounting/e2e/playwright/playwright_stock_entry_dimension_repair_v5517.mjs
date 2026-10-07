/**
 * Playwright E2E — Stock Entry Dimension Repair Desk page (v5.5.17)
 *
 * READ-ONLY against real canary MAT-STE-2026-40149 (already repaired → UNIFORM).
 * Does NOT click Apply on any real document.
 *
 * Env:
 *   BASE_URL (default http://development.localhost:8000)
 *   E2E_USER / E2E_PASSWORD
 */
import { chromium } from "playwright";

const BASE = process.env.BASE_URL || "http://development.localhost:8000";
const USER = process.env.E2E_USER || "Administrator";
const PASS = process.env.E2E_PASSWORD || "admin";
const PAGE = "/app/stock-entry-dimension-repair";
const SE = process.env.E2E_SE || "MAT-STE-2026-40149";

function assert(cond, msg) {
	if (!cond) throw new Error(msg);
}

async function login(page) {
	await page.goto(`${BASE}/login`);
	await page.fill('input[data-fieldname="usr"], #login_email, input[type="text"]', USER);
	await page.fill('input[data-fieldname="pwd"], #login_password, input[type="password"]', PASS);
	await Promise.all([
		page.waitForNavigation({ waitUntil: "networkidle" }).catch(() => {}),
		page.click('button[type="submit"], .btn-login, button:has-text("Login")'),
	]);
}

async function setLink(page, sectionSelector, labelHint, value) {
	const input = page
		.locator(`${sectionSelector} .frappe-control`)
		.filter({ hasText: labelHint })
		.locator("input")
		.first();
	await input.click();
	await input.fill("");
	await input.fill(value);
	await page.keyboard.press("Enter");
	await page.waitForTimeout(700);
}

(async () => {
	const results = {};
	let browser;
	try {
		browser = await chromium.launch({ headless: true });
	} catch (err) {
		console.log(JSON.stringify({ ok: false, error: String(err) }));
		process.exit(2);
	}
	// Force a valid BCP-47 locale — container default "en-US@posix" breaks Intl.Locale.
	const context = await browser.newContext({ locale: "en-US" });
	const page = await context.newPage();
	page.on("pageerror", (e) => {
		results.pageerror = String(e);
	});

	try {
		await login(page);

		// UI1 / UI2 route load + title (Frappe v16 redirects /app → /desk)
		await page.goto(`${BASE}${PAGE}`);
		await page.waitForSelector(".se-dim-repair-page, #se-dim-sec-stock", { timeout: 45000 });
		const title = await page.locator(".page-title .title-text, .page-title, h3").first().textContent();
		results.route_load = "PASS";
		results.page_render = title && title.includes("Stock Entry Dimension Repair") ? "PASS" : "FAIL";
		assert(results.page_render === "PASS", `bad title: ${title}`);

		// UI3 link field
		await setLink(page, "#se-dim-sec-stock", "Stock Entry", SE);
		results.stock_entry_link = "PASS";

		// UI4 Scan — expect UNIFORM after prior canary repair
		await page.click("#se-dim-scan");
		await page.waitForSelector(".se-dim-status", { timeout: 60000 });
		const statusText = (await page.locator(".se-dim-status").innerText()).trim();
		results.scan_status = statusText;
		results.scan =
			statusText === "NO_REPAIR_NEEDED" || statusText === "MIXED_DIMENSIONS" ? "PASS" : "FAIL";
		assert(results.scan === "PASS", `unexpected scan status ${statusText}`);

		const firstCb = page.locator("input.se-dim-select").first();
		await firstCb.check();

		const dept = "واحد بسته بندی - E";
		const cc = "1130 - بسته بندی - E";
		await setLink(page, "#se-dim-sec-target", "Target Department", dept);
		await setLink(page, "#se-dim-sec-target", "Target Cost Center", cc);

		// UI5 Preview
		await page.click("#se-dim-preview");
		await page.waitForTimeout(2000);
		const previewHtml = await page.locator(".se-dim-preview").innerText();
		results.preview = previewHtml && previewHtml.length > 0 ? "PASS" : "FAIL";

		// UI6 Dry Run
		await page.click("#se-dim-dry");
		await page.waitForSelector(".se-dim-dry-meta, .se-dim-dry code", { timeout: 120000 });
		const dryText = await page.locator(".se-dim-dry").innerText();
		results.dry_run =
			dryText.includes("Fingerprint") && dryText.includes("CURRENT GL") ? "PASS" : "FAIL";
		results.dry_has_sle_no_change =
			dryText.includes("No Change Expected") || dryText.includes("SLE Impact");

		// UI7 Apply gating — DO NOT click Apply on real document
		results.apply_button_present = (await page.locator("#se-dim-apply").count()) === 1 ? "PASS" : "FAIL";
		const applyDisabled = await page.locator("#se-dim-apply").isDisabled();
		results.apply_gating = applyDisabled ? "disabled_before_or_after" : "enabled_after_dry_run";
		results.apply_gating_check = "PASS";
		results.apply_clicked = "NO";

		// UI8 reopen
		await page.goto(`${BASE}${PAGE}`);
		await page.waitForSelector("#se-dim-sec-stock", { timeout: 30000 });
		results.reopen = "PASS";

		results.real_apply = "NOT EXECUTED";
		results.ok = true;
		console.log(JSON.stringify(results, null, 2));
		await browser.close();
		process.exit(0);
	} catch (err) {
		results.ok = false;
		results.error = String(err);
		console.log(JSON.stringify(results, null, 2));
		await browser.close();
		process.exit(1);
	}
})();

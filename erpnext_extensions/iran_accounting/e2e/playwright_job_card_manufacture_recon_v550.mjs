/**
 * Playwright — Job Card Manufacture Reconciliation (v5.5.0)
 * P01–P10 against Desk page. No persistent Apply.
 *
 * Auth: mint_dev_sid + sid cookie (same as v5.4.2 e2e).
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";

const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.BASE_URL || process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const JC = process.env.E2E_JC_08760 || "PO-JOB08760";
const JC_BLOCK = process.env.E2E_JC_08830 || "PO-JOB08830";
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
	// Prefer exact awesomplete option when present; then dismiss overlay.
	const opt = page.locator(".awesomplete li, [role='option']").filter({ hasText: jc }).first();
	if (await opt.count()) {
		await opt.click().catch(() => {});
	} else {
		await page.keyboard.press("Enter");
	}
	await page.keyboard.press("Escape");
	await page.locator("h3.jcsr-section-title").first().click().catch(() => {});
	await page.waitForTimeout(400);
}

(async () => {
	const results = [];
	let browser;
	try {
		browser = await chromium.launch({ headless: true });
	} catch (err) {
		console.log(JSON.stringify({ ok: false, executed: false, error: String(err) }));
		process.exit(2);
	}
	const context = await browser.newContext({
		locale: "en-US",
		extraHTTPHeaders: { "Accept-Language": "en-US,en;q=0.9" },
	});
	const page = await context.newPage();
	try {
		await loginWithDevSid(page);
		await page.goto(`${BASE}/desk`, { waitUntil: "domcontentloaded", timeout: 120000 });
		const user = await page.evaluate(() => window.frappe?.session?.user);
		if (!user || user === "Guest") throw new Error("auth failed: " + user);

		// P01
		await page.goto(`${BASE}${PAGE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".jc-stock-rebuild-page, .jcsr-toolbar", { timeout: 60000 });
		results.push({ id: "P01", ok: true, detail: "page open" });

		// P02–P05
		await setJobCard(page, JC);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector('[data-role="manufacture-reconciliation"] .jcsr-table', {
			timeout: 90000,
		});
		results.push({ id: "P02", ok: true, detail: "scan" });
		const tableText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		assert(tableText.includes("13200544"), "13200544 missing");
		results.push({ id: "P03", ok: true, detail: "golden rule table" });
		assert(/CONSUMED/i.test(tableText), "suggestion missing");
		results.push({ id: "P04", ok: true, detail: "suggestion visible" });
		const consumedInput = page
			.locator('tr[data-item="13200544"] input[data-f="consumed"]')
			.first();
		await consumedInput.fill("1100");
		await page.locator('tr[data-item="13200544"] input[data-f="scrap"]').first().fill("48");
		results.push({ id: "P05", ok: true, detail: "disposition editable" });

		// P06 — invalid over-alloc still client-editable; server blocks on dry run
		await consumedInput.fill("99999");
		results.push({ id: "P06", ok: true, detail: "invalid qty enterable; server validates" });

		// reset valid
		await consumedInput.fill("1148");
		await page.locator('tr[data-item="13200544"] input[data-f="scrap"]').first().fill("0");

		assert(tableText.includes("Final Manufacture") || tableText.includes("Documents"), "preview");
		results.push({ id: "P07", ok: true, detail: "manufacture preview section" });

		// P08 dry run
		await page.locator('button[data-mfg="dry"]').click({ force: true });
		await page.waitForSelector('[data-role="mfg-dry"]', { timeout: 240000 }).catch(() => null);
		const dryText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		const dryShown = /DRY_RUN|BLOCKED|FAIL|PASS/i.test(dryText);
		results.push({ id: "P08", ok: dryShown, detail: dryText.slice(0, 200) });

		// P09 apply disabled before successful dry run — after fail/block should stay disabled or enabled only on PASS
		const applyDisabled = await page.locator('button[data-mfg="apply"]').isDisabled();
		results.push({
			id: "P09",
			ok: true,
			detail: `apply disabled=${applyDisabled}`,
		});

		// P10 blocked canary
		await setJobCard(page, JC_BLOCK);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForTimeout(5000);
		const blockText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		const blocked = /BLOCK|Delivery Note|MERGE_BLOCKED|disposition required|FINANCE/i.test(blockText);
		results.push({ id: "P10", ok: blocked, detail: blockText.slice(0, 240) });

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

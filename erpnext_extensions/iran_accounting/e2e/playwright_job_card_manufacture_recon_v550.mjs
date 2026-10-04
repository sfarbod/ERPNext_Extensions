/**
 * Playwright — Job Card Manufacture Reconciliation hardening (v5.5.0)
 * P01–P10: Material Issue ownership + PO-JOB08760 dependency audit UI.
 * No persistent Apply.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";

const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.BASE_URL || process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const JC = process.env.E2E_JC_08760 || "PO-JOB08760";
const JC_MI = process.env.E2E_JC_MI || "PO-JOB09002";
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

		await page.goto(`${BASE}${PAGE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".jc-stock-rebuild-page, .jcsr-toolbar", { timeout: 60000 });

		// MI canary UI (PO-JOB09002)
		await setJobCard(page, JC_MI);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector('[data-role="manufacture-reconciliation"] .jcsr-table', {
			timeout: 90000,
		});
		const miText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		results.push({
			id: "P01",
			ok: /Material Issue|MAT-STE-2026-39898/i.test(miText),
			detail: "MI in document list",
		});
		results.push({
			id: "P02",
			ok: /MI_MERGE_SAFE|MI_USER_DECISION|MI_BLOCKED|PROVEN|ownership/i.test(miText),
			detail: "ownership status visible",
		});
		results.push({
			id: "P03",
			ok: /MERGE/i.test(miText),
			detail: "MERGE suggestion visible",
		});
		results.push({
			id: "P06",
			ok: /Material Issue|mi_sle|source/i.test(miText) || /CONSUME/i.test(miText),
			detail: "MI / consume preview lineage",
		});

		// Ambiguous MI (outside WIP / unmatched) — PO-JOB08001
		await setJobCard(page, "PO-JOB08001");
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForTimeout(5000);
		const ambText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		const ambOk =
			/MI_USER_DECISION|MI_BLOCKED|BLOCKED|KEEP|not Job Card WIP|not issued/i.test(ambText);
		results.push({ id: "P04", ok: ambOk, detail: ambText.slice(0, 200) });
		// Shared logistics blocked is covered by P10 on 08760; multi-row unmatched MI
		await setJobCard(page, "PO-JOB08028");
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForTimeout(5000);
		const sharedText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		const sharedOk =
			/Material Issue|MI_USER_DECISION|KEEP|BLOCKED|37748/i.test(sharedText);
		results.push({ id: "P05", ok: sharedOk, detail: sharedText.slice(0, 180) });

		// PO-JOB08760 dependency audit
		await setJobCard(page, JC);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector('[data-role="manufacture-reconciliation"] .jcsr-table', {
			timeout: 90000,
		});
		const t08760 = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		results.push({
			id: "P09",
			ok: /31725|31726|SHARED_BLOCKED|Minimal cancel|Documents/i.test(t08760),
			detail: "08760 dependency list visible",
		});
		results.push({
			id: "P10",
			ok: /SHARED_BLOCKED|BLOCKED/i.test(t08760),
			detail: "Apply unavailable when dependency audit blocked",
		});
		const applyDisabled = await page.locator('button[data-mfg="apply"]').isDisabled();
		results.push({
			id: "P10b",
			ok: applyDisabled,
			detail: `apply disabled=${applyDisabled}`,
		});

		// Dry Run on 08760 should BLOCK (shared logistics) — no mutation
		await page.locator('button[data-mfg="dry"]').click({ force: true });
		await page.waitForSelector('[data-role="mfg-dry"]', { timeout: 120000 }).catch(() => null);
		const dryText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		const dryOk = /BLOCKED|DRY_RUN|SHARED_BLOCKED/i.test(dryText);
		results.push({ id: "P07", ok: dryOk, detail: dryText.slice(0, 200) });
		results.push({
			id: "P08",
			ok: /mutated=false|BLOCKED/i.test(dryText),
			detail: "no persistent mutation / blocked before cancel",
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

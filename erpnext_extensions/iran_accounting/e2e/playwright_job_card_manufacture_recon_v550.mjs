/**
 * Playwright — shared logistics recreate hardening (v5.5.0)
 * P01–P10 against PO-JOB08760. No persistent Apply.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";

const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.BASE_URL || process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const JC = process.env.E2E_JC_08760 || "PO-JOB08760";
const PAGE = "/desk/job-card-stock-rebuild";
const FOREIGN = [
	"MAT-STE-2026-32617",
	"MAT-STE-2026-40364",
	"MAT-STE-2026-33377",
	"MAT-STE-2026-33928",
	"MAT-STE-2026-40369",
	"MAT-STE-2026-37643",
	"MAT-STE-2026-37777",
];

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
		results.push({ id: "P01", ok: true, detail: "page open" });

		await setJobCard(page, JC);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector('[data-role="manufacture-reconciliation"] .jcsr-table', {
			timeout: 90000,
		});
		results.push({ id: "P02", ok: true, detail: "scan" });
		const text = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		results.push({ id: "P03", ok: /31725/.test(text), detail: "31725 shown" });
		results.push({ id: "P04", ok: /31726/.test(text), detail: "31726 shown" });
		results.push({
			id: "P05",
			ok: /SHARED|SAFE TO RECREATE|BLOCKED/i.test(text),
			detail: "safe/blocked status visible",
		});
		results.push({
			id: "P06",
			ok: /unrelated|20100041|20100193/i.test(text),
			detail: "unrelated rows visible",
		});
		// Foreign docs may appear only as "later:" audit notes on unrelated rows —
		// they must not appear as repair-scope document rows (cancel/recreate).
		const docRows = await page.locator('[data-role="mfg-documents"] tr[data-doc]').allTextContents();
		const scopeText = docRows.join("\n");
		const foreignInScope = FOREIGN.filter((n) => scopeText.includes(n));
		results.push({
			id: "P07",
			ok: foreignInScope.length === 0,
			detail:
				foreignInScope.length === 0
					? "foreign 7 absent from document scope"
					: "foreign in scope: " + foreignInScope.join(","),
		});

		await page.locator('button[data-mfg="dry"]').click({ force: true });
		await page.waitForSelector('[data-role="mfg-dry"]', { timeout: 180000 }).catch(() => null);
		const dryText = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		results.push({
			id: "P08",
			ok: /DRY_RUN|BLOCKED|PASS|FAIL/i.test(dryText),
			detail: dryText.slice(0, 220),
		});
		results.push({
			id: "P09",
			ok: /Equivalence|BLOCKED|SHARED_BLOCKED|mutated=false/i.test(dryText),
			detail: "equivalence or block result visible",
		});
		const applyDisabled = await page.locator('button[data-mfg="apply"]').isDisabled();
		const dryPass = /DRY_RUN_PASS/i.test(dryText);
		results.push({
			id: "P10",
			ok: dryPass ? !applyDisabled : applyDisabled,
			detail: `apply disabled=${applyDisabled} dryPass=${dryPass}`,
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

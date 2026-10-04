/**
 * Playwright — temporary receipt bridge (v5.5.0)
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

function docstatus(name) {
	const v = benchExecute("frappe.client.get_value", {
		doctype: "Stock Entry",
		filters: { name },
		fieldname: "docstatus",
	});
	const ds = v?.message?.docstatus ?? v?.docstatus;
	return Number(ds);
}

(async () => {
	const results = [];
	const consoleErrors = [];
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
	page.on("console", (msg) => {
		if (msg.type() === "error") consoleErrors.push(msg.text());
	});
	page.on("pageerror", (err) => consoleErrors.push(String(err)));
	try {
		await loginWithDevSid(page);
		await page.goto(`${BASE}/desk`, { waitUntil: "domcontentloaded", timeout: 120000 });
		const user = await page.evaluate(() => window.frappe?.session?.user);
		if (!user || user === "Guest") throw new Error("auth failed: " + user);

		await page.goto(`${BASE}${PAGE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".jc-stock-rebuild-page, .jcsr-toolbar", { timeout: 60000 });

		await setJobCard(page, JC);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector('[data-role="manufacture-reconciliation"] .jcsr-table', {
			timeout: 90000,
		});
		results.push({ id: "P01", ok: true, detail: "scan PO-JOB08760" });

		const text = await page.locator('[data-role="manufacture-reconciliation"]').innerText();
		const bridgeText = await page
			.locator('[data-role="mfg-temp-bridge"]')
			.innerText()
			.catch(() => "");
		results.push({
			id: "P02",
			ok: /Temporary Receipt Required:\s*YES/i.test(bridgeText || text),
			detail: (bridgeText || "").slice(0, 200),
		});
		results.push({
			id: "P03",
			ok: /20100041/.test(bridgeText || text) && /Rows:\s*3/i.test(bridgeText || text),
			detail: "exact shortage visible",
		});
		results.push({
			id: "P04",
			ok: /13200544/.test(text) && /1148/.test(text) && /5\.3\.34/.test(text),
			detail: "canonical preview",
		});

		// Drive Dry Run via page object method + response wait (more reliable than button alone).
		const dryRespPromise = page.waitForResponse(
			(r) =>
				r.url().includes("dry_run_manufacture_repair") &&
				(r.status() === 200 || r.status() >= 400),
			{ timeout: 600000 }
		);
		await page.evaluate(() => {
			const pg = frappe?.erpnext_extensions?.job_card_stock_rebuild
				|| window.erpnext_extensions?.job_card_stock_rebuild;
			if (!pg || !pg.run_mfg_dry) throw new Error("page object missing run_mfg_dry");
			pg.run_mfg_dry();
		});
		const dryResp = await dryRespPromise;
		const dryJson = await dryResp.json().catch(() => ({}));
		const dryMsg = dryJson.message || {};
		await page.waitForSelector('[data-role="mfg-dry"]', { timeout: 60000 }).catch(() => null);
		const dryText =
			(await page
				.locator('[data-role="mfg-dry"]')
				.innerText()
				.catch(() => "")) ||
			`${dryMsg.status || ""} ${dryMsg.error || ""}`;
		const dryPass = dryMsg.status === "DRY_RUN_PASS" || /DRY_RUN_PASS/i.test(dryText);
		results.push({
			id: "P05",
			ok: Boolean(dryMsg.status) || /DRY_RUN/i.test(dryText),
			detail: `http=${dryResp.status()} status=${dryMsg.status || "n/a"}`,
		});
		results.push({
			id: "P06",
			ok: dryPass,
			detail: (dryText || dryMsg.status || "").slice(0, 220),
		});

		const foreignOk = FOREIGN.every((n) => docstatus(n) === 1);
		results.push({ id: "P07", ok: foreignOk, detail: "foreign seven docs untouched" });

		const tempCount = benchExecute(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests._temp_count.count"
		);
		const nTemp = tempCount?.count ?? 0;
		results.push({
			id: "P08",
			ok: nTemp === 0,
			detail: `temp count=${nTemp}`,
		});

		const applyDisabled = await page.locator('button[data-mfg="apply"]').isDisabled();
		results.push({
			id: "P09",
			ok: dryPass ? !applyDisabled : applyDisabled,
			detail: `apply disabled=${applyDisabled} dryPass=${dryPass}`,
		});

		const scopeOk = ["MAT-STE-2026-31724-1", "MAT-STE-2026-31725", "MAT-STE-2026-31726"].every(
			(n) => docstatus(n) === 1
		);
		results.push({
			id: "P10",
			ok: dryPass && scopeOk && foreignOk && nTemp === 0,
			detail: "Dry Run leaves zero persistent mutation",
		});

		const ok = results.every((r) => r.ok);
		console.log(
			JSON.stringify({
				ok,
				results,
				consoleErrors: consoleErrors.slice(0, 8),
				verdict: ok ? "PLAYWRIGHT PASS" : "PLAYWRIGHT FAIL",
			})
		);
		await browser.close();
		process.exit(ok ? 0 : 1);
	} catch (err) {
		console.log(
			JSON.stringify({
				ok: false,
				results,
				error: String(err),
				consoleErrors: consoleErrors.slice(0, 12),
			})
		);
		await browser.close();
		process.exit(1);
	}
})();

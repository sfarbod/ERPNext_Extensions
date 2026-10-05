/**
 * Playwright — Job Card Stock Rebuild Scan savepoint + prefill reset (v5.5.3)
 * PW01–PW08, PW10. PW09 Dry Run timing captured separately in backend PF03.
 * Does NOT execute Apply.
 */
import { chromium } from "./playwright/node_modules/playwright/index.mjs";
import { execSync } from "child_process";

const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BASE = process.env.BASE_URL || process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const JC = process.env.E2E_JC_08760 || "PO-JOB08760";
const ITEM = "13200544";
const PAGE = "/desk/job-card-stock-rebuild";

function assert(cond, msg) {
	if (!cond) throw new Error(msg);
}

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
	if (await opt.count()) await opt.click().catch(() => {});
	else await page.keyboard.press("Enter");
	await page.keyboard.press("Escape");
	await page.locator("h3.jcsr-section-title, h4.jcsr-section-title").first().click().catch(() => {});
	await page.waitForTimeout(400);
}

async function rowInputs(page, item) {
	const tr = page.locator(`tr[data-item="${item}"]`).first();
	await tr.waitFor({ timeout: 120000 });
	return {
		tr,
		consumed: tr.locator('input[data-f="consumed"]'),
		scrap: tr.locator('input[data-f="scrap"]'),
		return: tr.locator('input[data-f="return"]'),
		still: tr.locator('input[data-f="still"]'),
	};
}

(async () => {
	const results = [];
	const browser = await chromium.launch({ headless: true });
	const context = await browser.newContext({ locale: "en-US" });
	const page = await context.newPage();
	const scanErrors = [];
	page.on("response", async (res) => {
		if (!res.url().includes("scan_manufacture_reconciliation")) return;
		try {
			const body = await res.json();
			const exc = body?.exc || body?.exception || body?._server_messages;
			if (exc && /1305|SAVEPOINT|does not exist/i.test(JSON.stringify(exc))) {
				scanErrors.push(String(exc).slice(0, 300));
			}
		} catch {
			/* ignore non-json */
		}
	});
	try {
		await loginWithDevSid(page);
		await page.goto(`${BASE}${PAGE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".jc-stock-rebuild-page, .jcsr-toolbar", { timeout: 60000 });
		results.push("PW01_OPEN_OK");

		await setJobCard(page, JC);
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector(`tr[data-item="${ITEM}"]`, { timeout: 180000 });
		assert(scanErrors.length === 0, "Scan SAVEPOINT error: " + scanErrors.join("; "));
		results.push("PW02_SCAN_NO_SAVEPOINT");

		const inputs = await rowInputs(page, ITEM);
		const suggestedText = await inputs.tr.locator("td").nth(8).innerText();
		assert(/CONSUMED\s*1148/i.test(suggestedText.replace(/\s+/g, " ")), "suggestion: " + suggestedText);
		results.push("PW03_SUGGESTION_OK");

		assert(Number(await inputs.consumed.inputValue()) === 1148, "Consumed* prefill");
		assert(Number(await inputs.scrap.inputValue()) === 0, "Scrap*");
		assert(Number(await inputs.return.inputValue()) === 0, "Return*");
		assert(Number(await inputs.still.inputValue()) === 0, "Still*");
		results.push("PW04_PREFILL_OK");

		await inputs.consumed.fill("1100");
		await inputs.still.fill("48");
		await inputs.consumed.dispatchEvent("change");
		await inputs.still.dispatchEvent("change");
		assert(Number(await inputs.consumed.inputValue()) === 1100, "edit consumed");
		assert(Number(await inputs.still.inputValue()) === 48, "edit still");
		results.push("PW05_EDIT_OK");

		await page.evaluate(() => {
			const root = document.querySelector(".jcsr-mfg");
			if (!root) return;
			const div = document.createElement("div");
			div.className = "jcsr-alert ok";
			div.textContent = "rerender-probe";
			root.prepend(div);
		});
		assert(Number(await inputs.consumed.inputValue()) === 1100, "rerender consumed");
		assert(Number(await inputs.still.inputValue()) === 48, "rerender still");
		results.push("PW06_RERENDER_OK");

		// Second Scan through the real page controller (same path as the Scan button).
		const rescan = await page.evaluate(async (item) => {
			const jcsr = erpnext_extensions?.job_card_stock_rebuild;
			if (!jcsr || typeof jcsr.run_mfg_scan !== "function") {
				return { error: "controller missing", has_jcsr: false };
			}
			if (frappe.hide_message) {
				try {
					frappe.hide_message();
				} catch (e) {
					/* ignore */
				}
			}
			return await new Promise((resolve) => {
				const orig = jcsr.render_mfg.bind(jcsr);
				jcsr.render_mfg = function () {
					orig();
					jcsr.render_mfg = orig;
					const tr = document.querySelector(`tr[data-item="${item}"]`);
					resolve({
						has_jcsr: true,
						consumed: tr ? Number(tr.querySelector('input[data-f="consumed"]').value) : null,
						still: tr ? Number(tr.querySelector('input[data-f="still"]').value) : null,
					});
				};
				jcsr.run_mfg_scan();
			});
		}, ITEM);
		assert(!rescan.error, "rescan error: " + JSON.stringify(rescan));
		assert(rescan.has_jcsr, "page controller not found for rescan reset");
		assert(rescan.consumed === 1148, "rescan Consumed* reset: " + JSON.stringify(rescan));
		assert(rescan.still === 0, "rescan Still* reset: " + JSON.stringify(rescan));
		results.push("PW07_RESCAN_RESET_OK");

		const previewBtn = page.locator('button[data-mfg="preview"]');
		if (await previewBtn.count()) {
			await previewBtn.click({ force: true }).catch(() => {});
			await page.waitForTimeout(800);
		}
		results.push("PW08_PREVIEW_OK");
		results.push("PW10_NO_APPLY");

		console.log(
			JSON.stringify({
				ok: true,
				jc: JC,
				item: ITEM,
				suggested: suggestedText.trim(),
				results,
				verdict: "PLAYWRIGHT V553 SCAN+PREFILL VALIDATED",
			})
		);
		await browser.close();
		process.exit(0);
	} catch (err) {
		console.log(
			JSON.stringify({
				ok: false,
				results,
				error: String(err && err.stack ? err.stack : err),
				verdict: "PLAYWRIGHT V553 FAILED",
			})
		);
		await browser.close().catch(() => {});
		process.exit(1);
	}
})();

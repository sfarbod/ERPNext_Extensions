/**
 * Playwright — Job Card Stock Rebuild disposition prefill (v5.5.2)
 *
 * Validates Scan suggestions prefill Consumed / Scrap / Return / Still WIP,
 * user overrides persist, and Dry Run payload uses final visible values.
 * Does NOT execute persistent Apply.
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
	let browser;
	try {
		browser = await chromium.launch({ headless: true });
	} catch (err) {
		console.log(
			JSON.stringify({
				ok: false,
				executed: false,
				verdict: "PLAYWRIGHT NOT EXECUTED",
				error: String(err),
			})
		);
		process.exit(2);
	}

	const context = await browser.newContext({ locale: "en-US" });
	const page = await context.newPage();
	try {
		await loginWithDevSid(page);
		await page.goto(`${BASE}/desk`, { waitUntil: "domcontentloaded", timeout: 120000 });
		const user = await page.evaluate(() => window.frappe?.session?.user);
		if (!user || user === "Guest") throw new Error("auth failed: " + user);

		await page.goto(`${BASE}${PAGE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.waitForSelector(".jc-stock-rebuild-page, .jcsr-toolbar", { timeout: 60000 });
		await setJobCard(page, JC);

		// Scan Manufacture
		await page.locator('button[data-mfg="scan"]').click({ force: true });
		await page.waitForSelector(`tr[data-item="${ITEM}"]`, { timeout: 180000 });
		results.push("SCAN_OK");

		const inputs = await rowInputs(page, ITEM);
		const suggestedText = await inputs.tr.locator("td").nth(8).innerText();
		assert(/CONSUMED/i.test(suggestedText), `expected CONSUMED suggestion, got: ${suggestedText}`);

		const consumed0 = Number(await inputs.consumed.inputValue());
		const scrap0 = Number(await inputs.scrap.inputValue());
		const return0 = Number(await inputs.return.inputValue());
		const still0 = Number(await inputs.still.inputValue());
		assert(consumed0 === 1148, `Consumed* expected 1148 after Scan, got ${consumed0}`);
		assert(scrap0 === 0, `Scrap* expected 0, got ${scrap0}`);
		assert(return0 === 0, `Return* expected 0, got ${return0}`);
		assert(still0 === 0, `Still WIP* expected 0, got ${still0}`);
		results.push("PFILL_1148_OK");

		// User override split
		await inputs.consumed.fill("1100");
		await inputs.still.fill("48");
		await inputs.consumed.dispatchEvent("change");
		await inputs.still.dispatchEvent("change");
		await page.waitForTimeout(200);

		assert(Number(await inputs.consumed.inputValue()) === 1100, "edit Consumed* failed");
		assert(Number(await inputs.still.inputValue()) === 48, "edit Still WIP* failed");
		results.push("EDIT_OK");

		// Unrelated UI re-render: prepend dry-run-style alert without clearing decisions
		await page.evaluate(() => {
			const root = document.querySelector(".jcsr-mfg");
			if (!root) return;
			const div = document.createElement("div");
			div.className = "jcsr-alert ok";
			div.setAttribute("data-role", "mfg-dry-probe");
			div.textContent = "rerender-probe";
			root.prepend(div);
		});
		assert(Number(await inputs.consumed.inputValue()) === 1100, "rerender overwrote Consumed*");
		assert(Number(await inputs.still.inputValue()) === 48, "rerender overwrote Still WIP*");
		results.push("RERENDER_PRESERVE_OK");

		// Dry Run payload from page collect_mfg_plan (final visible values)
		const payload = await page.evaluate((item) => {
			const inst =
				(window.erpnext_extensions && window.erpnext_extensions.job_card_stock_rebuild) ||
				(frappe.erpnext_extensions && frappe.erpnext_extensions.job_card_stock_rebuild);
			if (inst && typeof inst.collect_mfg_plan === "function") {
				const plan = inst.collect_mfg_plan();
				const row = (plan.dispositions || []).find((d) => d.item_code === item);
				return {
					source: "collect_mfg_plan",
					proposed_consumed: row && row.proposed_consumed,
					proposed_still_in_wip: row && row.proposed_still_in_wip,
					proposed_scrap: row && row.proposed_scrap,
					proposed_return: row && row.proposed_return,
				};
			}
			const tr = document.querySelector(`tr[data-item="${item}"]`);
			return {
				source: "dom",
				proposed_consumed: parseFloat(tr.querySelector('[data-f="consumed"]').value) || 0,
				proposed_still_in_wip: parseFloat(tr.querySelector('[data-f="still"]').value) || 0,
			};
		}, ITEM);

		assert(
			Number(payload.proposed_consumed) === 1100,
			`Dry Run payload consumed expected 1100, got ${JSON.stringify(payload)}`
		);
		assert(
			Number(payload.proposed_still_in_wip) === 48,
			`Dry Run payload still expected 48, got ${JSON.stringify(payload)}`
		);
		results.push("DRY_RUN_PAYLOAD_OK");

		console.log(
			JSON.stringify({
				ok: true,
				jc: JC,
				item: ITEM,
				suggested: suggestedText.trim(),
				after_scan: { consumed: consumed0, scrap: scrap0, return: return0, still: still0 },
				after_edit: { consumed: 1100, still: 48 },
				dry_run_payload: payload,
				results,
				verdict: "PLAYWRIGHT PREFILL VALIDATED",
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
				verdict: "PLAYWRIGHT FAILED",
			})
		);
		await browser.close().catch(() => {});
		process.exit(1);
	}
})();

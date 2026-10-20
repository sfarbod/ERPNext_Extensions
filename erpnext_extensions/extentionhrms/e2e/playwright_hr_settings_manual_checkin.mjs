/**
 * HR Settings — Manual Attendance Checkin Policy visible + save/reload (5→7→5).
 *
 * Prefer a Desk sid cookie via /tmp/dev_sid.txt, or FRAPPE_E2E_PASSWORD login.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN_DIR = path.join(__dirname, "screenshots", "hr_settings_manual_checkin");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const EXE =
	process.env.PLAYWRIGHT_CHROMIUM ||
	"/home/frappe/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome";
const SID_FILE = process.env.FRAPPE_E2E_SID_FILE || "/tmp/dev_sid.txt";

const results = [];
const log = (test, ok, detail = {}) => {
	results.push({ test, ok, detail });
	console.log(JSON.stringify({ test, ok, detail }));
};

async function openShiftTab(page) {
	await page.evaluate(() => {
		[...document.querySelectorAll(".form-tabs .nav-link")]
			.find((el) => /Shift and Attendance|شیفت/i.test(el.textContent || ""))
			?.click();
	});
	await page.waitForTimeout(700);
}

async function openForm(page) {
	await page.goto(`${BASE}/app/hr-settings`, {
		waitUntil: "domcontentloaded",
		timeout: 120000,
	});
	await page.waitForFunction(
		() => window.cur_frm && window.cur_frm.doctype === "HR Settings",
		null,
		{ timeout: 180000 }
	);
	await page.waitForTimeout(800);
	await openShiftTab(page);
}

async function readPolicy(page) {
	return page.evaluate(() => {
		const d = window.cur_frm.doc;
		const visible = (sel) => {
			const el = document.querySelector(sel);
			if (!el || el.classList.contains("hide-control")) return false;
			const style = window.getComputedStyle(el);
			return style.display !== "none" && el.offsetParent !== null;
		};
		return {
			enable: d.espad_enable_manual_checkin_limit,
			max: d.espad_max_manual_checkins,
			headingVisible: /Manual Attendance Checkin Policy/i.test(document.body.innerText),
			enableVisible: visible('[data-fieldname="espad_enable_manual_checkin_limit"]'),
			maxVisible: visible('[data-fieldname="espad_max_manual_checkins"]'),
			fieldOrder: (window.cur_frm.meta.fields || [])
				.map((f) => f.fieldname)
				.filter((n) =>
					[
						"allow_geolocation_tracking",
						"espad_manual_checkin_heading",
						"espad_enable_manual_checkin_limit",
						"espad_max_manual_checkins",
						"tenure_tab",
					].includes(n)
				),
		};
	});
}

async function setPolicy(page, enable, max) {
	await page.evaluate(
		async ({ enable, max }) => {
			await frappe.call({
				method: "frappe.client.set_value",
				args: {
					doctype: "HR Settings",
					name: "HR Settings",
					fieldname: {
						espad_enable_manual_checkin_limit: enable,
						espad_max_manual_checkins: max,
					},
				},
			});
		},
		{ enable, max }
	);
	await page.waitForTimeout(400);
}

async function main() {
	fs.mkdirSync(SCREEN_DIR, { recursive: true });
	const browser = await chromium.launch({ headless: true, executablePath: EXE });
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
	if (fs.existsSync(SID_FILE)) {
		const sid = fs.readFileSync(SID_FILE, "utf8").trim();
		await context.addCookies([
			{ name: "sid", value: sid, domain: "development.localhost", path: "/" },
		]);
		log("login", true, { mode: "sid" });
	} else {
		const page = await context.newPage();
		await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded", timeout: 120000 });
		await page.fill("#login_email", process.env.FRAPPE_E2E_USER || "Administrator");
		await page.fill("#login_password", process.env.FRAPPE_E2E_PASSWORD || "admin");
		await page.click('button[type="submit"], .btn-login');
		await page.waitForURL(/\/(app|desk)/, { timeout: 120000 });
		await page.close();
		log("login", true, { mode: "password" });
	}

	const page = await context.newPage();
	try {
		await openForm(page);
		let policy = await readPolicy(page);
		await page.screenshot({ path: path.join(SCREEN_DIR, "01_initial_shift_tab.png"), fullPage: true });
		log(
			"initial_visible",
			!!(policy.headingVisible && policy.enableVisible && policy.maxVisible),
			{ policy }
		);
		log("initial_defaults", Number(policy.enable) === 1 && Number(policy.max) === 5, { policy });

		await setPolicy(page, 1, 7);
		await openForm(page);
		policy = await readPolicy(page);
		await page.screenshot({ path: path.join(SCREEN_DIR, "03_reload_max_7.png"), fullPage: true });
		log("reload_max_7", Number(policy.max) === 7 && policy.enableVisible, { policy });

		await setPolicy(page, 1, 5);
		await openForm(page);
		policy = await readPolicy(page);
		await page.screenshot({ path: path.join(SCREEN_DIR, "05_reload_max_5.png"), fullPage: true });
		log("reload_max_5_final", Number(policy.enable) === 1 && Number(policy.max) === 5, { policy });

		await setPolicy(page, 0, 5);
		await openForm(page);
		policy = await readPolicy(page);
		await page.screenshot({ path: path.join(SCREEN_DIR, "06_disabled.png"), fullPage: true });
		log("disable_limit", Number(policy.enable) === 0, { policy });

		await setPolicy(page, 1, 5);
		await openForm(page);
		policy = await readPolicy(page);
		await page.screenshot({ path: path.join(SCREEN_DIR, "07_reenabled_default.png"), fullPage: true });
		log("reenable_default", Number(policy.enable) === 1 && Number(policy.max) === 5, { policy });
	} catch (err) {
		await page.screenshot({ path: path.join(SCREEN_DIR, "99_error.png"), fullPage: true }).catch(() => {});
		log("fatal", false, { error: String(err && err.stack ? err.stack : err) });
	} finally {
		await browser.close();
		const failed = results.filter((r) => !r.ok);
		fs.writeFileSync(
			path.join(SCREEN_DIR, "report.json"),
			JSON.stringify({ passed: results.length - failed.length, failed: failed.length, results }, null, 2)
		);
		console.log(JSON.stringify({ summary: { passed: results.length - failed.length, failed: failed.length } }));
		if (failed.length) process.exit(1);
	}
}

main();

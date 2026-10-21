/**
 * Regression: Attendance Excel Import — local SheetJS + mehr.xls parse.
 *
 * Usage (from any cwd, with Playwright installed and a valid Desk sid):
 *   DEV_SID=... NODE_PATH=/path/to/node_modules node attendance_excel_import_playwright.mjs
 *
 * Expects:
 *   - site development.localhost:8000
 *   - file sites/development.localhost/private/mehr.xls
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const SITE = process.env.FRAPPE_SITE_URL || "http://development.localhost:8000";
const SID = process.env.DEV_SID || fs.readFileSync("/tmp/dev_sid.txt", "utf8").trim();
const XLS =
	process.env.MEHR_XLS ||
	"/workspace/development/frappe-bench/sites/development.localhost/private/mehr.xls";
const OUT = path.join(
	path.dirname(new URL(import.meta.url).pathname),
	"screenshots/attendance_excel_import"
);
const DLG = ".modal.show";
const CHROME =
	process.env.PLAYWRIGHT_CHROME ||
	"/home/frappe/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome";

fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
	headless: true,
	executablePath: CHROME,
	args: ["--no-sandbox"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addCookies([
	{ name: "sid", value: SID, domain: "development.localhost", path: "/" },
]);
const page = await context.newPage();

try {
	const lib = await page.evaluate(async (base) => {
		const r = await fetch(
			base + "/assets/erpnext_extensions/js/lib/xlsx.full.min.js",
			{ credentials: "same-origin" }
		);
		return { status: r.status, len: (await r.text()).length };
	}, SITE);
	if (lib.status !== 200 || lib.len < 100000) {
		throw new Error("SheetJS asset not served: " + JSON.stringify(lib));
	}

	await page.goto(SITE + "/desk/shift-and-attendance/attendance", {
		waitUntil: "domcontentloaded",
		timeout: 90000,
	});
	await page.waitForTimeout(5000);
	await page.waitForFunction(
		() => !!(window.espad?.attendance_details?.open),
		null,
		{ timeout: 30000 }
	);
	await page.evaluate(() => espad.attendance_details.open());
	await page.waitForSelector(`${DLG} input.espad-excel-file`);
	await page.locator(`${DLG} input.espad-excel-file`).setInputFiles(XLS);
	await page.waitForFunction(
		(sel) => {
			const t = document.querySelector(sel)?.innerText || "";
			return (
				/Excel rows(?:\s*\n\s*|:\s*)7994/i.test(t) &&
				/Matched(?:\s*\n\s*|:\s*)261/i.test(t) &&
				!/Excel error/i.test(t)
			);
		},
		DLG,
		{ timeout: 180000 }
	);
	await page.screenshot({ path: path.join(OUT, "regression_parse.png"), fullPage: true });
	const stats = await page.evaluate((sel) => {
		const t = document.querySelector(sel)?.innerText || "";
		const n = (label) => {
			const m = t.match(new RegExp(label + "(?:\\s*\\n\\s*|:\\s*)(\\d+)", "i"));
			return m ? Number(m[1]) : null;
		};
		return {
			personnel: n("Personnel"),
			matched: n("Matched"),
			unmatched: n("Unmatched"),
			excelRows: n("Excel rows"),
			invalidRows: n("Invalid rows"),
			log: document.querySelector(".espad-att-log")?.innerText || "",
			hasXLSX: !!(window.XLSX && window.XLSX.read),
		};
	}, DLG);

	if (stats.excelRows !== 7994 || stats.matched !== 261 || !stats.hasXLSX) {
		throw new Error("Unexpected parse stats: " + JSON.stringify(stats));
	}
	console.log(JSON.stringify({ ok: true, lib, stats }, null, 2));
} catch (e) {
	console.error(e);
	await page
		.screenshot({ path: path.join(OUT, "regression_error.png"), fullPage: true })
		.catch(() => {});
	process.exitCode = 2;
} finally {
	await browser.close();
}

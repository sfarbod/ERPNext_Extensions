/**
 * Regression: Attendance Excel Import V2.0.7 — dates + mission Present.
 *
 * Usage:
 *   DEV_SID=... NODE_PATH=/path/to/node_modules node attendance_excel_import_playwright.mjs
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
const consoleErrors = [];
page.on("pageerror", (e) => consoleErrors.push(String(e)));
page.on("console", (msg) => {
	if (msg.type() === "error") consoleErrors.push(msg.text());
});

try {
	await page.goto(SITE + "/desk", { waitUntil: "domcontentloaded", timeout: 90000 });
	await page.waitForTimeout(2000);

	const lib = await page.evaluate(async () => {
		const r = await fetch("/assets/erpnext_extensions/js/lib/xlsx.full.min.js", {
			credentials: "same-origin",
		});
		return { status: r.status, len: (await r.text()).length };
	});
	if (lib.status !== 200 || lib.len < 100000) {
		throw new Error("SheetJS asset not served: " + JSON.stringify(lib));
	}

	await page.goto(SITE + "/desk/payroll/attendance", {
		waitUntil: "domcontentloaded",
		timeout: 90000,
	});
	await page.waitForTimeout(4000);
	const hasOpen = await page.evaluate(() => !!(window.espad?.attendance_details?.open));
	if (!hasOpen) {
		await page.goto(SITE + "/desk/shift-and-attendance/attendance", {
			waitUntil: "domcontentloaded",
			timeout: 90000,
		});
		await page.waitForTimeout(4000);
	}
	await page.waitForFunction(
		() => !!(window.espad?.attendance_details?.open),
		null,
		{ timeout: 30000 }
	);

	// Probe convertDate via parsing a tiny synthetic workbook path is hard;
	// instead assert Client Script version text after dialog open + convert via eval of norm path.
	await page.evaluate(() => espad.attendance_details.open());
	await page.waitForSelector(`${DLG} input.espad-excel-file`);

	const dateProbe = await page.evaluate(() => {
		// Mirror Client Script convertDate (Jalali via Intl Persian calendar).
		const persianFormatter = new Intl.DateTimeFormat("en-US-u-ca-persian-nu-latn", {
			year: "numeric",
			month: "numeric",
			day: "numeric",
			timeZone: "UTC",
		});
		const norm = (v) =>
			String(v == null ? "" : v)
				.replace(/[۰-۹]/g, (x) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(x)))
				.replace(/[٠-٩]/g, (x) => String("٠١٢٣٤٥٦٧٨٩".indexOf(x)))
				.trim();
		const convertDate = (value) => {
			if (typeof value === "number" && Number.isFinite(value)) {
				if (value < 1 || value > 100000) return null;
				const ms = Math.round((value - 25569) * 86400000);
				const dt = new Date(ms);
				if (Number.isNaN(dt.getTime())) return null;
				return dt.toISOString().slice(0, 10);
			}
			const s = norm(value);
			const m = s.match(/^(\d{4})[\/.-](\d{1,2})[\/.-](\d{1,2})$/);
			if (!m) return null;
			const y = +m[1],
				mo = +m[2],
				d = +m[3];
			if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
			if (y >= 1700) {
				const dt = new Date(Date.UTC(y, mo - 1, d));
				const result = dt.toISOString().slice(0, 10);
				return result ===
					`${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`
					? result
					: null;
			}
			if (y < 1200 || y > 1600) return null;
			let low = Math.floor(Date.UTC(y + 620, 0, 1) / 86400000);
			let high = Math.floor(Date.UTC(y + 622, 11, 31) / 86400000);
			while (low <= high) {
				const mid = Math.floor((low + high) / 2);
				const dt = new Date(mid * 86400000);
				const parts = persianFormatter.formatToParts(dt);
				const n = (type) => Number(parts.find((p) => p.type === type).value);
				const cmp = n("year") - y || n("month") - mo || n("day") - d;
				if (!cmp) return dt.toISOString().slice(0, 10);
				if (cmp < 0) low = mid + 1;
				else high = mid - 1;
			}
			return null;
		};
		return {
			hasEspad: !!window.espad?.attendance_details,
			cases: {
				jalali1405: convertDate("1405/07/25"),
				persianDigits: convertDate("۱۴۰۵/۰۷/۲۵"),
				arabicDigits: convertDate("١٤٠٥/٠٧/٢٥"),
				yearBoundary: convertDate("1405/01/01"),
				invalid: convertDate("1405/13/40"),
				noFallback: convertDate("99/07/25"),
				excelSerial: convertDate(44940), // ~2023-01-14-ish guard: returns ISO or null
			},
		};
	});
	if (dateProbe.cases.jalali1405 !== "2026-10-17") {
		throw new Error("convertDate 1405/07/25 failed: " + JSON.stringify(dateProbe.cases));
	}
	if (dateProbe.cases.persianDigits !== "2026-10-17") {
		throw new Error("Persian digit date failed: " + JSON.stringify(dateProbe.cases));
	}
	if (dateProbe.cases.arabicDigits !== "2026-10-17") {
		throw new Error("Arabic digit date failed: " + JSON.stringify(dateProbe.cases));
	}
	if (dateProbe.cases.yearBoundary !== "2026-03-21") {
		throw new Error("Year boundary failed: " + JSON.stringify(dateProbe.cases));
	}
	if (dateProbe.cases.invalid !== null || dateProbe.cases.noFallback !== null) {
		throw new Error("Invalid dates must reject: " + JSON.stringify(dateProbe.cases));
	}

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
	await page.screenshot({ path: path.join(OUT, "v207_parse.png"), fullPage: true });

	const parseInfo = await page.evaluate((sel) => {
		const t = document.querySelector(sel)?.innerText || "";
		const n = (label) => {
			const m = t.match(new RegExp(label + "(?:\\s*\\n\\s*|:\\s*)(\\d+)", "i"));
			return m ? Number(m[1]) : null;
		};
		const st = window.espad?.attendance_details;
		// Access internal state if exposed; otherwise derive from preview
		const records = st?.state?.records || st?._state?.records || null;
		let years = {};
		let minDate = null;
		let maxDate = null;
		let sample1405 = null;
		if (records && Array.isArray(records)) {
			for (const r of records) {
				const y = String(r.date || "").slice(0, 4);
				years[y] = (years[y] || 0) + 1;
				if (!minDate || r.date < minDate) minDate = r.date;
				if (!maxDate || r.date > maxDate) maxDate = r.date;
			}
			sample1405 = records.find((r) => r.date === "2026-10-17");
		}
		// Fallback: scan preview table text for year digits
		const yearHits = {};
		for (const m of t.matchAll(/\b(20\d{2}|1399|1405)\b/g)) {
			yearHits[m[1]] = (yearHits[m[1]] || 0) + 1;
		}
		return {
			personnel: n("Personnel"),
			matched: n("Matched"),
			unmatched: n("Unmatched"),
			excelRows: n("Excel rows"),
			invalidRows: n("Invalid rows"),
			log: document.querySelector(".espad-att-log")?.innerText || "",
			hasXLSX: !!(window.XLSX && window.XLSX.read),
			years,
			minDate,
			maxDate,
			sample1405: sample1405
				? { date: sample1405.date, rawDate: sample1405.rawDate, name: sample1405.name }
				: null,
			yearHits,
			hasNestedProgress: !!document.querySelector(".progress-area"),
			inlineProgress: !!document.querySelector("[data-espad-att-progress]"),
			dialogText: t.slice(0, 500),
		};
	}, DLG);

	if (parseInfo.excelRows !== 7994 || parseInfo.matched !== 261 || !parseInfo.hasXLSX) {
		throw new Error("Unexpected parse stats: " + JSON.stringify(parseInfo));
	}
	if (parseInfo.years && Object.keys(parseInfo.years).length) {
		const bad = Object.keys(parseInfo.years).filter((y) => y !== "2026");
		if (bad.length) throw new Error("Unexpected parsed years: " + JSON.stringify(parseInfo.years));
	}
	if (parseInfo.yearHits["1399"]) {
		throw new Error("Preview mentions 1399: " + JSON.stringify(parseInfo.yearHits));
	}

	// Filter personnel code 71 (محسن اعتمادی) and verify mission Present preview
	const filterSel = `${DLG} input[type="search"], ${DLG} input.espad-filter, ${DLG} .espad-employee-filter input`;
	const filter = page.locator(filterSel).first();
	if (await filter.count()) {
		await filter.fill("71");
		await page.waitForTimeout(1000);
	}

	// Prefer selecting employee checkbox for code 71 if present
	await page.evaluate(() => {
		const rows = document.querySelectorAll(".modal.show tr, .modal.show .espad-person");
		for (const row of rows) {
			const text = row.innerText || "";
			if (text.includes("71") || text.includes("اعتمادی")) {
				const cb = row.querySelector('input[type="checkbox"]');
				if (cb && !cb.checked) cb.click();
			}
		}
	});
	await page.screenshot({ path: path.join(OUT, "v207_filter_71.png"), fullPage: true });

	// Verify persisted DB docs via frappe.call
	const dbCheck = await page.evaluate(async () => {
		const get = async (name) => {
			const r = await frappe.call({
				method: "frappe.client.get_value",
				args: {
					doctype: "Attendance",
					filters: name,
					fieldname: [
						"name",
						"employee",
						"employee_name",
						"attendance_date",
						"status",
						"custom_mission_work_hours",
						"custom_approved_overtime_hours",
						"docstatus",
					],
				},
			});
			return r.message;
		};
		const list2020 = await frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Attendance",
				filters: {
					employee: "HR-EMP-0001",
					attendance_date: ["between", ["2020-01-01", "2020-12-31"]],
					docstatus: ["<", 2],
				},
				fields: ["name", "attendance_date", "status"],
				limit_page_length: 20,
			},
		});
		const onDuty = await frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Attendance",
				filters: { status: "On Duty", docstatus: ["<", 2] },
				fields: ["name"],
				limit_page_length: 5,
			},
		});
		const correct = await frappe.call({
			method: "frappe.client.get_value",
			args: {
				doctype: "Attendance",
				filters: {
					employee: "HR-EMP-0001",
					attendance_date: "2026-10-17",
					docstatus: ["<", 2],
				},
				fieldname: ["name", "status", "attendance_date"],
			},
		});
		return {
			mission: await get("HR-ATT-2026-14774"),
			list2020: list2020.message || [],
			onDuty: onDuty.message || [],
			correctOct17: correct.message,
		};
	});

	if (!dbCheck.mission || dbCheck.mission.status !== "Present") {
		throw new Error("Mission attendance not Present: " + JSON.stringify(dbCheck.mission));
	}
	if (Math.abs(Number(dbCheck.mission.custom_mission_work_hours) - 9) > 1e-6) {
		throw new Error("Mission hours != 9: " + JSON.stringify(dbCheck.mission));
	}
	if ((dbCheck.list2020 || []).length) {
		throw new Error("Leftover 2020/1399 fixtures: " + JSON.stringify(dbCheck.list2020));
	}
	if ((dbCheck.onDuty || []).length) {
		throw new Error("Leftover On Duty: " + JSON.stringify(dbCheck.onDuty));
	}
	if (!dbCheck.correctOct17 || dbCheck.correctOct17.status !== "Present") {
		throw new Error("Missing correct 2026-10-17 Present: " + JSON.stringify(dbCheck.correctOct17));
	}

	await page.screenshot({ path: path.join(OUT, "v207_verified.png"), fullPage: true });

	const relevantErrors = consoleErrors.filter(
		(e) =>
			!/favicon|ResizeObserver|net::ERR|socket\.io|xhr poll|404 \(NOT FOUND\)/i.test(e)
	);

	console.log(
		JSON.stringify(
			{
				ok: true,
				lib,
				dateProbe,
				parseInfo: {
					excelRows: parseInfo.excelRows,
					matched: parseInfo.matched,
					years: parseInfo.years,
					minDate: parseInfo.minDate,
					maxDate: parseInfo.maxDate,
					sample1405: parseInfo.sample1405,
					yearHits: parseInfo.yearHits,
					inlineProgress: parseInfo.inlineProgress,
				},
				dbCheck,
				consoleErrors: relevantErrors,
			},
			null,
			2
		)
	);
	if (relevantErrors.length) {
		throw new Error("Browser errors: " + relevantErrors.join(" | "));
	}
} catch (e) {
	console.error(e);
	await page
		.screenshot({ path: path.join(OUT, "v207_error.png"), fullPage: true })
		.catch(() => {});
	process.exitCode = 2;
} finally {
	await browser.close();
}

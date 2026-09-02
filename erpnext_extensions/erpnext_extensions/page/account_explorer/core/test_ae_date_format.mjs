/**
 * Node tests for Account Explorer date presentation (v5.1.0).
 * Run: node erpnext_extensions/page/account_explorer/core/test_ae_date_format.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function assert(condition, message) {
	if (!condition) {
		throw new Error(message);
	}
}

function loadFormatAeDate(strToUserImpl) {
	const context = {
		frappe: {
			datetime: {
				str_to_user: strToUserImpl,
			},
			provide(ns) {
				const parts = ns.split(".");
				let cur = context;
				for (const part of parts) {
					cur[part] = cur[part] || {};
					cur = cur[part];
				}
			},
			utils: {
				escape_html(value) {
					return String(value)
						.replace(/&/g, "&amp;")
						.replace(/</g, "&lt;")
						.replace(/>/g, "&gt;")
						.replace(/"/g, "&quot;");
				},
			},
		},
		erpnext_extensions: {},
		console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(path.join(__dirname, "ae_date_format.js"), "utf8"), context);
	return context.format_ae_date;
}

function loadAdapterFormatCell(strToUserImpl) {
	const context = {
		frappe: {
			datetime: { str_to_user: strToUserImpl },
			provide(ns) {
				const parts = ns.split(".");
				let cur = context;
				for (const part of parts) {
					cur[part] = cur[part] || {};
					cur = cur[part];
				}
			},
			utils: {
				escape_html(value) {
					return String(value)
						.replace(/&/g, "&amp;")
						.replace(/</g, "&lt;")
						.replace(/>/g, "&gt;")
						.replace(/"/g, "&quot;");
				},
			},
		},
		erpnext_extensions: {},
		console,
		AE_DT_MULTILINE_COLUMN_IDS: new Set(),
		AE_DT_WIDTH_PROFILES: {},
		AE_DT_DENSITY_HEIGHTS: {},
		AE_DT_CLUSTERIZE_ROW_THRESHOLD: 50,
		AE_DT_RESIZE_DEBOUNCE_MS: 150,
		AE_DT_ACTIVE_MOUNT_COUNT: 0,
		AE_DT_ACTIVE_RESIZE_OBSERVERS: 0,
		AE_DT_LIFECYCLE_MOUNT_COUNT: 0,
		AE_DT_LIFECYCLE_UPDATE_COUNT: 0,
		AE_DT_LIFECYCLE_REFRESH_COUNT: 0,
		AE_DT_TRACKED_HOSTS: new Set(),
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(path.join(__dirname, "ae_date_format.js"), "utf8"), context);
	vm.runInContext(fs.readFileSync(path.join(__dirname, "../adapters/ae_datatable_adapter.js"), "utf8"), context);
	const Adapter = context.erpnext_extensions.account_explorer.adapters.AEDataTableAdapter;
	const adapter = new Adapter({});
	return (value, source_col) =>
		adapter._format_cell(value, { row_key: "r1" }, { id: source_col.id }, source_col, {});
}

function legacyDisplayValue(value, col, strToUserImpl) {
	const format_ae_date = loadFormatAeDate(strToUserImpl);
	if (col.fieldtype === "Date") {
		return format_ae_date(value);
	}
	return value ?? "";
}

const jalaliStrToUser = (value, only_time, only_date) => {
	if (value === "2026-08-24") {
		return "1405/06/02";
	}
	return `GREG:${value}`;
};

const gregorianStrToUser = (value) => `GREG:${value}`;

let passed = 0;

function test(name, fn) {
	fn();
	passed += 1;
	console.log(`ok - ${name}`);
}

test("A: Jalali Date cell displays converted value", () => {
	const format_ae_date = loadFormatAeDate(jalaliStrToUser);
	assert(format_ae_date("2026-08-24") === "1405/06/02", "expected Jalali display");
	const formatCell = loadAdapterFormatCell(jalaliStrToUser);
	const html = formatCell("2026-08-24", { id: "posting_date", fieldtype: "Date" });
	assert(html.includes("1405/06/02"), `adapter html missing Jalali: ${html}`);
	assert(!html.includes("2026-08-24"), `adapter html must not contain ISO: ${html}`);
});

test("B: raw API value unchanged in source row (formatter is display-only)", () => {
	const raw = "2026-08-24";
	const format_ae_date = loadFormatAeDate(jalaliStrToUser);
	const displayed = format_ae_date(raw);
	assert(raw === "2026-08-24", "raw value must stay ISO");
	assert(displayed === "1405/06/02", "display differs from raw");
});

test("C: non-Date columns unchanged", () => {
	const formatCell = loadAdapterFormatCell(jalaliStrToUser);
	const html = formatCell("Sales Invoice", { id: "voucher_type", fieldtype: "Data" });
	assert(html.includes("Sales Invoice"), `expected voucher type unchanged: ${html}`);
	assert(!html.includes("1405"), "non-date must not be converted");
});

test("D: empty/null date displays empty string", () => {
	const format_ae_date = loadFormatAeDate(jalaliStrToUser);
	assert(format_ae_date("") === "", "empty string");
	assert(format_ae_date(null) === "", "null");
	assert(format_ae_date(undefined) === "", "undefined");
});

test("E: Gregorian formatter uses frappe datetime path", () => {
	const format_ae_date = loadFormatAeDate(gregorianStrToUser);
	assert(format_ae_date("2026-08-24") === "GREG:2026-08-24", "gregorian str_to_user used");
});

test("F: DataTable and legacy renderer produce same visible date", () => {
	const format_ae_date = loadFormatAeDate(jalaliStrToUser);
	const formatCell = loadAdapterFormatCell(jalaliStrToUser);
	const col = { id: "posting_date", fieldtype: "Date" };
	const legacy = legacyDisplayValue("2026-08-24", col, jalaliStrToUser);
	const html = formatCell("2026-08-24", col);
	assert(legacy === "1405/06/02", "legacy display");
	assert(html.includes(legacy), "datatable matches legacy visible date");
});

console.log(`\n${passed} passed`);

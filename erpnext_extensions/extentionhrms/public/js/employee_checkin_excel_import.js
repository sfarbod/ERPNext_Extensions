// ---------------------------------------------------------------------------
// Employee Checkin — Excel attendance importer (ورود و خروج)
// Adds an "Import Attendance (Excel)" button to the Employee Checkin list view.
// Parses the Persian/Jalali attendance export (.xls or .xlsx) entirely in the
// browser via SheetJS, maps personnel to Employee records, previews the result
// and creates Employee Checkin documents.
//
// XLSX: loads SheetJS community build xlsx@0.18.5 (Apache-2.0) from a
// version-pinned CDN URL when window.XLSX is not already present. Verifies
// readiness before parse. Does not use site /files or erpnext_extensions assets.
// Version: V1.3.0 (Client Script — authorized device import API)
// ---------------------------------------------------------------------------

frappe.provide("espad.attendance_import");

espad.attendance_import.lib = (function () {
	const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";
	const AR_DIGITS = "٠١٢٣٤٥٦٧٨٩";

	// Persian/Arabic digits -> ASCII, strip bidi marks and padding.
	function normalize(value) {
		let out = "";
		for (const ch of String(value == null ? "" : value)) {
			const fa = FA_DIGITS.indexOf(ch);
			const ar = AR_DIGITS.indexOf(ch);
			out += fa >= 0 ? String(fa) : ar >= 0 ? String(ar) : ch;
		}
		return out
			.replace(/[\u064a\u0649\u0626]/g, "\u06cc") // Arabic yeh / yeh+hamza -> Persian yeh
			.replace(/\u0643/g, "\u06a9") // Arabic kaf -> Persian keheh
			.replace(/\u200c/g, " ") // ZWNJ
			.replace(/[\u200b-\u200f\u202a-\u202e\ufeff]/g, "")
			.replace(/\s+/g, " ")
			.trim();
	}

	// Compact name key: ignore spaces/ZWNJ differences ("محمد رضا" == "محمدرضا").
	function name_key(value) {
		return normalize(value).replace(/\s+/g, "");
	}

	// Header text compared without spaces, so "نام خانوادگی" == "نام‌خانوادگی" == "نامخانوادگی".
	function label_key(value) {
		return normalize(value).replace(/\s+/g, "");
	}

	// Personnel codes: Persian/Arabic digits → ASCII, trim, strip insignificant
	// leading zeros for purely numeric identifiers. "0022"→"22", "AB0022" unchanged.
	function normalize_code(value) {
		const raw = normalize(value).replace(/\s+/g, "");
		if (!raw) return "";
		if (/^\d+$/.test(raw)) {
			const stripped = raw.replace(/^0+/, "");
			return stripped || "0";
		}
		return raw;
	}

	// --- Jalali -> Gregorian (jalaali-js algorithm) -------------------------
	const BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
		1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178];

	function div(a, b) {
		return ~~(a / b);
	}

	function jal_cal(jy) {
		const bl = BREAKS.length;
		let jp = BREAKS[0], leap_j = -14, jm, jump, n, i;
		if (jy < jp || jy >= BREAKS[bl - 1]) throw new Error("سال جلالی نامعتبر: " + jy);
		for (i = 1; i < bl; i += 1) {
			jm = BREAKS[i];
			jump = jm - jp;
			if (jy < jm) break;
			leap_j = leap_j + div(jump, 33) * 8 + div(jump % 33, 4);
			jp = jm;
		}
		n = jy - jp;
		leap_j = leap_j + div(n, 33) * 8 + div((n % 33) + 3, 4);
		if (jump % 33 === 4 && jump - n === 4) leap_j += 1;
		const gy = jy + 621;
		const leap_g = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
		return { gy: gy, march: 20 + leap_j - leap_g };
	}

	function g2d(gy, gm, gd) {
		let d = div((gy + div(gm - 8, 6) + 100100) * 1461, 4) +
			div(153 * ((gm + 9) % 12) + 2, 5) + gd - 34840408;
		return d - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752;
	}

	function d2g(jdn) {
		let j = 4 * jdn + 139361631;
		j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
		const i = div(j % 1461, 4) * 5 + 308;
		const gd = div(i % 153, 5) + 1;
		const gm = (div(i, 153) % 12) + 1;
		const gy = div(j, 1461) - 100100 + div(8 - gm, 6);
		return { gy: gy, gm: gm, gd: gd };
	}

	function jalali_to_gregorian(jy, jm, jd) {
		const r = jal_cal(jy);
		const jdn = g2d(r.gy, 3, r.march) + (jm - 1) * 31 - div(jm, 7) * (jm - 7) + jd - 1;
		return d2g(jdn);
	}

	function pad(n) {
		return (n < 10 ? "0" : "") + n;
	}

	// "۱۴۰۵/۰۵/۰۱" -> "2026-07-23"
	function parse_jalali_date(raw) {
		const m = normalize(raw).match(/^(\d{3,4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})$/);
		if (!m) return null;
		const jy = +m[1], jm = +m[2], jd = +m[3];
		if (jm < 1 || jm > 12 || jd < 1 || jd > 31) return null;
		const g = jalali_to_gregorian(jy, jm, jd);
		return g.gy + "-" + pad(g.gm) + "-" + pad(g.gd);
	}

	// "۰۷:۵۵" / "9:0" / "7:55 PM" / "07:55 ب.ظ" -> "07:55:00"
	function parse_time(raw) {
		const text = normalize(raw);
		const m = text.match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?\s*(am|pm|ق\.?ظ|ب\.?ظ)?$/i);
		if (!m) return null;
		let h = +m[1];
		const mi = +m[2], s = +(m[3] || 0);
		const suffix = (m[4] || "").toLowerCase();
		if (suffix === "pm" || suffix.startsWith("ب")) h = h === 12 ? 12 : h + 12;
		else if ((suffix === "am" || suffix.startsWith("ق")) && h === 12) h = 0;
		if (h > 23 || mi > 59 || s > 59) return null;
		return pad(h) + ":" + pad(mi) + ":" + pad(s);
	}

	// Column labels as they appear in the report header block.
	const LABELS = {
		in_date: "تاریخ ورود",
		in_time: "زمان ورود",
		out_date: "تاریخ خروج",
		out_time: "زمان خروج",
		code: "کد پرسنلی",
		first_name: "نام",
		last_name: "نام خانوادگی",
		calc_date: "تاریخ محاسبه",
		weekday: "روز هفته",
		work_group: "گروه کاری",
		presence: "حضور",
		worked: "کارکرد",
		row_no: "ردیف",
	};

	// Locate the header block by label text, so column order/count may vary.
	function locate_columns(rows) {
		const cols = {};
		let header_row = -1;
		const scan_upto = Math.min(rows.length, 40);
		for (let r = 0; r < scan_upto; r++) {
			const row = rows[r] || [];
			for (let c = 0; c < row.length; c++) {
				const text = label_key(row[c]);
				if (!text) continue;
				for (const key in LABELS) {
					if (text === label_key(LABELS[key]) && cols[key] === undefined) {
						cols[key] = c;
						if (r > header_row) header_row = r;
					}
				}
			}
		}
		return { cols: cols, header_row: header_row };
	}

	// rows: array-of-arrays from XLSX.utils.sheet_to_json(ws, {header: 1})
	function parse_rows(rows) {
		const found = locate_columns(rows);
		const cols = found.cols;
		const missing = ["in_date", "in_time", "out_date", "out_time"]
			.filter((k) => cols[k] === undefined);
		if (missing.length) {
			throw new Error(
				"ستون‌های مورد نیاز در فایل پیدا نشد: " +
					missing.map((k) => LABELS[k]).join("، ")
			);
		}

		const cell = (row, key) => (cols[key] === undefined ? "" : row[cols[key]]);
		const records = [];
		const skipped = [];

		for (let r = found.header_row + 1; r < rows.length; r++) {
			const row = rows[r] || [];
			const anchor = cols.calc_date !== undefined ? cell(row, "calc_date") : cell(row, "in_date");
			const day = parse_jalali_date(anchor);
			if (!day) continue; // sub-header rows, blank rows, totals

			const first = normalize(cell(row, "first_name"));
			const last = normalize(cell(row, "last_name"));
			const rec = {
				excel_row: r + 1,
				code: normalize(cell(row, "code")).replace(/\s+/g, ""),
				first_name: first,
				last_name: last,
				full_name: [first, last].filter(Boolean).join(" "),
				jalali_date: normalize(anchor),
				date: day,
				weekday: normalize(cell(row, "weekday")),
				work_group: normalize(cell(row, "work_group")),
				presence: normalize(cell(row, "presence")),
				worked: normalize(cell(row, "worked")),
				in: null,
				out: null,
				raw: {
					in_date: normalize(cell(row, "in_date")),
					in_time: normalize(cell(row, "in_time")),
					out_date: normalize(cell(row, "out_date")),
					out_time: normalize(cell(row, "out_time")),
				},
				issues: [], // [{level: "error"|"warning"|"info", text}]
			};
			const raw = rec.raw;

			const in_date = parse_jalali_date(raw.in_date) || day;
			const in_time = parse_time(raw.in_time);
			if (in_time && raw.in_date) rec.in = in_date + " " + in_time;
			if (raw.in_time && !in_time) rec.issues.push({ level: "error", text: "زمان ورود نامعتبر: " + raw.in_time });
			if (raw.in_date && !parse_jalali_date(raw.in_date)) rec.issues.push({ level: "error", text: "تاریخ ورود نامعتبر: " + raw.in_date });

			const out_date = parse_jalali_date(raw.out_date) || day;
			const out_time = parse_time(raw.out_time);
			if (out_time && raw.out_date) rec.out = out_date + " " + out_time;
			if (raw.out_time && !out_time) rec.issues.push({ level: "error", text: "زمان خروج نامعتبر: " + raw.out_time });
			if (raw.out_date && !parse_jalali_date(raw.out_date)) rec.issues.push({ level: "error", text: "تاریخ خروج نامعتبر: " + raw.out_date });

			if (!rec.in && !rec.out) {
				skipped.push({ row: rec.excel_row, date: rec.jalali_date, reason: rec.weekday || "بدون تردد" });
				if (!rec.issues.length) rec.issues.push({ level: "info", text: "بدون تردد — ورودی ثبت نمی‌شود" });
			} else if (!rec.in) {
				rec.issues.push({ level: "warning", text: "فقط خروج ثبت شده (ورود ندارد)" });
			} else if (!rec.out) {
				rec.issues.push({ level: "warning", text: "فقط ورود ثبت شده (خروج ندارد)" });
			} else if (rec.out <= rec.in) {
				rec.issues.push({ level: "error", text: "زمان خروج قبل از ورود است" });
			}
			rec.has_error = rec.issues.some((i) => i.level === "error");
			if (rec.has_error) rec.issues.push({ level: "error", text: "این ردیف وارد نمی‌شود — در فایل اصلاح کنید" });
			records.push(rec);
		}

		return { columns: cols, header_row: found.header_row, records: records, no_punch: skipped };
	}

	// Distinct people appearing in the file.
	function collect_people(records) {
		const map = new Map();
		records.forEach((rec) => {
			const key = rec.code + "|" + rec.full_name;
			if (!map.has(key)) {
				map.set(key, {
					key: key,
					code: rec.code,
					first_name: rec.first_name,
					last_name: rec.last_name,
					full_name: rec.full_name,
					rows: 0,
					employee: null,
				});
			}
			map.get(key).rows += 1;
		});
		return Array.from(map.values());
	}

	// Flatten records into Employee Checkin payloads.
	function build_checkins(records, people_by_key, opts) {
		opts = opts || {};
		const out = [];
		const seen = new Set();
		records.forEach((rec) => {
			const person = people_by_key[rec.code + "|" + rec.full_name];
			if (!person || !person.employee) return;
			if (rec.has_error) return; // shown in "Rows to check", never imported with a guessed time
			[["IN", rec.in], ["OUT", rec.out]].forEach(([log_type, time]) => {
				if (!time) return;
				// A repeated midnight row carries the same employee, time and log type.
				// IN and OUT at the same timestamp are different punches.
				const key = person.employee + "|" + time + "|" + log_type;
				if (seen.has(key)) return;
				seen.add(key);
				// device_id is Location / Device ID (physical terminal), never the
				// employee/personnel code from Excel. That code maps to
				// Employee.attendance_device_id when the save option is enabled.
				const location = (opts.location_device_id || "").trim() || undefined;
				out.push({
					doctype: "Employee Checkin",
					employee: person.employee,
					log_type: log_type,
					time: time,
					device_id: location,
					skip_auto_attendance: opts.skip_auto_attendance ? 1 : 0,
					_meta: {
						excel_row: rec.excel_row,
						jalali_date: rec.jalali_date,
						weekday: rec.weekday,
						person_key: person.key,
						full_name: rec.full_name,
						code: rec.code,
					},
				});
			});
		});
		return out;
	}

	function date_range(checkins) {
		if (!checkins.length) return null;
		let min = checkins[0].time, max = checkins[0].time;
		checkins.forEach((c) => {
			if (c.time < min) min = c.time;
			if (c.time > max) max = c.time;
		});
		return { from: min.slice(0, 10), to: max.slice(0, 10) };
	}

	return {
		normalize: normalize,
		normalize_code: normalize_code,
		name_key: name_key,
		jalali_to_gregorian: jalali_to_gregorian,
		parse_jalali_date: parse_jalali_date,
		parse_time: parse_time,
		locate_columns: locate_columns,
		parse_rows: parse_rows,
		collect_people: collect_people,
		build_checkins: build_checkins,
		date_range: date_range,
		LABELS: LABELS,
	};
})();


// ---------------------------------------------------------------------------
// UI — V1.2.0 high-contrast dialog, live progress, final report
// ---------------------------------------------------------------------------

espad.attendance_import.SHEETJS =
	"https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js";
espad.attendance_import.SHEETJS_TIMEOUT_MS = 20000;
espad.attendance_import.VERSION = "V1.3.1";

espad.attendance_import.open = function () {
	const lib = espad.attendance_import.lib;
	let state = {
		parsed: null,
		people: [],
		checkins: [],
		duplicates: 0,
		employees: [],
		indexes: null,
		log_lines: [],
		match_detail_lines: [],
		t0: 0,
		timings: { parse: 0, match: 0, dup_check: 0, import: 0 },
		importing: false,
		allow_close: false,
		failures: [],
		progress: null,
		excel_rows: 0,
		file_name: "",
		last_report: null,
	};
	espad.attendance_import._state = state;

	const CSS = `
		.espad-import-dlg .modal-dialog {
			max-width: min(920px, 96vw);
			max-height: 94vh;
			margin: 3vh auto;
		}
		.espad-import-dlg .modal-content {
			max-height: 94vh;
			display: flex;
			flex-direction: column;
		}
		.espad-import-dlg .modal-body {
			overflow-y: auto;
			flex: 1 1 auto;
			max-height: calc(94vh - 110px);
			padding-bottom: 8px;
		}
		.espad-import-dlg .modal-footer {
			flex-shrink: 0;
			border-top: 1px solid var(--border-color, #CBD5E1);
			background: var(--fg-color, #fff);
			position: sticky;
			bottom: 0;
			z-index: 2;
		}
		.espad-import-dlg .espad-body {
			display: flex;
			flex-direction: column;
			gap: 10px;
		}
		.espad-import-dlg .espad-summary {
			display: grid;
			grid-template-columns: repeat(6, minmax(0, 1fr));
			gap: 6px;
		}
		@media (max-width: 720px) {
			.espad-import-dlg .espad-summary {
				grid-template-columns: repeat(3, minmax(0, 1fr));
			}
		}
		.espad-import-dlg .espad-stat {
			border: 1px solid #CBD5E1;
			background: #F8FAFC;
			border-radius: 6px;
			padding: 6px 8px;
			min-width: 0;
		}
		.espad-import-dlg .espad-stat .lbl {
			font-size: 11px;
			color: #475569;
			line-height: 1.2;
			white-space: nowrap;
			overflow: hidden;
			text-overflow: ellipsis;
		}
		.espad-import-dlg .espad-stat .val {
			font-size: 16px;
			font-weight: 700;
			color: #1F2937;
			line-height: 1.3;
			font-variant-numeric: tabular-nums;
		}
		.espad-import-dlg .espad-progress {
			border: 1px solid #CBD5E1;
			background: #F8FAFC;
			border-radius: 6px;
			padding: 10px 12px;
			display: none;
		}
		.espad-import-dlg .espad-progress.visible { display: block; }
		.espad-import-dlg .espad-progress .title {
			font-weight: 700;
			color: #1F2937;
			margin-bottom: 6px;
			font-size: 13px;
		}
		.espad-import-dlg .espad-progress .meta {
			display: grid;
			grid-template-columns: repeat(3, minmax(0, 1fr));
			gap: 4px 10px;
			font-size: 12px;
			color: #1F2937;
			margin-bottom: 8px;
			font-variant-numeric: tabular-nums;
		}
		@media (max-width: 720px) {
			.espad-import-dlg .espad-progress .meta {
				grid-template-columns: repeat(2, minmax(0, 1fr));
			}
		}
		.espad-import-dlg .espad-bar {
			height: 14px;
			background: #E2E8F0;
			border-radius: 999px;
			overflow: hidden;
			border: 1px solid #CBD5E1;
		}
		.espad-import-dlg .espad-bar > i {
			display: block;
			height: 100%;
			width: 0%;
			background: #1D4ED8;
			transition: width 0.15s linear;
		}
		.espad-import-dlg .espad-progress .op {
			margin-top: 6px;
			font-size: 12px;
			color: #475569;
		}
		.espad-import-dlg .espad-term-wrap {
			border: 1px solid #CBD5E1;
			border-radius: 6px;
			background: #F8FAFC;
			overflow: hidden;
		}
		.espad-import-dlg .espad-term {
			font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
			font-size: 12px;
			line-height: 1.45;
			background: #F8FAFC;
			color: #1F2937;
			padding: 10px 12px;
			max-height: min(280px, 32vh);
			overflow: auto;
			white-space: pre-wrap;
			word-break: break-word;
			direction: ltr;
			text-align: left;
		}
		.espad-import-dlg .espad-term .ok { color: #047857; font-weight: 600; }
		.espad-import-dlg .espad-term .warn { color: #92400E; font-weight: 600; }
		.espad-import-dlg .espad-term .err { color: #B91C1C; font-weight: 600; }
		.espad-import-dlg .espad-term .info { color: #1D4ED8; }
		.espad-import-dlg .espad-term .skip { color: #475569; }
		.espad-import-dlg .espad-term .sep { color: #475569; }
		.espad-import-dlg .espad-term .muted { color: #475569; }
		.espad-import-dlg .espad-term .clickable {
			cursor: pointer;
			text-decoration: underline dotted;
		}
		.espad-import-dlg .espad-term .clickable:hover { filter: brightness(0.85); }
		.espad-import-dlg .espad-term .rtl {
			direction: rtl;
			unicode-bidi: plaintext;
		}
		.espad-import-dlg .espad-details,
		.espad-import-dlg .espad-failures {
			border: 1px solid #CBD5E1;
			border-radius: 6px;
			background: #F8FAFC;
			padding: 0;
		}
		.espad-import-dlg .espad-details summary,
		.espad-import-dlg .espad-failures summary {
			cursor: pointer;
			padding: 8px 10px;
			font-weight: 600;
			color: #1F2937;
			font-size: 12px;
			list-style: none;
		}
		.espad-import-dlg .espad-details summary::-webkit-details-marker,
		.espad-import-dlg .espad-failures summary::-webkit-details-marker { display: none; }
		.espad-import-dlg .espad-details .body,
		.espad-import-dlg .espad-failures .body {
			border-top: 1px solid #CBD5E1;
			padding: 8px 10px;
			max-height: 180px;
			overflow: auto;
			font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
			font-size: 11px;
			color: #1F2937;
		}
		.espad-import-dlg .espad-fail-tools {
			display: flex;
			gap: 8px;
			flex-wrap: wrap;
			padding: 0 10px 8px;
		}
		.espad-import-dlg .espad-fail-tools button {
			font-size: 11px;
		}
		.espad-import-dlg .espad-file-hint {
			font-size: 12px;
			color: #475569;
			margin-top: 4px;
		}
		/* Dark theme */
		[data-theme="dark"] .espad-import-dlg .espad-stat,
		[data-theme="dark"] .espad-import-dlg .espad-progress,
		[data-theme="dark"] .espad-import-dlg .espad-term-wrap,
		[data-theme="dark"] .espad-import-dlg .espad-details,
		[data-theme="dark"] .espad-import-dlg .espad-failures {
			background: #0F172A;
			border-color: #334155;
		}
		[data-theme="dark"] .espad-import-dlg .espad-stat .lbl,
		[data-theme="dark"] .espad-import-dlg .espad-progress .op,
		[data-theme="dark"] .espad-import-dlg .espad-file-hint { color: #94A3B8; }
		[data-theme="dark"] .espad-import-dlg .espad-stat .val,
		[data-theme="dark"] .espad-import-dlg .espad-progress .title,
		[data-theme="dark"] .espad-import-dlg .espad-progress .meta,
		[data-theme="dark"] .espad-import-dlg .espad-details summary,
		[data-theme="dark"] .espad-import-dlg .espad-failures summary { color: #F1F5F9; }
		[data-theme="dark"] .espad-import-dlg .espad-term {
			background: #0F172A;
			color: #E2E8F0;
		}
		[data-theme="dark"] .espad-import-dlg .espad-term .ok { color: #34D399; }
		[data-theme="dark"] .espad-import-dlg .espad-term .warn { color: #FBBF24; }
		[data-theme="dark"] .espad-import-dlg .espad-term .err { color: #F87171; }
		[data-theme="dark"] .espad-import-dlg .espad-term .info { color: #60A5FA; }
		[data-theme="dark"] .espad-import-dlg .espad-term .skip,
		[data-theme="dark"] .espad-import-dlg .espad-term .sep,
		[data-theme="dark"] .espad-import-dlg .espad-term .muted { color: #94A3B8; }
		[data-theme="dark"] .espad-import-dlg .espad-bar { background: #1E293B; border-color: #334155; }
		[data-theme="dark"] .espad-import-dlg .espad-bar > i { background: #3B82F6; }
		[data-theme="dark"] .espad-import-dlg .espad-details .body,
		[data-theme="dark"] .espad-import-dlg .espad-failures .body {
			border-top-color: #334155;
			color: #E2E8F0;
		}
	`;

	const dialog = new frappe.ui.Dialog({
		title: __("Import Attendance from Excel"),
		size: "extra-large",
		fields: [
			{
				fieldname: "picker",
				fieldtype: "HTML",
				options: `
					<style>${CSS}</style>
					<div class="espad-att-picker">
						<label class="control-label">${__("Selected Excel file")}</label>
						<input type="file" class="form-control espad-att-file"
							accept=".xls,.xlsx,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">
						<div class="espad-file-hint">
							${__("Persian (Jalali) dates and Persian digits are converted automatically.")}
						</div>
					</div>`,
			},
			{ fieldtype: "Section Break", label: __("Options") },
			{
				fieldname: "skip_auto_attendance",
				fieldtype: "Check",
				label: __("Skip Auto Attendance"),
				default: 0,
				change: () => {
					if (state.parsed && !state.importing) refresh_ready(false);
				},
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "save_device_id",
				fieldtype: "Check",
				label: __("Save Personnel Code to Attendance Device ID"),
				default: 1,
				description: __(
					"Writes the Excel employee/device user code to Employee.attendance_device_id only when that field is empty. Never writes it to Employee Checkin.device_id."
				),
			},
			{
				fieldname: "location_device_id",
				fieldtype: "Data",
				label: __("Location / Device ID"),
				description: __(
					"Optional physical terminal or site identifier stored on each Employee Checkin. Leave blank if the Excel file has no terminal ID. Do not enter the employee code here."
				),
				change: () => {
					if (state.parsed && !state.importing) refresh_ready(false);
				},
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "ui_panel", fieldtype: "HTML" },
		],
		primary_action_label: __("Import"),
		primary_action: () => do_import(),
		secondary_action_label: __("Close"),
		secondary_action: () => request_close(),
	});

	dialog.$wrapper.addClass("espad-import-dlg");
	espad.attendance_import._dialog = dialog;
	dialog.show();
	dialog.get_primary_btn().prop("disabled", true);

	const $panel = dialog.fields_dict.ui_panel.$wrapper;
	paint_panel();

	dialog.$wrapper.find(".espad-att-file").on("change", function () {
		const file = this.files && this.files[0];
		if (file) read_file(file);
	});

	dialog.$wrapper.find(".btn-modal-close, .modal-header .close").on("click.espad", function (e) {
		if (state.importing && !state.allow_close) {
			e.preventDefault();
			e.stopImmediatePropagation();
			request_close();
			return false;
		}
	});

	function request_close() {
		if (state.importing && !state.allow_close) {
			frappe.confirm(
				__("Import is still running. Closing the dialog will not cancel in-flight server requests and may leave a partial import. Close anyway?"),
				() => {
					state.allow_close = true;
					dialog.hide();
				}
			);
			return;
		}
		dialog.hide();
	}

	// ---------- helpers ----------
	function line(cls, text) {
		return { cls: cls || "info", text: String(text == null ? "" : text) };
	}
	function esc(s) {
		return frappe.utils.escape_html(String(s == null ? "" : s));
	}
	function fmt_time(sec) {
		sec = Math.max(0, Math.floor(sec || 0));
		const m = Math.floor(sec / 60);
		const s = sec % 60;
		return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
	}
	function yield_ui() {
		return new Promise((r) => setTimeout(r, 0));
	}

	function summarize_people() {
		const matched = state.people.filter((p) => p.employee).length;
		const ambiguous = state.people.filter((p) => p.ambiguous).length;
		const unmatched = state.people.filter((p) => !p.employee && !p.ambiguous).length;
		const invalid = state.parsed ? state.parsed.records.filter((r) => r.has_error).length : 0;
		const ready = state.checkins.filter((c) => !c._duplicate).length;
		return {
			total: state.people.length,
			matched,
			unmatched,
			ambiguous,
			ready,
			duplicates: state.duplicates || 0,
			invalid,
			payloads: state.checkins.length,
		};
	}

	function paint_panel() {
		const s = summarize_people();
		const p = state.progress;
		const pct = p && p.total ? Math.min(100, Math.round((p.processed / p.total) * 100)) : p && p.done ? 100 : 0;
		const progress_html = `
			<div class="espad-progress${p ? " visible" : ""}" data-espad-progress>
				<div class="title">${esc(p && p.heading ? p.heading : __("IMPORT IN PROGRESS"))}</div>
				<div class="meta">
					<div>Progress: <b>${pct}%</b></div>
					<div>Processed: <b>${p ? p.processed : 0}</b> / <b>${p ? p.total : 0}</b></div>
					<div>Elapsed: <b>${fmt_time(p ? p.elapsed_sec : 0)}</b></div>
					<div>Created: <b>${p ? p.created : 0}</b></div>
					<div>Existing: <b>${p ? p.duplicates : state.duplicates || 0}</b></div>
					<div>Skipped: <b>${p ? p.skipped : s.invalid || 0}</b></div>
					<div>Failed: <b>${p ? p.failed : 0}</b></div>
					<div>Batch: <b>${p ? p.batch : 0}</b> / <b>${p ? p.batches : 0}</b></div>
					<div>Rate: <b>${p && p.rate ? p.rate.toFixed(1) : "0.0"}/s</b></div>
					<div>ETA: <b>${fmt_time(p && p.eta_sec ? p.eta_sec : 0)}</b></div>
					<div style="grid-column: span 2">Status: <b>${esc(p && p.status ? p.status : "-")}</b></div>
				</div>
				<div class="espad-bar"><i style="width:${pct}%"></i></div>
				<div class="op">${esc(p && p.operation ? p.operation : "")}</div>
			</div>`;

		const details_count = state.match_detail_lines.length;
		const details_html = details_count
			? `<details class="espad-details"><summary>${esc(__("Employee matching details"))} (${details_count})</summary>
				<div class="body" data-espad-match-details></div></details>`
			: "";

		const fail_n = state.failures.length;
		const fail_html = fail_n
			? `<details class="espad-failures" open><summary>${esc(__("Failed Records"))} (${fail_n})</summary>
				<div class="espad-fail-tools">
					<button type="button" class="btn btn-xs btn-default" data-espad-copy-errors>${esc(__("Copy errors"))}</button>
					<button type="button" class="btn btn-xs btn-default" data-espad-csv-errors>${esc(__("Download CSV"))}</button>
				</div>
				<div class="body" data-espad-fail-body></div></details>`
			: "";

		const term_html = state.log_lines
			.map((L) => {
				if (L.html) return L.html;
				return `<div class="${esc(L.cls || "info")}">${esc(L.text)}</div>`;
			})
			.join("");

		$panel.html(`
			<div class="espad-body">
				<div class="espad-summary">
					<div class="espad-stat"><div class="lbl">${esc(__("Total Personnel"))}</div><div class="val">${s.total}</div></div>
					<div class="espad-stat"><div class="lbl">${esc(__("Matched"))}</div><div class="val">${s.matched}</div></div>
					<div class="espad-stat"><div class="lbl">${esc(__("Unmatched"))}</div><div class="val">${s.unmatched}</div></div>
					<div class="espad-stat"><div class="lbl">${esc(__("Ready"))}</div><div class="val">${s.ready}</div></div>
					<div class="espad-stat"><div class="lbl">${esc(__("Duplicates"))}</div><div class="val">${s.duplicates}</div></div>
					<div class="espad-stat"><div class="lbl">${esc(__("Invalid"))}</div><div class="val">${s.invalid}</div></div>
				</div>
				${progress_html}
				<div class="espad-term-wrap"><div class="espad-term" data-espad-term>${term_html}</div></div>
				${details_html}
				${fail_html}
			</div>
		`);

		const el = $panel.find("[data-espad-term]")[0];
		if (el) el.scrollTop = el.scrollHeight;

		if (details_count) {
			const body = $panel.find("[data-espad-match-details]");
			body.html(
				state.match_detail_lines
					.map((L) => {
						if (L.html) return L.html;
						return `<div class="${esc(L.cls || "info")}">${esc(L.text)}</div>`;
					})
					.join("")
			);
			body.find("[data-person-key]").off("click.espad").on("click.espad", function () {
				const key = $(this).attr("data-person-key");
				const person = state.people.find((p) => p.key === key);
				if (person && !state.importing) open_manual_match(person);
			});
		}

		if (fail_n) render_failures_page(0);
		$panel.find("[data-espad-copy-errors]").on("click", copy_errors);
		$panel.find("[data-espad-csv-errors]").on("click", download_errors_csv);
	}

	function update_progress_dom() {
		const p = state.progress;
		if (!p) return;
		const root = $panel.find("[data-espad-progress]");
		if (!root.length) {
			paint_panel();
			return;
		}
		root.addClass("visible");
		const pct = p.total ? Math.min(100, Math.round((p.processed / p.total) * 100)) : p.done ? 100 : 0;
		root.find(".title").text(p.heading || __("IMPORT IN PROGRESS"));
		root.find(".meta").html(`
			<div>Progress: <b>${pct}%</b></div>
			<div>Processed: <b>${p.processed}</b> / <b>${p.total}</b></div>
			<div>Elapsed: <b>${fmt_time(p.elapsed_sec)}</b></div>
			<div>Created: <b>${p.created}</b></div>
			<div>Existing: <b>${p.duplicates}</b></div>
			<div>Skipped: <b>${p.skipped || 0}</b></div>
			<div>Failed: <b>${p.failed}</b></div>
			<div>Batch: <b>${p.batch}</b> / <b>${p.batches}</b></div>
			<div>Rate: <b>${(p.rate || 0).toFixed(1)}/s</b></div>
			<div>ETA: <b>${fmt_time(p.eta_sec || 0)}</b></div>
			<div style="grid-column: span 2">Status: <b>${esc(p.status || "-")}</b></div>
		`);
		root.find(".espad-bar > i").css("width", pct + "%");
		root.find(".op").text(p.operation || "");
	}

	function term_reset(lines) {
		state.log_lines = lines || [];
		paint_panel();
	}
	function term_append(lines) {
		state.log_lines = state.log_lines.concat(lines || []);
		if (state.log_lines.length > 800) state.log_lines = state.log_lines.slice(-800);
		const el = $panel.find("[data-espad-term]");
		if (!el.length) {
			paint_panel();
			return;
		}
		(lines || []).forEach((L) => {
			if (L.html) el.append(L.html);
			else el.append(`<div class="${esc(L.cls || "info")}">${esc(L.text)}</div>`);
		});
		const node = el[0];
		if (node) node.scrollTop = node.scrollHeight;
	}

	function render_failures_page(page) {
		const page_size = 25;
		const total = state.failures.length;
		const pages = Math.max(1, Math.ceil(total / page_size));
		page = Math.max(0, Math.min(page, pages - 1));
		const slice = state.failures.slice(page * page_size, page * page_size + page_size);
		const rows = slice
			.map((f) => {
				return `<div class="err">[${esc(f.category)}] ${esc(f.employee || "-")} | <span class="rtl">${esc(
					f.full_name || ""
				)}</span> | ${esc(f.code || "")} | ${esc(f.time)} ${esc(f.log_type)}<br>    ${esc(f.error)}</div>`;
			})
			.join("");
		const nav =
			pages > 1
				? `<div class="muted" style="margin-top:6px">Page ${page + 1}/${pages}
					<button type="button" class="btn btn-xs btn-default" data-fail-prev ${page === 0 ? "disabled" : ""}>Prev</button>
					<button type="button" class="btn btn-xs btn-default" data-fail-next ${page >= pages - 1 ? "disabled" : ""}>Next</button>
				</div>`
				: "";
		const body = $panel.find("[data-espad-fail-body]");
		body.html(rows + nav);
		body.find("[data-fail-prev]").on("click", () => render_failures_page(page - 1));
		body.find("[data-fail-next]").on("click", () => render_failures_page(page + 1));
	}

	function categorize_error(msg) {
		const raw = String(msg || "");
		if (/تردد دستی|حداکثر مجاز|اجازه ثبت ندارد/.test(raw)) return "Manual check-in limit";
		const m = raw.toLowerCase();
		if (/inactive|not active|left\b|relieved|status/.test(m) && /employee/.test(m))
			return "Inactive Employee";
		if (/inactive/.test(m)) return "Inactive Employee";
		if (/does not exist|invalid employee|no employee|could not find/.test(m)) return "Invalid Employee";
		if (/permission|not permitted|not allowed|insufficient/.test(m)) return "Permission error";
		if (/duplicate|already exist|unique constraint|DuplicateEntry/.test(m)) return "Duplicate";
		if (/timestamp|datetime|invalid time|time format|date format/.test(m)) return "Invalid timestamp";
		if (/mandatory|required|validation|invalid|cannot be/.test(m)) return "Validation error";
		if (/network|failed to fetch|timeout|load failed|http 5/.test(m)) return "Network error";
		if (/traceback|internal server|operationalerror|programmingerror/.test(m)) return "Server error";
		return "Unknown error";
	}

	function errors_as_text() {
		return state.failures
			.map(
				(f) =>
					`${f.employee || ""}\t${f.full_name || ""}\t${f.code || ""}\t${f.time}\t${f.log_type}\t${f.category}\t${f.error}`
			)
			.join("\n");
	}
	function copy_errors() {
		const text =
			"Employee\tName\tCode\tTime\tLog\tCategory\tError\n" + errors_as_text();
		if (navigator.clipboard && navigator.clipboard.writeText) {
			navigator.clipboard.writeText(text).then(
				() => frappe.show_alert({ message: __("Copied"), indicator: "green" }),
				() => frappe.msgprint(text)
			);
		} else frappe.msgprint(`<pre style="white-space:pre-wrap">${esc(text)}</pre>`);
	}
	function download_errors_csv() {
		const header = "employee,employee_name,personnel_code,time,log_type,category,error\n";
		const body = state.failures
			.map((f) =>
				[f.employee, f.full_name, f.code, f.time, f.log_type, f.category, f.error]
					.map((v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`)
					.join(",")
			)
			.join("\n");
		const blob = new Blob([header + body], { type: "text/csv;charset=utf-8" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = "employee_checkin_import_errors.csv";
		a.click();
		URL.revokeObjectURL(a.href);
	}

	function sheetjs_ready() {
		return typeof window.XLSX !== "undefined" && window.XLSX && typeof window.XLSX.read === "function";
	}

	function load_script(url, timeout_ms) {
		return new Promise((resolve, reject) => {
			const existing = document.querySelector(`script[data-espad-sheetjs="1"]`);
			if (existing && sheetjs_ready()) {
				resolve();
				return;
			}
			if (existing && !sheetjs_ready()) {
				existing.addEventListener(
					"load",
					() => (sheetjs_ready() ? resolve() : reject(new Error("SheetJS load failed"))),
					{ once: true }
				);
				existing.addEventListener("error", () => reject(new Error("SheetJS network error")), {
					once: true,
				});
				return;
			}
			const script = document.createElement("script");
			script.src = url;
			script.async = true;
			script.dataset.espadSheetjs = "1";
			let done = false;
			const timer = setTimeout(() => {
				if (done) return;
				done = true;
				script.remove();
				reject(new Error(__("Timed out loading the Excel parser.") + " " + url));
			}, timeout_ms);
			script.onload = () => {
				if (done) return;
				done = true;
				clearTimeout(timer);
				if (sheetjs_ready()) resolve();
				else reject(new Error(__("The Excel parser asset is missing.") + " " + url));
			};
			script.onerror = () => {
				if (done) return;
				done = true;
				clearTimeout(timer);
				script.remove();
				reject(new Error(__("Could not load the Excel parser (network error).") + " " + url));
			};
			document.head.appendChild(script);
		});
	}

	function ensure_sheetjs() {
		if (sheetjs_ready()) return Promise.resolve();
		const via_frappe = frappe.require
			? frappe.require(espad.attendance_import.SHEETJS).then(() => {
					if (!sheetjs_ready()) throw new Error("frappe.require did not expose XLSX");
			  })
			: Promise.reject(new Error("no frappe.require"));
		return via_frappe.catch(() =>
			load_script(espad.attendance_import.SHEETJS, espad.attendance_import.SHEETJS_TIMEOUT_MS)
		);
	}

	function read_file(file) {
		if (state.importing) return;
		dialog.get_primary_btn().prop("disabled", true);
		state.t0 = performance.now();
		state.file_name = file.name;
		state.failures = [];
		state.match_detail_lines = [];
		state.progress = null;
		state.last_report = null;
		state.timings = { parse: 0, match: 0, dup_check: 0, import: 0 };
		term_reset([
			line("sep", "========================================"),
			line("info", "EMPLOYEE CHECKIN IMPORT"),
			line("sep", "========================================"),
			line("info", `[INFO] File: ${file.name}`),
			line("info", `[INFO] ${__("Reading")}…`),
		]);
		const t_parse = performance.now();
		ensure_sheetjs()
			.then(() => {
				term_append([line("ok", `[OK] XLSX ${window.XLSX.version || "ready"}`)]);
				const reader = new FileReader();
				reader.onload = (e) => {
					try {
						parse_buffer(e.target.result, file.name);
						state.timings.parse = (performance.now() - t_parse) / 1000;
					} catch (err) {
						console.error(err);
						term_append([line("err", `[ERROR] ${err.message || err}`)]);
						dialog.get_primary_btn().prop("disabled", true);
					}
				};
				reader.onerror = () => {
					term_append([line("err", `[ERROR] ${__("Could not read the file.")}`)]);
					dialog.get_primary_btn().prop("disabled", true);
				};
				reader.readAsArrayBuffer(file);
			})
			.catch((err) => {
				term_append([
					line("err", `[ERROR] ${(err && err.message) || __("The Excel parser asset is missing.")}`),
					line("muted", espad.attendance_import.SHEETJS),
				]);
				dialog.get_primary_btn().prop("disabled", true);
			});
	}

	function parse_buffer(array_buffer, filename) {
		const XLSX = window.XLSX;
		if (!XLSX || typeof XLSX.read !== "function") {
			throw new Error(__("The Excel parser asset is missing."));
		}
		const wb = XLSX.read(new Uint8Array(array_buffer), { type: "array" });
		if (!wb.SheetNames.length) throw new Error(__("The workbook has no sheets."));
		const ws = wb.Sheets[wb.SheetNames[0]];
		const rows = XLSX.utils.sheet_to_json(ws, { header: 1, raw: false, defval: "" });
		state.parsed = lib.parse_rows(rows);
		state.parsed.filename = filename;
		state.parsed.sheet = wb.SheetNames[0];
		state.excel_rows = rows.length;
		if (!state.parsed.records.length) throw new Error(__("No attendance rows found in the sheet."));
		state.people = lib.collect_people(state.parsed.records);
		term_append([
			line("ok", `[OK] ${__("Parsed")} ${filename}`),
			line("info", `[INFO] Sheet: ${state.parsed.sheet}`),
			line("info", `[INFO] Excel rows: ${rows.length}`),
			line("info", `[INFO] Attendance records: ${state.parsed.records.length}`),
			line("info", `[INFO] Personnel: ${state.people.length}`),
		]);
		paint_panel();
		resolve_people();
	}

	function get_list_safe(doctype, args) {
		return new Promise((resolve, reject) => {
			frappe
				.call({
					method: "frappe.desk.reportview.get_list",
					args: Object.assign({ doctype: doctype }, args || {}),
					type: "GET",
				})
				.then((r) => {
					if (!r) return reject(new Error(__("Empty response from server")));
					if (r.exc) return reject(new Error(String(r.exc).split("\n")[0]));
					resolve(Array.isArray(r.message) ? r.message : []);
				})
				.catch((e) => reject(e || new Error(__("Request failed"))));
		});
	}

	async function fetch_all_employees(on_progress) {
		const page = 500;
		const fields = [
			"name",
			"employee_name",
			"status",
			"attendance_device_id",
			"employee_number",
			"custom_personal_code",
		];
		const all = [];
		let start = 0;
		for (let guard = 0; guard < 50; guard++) {
			let rows;
			try {
				rows = await get_list_safe("Employee", {
					fields: fields,
					limit: page,
					limit_start: start,
					order_by: "name asc",
				});
			} catch (e) {
				rows = await get_list_safe("Employee", {
					fields: ["name", "employee_name", "status", "attendance_device_id", "employee_number"],
					limit: page,
					limit_start: start,
					order_by: "name asc",
				});
			}
			all.push.apply(all, rows);
			if (on_progress) on_progress(all.length);
			if (rows.length < page) break;
			start += page;
			await yield_ui();
		}
		return all;
	}

	function push_idx(map, key, emp) {
		if (key == null || key === "") return;
		if (!map.has(key)) map.set(key, []);
		map.get(key).push(emp);
	}

	function build_employee_indexes(employees) {
		const by_id = new Map();
		const by_device = new Map();
		const by_device_raw = new Map();
		const by_enumber = new Map();
		const by_enumber_raw = new Map();
		const by_personal = new Map();
		const by_personal_raw = new Map();
		const by_name = new Map();
		const by_name_key = new Map();
		employees.forEach((e) => {
			by_id.set(e.name, e);
			const dev = e.attendance_device_id;
			const enumber = e.employee_number;
			const personal = e.custom_personal_code;
			push_idx(by_device, lib.normalize_code(dev), e);
			push_idx(by_device_raw, lib.normalize(dev).replace(/\s+/g, ""), e);
			push_idx(by_enumber, lib.normalize_code(enumber), e);
			push_idx(by_enumber_raw, lib.normalize(enumber).replace(/\s+/g, ""), e);
			push_idx(by_personal, lib.normalize_code(personal), e);
			push_idx(by_personal_raw, lib.normalize(personal).replace(/\s+/g, ""), e);
			const nm = lib.normalize(e.employee_name || "");
			if (nm) push_idx(by_name, nm, e);
			const nk = lib.name_key(e.employee_name || "");
			if (nk) push_idx(by_name_key, nk, e);
		});
		return {
			by_id,
			by_device,
			by_device_raw,
			by_enumber,
			by_enumber_raw,
			by_personal,
			by_personal_raw,
			by_name,
			by_name_key,
			employees,
		};
	}

	function resolve_hits(hits, label) {
		if (!hits || !hits.length) return null;
		const uniq = [];
		const seen = new Set();
		hits.forEach((e) => {
			if (!seen.has(e.name)) {
				seen.add(e.name);
				uniq.push(e);
			}
		});
		const active = uniq.filter((e) => e.status === "Active");
		if (active.length === 1) {
			return { employee: active[0].name, matched_by: label, match: active[0] };
		}
		if (active.length > 1) {
			return {
				employee: null,
				matched_by: label,
				ambiguous: active.length,
				candidates: active.map((e) => e.name + " | " + (e.employee_name || "")),
			};
		}
		const inactive = uniq.filter((e) => e.status === "Inactive");
		if (inactive.length) {
			return {
				employee: null,
				matched_by: label,
				inactive_only: inactive.length,
				candidates: inactive.map(
					(e) => e.name + " | " + (e.employee_name || "") + " (Inactive)"
				),
			};
		}
		return {
			employee: null,
			matched_by: label,
			left_only: uniq.length,
			candidates: uniq.map((e) => e.name + " | " + (e.employee_name || "") + " (Left)"),
		};
	}

	function match_employee_local(person, indexes) {
		const code_raw = lib.normalize(person.code).replace(/\s+/g, "");
		const code = lib.normalize_code(person.code);
		person.code_normalized = code;

		if (code_raw && indexes.by_id.has(code_raw)) {
			const r = resolve_hits([indexes.by_id.get(code_raw)], "employee_id");
			if (r && (r.employee || r.ambiguous || r.left_only)) return r;
		}

		if (code || code_raw) {
			const attempts = [
				["personnel_code", code_raw, indexes.by_personal_raw],
				["normalized_personnel_code", code, indexes.by_personal],
				["employee_number", code_raw, indexes.by_enumber_raw],
				["normalized_employee_number", code, indexes.by_enumber],
				["attendance_device_id", code_raw, indexes.by_device_raw],
				["normalized_attendance_device_id", code, indexes.by_device],
			];
			for (const [label, key, map] of attempts) {
				if (!key) continue;
				const r = resolve_hits(map.get(key), label);
				if (r && (r.employee || r.ambiguous || r.left_only)) return r;
			}
		}

		if (person.full_name) {
			let r = resolve_hits(indexes.by_name.get(lib.normalize(person.full_name)), "employee_name");
			if (r && (r.employee || r.ambiguous || r.left_only)) return r;
			r = resolve_hits(indexes.by_name_key.get(lib.name_key(person.full_name)), "normalized_employee_name");
			if (r && (r.employee || r.ambiguous || r.left_only)) return r;
		}

		return { employee: null, matched_by: null };
	}

	async function resolve_people() {
		dialog.get_primary_btn().prop("disabled", true);
		const t_match = performance.now();
		term_append([
			line("sep", "----------------------------------------"),
			line("info", `[INFO] ${__("Loading Employee records")}…`),
		]);
		try {
			const employees = await fetch_all_employees((n) => {
				if (n % 500 === 0) term_append([line("info", `[INFO] Loaded ${n} employees…`)]);
			});
			state.employees = employees;
			state.indexes = build_employee_indexes(employees);
			term_append([line("ok", `[OK] Employees loaded: ${employees.length}`)]);

			const method_counts = {};
			for (let i = 0; i < state.people.length; i++) {
				const person = state.people[i];
				Object.assign(person, match_employee_local(person, state.indexes));
				const m = person.matched_by || (person.ambiguous ? "ambiguous" : "unmatched");
				method_counts[m] = (method_counts[m] || 0) + 1;
				if (i % 50 === 0) await yield_ui();
			}

			const matched = state.people.filter((p) => p.employee);
			const unmatched = state.people.filter((p) => !p.employee && !p.ambiguous);
			const ambiguous = state.people.filter((p) => p.ambiguous);
			state.timings.match = (performance.now() - t_match) / 1000;

			const log_people = state.people.slice().sort((a, b) => {
				const rank = (p) => (p.ambiguous ? 0 : !p.employee ? 1 : 2);
				return rank(a) - rank(b) || String(a.code).localeCompare(String(b.code), undefined, { numeric: true });
			});

			const detail_lines = [];
			log_people.forEach((p) => {
				const code = p.code_normalized || p.code || "-";
				const name = p.full_name || "(no name)";
				if (p.employee) {
					detail_lines.push(
						line("ok", `[OK] ${code} | ${name} -> ${p.employee} (${p.matched_by || "manual"})`)
					);
				} else if (p.ambiguous) {
					detail_lines.push({
						html: `<div class="warn clickable" data-person-key="${esc(p.key)}">[WARN] ${esc(
							code
						)} | <span class="rtl">${esc(name)}</span> — multiple matches. Click to select.</div>`,
					});
					(p.candidates || []).slice(0, 5).forEach((c) => detail_lines.push(line("warn", `  - ${c}`)));
				} else if (p.inactive_only || p.left_only) {
					detail_lines.push({
						html: `<div class="warn clickable" data-person-key="${esc(p.key)}">[WARN] ${esc(
							code
						)} | <span class="rtl">${esc(name)}</span> — ${
							p.inactive_only ? "Inactive" : "Left"
						}. Click to confirm.</div>`,
					});
					(p.candidates || []).slice(0, 5).forEach((c) => detail_lines.push(line("warn", `  - ${c}`)));
				} else {
					detail_lines.push({
						html: `<div class="err clickable" data-person-key="${esc(p.key)}">[ERROR] ${esc(
							code
						)} | <span class="rtl">${esc(name)}</span> — not found. Click to select.</div>`,
					});
				}
			});
			state.match_detail_lines = detail_lines;

			term_append([
				line("ok", `[OK] Employee matching completed.`),
				line("ok", `[OK] Matched: ${matched.length}`),
				line(unmatched.length ? "err" : "ok", `[${unmatched.length ? "WARN" : "OK"}] Unmatched: ${unmatched.length}`),
				line(ambiguous.length ? "warn" : "ok", `[${ambiguous.length ? "WARN" : "OK"}] Ambiguous: ${ambiguous.length}`),
				line("muted", `Matching time: ${state.timings.match.toFixed(2)}s | Methods: ${JSON.stringify(method_counts)}`),
			]);
			paint_panel();
			await refresh_ready(true);
		} catch (err) {
			console.error(err);
			term_append([line("err", `[ERROR] ${(err && err.message) || __("Employee matching failed.")}`)]);
			dialog.get_primary_btn().prop("disabled", true);
			paint_panel();
		}
	}

	function open_manual_match(person) {
		const d = new frappe.ui.Dialog({
			title: __("Select Employee"),
			fields: [
				{
					fieldtype: "HTML",
					options: `<div class="text-muted small" style="margin-bottom:8px;color:#475569">
						${__("Code")}: <b dir="ltr" style="color:#1F2937">${esc(person.code || "-")}</b>
						${
							person.code_normalized && person.code_normalized !== person.code
								? ` → <b dir="ltr">${esc(person.code_normalized)}</b>`
								: ""
						}<br>
						<span class="rtl">${esc(person.full_name || "")}</span>
					</div>`,
				},
				{
					fieldname: "employee",
					fieldtype: "Link",
					options: "Employee",
					label: __("Employee"),
					reqd: 1,
					get_query: () => ({ filters: { status: ["in", ["Active", "Inactive"]] } }),
				},
			],
			primary_action_label: __("Apply"),
			primary_action: (values) => {
				if (!values.employee) return;
				person.employee = values.employee;
				person.manual = true;
				person.ambiguous = null;
				person.left_only = null;
				person.inactive_only = null;
				person.matched_by = "manual";
				person.candidates = null;
				d.hide();
				term_append([
					line("ok", `[OK] Manual: ${person.code_normalized || person.code} -> ${values.employee}`),
				]);
				refresh_ready(true);
			},
		});
		d.show();
	}

	function canon_time(time) {
		const value = String(time || "").replace("T", " ").trim();
		const head = value.slice(0, 19);
		return head.length === 16 ? head + ":00" : head;
	}

	function dup_key(employee, time, log_type) {
		return employee + "|" + canon_time(time) + "|" + (log_type || "");
	}

	async function fetch_existing(checkins) {
		const employees = Array.from(new Set(checkins.map((c) => c.employee).filter(Boolean)));
		if (!employees.length) return { set: new Set(), rows: [] };
		const rows = [];
		const page = 500;
		let start = 0;
		for (let guard = 0; guard < 200; guard++) {
			const batch = await get_list_safe("Employee Checkin", {
				filters: [["employee", "in", employees]],
				fields: ["employee", "time", "creation", "log_type"],
				limit: page,
				limit_start: start,
				order_by: "time asc",
			});
			rows.push.apply(rows, batch);
			if (batch.length < page) break;
			start += page;
			await yield_ui();
		}
		const set = new Set();
		rows.forEach((r) => set.add(dup_key(r.employee, r.time, r.log_type)));
		return { set: set, rows: rows };
	}

	// Mirrors Server Script "# Employee Checkin - Outside Shift Day Limit".
	// A check-in is manual when its calendar date is not the document creation date.
	// New Excel rows are created today, so every non-today punch counts.
	// Quota is 5 manual check-ins per employee per Jalali period (26th–25th).
	const MAX_MANUAL_CHECKINS = 5;
	const G_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
	const J_DAYS = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29];

	function gregorian_to_jalali(gy, gm, gd) {
		let gy2 = gy - 1600;
		let gm2 = gm - 1;
		let gd2 = gd - 1;
		let g_day_no =
			365 * gy2 +
			Math.floor((gy2 + 3) / 4) -
			Math.floor((gy2 + 99) / 100) +
			Math.floor((gy2 + 399) / 400);
		for (let i = 0; i < gm2; i++) g_day_no += G_DAYS[i];
		const leap_g = (gy2 % 4 === 0 && gy2 % 100 !== 0) || gy2 % 400 === 0;
		if (gm2 > 1 && leap_g) g_day_no += 1;
		g_day_no += gd2;
		let j_day_no = g_day_no - 79;
		const j_np = Math.floor(j_day_no / 12053);
		j_day_no = j_day_no % 12053;
		let jy = 979 + 33 * j_np + 4 * Math.floor(j_day_no / 1461);
		j_day_no = j_day_no % 1461;
		if (j_day_no >= 366) {
			jy += Math.floor((j_day_no - 1) / 365);
			j_day_no = (j_day_no - 1) % 365;
		}
		let i = 0;
		while (i < 11 && j_day_no >= J_DAYS[i]) {
			j_day_no -= J_DAYS[i];
			i += 1;
		}
		return { jy: jy, jm: i + 1, jd: j_day_no + 1 };
	}

	function jalali_period(date_str) {
		const parts = String(date_str).slice(0, 10).split("-");
		const j = gregorian_to_jalali(+parts[0], +parts[1], +parts[2]);
		let sy, sm, ey, em;
		if (j.jd >= 26) {
			sy = j.jy;
			sm = j.jm;
			if (j.jm === 12) {
				ey = j.jy + 1;
				em = 1;
			} else {
				ey = j.jy;
				em = j.jm + 1;
			}
		} else {
			ey = j.jy;
			em = j.jm;
			if (j.jm === 1) {
				sy = j.jy - 1;
				sm = 12;
			} else {
				sy = j.jy;
				sm = j.jm - 1;
			}
		}
		const pad = (n) => (n < 10 ? "0" : "") + n;
		return {
			key: sy + "/" + sm,
			label: pad(sy) + "/" + pad(sm) + "/26 → " + pad(ey) + "/" + pad(em) + "/25",
		};
	}

	function apply_manual_quota(checkins, existing_rows) {
		const today = frappe.datetime.get_today();
		const counts = {};
		(existing_rows || []).forEach((row) => {
			if (!row.time) return;
			const checkin_date = String(row.time).slice(0, 10);
			const creation_date = row.creation ? String(row.creation).slice(0, 10) : "";
			if (creation_date && checkin_date === creation_date) return;
			const period = jalali_period(checkin_date);
			const key = row.employee + "|" + period.key;
			counts[key] = (counts[key] || 0) + 1;
		});
		const ordered = checkins
			.filter((c) => !c._duplicate)
			.slice()
			.sort((a, b) => String(a.time).localeCompare(String(b.time)) || String(a.log_type).localeCompare(String(b.log_type)));
		ordered.forEach((c) => {
			const checkin_date = String(c.time).slice(0, 10);
			if (checkin_date === today) {
				c._quota_skip = false;
				return;
			}
			const period = jalali_period(checkin_date);
			const key = c.employee + "|" + period.key;
			const used = counts[key] || 0;
			if (used >= MAX_MANUAL_CHECKINS) {
				c._quota_skip = true;
				c._quota_period = period.label;
			} else {
				c._quota_skip = false;
				counts[key] = used + 1;
			}
		});
	}

	async function refresh_ready(show_header) {
		dialog.get_primary_btn().prop("disabled", true);
		if (!state.parsed || state.importing) return;
		const by_key = {};
		state.people.forEach((p) => (by_key[p.key] = p));
		state.checkins = lib.build_checkins(state.parsed.records, by_key, {
			skip_auto_attendance: dialog.get_value("skip_auto_attendance"),
			location_device_id: dialog.get_value("location_device_id"),
		});
		state.location_device_id = (dialog.get_value("location_device_id") || "").trim();
		const invalid = state.parsed.records.filter((r) => r.has_error).length;
		const matched_people = state.people.filter((p) => p.employee).length;

		if (!state.checkins.length) {
			term_append([
				line("warn", `[WARN] ${__("Nothing to import.")} (matched people: ${matched_people})`),
				line("info", "Status: BLOCKED"),
			]);
			paint_panel();
			return;
		}

		term_append([line("info", `[INFO] ${__("Checking existing records")}…`)]);
		const t_dup = performance.now();
		const existing = await fetch_existing(state.checkins);
		state.timings.dup_check = (performance.now() - t_dup) / 1000;
		state.checkins.forEach((c) => {
			c._duplicate = existing.set.has(dup_key(c.employee, c.time, c.log_type));
			c._quota_skip = false;
		});
		// The 5-entry manual limit is a server rule for Desk entries.
		// Authorized device imports do not apply it on the client.
		state.duplicates = state.checkins.filter((c) => c._duplicate).length;
		state.quota_skipped = 0;
		const fresh = state.checkins.filter((c) => !c._duplicate).length;
		const range = lib.date_range(state.checkins);

		term_append([
			line("ok", `[OK] Ready for import: ${fresh}`),
			line("skip", `[SKIP] Already imported: ${state.duplicates}`),
			line("info", "[INFO] Manual check-in limit stays on Desk entries. Device import does not use it."),
			line(invalid ? "err" : "ok", `[${invalid ? "ERROR" : "OK"}] Invalid records: ${invalid}`),
			line("info", `[INFO] Check-in payloads: ${state.checkins.length}`),
			range ? line("info", `[INFO] Date range: ${range.from} → ${range.to}`) : line("muted", ""),
			line(fresh > 0 ? "ok" : "warn", `Status: ${fresh > 0 ? "READY" : "NO NEW RECORDS"}`),
		]);
		paint_panel();
		dialog.get_primary_btn().prop("disabled", fresh === 0);
		dialog.set_primary_action(`${__("Import")} (${fresh})`, () => do_import());
	}

	async function post_import(records) {
		try {
			const r = await fetch(
				"/api/method/erpnext_extensions.extentionhrms.employee_checkin_import.import_device_checkins",
				{
					method: "POST",
					headers: {
						"Content-Type": "application/json",
						Accept: "application/json",
						"X-Frappe-CSRF-Token": frappe.csrf_token,
					},
					body: JSON.stringify({
						records: records,
						location_device_id: state.location_device_id || "",
					}),
				}
			);
			const data = await r.json().catch(() => ({}));
			if (r.ok) {
				const message = data.message || {};
				return {
					ok: true,
					status: r.status,
					created: message.created || [],
					duplicates: message.duplicates || [],
					failed: message.failed || [],
				};
			}
			return { ok: false, status: r.status, error: server_error(data) || `HTTP ${r.status}` };
		} catch (e) {
			return { ok: false, status: 0, error: (e && e.message) || __("import failed"), network: true };
		}
	}

	function to_record(c) {
		return {
			employee: c.employee,
			time: c.time,
			log_type: c.log_type,
			device_id: c.device_id || "",
			skip_auto_attendance: c.skip_auto_attendance ? 1 : 0,
		};
	}

	function failure_from_row(row, fallback) {
		const checkin =
			fallback ||
			{
				employee: row.employee,
				time: row.time,
				log_type: row.log_type,
				_meta: {},
			};
		return {
			checkin: checkin,
			error: row.error || "Unknown",
			network: false,
			category: row.category || categorize_error(row.error),
		};
	}

	// The server returns per-row created / duplicate / failed results and
	// rolls a single bad insert back to a savepoint. A dropped connection is
	// reconciled against the database before one retry. The request is not
	// repeated when the response is ambiguous and the rows are already stored.
	async function insert_group(checkins, stats, allowRetry) {
		if (!checkins.length) return { created: 0, duplicates: 0, failed: [] };
		if (allowRetry === undefined) allowRetry = true;
		const started = performance.now();
		const response = await post_import(checkins.map(to_record));
		stats.latencies.push(performance.now() - started);
		stats.requests += 1;
		if (response.ok) {
			const byKey = {};
			checkins.forEach((c) => {
				byKey[dup_key(c.employee, c.time, c.log_type)] = c;
			});
			return {
				created: (response.created || []).length,
				duplicates: (response.duplicates || []).length,
				failed: (response.failed || []).map((row) =>
					failure_from_row(row, byKey[dup_key(row.employee, row.time, row.log_type)])
				),
			};
		}

		if (response.network || response.status === 0 || response.status >= 500) {
			const found = await fetch_existing(checkins);
			const pending = checkins.filter((c) => !found.set.has(dup_key(c.employee, c.time, c.log_type)));
			const already = checkins.length - pending.length;
			if (!pending.length) return { created: already, duplicates: 0, failed: [] };
			if (allowRetry) {
				const retry = await insert_group(pending, stats, false);
				return {
					created: already + retry.created,
					duplicates: retry.duplicates,
					failed: retry.failed,
				};
			}
		}

		if (checkins.length === 1 || response.status === 403 || response.status === 401) {
			return {
				created: 0,
				duplicates: 0,
				failed: checkins.map((c) => ({
					checkin: c,
					error: response.error || "Unknown",
					network: !!response.network,
					category: response.network ? "Network error" : categorize_error(response.error),
				})),
			};
		}

		const mid = Math.floor(checkins.length / 2);
		const left = await insert_group(checkins.slice(0, mid), stats, false);
		const right = await insert_group(checkins.slice(mid), stats, false);
		return {
			created: left.created + right.created,
			duplicates: left.duplicates + right.duplicates,
			failed: left.failed.concat(right.failed),
		};
	}

	function stamp_rate(processed, total, t0) {
		const elapsed = (performance.now() - t0) / 1000;
		const rate = elapsed > 0.2 ? processed / elapsed : 0;
		state.progress.elapsed_sec = elapsed;
		state.progress.rate = rate;
		state.progress.eta_sec = rate > 0 ? Math.max(0, total - processed) / rate : 0;
	}

	async function do_import() {
		if (state.importing) return;
		const to_create = state.checkins.filter((c) => !c._duplicate);
		if (!to_create.length) return;

		state.importing = true;
		state.allow_close = false;
		state.failures = [];
		dialog.get_primary_btn().prop("disabled", true).text(__("Importing…"));
		dialog.get_secondary_btn && dialog.get_secondary_btn().prop("disabled", false);

		const t0 = performance.now();
		const chunk_size = 200;
		const batches = Math.ceil(to_create.length / chunk_size);
		let created = 0;
		let server_duplicates = 0;
		let processed = 0;
		const pre_dup = state.duplicates || 0;
		const invalid = state.parsed ? state.parsed.records.filter((r) => r.has_error).length : 0;
		const stats = { latencies: [], requests: 0 };
		state.timings.latencies = stats.latencies;

		state.progress = {
			heading: __("IMPORT IN PROGRESS"),
			processed: 0,
			total: to_create.length,
			created: 0,
			duplicates: pre_dup,
			skipped: invalid,
			failed: 0,
			batch: 0,
			batches: batches,
			elapsed_sec: 0,
			rate: 0,
			eta_sec: 0,
			status: __("Starting"),
			operation: __("Importing Employee Checkin records…"),
			done: false,
		};
		paint_panel();
		term_append([
			line("sep", "----------------------------------------"),
			line("info", `[INFO] Import started — ${to_create.length} records, ${batches} batches of ${chunk_size}`),
			line("info", "[INFO] Authorized device import. Desk manual limit is not applied."),
		]);

		const tick = setInterval(() => {
			if (!state.progress || state.progress.done) return;
			stamp_rate(state.progress.processed, state.progress.total, t0);
			update_progress_dom();
		}, 500);

		try {
			for (let i = 0; i < to_create.length; i += chunk_size) {
				const batch_no = Math.floor(i / chunk_size) + 1;
				const chunk = to_create.slice(i, i + chunk_size);
				state.progress.batch = batch_no;
				state.progress.status = __("Importing");
				state.progress.operation = `Importing batch ${batch_no} / ${batches}…`;
				stamp_rate(processed, to_create.length, t0);
				update_progress_dom();
				await yield_ui();

				const result = await insert_group(chunk, stats);
				created += result.created;
				server_duplicates += result.duplicates || 0;
				processed += chunk.length;
				result.failed.forEach((item) => {
					const c = item.checkin;
					const per = person_for_checkin(c);
					const err = item.error || "Unknown";
					state.failures.push({
						employee: c.employee,
						full_name: (c._meta && c._meta.full_name) || per.full_name || "",
						code: (c._meta && c._meta.code) || per.code || "",
						time: c.time,
						log_type: c.log_type,
						excel_row: c._meta && c._meta.excel_row,
						category: item.category || (item.network ? "Network error" : categorize_error(err)),
						error: err,
					});
				});
				state.progress.created = created;
				state.progress.duplicates = pre_dup + server_duplicates;
				state.progress.failed = state.failures.length;
				state.progress.processed = processed;
				state.progress.skipped = invalid;
				stamp_rate(processed, to_create.length, t0);
				update_progress_dom();
			}

			state.timings.import_api = (performance.now() - t0) / 1000;
			if (dialog.get_value("save_device_id")) {
				state.progress.operation = __("Updating Attendance Device IDs…");
				state.progress.status = __("Post-process");
				update_progress_dom();
				const t_dev = performance.now();
				await save_device_ids();
				state.timings.device_ids = (performance.now() - t_dev) / 1000;
			}

			state.timings.import = (performance.now() - t0) / 1000;
			state.timings.requests = stats.requests;
			state.progress.processed = to_create.length;
			state.progress.created = created;
			state.progress.failed = state.failures.length;
			state.progress.skipped = invalid;
			state.progress.duplicates = pre_dup + server_duplicates;
			stamp_rate(to_create.length, to_create.length, t0);
			state.progress.done = true;
			state.progress.eta_sec = 0;
			state.progress.heading = __("IMPORT COMPLETED");
			state.progress.status = state.failures.length ? __("COMPLETED WITH ERRORS") : __("COMPLETED");
			state.progress.operation = __("Finished");
			update_progress_dom();

			show_final_report(created, pre_dup + server_duplicates, to_create.length);
			if (cur_list && cur_list.doctype === "Employee Checkin") cur_list.refresh();
		} catch (err) {
			console.error(err);
			term_append([line("err", `[ERROR] ${(err && err.message) || err}`)]);
			if (state.progress) {
				state.progress.status = __("FAILED");
				state.progress.operation = String((err && err.message) || err);
				update_progress_dom();
			}
		} finally {
			clearInterval(tick);
			state.importing = false;
			dialog.get_primary_btn().prop("disabled", true).text(__("Import"));
			paint_panel();
		}
	}

	function percentile(values) {
		const arr = (values || []).slice().sort((a, b) => a - b);
		if (!arr.length) return { p50: 0, p95: 0, max: 0 };
		const at = (p) => arr[Math.min(arr.length - 1, Math.max(0, Math.ceil((p / 100) * arr.length) - 1))];
		return { p50: at(50), p95: at(95), max: arr[arr.length - 1] };
	}

	function show_final_report(created, already_existing, attempted) {
		const range = lib.date_range(state.checkins);
		const invalid = state.parsed ? state.parsed.records.filter((r) => r.has_error).length : 0;
		const matched = state.people.filter((p) => p.employee).length;
		const unmatched = state.people.filter((p) => !p.employee && !p.ambiguous).length;
		const ambiguous = state.people.filter((p) => p.ambiguous).length;
		const skipped_people = state.people.filter((p) => !p.employee).length;
		const in_count = state.checkins.filter((c) => c.log_type === "IN").length;
		const out_count = state.checkins.filter((c) => c.log_type === "OUT").length;
		const failed = state.failures.length;
		const success_rate = attempted ? (((created) / attempted) * 100).toFixed(1) : "0.0";
		const cats = {};
		state.failures.forEach((f) => {
			cats[f.category] = (cats[f.category] || 0) + 1;
		});
		const emp_ok = new Set(
			state.checkins.filter((c) => !c._duplicate).map((c) => c.employee)
		);
		const emp_err = new Set(state.failures.map((f) => f.employee));
		const total_elapsed =
			(state.timings.parse || 0) +
			(state.timings.match || 0) +
			(state.timings.dup_check || 0) +
			(state.timings.import || 0);
		const status =
			failed === 0 && created > 0
				? "COMPLETED"
				: failed > 0 && created > 0
				? "COMPLETED WITH ERRORS"
				: failed > 0 && created === 0
				? "FAILED"
				: attempted === 0
				? "NO NEW RECORDS"
				: "COMPLETED";

		state.last_report = {
			created,
			already_existing,
			attempted,
			failed,
			status,
			cats,
		};

		const cat_lines = Object.keys(cats).length
			? Object.keys(cats)
					.sort()
					.map((k) => line("err", `${k}: ${cats[k]}`))
			: [line("ok", "None")];

		term_append(
			[
				line("sep", "========================================"),
				line("info", "EMPLOYEE CHECKIN IMPORT REPORT"),
				line("sep", "========================================"),
				line("info", `File: ${state.file_name || (state.parsed && state.parsed.filename) || "-"}`),
				line(
					"info",
					`Date range: ${range ? range.from + " to " + range.to : "-"}`
				),
				line("sep", "----------------------------------------"),
				line("info", "FILE ANALYSIS"),
				line("sep", "----------------------------------------"),
				line("info", `Excel rows: ${state.excel_rows}`),
				line("info", `Attendance source rows: ${state.parsed ? state.parsed.records.length : 0}`),
				line("info", `Personnel: ${state.people.length}`),
				line("ok", `Matched: ${matched}`),
				line(unmatched ? "warn" : "ok", `Unmatched: ${unmatched}`),
				line(ambiguous ? "warn" : "ok", `Ambiguous: ${ambiguous}`),
				line("sep", "----------------------------------------"),
				line("info", "IMPORT RESULTS"),
				line("sep", "----------------------------------------"),
				line("info", `Total check-in payloads: ${state.checkins.length}`),
				line("info", `Attempted (new): ${attempted}`),
				line("ok", `Created: ${created}`),
				line("skip", `Already existing: ${already_existing}`),
				line("skip", `Skipped (invalid Excel rows): ${invalid}`),
				line("skip", `Skipped (unmatched people): ${skipped_people}`),
				line(failed ? "err" : "ok", `Failed: ${failed}`),
				line("info", `Success rate: ${success_rate}%  (Created / Attempted)`),
				line("muted", `Reconcile: Attempted = Created + Failed → ${attempted} = ${created} + ${failed}`),
				line("sep", "----------------------------------------"),
				line("info", "IN / OUT SUMMARY"),
				line("sep", "----------------------------------------"),
				line("info", `IN records: ${in_count}`),
				line("info", `OUT records: ${out_count}`),
				line("info", `Employees with unequal IN/OUT: ${unpaired_employees(state.checkins)}`),
				line("sep", "----------------------------------------"),
				line("info", "EMPLOYEE SUMMARY"),
				line("sep", "----------------------------------------"),
				line("info", `Employees with payloads: ${emp_ok.size}`),
				line(emp_err.size ? "err" : "ok", `Employees with errors: ${emp_err.size}`),
				line("sep", "----------------------------------------"),
				line("info", "ERROR SUMMARY"),
				line("sep", "----------------------------------------"),
			]
				.concat(cat_lines)
				.concat([
					line("sep", "----------------------------------------"),
					line("info", "PERFORMANCE"),
					line("sep", "----------------------------------------"),
					line("muted", `Parsing: ${state.timings.parse.toFixed(2)} seconds`),
					line("muted", `Matching: ${state.timings.match.toFixed(2)} seconds`),
					line("muted", `Duplicate checking: ${state.timings.dup_check.toFixed(2)} seconds`),
					line("muted", `Import API: ${(state.timings.import_api || state.timings.import).toFixed(2)} seconds`),
					line("muted", `Device IDs: ${(state.timings.device_ids || 0).toFixed(2)} seconds`),
					line("muted", `Import total: ${state.timings.import.toFixed(2)} seconds`),
					line("muted", `Requests: ${state.timings.requests || 0}`),
					line("muted", `Batch latency P50/P95: ${percentile(state.timings.latencies).p50.toFixed(0)} / ${percentile(state.timings.latencies).p95.toFixed(0)} ms`),
					line("muted", `Records/s: ${state.progress && state.progress.rate ? state.progress.rate.toFixed(1) : "0"}`),
					line("muted", `Total: ${total_elapsed.toFixed(2)} seconds`),
					line("sep", "----------------------------------------"),
					line("info", "FINAL STATUS"),
					line("sep", "----------------------------------------"),
					line(status.indexOf("FAIL") >= 0 ? "err" : status.indexOf("ERROR") >= 0 ? "warn" : "ok", status),
					line("sep", "========================================"),
				])
		);
		paint_panel();
	}

	function unpaired_employees(checkins) {
		const counts = {};
		(checkins || []).forEach((c) => {
			if (!counts[c.employee]) counts[c.employee] = { IN: 0, OUT: 0 };
			if (c.log_type === "IN" || c.log_type === "OUT") counts[c.employee][c.log_type] += 1;
		});
		return Object.keys(counts).filter((name) => counts[name].IN !== counts[name].OUT).length;
	}

	async function save_device_ids() {
		const by_id = state.indexes && state.indexes.by_id;
		const assignments = [];
		state.people.forEach((person) => {
			if (!person.employee || !person.code) return;
			const cached = by_id && by_id.get(person.employee);
			if (cached && cached.attendance_device_id) return;
			assignments.push({
				employee: person.employee,
				personnel_code: person.code_normalized || person.code,
			});
		});
		const chunk = 200;
		for (let i = 0; i < assignments.length; i += chunk) {
			const slice = assignments.slice(i, i + chunk);
			const r = await fetch(
				"/api/method/erpnext_extensions.extentionhrms.employee_checkin_import.assign_empty_attendance_device_ids",
				{
					method: "POST",
					headers: {
						"Content-Type": "application/json",
						Accept: "application/json",
						"X-Frappe-CSRF-Token": frappe.csrf_token,
					},
					body: JSON.stringify({ assignments: slice }),
				}
			);
			const data = await r.json().catch(() => ({}));
			if (!r.ok) {
				term_append([line("warn", `[WARN] Device ID update: ${server_error(data) || r.status}`)]);
				continue;
			}
			const message = data.message || {};
			(message.updated || []).forEach((employee) => {
				const cached = by_id && by_id.get(employee);
				const person = state.people.find((p) => p.employee === employee);
				if (cached && person) cached.attendance_device_id = person.code_normalized || person.code;
			});
		}
	}
};

(function attach_button() {
	// Reliable List View registration for Frappe v16:
	// 1) HRMS employee_checkin_list.js assigns listview_settings first (__list_js).
	// 2) This Client Script runs last (__custom_list_js) and must wrap onload.
	// 3) Never early-return solely on a flag — always ensure onload chain + DOM button.
	const doctype = "Employee Checkin";
	const BTN = "Import Attendance (Excel)";
	const WRAP_KEY = "__espad_attendance_import_wrap";

	function button_in_dom(listview) {
		if (!listview || !listview.page) return false;
		const label = __(BTN);
		const sel = `button[data-label="${encodeURIComponent(label)}"]`;
		const $toolbar = listview.page.inner_toolbar || listview.page.custom_actions;
		if ($toolbar && $toolbar.length && $toolbar.find(sel).length) return true;
		// Also accept menu entry (mobile / overflow)
		if (listview.page.menu && listview.page.menu.find(`[data-label="${encodeURIComponent(label)}"]`).length)
			return true;
		return false;
	}

	function ensure_button(listview) {
		try {
			if (!listview || !listview.page || !listview.page.add_inner_button) return;
			// HRMS 16.51 marks Employee Checkin as in_create.
			// Frappe then keeps it out of boot.user.can_create, so
			// model.can_create() is false even when create is allowed.
			const bootUser = (frappe.boot && frappe.boot.user) || {};
			const canCreate =
				(bootUser.can_create || []).indexOf(doctype) !== -1 ||
				(bootUser.in_create || []).indexOf(doctype) !== -1;
			const roles = bootUser.roles || [];
			const canImport =
				roles.indexOf("System Manager") !== -1 ||
				roles.indexOf("Attendance Device Importer") !== -1;
			if (!canCreate || !canImport) return;
			if (button_in_dom(listview)) {
				listview.__espad_attendance_import_btn = true;
				return;
			}
			listview.page.add_inner_button(__(BTN), () => {
				if (espad.attendance_import && espad.attendance_import.open) {
					espad.attendance_import.open();
				} else {
					frappe.msgprint(__("Attendance importer is not loaded."));
				}
			});
			listview.__espad_attendance_import_btn = true;
		} catch (e) {
			console.error("espad.attendance_import: failed to add List View button", e);
		}
	}

	function wrap_listview_settings() {
		const previous = frappe.listview_settings[doctype] || {};
		const previous_onload = previous.onload;
		const already =
			previous[WRAP_KEY] === (espad.attendance_import.VERSION || "V1.2.1") &&
			typeof previous.onload === "function" &&
			previous.__espad_attendance_import;

		if (already) return;

		frappe.listview_settings[doctype] = Object.assign({}, previous, {
			__espad_attendance_import: true,
			[WRAP_KEY]: espad.attendance_import.VERSION || "V1.2.1",
			onload: function (listview) {
				if (typeof previous_onload === "function") {
					try {
						previous_onload.call(this, listview);
					} catch (e) {
						console.error("Employee Checkin previous onload error", e);
					}
				}
				ensure_button(listview);
			},
		});
	}

	wrap_listview_settings();

	// If List View is already open when this script evaluates, attach immediately.
	if (typeof cur_list !== "undefined" && cur_list && cur_list.doctype === doctype) {
		ensure_button(cur_list);
	}
})();

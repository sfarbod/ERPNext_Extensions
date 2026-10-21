// ---------------------------------------------------------------------------
// Attendance — Excel attendance-details importer (جزئیات کارکرد)
// Adds "Import Attendance Details (Excel)" to the Attendance list view.
// Parses Persian/Jalali daily-detail exports (.xls / .xlsx), matches
// personnel via Employee.custom_personal_code, resolves shift rotations,
// and creates/updates Submitted Attendance with overtime/leave hours.
//
// XLSX: SheetJS community build xlsx@0.18.5 (Apache-2.0), served locally from
// /assets/erpnext_extensions/js/lib/xlsx.full.min.js (no runtime CDN).
// Version: V2.0.8 (Mission Present even with incomplete IN/OUT)
// ---------------------------------------------------------------------------

frappe.provide("espad.attendance_details");

(function () {
    "use strict";

    const NS = espad.attendance_details;

    const CONFIG = {
        regularOT: "custom_regular_overtime_hours",
        approvedOT: "custom_approved_overtime_hours",
        previewLimit: 300,
        sheetJsUrl: "/assets/erpnext_extensions/js/lib/xlsx.full.min.js",
        sheetJsTimeoutMs: 20000
    };

    // Excel columns A to AC, zero-based
    const C = {
        paidLeave: 0,
        unpaidLeave: 1,
        nightTotal: 2,
        nightRegular: 3,
        mission: 4,
        unauthorizedExit: 5,
        early: 6,
        late: 7,
        absence: 8,
        nightOTRegular: 9,
        nightOTApproved: 10,
        missionOTRegular: 11,
        missionOTApproved: 12,
        holidayOTRegular: 13,
        holidayOTApproved: 14,
        normalOTRegular: 15,
        normalOTApproved: 16,
        presence: 17,
        worked: 18,
        outClock: 19,
        outDate: 20,
        inClock: 21,
        inDate: 22,
        workGroup: 23,
        date: 24,
        weekday: 25,
        lastName: 26,
        firstName: 27,
        code: 28
    };

    function codeKey(v) {
        const x = norm(v).replace(/\s/g, "");
        return /^\d+$/.test(x) ? (x.replace(/^0+/, "") || "0") : x;
    }

    function dateTimeMs(v) {
        return v ? new Date(v.replace(" ", "T") + "Z").getTime() : NaN;
    }

    function minutesBetween(a, b) {
        return Math.max(0, (dateTimeMs(b) - dateTimeMs(a)) / 60000);
    }

    function norm(v) {
        return String(v == null ? "" : v)
            .replace(/[۰-۹]/g, x =>
                String("۰۱۲۳۴۵۶۷۸۹".indexOf(x)))
            .replace(/[٠-٩]/g, x =>
                String("٠١٢٣٤٥٦٧٨٩".indexOf(x)))
            .replace(/ي/g, "ی")
            .replace(/ك/g, "ک")
            .replace(/[\u200c\u200d]/g, " ")
            .replace(/\s+/g, " ")
            .trim();
    }

    function esc(v) {
        return frappe.utils.escape_html(String(v ?? ""));
    }

    function fmtTime(sec) {
        sec = Math.max(0, Math.floor(sec || 0));
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    }

    function yieldUi() {
        return new Promise((resolve) => setTimeout(resolve, 0));
    }

    function hours(minutes) {
        return Math.round(minutes / 60 * 1000000) / 1000000;
    }

    const ATT_IMPORT_CSS = `
.espad-att-import-dlg .espad-att-progress {
    border: 1px solid var(--border-color, #CBD5E1);
    background: var(--control-bg, #F8FAFC);
    border-radius: 6px;
    padding: 10px 12px;
    margin: 8px 0;
    display: none;
}
.espad-att-import-dlg .espad-att-progress.visible { display: block; }
.espad-att-import-dlg .espad-att-progress .title {
    font-weight: 700; margin-bottom: 6px; font-size: 13px;
}
.espad-att-import-dlg .espad-att-progress .meta {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 4px 10px;
    font-size: 12px;
    margin-bottom: 8px;
    font-variant-numeric: tabular-nums;
}
@media (max-width: 720px) {
    .espad-att-import-dlg .espad-att-progress .meta {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}
.espad-att-import-dlg .espad-att-bar {
    height: 14px;
    background: #E2E8F0;
    border-radius: 999px;
    overflow: hidden;
    border: 1px solid #CBD5E1;
}
.espad-att-import-dlg .espad-att-bar > i {
    display: block; height: 100%; width: 0%;
    background: #1D4ED8; transition: width 0.15s linear;
}
.espad-att-import-dlg .espad-att-progress .op {
    margin-top: 6px; font-size: 12px; color: var(--text-muted, #475569);
}
.espad-att-import-dlg .espad-att-term {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px; line-height: 1.45;
    max-height: min(220px, 28vh); overflow: auto;
    white-space: pre-wrap; word-break: break-word;
    border: 1px solid var(--border-color, #CBD5E1);
    border-radius: 6px; padding: 8px 10px;
    background: var(--control-bg, #F8FAFC);
    margin-top: 8px; display: none;
}
.espad-att-import-dlg .espad-att-term.visible { display: block; }
.espad-att-import-dlg .espad-att-term .ok { color: #047857; font-weight: 600; }
.espad-att-import-dlg .espad-att-term .warn { color: #92400E; font-weight: 600; }
.espad-att-import-dlg .espad-att-term .err { color: #B91C1C; font-weight: 600; }
.espad-att-import-dlg .espad-att-term .info { color: #1D4ED8; }
.espad-att-import-dlg .espad-att-term .muted { color: #475569; }
.espad-att-import-dlg .espad-att-result {
    margin-top: 10px; padding: 10px 12px;
    border: 1px solid var(--border-color, #CBD5E1);
    border-radius: 6px; display: none;
}
.espad-att-import-dlg .espad-att-result.visible { display: block; }
.espad-att-import-dlg .espad-att-result h4 { margin: 0 0 8px; }
.espad-att-import-dlg .espad-att-fail-list {
    max-height: 220px; overflow: auto; margin-top: 8px;
}
`;

    function ensureAttImportCss() {
        if (document.getElementById("espad-att-import-css")) return;
        const style = document.createElement("style");
        style.id = "espad-att-import-css";
        style.textContent = ATT_IMPORT_CSS;
        document.head.appendChild(style);
    }

    function duration(v) {
        const s = norm(v);
        if (!s || s === "-" || s === "--") return 0;

        const m = s.match(/^(\d+):(\d{1,2})(?::(\d{1,2}))?$/);

        if (m) {
            if (+m[2] >= 60 || +(m[3] || 0) >= 60)
                throw Error("Invalid duration: " + s);

            return +m[1] * 60 + +m[2] +
                +(m[3] || 0) / 60;
        }

        if (/^\d+(\.\d+)?$/.test(s))
            return Number(s) * 60;

        throw Error("Invalid duration: " + s);
    }

    const persianFormatter = new Intl.DateTimeFormat(
        "en-US-u-ca-persian-nu-latn",
        {
            year: "numeric",
            month: "numeric",
            day: "numeric",
            timeZone: "UTC"
        }
    );

    function convertDate(value) {
        // Never use Date.parse on Jalali strings — ambiguous and can invent years.
        // Excel serial numbers (SheetJS raw:true) → Gregorian via the 1899-12-30 epoch.
        if (typeof value === "number" && Number.isFinite(value)) {
            if (value < 1 || value > 100000) return null;
            const ms = Math.round((value - 25569) * 86400000);
            const dt = new Date(ms);
            if (Number.isNaN(dt.getTime())) return null;
            return dt.toISOString().slice(0, 10);
        }

        const s = norm(value);
        const m = s.match(
            /^(\d{4})[\/.-](\d{1,2})[\/.-](\d{1,2})$/
        );

        if (!m) return null;

        const y = +m[1], mo = +m[2], d = +m[3];

        if (mo < 1 || mo > 12 || d < 1 || d > 31)
            return null;

        // Gregorian date
        if (y >= 1700) {
            const dt = new Date(Date.UTC(y, mo - 1, d));
            const result = dt.toISOString().slice(0, 10);
            return result ===
                `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`
                ? result : null;
        }

        // Jalali date — reject out-of-range years (no silent year inventing).
        if (y < 1200 || y > 1600) return null;

        let low = Math.floor(
            Date.UTC(y + 620, 0, 1) / 86400000
        );
        let high = Math.floor(
            Date.UTC(y + 622, 11, 31) / 86400000
        );

        while (low <= high) {
            const mid = Math.floor((low + high) / 2);
            const dt = new Date(mid * 86400000);

            const parts = persianFormatter.formatToParts(dt);
            const n = type => Number(
                parts.find(p => p.type === type).value
            );

            const cmp =
                n("year") - y ||
                n("month") - mo ||
                n("day") - d;

            if (!cmp) return dt.toISOString().slice(0, 10);
            if (cmp < 0) low = mid + 1;
            else high = mid - 1;
        }

        return null;
    }

    function clock(v) {
        const s = norm(v);
        if (!s) return null;

        const m = s.match(
            /^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/
        );

        if (!m || +m[1] > 23 ||
            +m[2] > 59 || +(m[3] || 0) > 59)
            throw Error("Invalid time: " + s);

        return [m[1], m[2], m[3] || "00"]
            .map(x => String(+x).padStart(2, "0"))
            .join(":");
    }

    function parseRow(row, rowNumber) {
        const rawDate = row[C.date];
        const date = convertDate(rawDate);
        if (!date) return null;

        const get = key => duration(row[C[key]]);

        const keys = [
            "paidLeave", "unpaidLeave", "nightTotal",
            "nightRegular", "mission", "unauthorizedExit",
            "early", "late", "absence",
            "nightOTRegular", "nightOTApproved",
            "missionOTRegular", "missionOTApproved",
            "holidayOTRegular", "holidayOTApproved",
            "normalOTRegular", "normalOTApproved",
            "presence", "worked"
        ];

        const d = {};
        keys.forEach(k => d[k] = get(k));

        const inClock = clock(row[C.inClock]);
        const outClock = clock(row[C.outClock]);

        const inDate = norm(row[C.inDate])
            ? convertDate(row[C.inDate]) : date;
        const outDate = norm(row[C.outDate])
            ? convertDate(row[C.outDate]) : date;

        if (inClock && !inDate)
            throw Error("Invalid IN date");
        if (outClock && !outDate)
            throw Error("Invalid OUT date");

        const inTime = inClock
            ? inDate + " " + inClock : null;
        const outTime = outClock
            ? outDate + " " + outClock : null;

        if (inTime && outTime && outTime <= inTime)
            throw Error("OUT must be after IN");

        return {
            row: rowNumber,
            date,
            rawDate,
            code: codeKey(row[C.code]),
            name: [
                norm(row[C.firstName]),
                norm(row[C.lastName])
            ].filter(Boolean).join(" "),
            inTime,
            outTime,
            details: d,
            // Legacy sum of all *unauthorized* OT columns (audit / calculation_details only).
            regularOT:
                d.nightOTRegular +
                d.missionOTRegular +
                d.holidayOTRegular +
                d.normalOTRegular,
            // عادی غیرمجاز alone — dual-maps to regular_work + extra_presence.
            unauthorizedNormalOT: d.normalOTRegular,
            // خروج غیرمجاز + تعجیل + تأخیر (absence excluded).
            workShortage:
                d.unauthorizedExit + d.early + d.late,
            // Approved OT = عادی مجاز + تعطیل مجاز ONLY (night/mission approved excluded).
            approvedOT:
                d.holidayOTApproved + d.normalOTApproved,
            workGroup: norm(row[C.workGroup]),
            weekday: norm(row[C.weekday])
        };
    }

    function excelImportStatus(details, inTime, outTime) {
        // Precedence (importer-only):
        // 1) Mission + absence both > 0 → conflict (classify as Review; defensive Absent)
        // 2) Absence > 0 → Absent
        // 3) Mission > 0 → Present (mission hours still mapped; status is not On Duty)
        // 4) Presence / worked / IN-OUT → Present
        // Blank/zero absence never forces Absent.
        const d = details || {};
        const mission = d.mission || 0;
        const absence = d.absence || 0;
        if (mission > 0 && absence > 0) return "Absent";
        if (absence > 0) return "Absent";
        if (mission > 0) return "Present";
        if ((d.presence || 0) > 0 || (d.worked || 0) > 0 || inTime || outTime)
            return "Present";
        return "Present";
    }

    function workbookDateYearStats(records) {
        const counts = new Map();
        for (const r of records) {
            const y = Number(String(r.date || "").slice(0, 4));
            if (!Number.isFinite(y)) continue;
            counts.set(y, (counts.get(y) || 0) + 1);
        }
        let modeYear = null, modeCount = 0;
        for (const [y, c] of counts) {
            if (c > modeCount) { modeYear = y; modeCount = c; }
        }
        return { counts, modeYear, modeCount, total: records.length };
    }

    function rejectOutlierWorkbookYears(records, errors) {
        // Block minority Gregorian years that are far from the workbook majority.
        // Does not hardcode 1405 — uses the actual parsed distribution.
        const { counts, modeYear, modeCount, total } = workbookDateYearStats(records);
        if (!modeYear || total < 5) return records;
        const kept = [];
        for (const r of records) {
            const y = Number(String(r.date || "").slice(0, 4));
            const share = (counts.get(y) || 0) / total;
            // Keep the mode year and any year that is itself a substantial cluster (>= 10%).
            if (y === modeYear || share >= 0.10) {
                kept.push(r);
                continue;
            }
            errors.push({
                row: r.row,
                error: `Date year ${y} is outside the workbook majority year ${modeYear} ` +
                    `(${modeCount}/${total} rows). Review the Excel date cell before import.`
            });
        }
        return kept;
    }

    async function api(method, args) {
        const r = await frappe.call({ method, args });
        return r.message;
    }

    function sheetjsReady() {
        return typeof window.XLSX !== "undefined"
            && window.XLSX
            && typeof window.XLSX.read === "function";
    }

    function loadSheetJsScript(url, timeoutMs) {
        return new Promise((resolve, reject) => {
            const existing = document.querySelector(
                'script[data-espad-attendance-sheetjs="1"]'
            );
            if (existing && sheetjsReady()) {
                resolve();
                return;
            }
            if (existing && !sheetjsReady()) {
                existing.addEventListener(
                    "load",
                    () => (sheetjsReady()
                        ? resolve()
                        : reject(Error(
                            "Excel library loaded but XLSX.read is missing: "
                            + url
                        ))),
                    { once: true }
                );
                existing.addEventListener(
                    "error",
                    () => reject(Error(
                        "Excel library could not load (script error): " + url
                    )),
                    { once: true }
                );
                return;
            }

            const script = document.createElement("script");
            script.src = url;
            script.async = true;
            script.dataset.espadAttendanceSheetjs = "1";
            let done = false;
            const timer = setTimeout(() => {
                if (done) return;
                done = true;
                script.remove();
                reject(Error(
                    "Excel library could not load (timeout): " + url
                ));
            }, timeoutMs);
            script.onload = () => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                if (sheetjsReady()) resolve();
                else reject(Error(
                    "Excel library loaded but XLSX.read is missing: " + url
                ));
            };
            script.onerror = () => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                script.remove();
                reject(Error(
                    "Excel library could not load (network/asset error): " + url
                ));
            };
            document.head.appendChild(script);
        });
    }

    async function loadXLSX() {
        if (sheetjsReady()) return;

        const url = CONFIG.sheetJsUrl;
        // Prefer frappe.require when available; fall back to a single script tag.
        if (frappe.require) {
            try {
                await frappe.require(url);
                if (sheetjsReady()) return;
            } catch (e) {
                // fall through to script injection
            }
        }
        await loadSheetJsScript(url, CONFIG.sheetJsTimeoutMs);
        if (!sheetjsReady()) {
            throw Error(
                "Excel library could not load: " + url
            );
        }
    }

    NS.open = function () {
        ensureAttImportCss();

        const state = {
            records: [],
            people: new Map(),
            existing: new Map(),
            fields: new Set(),
            errors: [],
            fileName: "",
            busy: false,
            importing: false,
            allowClose: false,
            previewReady: false,
            previewVersion: 0,
            employeeCodeIndex: new Map(),
            fieldMap: new Map(),
            shiftCache: new Map(),
            holidayCache: new Map(),
            context: null,
            timings: {},
            progress: null,
            logLines: []
        };

        const dialog = new frappe.ui.Dialog({
            title: "Import Attendance Details (Excel)",
            size: "extra-large",
            fields: [
                {
                    fieldtype: "HTML",
                    fieldname: "file_html",
                    options: `
                        <div style="padding:8px 0">
                            <label class="control-label">
                                Select Attendance Excel File
                            </label>
                            <input type="file"
                                class="form-control espad-excel-file"
                                accept=".xlsx,.xls"
                                style="display:block;width:100%">
                        </div>`
                },
                {
                    fieldtype: "Section Break",
                    label: "Employee Selection"
                },
                {
                    fieldtype: "HTML",
                    fieldname: "employee_html"
                },
                {
                    fieldtype: "Section Break",
                    label: "Import Preview"
                },
                {
                    fieldtype: "HTML",
                    fieldname: "preview_html"
                },
                {
                    fieldtype: "Check",
                    fieldname: "confirm",
                    label: "I reviewed the preview and authorize Attendance updates, including submitted records",
                    default: 0
                },
                {
                    fieldtype: "Section Break",
                    label: "Import Progress"
                },
                {
                    fieldtype: "HTML",
                    fieldname: "progress_html"
                }
            ],
            primary_action_label: "Import Selected Attendance",
            primary_action: () => importRecords(),
            secondary_action_label: "Close",
            secondary_action: () => requestClose()
        });

        dialog.$wrapper.addClass("espad-att-import-dlg");
        dialog.show();

        const employeeArea =
            dialog.fields_dict.employee_html.$wrapper;
        const previewArea =
            dialog.fields_dict.preview_html.$wrapper;
        const progressArea =
            dialog.fields_dict.progress_html.$wrapper;

        dialog.$wrapper.find(".modal-body").prepend(
            `<div class="espad-att-stats" style="padding:4px 8px"></div>
             <div class="espad-att-log text-muted small" style="display:none;max-height:120px;overflow:auto;padding:4px 8px;border:1px solid var(--border-color);border-radius:6px;margin-bottom:8px"></div>`
        );

        employeeArea.html(
            '<p class="text-muted">Upload Excel to load employees.</p>'
        );
        previewArea.empty();
        progressArea.html(`
            <div class="espad-att-progress" data-espad-att-progress>
                <div class="title">Import Progress</div>
                <div class="meta"></div>
                <div class="espad-att-bar"><i></i></div>
                <div class="op"></div>
            </div>
            <div class="espad-att-term" data-espad-att-term></div>
            <div class="espad-att-result" data-espad-att-result></div>
        `);

        const primary = dialog.get_primary_btn();
        primary.prop("disabled", true);

        dialog.$wrapper.find(".btn-modal-close, .modal-header .close").on(
            "click.espadAtt",
            function (e) {
                if (state.importing && !state.allowClose) {
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    requestClose();
                    return false;
                }
            }
        );

        function requestClose() {
            if (state.importing && !state.allowClose) {
                frappe.confirm(
                    "Import is still running. Closing will not cancel in-flight server requests and may leave a partial import. Close anyway?",
                    () => {
                        state.allowClose = true;
                        dialog.hide();
                    }
                );
                return;
            }
            dialog.hide();
        }

        function stampRate(processed, total, t0) {
            if (!state.progress) return;
            const elapsed = Math.max(0.001, (performance.now() - t0) / 1000);
            const rate = processed / elapsed;
            state.progress.elapsed_sec = elapsed;
            state.progress.rate = rate;
            state.progress.eta_sec = rate > 0
                ? Math.max(0, total - processed) / rate
                : 0;
        }

        function updateProgressDom() {
            const p = state.progress;
            const root = progressArea.find("[data-espad-att-progress]");
            if (!root.length || !p) return;
            root.addClass("visible");
            const pct = p.total
                ? Math.min(100, Math.round((p.processed / p.total) * 100))
                : (p.done ? 100 : 0);
            root.find(".title").text(p.heading || "Import Progress");
            root.find(".meta").html(`
                <div>Progress: <b>${pct}%</b></div>
                <div>Processed: <b>${p.processed}</b> / <b>${p.total}</b></div>
                <div>Elapsed: <b>${fmtTime(p.elapsed_sec)}</b></div>
                <div>Created: <b>${p.created}</b></div>
                <div>Updated: <b>${p.updated}</b></div>
                <div>Skipped: <b>${p.skipped}</b></div>
                <div>Failed: <b>${p.failed}</b></div>
                <div>Batch: <b>${p.batch}</b> / <b>${p.batches}</b></div>
                <div>Rate: <b>${(p.rate || 0).toFixed(1)}/s</b></div>
                <div style="grid-column: span 2">Status: <b>${esc(p.status || "-")}</b></div>
            `);
            root.find(".espad-att-bar > i").css("width", pct + "%");
            root.find(".op").text(p.operation || "");
        }

        function termAppend(lines) {
            const el = progressArea.find("[data-espad-att-term]");
            if (!el.length) return;
            el.addClass("visible");
            (lines || []).forEach((L) => {
                state.logLines.push(L);
                el.append(
                    `<div class="${esc(L.cls || "info")}">${esc(L.text)}</div>`
                );
            });
            if (state.logLines.length > 800) {
                state.logLines = state.logLines.slice(-800);
            }
            const node = el[0];
            if (node) node.scrollTop = node.scrollHeight;
        }

        function showInlineResult(payload) {
            const box = progressArea.find("[data-espad-att-result]");
            if (!box.length) return;
            const failedHtml = (payload.failed || []).map((x) => `
                <div class="err text-danger">
                    Row ${esc(x.row)} — ${esc(x.employee)} — ${esc(x.date)}:
                    ${esc(x.error)}
                </div>
            `).join("");
            const skippedHtml = (payload.skipped || []).slice(0, 100).map((x) => `
                <div class="muted">${esc(x)}</div>
            `).join("");
            const moreSkipped = (payload.skipped || []).length > 100
                ? `<div class="muted">… and ${(payload.skipped || []).length - 100} more skipped</div>`
                : "";
            box.addClass("visible").html(`
                <h4>Import Result</h4>
                <p>Created: <b>${payload.created}</b></p>
                <p>Updated: <b>${payload.updated}</b></p>
                <p>Skipped: <b>${(payload.skipped || []).length}</b></p>
                <p>Failed: <b>${(payload.failed || []).length}</b></p>
                <p>Duration: <b>${payload.durationMs} ms</b>
                    (${payload.rate} records/s)</p>
                <div class="espad-att-fail-list">
                    ${failedHtml || '<div class="muted">No failed rows.</div>'}
                    ${skippedHtml}
                    ${moreSkipped}
                </div>
            `);
        }

        function mappedRows() {
            return state.records.map(r => {
                const p = state.people.get(r.code);
                return {
                    ...r,
                    employee: p?.employee || null,
                    included: !!p?.included
                };
            });
        }

        function selectedRows() {
            return mappedRows().filter(
                r => r.included && r.employee
            );
        }

        // Match Excel personnel code to Employee.custom_personal_code.
        // Example: Excel 67 must match Employee 000067.
        // Uses the prebuilt index only (no per-person get_list).
        function matchEmployee(person) {
            const key = codeKey(person.code);
            if (!key) return { employee: null, ambiguous: false };
            const names = [...new Set(state.employeeCodeIndex.get(key) || [])];
            if (names.length > 1) {
                console.warn("Ambiguous personnel code", person.code, names);
                return { employee: null, ambiguous: true, candidates: names };
            }
            if (names.length === 1)
                return { employee: names[0], ambiguous: false };
            return { employee: null, ambiguous: false };
        }

        async function buildEmployeeIndex() {
            // Server-side index: client frappe.db.get_list defaults limit=20 and
            // ignores limit_page_length; custom_personal_code is also permlevel 1
            // and may be stripped for HR Manager. The whitelist returns only
            // employees the current user may read.
            state.employeeCodeIndex = new Map();
            const message = await api(
                "erpnext_extensions.extentionhrms.attendance_excel_import.get_personnel_code_index",
                {}
            );
            const index = (message && message.index) || {};
            for (const [key, names] of Object.entries(index)) {
                if (!key || !Array.isArray(names) || !names.length) continue;
                state.employeeCodeIndex.set(key, names.slice());
            }
        }

        function renderEmployees() {
            employeeArea.html(`
                <div class="espad-employee-selector">
                    <div style="display:flex;gap:8px;
                        align-items:center;flex-wrap:wrap;
                        margin-bottom:12px">

                        <input type="text"
                            class="form-control emp-search"
                            placeholder="Search Employee / Code"
                            style="max-width:300px">

                        <button type="button"
                            class="btn btn-default btn-sm emp-all">
                            Select All
                        </button>

                        <button type="button"
                            class="btn btn-default btn-sm emp-none">
                            Exclude All
                        </button>

                        <span class="emp-count text-muted"></span>
                    </div>

                    <div style="max-height:330px;overflow:auto;
                        border:1px solid var(--border-color);
                        border-radius:6px">

                        <table class="table table-bordered table-sm"
                            style="margin:0">
                            <thead>
                                <tr>
                                    <th>Include</th>
                                    <th>Code</th>
                                    <th>Employee Name</th>
                                    <th>Matched Employee</th>
                                    <th>Days</th>
                                    <th>Action</th>
                                </tr>
                            </thead>
                            <tbody class="emp-rows"></tbody>
                        </table>
                    </div>
                </div>
            `);

            const root = employeeArea.find(
                ".espad-employee-selector"
            );
            const tbody = root.find(".emp-rows");

            function updateCount() {
                const count = [...state.people.values()]
                    .filter(p => p.included).length;

                root.find(".emp-count").text(
                    `${count} / ${state.people.size} selected`
                );
            }

            function updateRow(row, p) {
                row.find(".emp-check")
                    .prop("checked", p.included);

                row.find(".emp-match").text(
                    p.employee || "Not Matched"
                );

                row.find(".emp-status").text(
                    p.included ? "Included" : "Excluded"
                );
            }

            for (const p of state.people.values()) {
                const row = $(`
                    <tr class="emp-item">
                        <td>
                            <input type="checkbox"
                                class="emp-check">
                        </td>
                        <td class="emp-code"></td>
                        <td class="emp-name"></td>
                        <td class="emp-match"></td>
                        <td class="emp-days"></td>
                        <td>
                            <button type="button"
                                class="btn btn-default btn-xs emp-change">
                                Change Employee
                            </button>
                            <span class="emp-status"></span>
                        </td>
                    </tr>
                `);

                row.find(".emp-code").text(p.code);
                row.find(".emp-name").text(p.name);
                row.find(".emp-days").text(p.days);

                tbody.append(row);
                updateRow(row, p);

                row.find(".emp-check").on("change", function () {
                    p.included = this.checked;
                    updateRow(row, p);
                    updateCount();
                    invalidatePreview();
                });

                row.find(".emp-change").on("click", function () {
                    const lookup = new frappe.ui.Dialog({
                        title: "Select Employee: " + p.name,
                        fields: [{
                            fieldtype: "Link",
                            fieldname: "employee",
                            label: "Employee",
                            options: "Employee",
                            reqd: 1,
                            default: p.employee || ""
                        }],
                        primary_action_label: "Apply",
                        primary_action(values) {
                            p.employee = values.employee;
                            updateRow(row, p);
                            lookup.hide();
                            invalidatePreview();
                        }
                    });

                    lookup.show();
                });
            }

            root.find(".emp-all").on("click", () => {
                state.people.forEach(p => p.included = true);

                tbody.find(".emp-item").each(function (i) {
                    const p = [...state.people.values()][i];
                    updateRow($(this), p);
                });

                updateCount();
                invalidatePreview();
            });

            root.find(".emp-none").on("click", () => {
                state.people.forEach(p => p.included = false);

                tbody.find(".emp-item").each(function (i) {
                    const p = [...state.people.values()][i];
                    updateRow($(this), p);
                });

                updateCount();
                invalidatePreview();
            });

            root.find(".emp-search").on("input", function () {
                const q = norm(this.value).toLowerCase();
                const people = [...state.people.values()];

                tbody.find(".emp-item").each(function (i) {
                    const p = people[i];
                    const searchText = norm(
                        `${p.code} ${p.name} ${p.employee || ""}`
                    ).toLowerCase();

                    $(this).toggle(searchText.includes(q));
                });
            });

            updateCount();
        }

        async function fetchExisting(rows) {
            const map = new Map();
            if (!rows.length) return map;
            const keys = rows
                .filter(r => r.employee && r.date)
                .map(r => ({ employee: r.employee, attendance_date: r.date }));
            const page = 2000;
            for (let start = 0; start < keys.length; start += page) {
                const chunk = keys.slice(start, start + page);
                const message = await api(
                    "erpnext_extensions.extentionhrms.attendance_excel_import.find_existing_attendance",
                    { keys: chunk }
                );
                for (const a of (message && message.rows) || []) {
                    const key = a.employee + "|" + a.attendance_date;
                    if (map.has(key)) map.get(key).duplicate = true;
                    else map.set(key, a);
                }
            }
            return map;
        }

        async function preloadContext(employees) {
            state.context = {
                employees: {},
                assignments: {},
                shift_types: {},
                schedule_assignments: {},
                schedules: {},
                holiday_lists: {}
            };
            if (!employees.length) return;
            const page = 400;
            for (let i = 0; i < employees.length; i += page) {
                const chunk = employees.slice(i, i + page);
                const message = await api(
                    "erpnext_extensions.extentionhrms.attendance_excel_import.preload_attendance_import_context",
                    { employees: chunk }
                );
                Object.assign(state.context.employees, message.employees || {});
                Object.assign(state.context.assignments, message.assignments || {});
                Object.assign(state.context.shift_types, message.shift_types || {});
                Object.assign(state.context.schedule_assignments, message.schedule_assignments || {});
                Object.assign(state.context.schedules, message.schedules || {});
                Object.assign(state.context.holiday_lists, message.holiday_lists || {});
            }
            // Normalize holiday lists to Set for fast has()
            for (const [name, dates] of Object.entries(state.context.holiday_lists)) {
                state.holidayCache.set(name, new Set(dates || []));
            }
        }

        function paintStats() {
            const people = [...state.people.values()];
            const matched = people.filter(p => p.employee).length;
            const unmatched = people.filter(p => !p.employee).length;
            const ambiguous = people.filter(p => p.ambiguous).length;
            const stats = dialog.$wrapper.find(".espad-att-stats");
            if (!stats.length) return;
            stats.html(`
                <div style="display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 12px">
                    <div class="espad-stat"><div class="text-muted">Personnel</div><b>${people.length}</b></div>
                    <div class="espad-stat"><div class="text-muted">Matched</div><b>${matched}</b></div>
                    <div class="espad-stat"><div class="text-muted">Unmatched</div><b>${unmatched}</b></div>
                    <div class="espad-stat"><div class="text-muted">Ambiguous</div><b>${ambiguous}</b></div>
                    <div class="espad-stat"><div class="text-muted">Excel rows</div><b>${state.records.length}</b></div>
                    <div class="espad-stat"><div class="text-muted">Invalid rows</div><b>${state.errors.length}</b></div>
                </div>`);
            const log = dialog.$wrapper.find(".espad-att-log");
            if (!log.length) return;
            const t = state.timings || {};
            const parts = [];
            if (state.fileName) parts.push("File: " + state.fileName);
            if (t.libraryMs != null) parts.push("Library " + t.libraryMs + " ms");
            if (t.parseMs != null) parts.push("Parse " + t.parseMs + " ms");
            if (t.matchMs != null) parts.push("Match " + t.matchMs + " ms");
            if (t.preloadMs != null) parts.push("Preload " + t.preloadMs + " ms");
            if (t.existingMs != null) parts.push("Existing " + t.existingMs + " ms");
            if (t.importMs != null) parts.push("Import " + t.importMs + " ms");
            if (parts.length) {
                log.text(parts.join(" · ")).show();
            } else {
                log.hide().empty();
            }
        }

        function classify(r, existing, duplicates) {
            if (!r.included)
                return ["Exclude", "Employee excluded"];

            if (!r.employee)
                return ["Skip", "Employee not matched"];

            const key = r.employee + "|" + r.date;

            if (duplicates.has(key))
                return ["Review", "Duplicate Excel employee/date"];

            const a = existing.get(key);

            if (a?.duplicate)
                return ["Review", "Duplicate Attendance in ERP"];

            // Submitted records are eligible for the dedicated controlled API.
            // Cancelled records are excluded by fetchExisting().

            if (a?.status === "On Leave" || a?.status === "Half Day")
                return ["Review", "Existing Leave Attendance"];

            const d = r.details || {};
            const missionPresent =
                (d.mission || 0) > 0 && (d.absence || 0) <= 0;

            // Incomplete punch pair normally needs review. Exception: Excel
            // mission > 0 with blank/zero absence — punches are not required
            // for mission days (status Present + mission hours).
            if (!!r.inTime !== !!r.outTime && !missionPresent)
                return ["Review", "Missing IN / OUT"];

            if (
                r.approvedOT > 0 &&
                !state.fields.has(CONFIG.approvedOT)
            ) {
                return ["Review", "Approved OT field missing"];
            }

            // Mission + absence conflict: never silently choose a status.
            if ((d.mission || 0) > 0 && (d.absence || 0) > 0)
                return ["Review", "Mission and absence conflict"];

            if (!r.inTime && !r.outTime) {
                // Paid/unpaid leave without punches still needs human review.
                // Pure mission days import as Present with mission hours.
                if ((d.paidLeave || 0) > 0 || (d.unpaidLeave || 0) > 0) {
                    if ((d.mission || 0) > 0)
                        return ["Review", "Mission and leave need verification"];
                    return ["Review", "Leave needs verification"];
                }

                // Excel حضور / کارکرد / مأموریت / غیبت are authoritative —
                // missing punches alone must not block import.
                if (
                    d.presence <= 0 &&
                    d.worked <= 0 &&
                    d.absence <= 0 &&
                    (d.mission || 0) <= 0 &&
                    !a
                )
                    return ["Review", "No punches / absence"];
            }

            return a
                ? ["Update", Number(a.docstatus) === 1 ? "Existing Submitted" : "Existing Draft"]
                : ["Create", "New Attendance"];
        }

        function duplicateKeys(rows) {
            const seen = new Set();
            const duplicates = new Set();

            for (const r of rows) {
                if (!r.included || !r.employee) continue;
                const key = r.employee + "|" + r.date;

                if (seen.has(key))
                    duplicates.add(key);
                seen.add(key);
            }

            return duplicates;
        }

        function invalidatePreview() {
            state.previewReady = false;
            state.previewVersion++;
            primary.prop("disabled", true);
            previewArea.html(
                '<p class="text-muted">Employee selection changed. Click Refresh Preview.</p>'
            );
            renderPreviewButton();
        }

        function renderPreviewButton() {
            // Keep one refresh button, even after replacing preview content.
            previewArea.find(".espad-refresh").remove();
            // Use btn-default so this never competes with the dialog's
            // primary "Import Selected Attendance" button (btn-primary).
            const button = $("<button>", {
                type: "button",
                class: "btn btn-default btn-sm espad-refresh",
                text: "Refresh Preview"
            }).css("margin-bottom", "12px");
            button.on("click", () => refreshPreview());
            previewArea.prepend(button);
        }

        async function refreshPreview() {
            if (state.busy) return;

            const version = ++state.previewVersion;
            state.previewReady = false;
            primary.prop("disabled", true);
            previewArea.html('<p>Checking existing Attendance records...</p>');
            renderPreviewButton();

            try {
                // Preview only the Excel rows belonging to selected employees.
                // Include unmatched selections so their Skip reason is visible.
                const rows = mappedRows().filter(r => r.included);
                const active = rows.filter(r => r.employee);
                const tExisting = performance.now();
                const existing = await fetchExisting(active);
                state.timings.existingMs = Math.round(performance.now() - tExisting);
                paintStats();
                if (version !== state.previewVersion) return;

                state.existing = existing;
                const duplicates = duplicateKeys(rows);
                const counts = { Create: 0, Update: 0, Review: 0, Skip: 0 };
                const classified = rows.map(r => {
                    const [action, reason] = classify(r, existing, duplicates);
                    if (counts[action] !== undefined) counts[action]++;
                    return { ...r, action, reason };
                });
                const selected = [...state.people.values()]
                    .filter(p => p.included).length;

                previewArea.html(`
                    <div style="margin:8px 0">
                        <b>Employees:</b> ${selected} / ${state.people.size}
                        &nbsp; | &nbsp;
                        <b>Daily Rows:</b> ${rows.length}
                        &nbsp; | &nbsp;
                        <b>Excel Errors:</b> ${state.errors.length}
                    </div>
                    <div style="margin-bottom:12px">
                        <span class="indicator-pill green">Create: ${counts.Create}</span>
                        <span class="indicator-pill blue">Update: ${counts.Update}</span>
                        <span class="indicator-pill orange">Review: ${counts.Review}</span>
                        <span class="indicator-pill gray">Skip: ${counts.Skip}</span>
                    </div>
                    <p class="text-muted small">
                        Showing first ${Math.min(CONFIG.previewLimit, rows.length)}
                        of ${rows.length} selected Excel rows. Hours are Excel-authoritative.
                        Regular Work Hours and Extra Presence Hours both come from عادی غیرمجاز.
                    </p>
                    <div style="max-height:330px;overflow:auto">
                        <table class="table table-bordered table-sm" style="font-size:11px">
                            <thead><tr>
                                <th>Excel Row</th><th>Employee</th><th>Date</th><th>Status</th>
                                <th>Paid Leave</th><th>Unpaid Leave</th>
                                <th>Night</th><th>Mission</th><th>Shortage</th>
                                <th>Regular Work</th><th>Extra Presence</th>
                                <th>Approved OT</th><th>Holiday</th><th>Working Hrs</th>
                                <th>Action</th><th>Reason</th>
                            </tr></thead>
                            <tbody>${classified.slice(0, CONFIG.previewLimit).map(r => {
                                const d = r.details || {};
                                const status = excelImportStatus(d, r.inTime, r.outTime);
                                const unauth = hours(r.unauthorizedNormalOT || 0);
                                return `
                                <tr>
                                    <td>${r.row}</td><td>${esc(r.name)}</td>
                                    <td>${esc(r.date)}</td><td>${esc(status)}</td>
                                    <td>${hours(d.paidLeave || 0)}</td>
                                    <td>${hours(d.unpaidLeave || 0)}</td>
                                    <td>${hours(d.nightTotal || 0)}</td>
                                    <td>${hours(d.mission || 0)}</td>
                                    <td>${hours(r.workShortage || 0)}</td>
                                    <td>${unauth}</td><td>${unauth}</td>
                                    <td>${hours(r.approvedOT || 0)}</td>
                                    <td>${hours(d.holidayOTApproved || 0)}</td>
                                    <td>${hours(d.presence || 0)}</td>
                                    <td>${esc(r.action)}</td><td>${esc(r.reason)}</td>
                                </tr>`;
                            }).join("")}</tbody>
                        </table>
                    </div>
                `);
                renderPreviewButton();
                state.previewReady = selected > 0 && active.length > 0;
                const confirmed = Number(dialog.get_value("confirm") || 0) === 1;
                primary.prop("disabled",
                    !state.previewReady ||
                    !confirmed ||
                    counts.Create + counts.Update === 0);
            } catch (e) {
                if (version !== state.previewVersion) return;
                console.error(e);
                previewArea.text("Preview error: " + (e.message || e));
                renderPreviewButton();
            }
        }

        dialog.fields_dict.confirm.$input.on(
            "change",
            function () {
                if (state.previewReady && !state.busy) {
                    const confirmed = Number(dialog.get_value("confirm") || 0) === 1;
                    primary.prop(
                        "disabled",
                        !confirmed ||
                        !selectedRows().some(r => {
                            const [action] = classify(r, state.existing, duplicateKeys(selectedRows()));
                            return action === "Create" || action === "Update";
                        })
                    );
                }
            }
        );

        dialog.$wrapper.find(".espad-excel-file")
            .on("change", async function () {
                const file = this.files?.[0];
                if (!file) return;

                state.busy = true;
                state.previewReady = false;
                state.fileName = file.name;
                primary.prop("disabled", true);
                state.timings = {};

                let phase = "load library";
                employeeArea.text("Phase: " + phase + "…");
                previewArea.empty();

                try {
                    const t0 = performance.now();
                    await loadXLSX();
                    state.timings.libraryMs = Math.round(performance.now() - t0);

                    phase = "parse Excel";
                    employeeArea.text(
                        "Phase: " + phase + "… (SheetJS "
                        + (window.XLSX.version || "ready") + ")"
                    );
                    const tParse = performance.now();
                    const wb = XLSX.read(
                        await file.arrayBuffer(),
                        { type: "array" }
                    );

                    const sheet = wb.Sheets[wb.SheetNames[0]];

                    const data = XLSX.utils.sheet_to_json(
                        sheet, {
                            header: 1,
                            raw: false,
                            defval: ""
                        }
                    );

                    state.records = [];
                    state.people.clear();
                    state.errors = [];
                    state.existing.clear();

                    state.fields = new Set(
                        frappe.get_meta("Attendance")
                            .fields.map(f => f.fieldname)
                    );

                    if (!state.fields.has(CONFIG.regularOT))
                        throw Error(
                            "Missing field: " + CONFIG.regularOT
                        );

                    for (let i = 0; i < data.length; i++) {
                        if (!convertDate(data[i][C.date]))
                            continue;

                        try {
                            const r = parseRow(data[i], i + 1);
                            if (!r) continue;

                            state.records.push(r);

                            if (!r.code) continue;

                            if (!state.people.has(r.code)) {
                                state.people.set(r.code, {
                                    code: r.code,
                                    name: r.name,
                                    employee: null,
                                    included: false,
                                    days: 0
                                });
                            }

                            state.people.get(r.code).days++;

                        } catch (e) {
                            state.errors.push({
                                row: i + 1,
                                error: e.message
                            });
                        }
                    }
                    state.timings.parseMs = Math.round(performance.now() - tParse);

                    if (!state.records.length)
                        throw Error("No valid daily rows found");

                    // Reject minority Gregorian years outside the workbook majority cluster.
                    state.records = rejectOutlierWorkbookYears(
                        state.records, state.errors
                    );
                    // Rebuild personnel index from retained rows only.
                    state.people.clear();
                    for (const r of state.records) {
                        if (!r.code) continue;
                        if (!state.people.has(r.code)) {
                            state.people.set(r.code, {
                                code: r.code,
                                name: r.name,
                                employee: null,
                                included: false,
                                days: 0
                            });
                        }
                        state.people.get(r.code).days++;
                    }

                    if (!state.records.length)
                        throw Error(
                            "No valid daily rows remain after workbook date-year validation"
                        );

                    phase = "match employees";
                    employeeArea.text(
                        "Phase: " + phase + "… (" + state.people.size
                        + " personnel, " + state.records.length + " rows)"
                    );

                    const tMatch = performance.now();
                    await buildEmployeeIndex();
                    for (const p of state.people.values()) {
                        const hit = matchEmployee(p);
                        p.employee = hit.employee;
                        p.ambiguous = !!hit.ambiguous;
                        p.candidates = hit.candidates || null;
                        p.included = !!p.employee;
                    }
                    state.timings.matchMs = Math.round(performance.now() - tMatch);
                    const matchedEmployees = [...new Set(
                        [...state.people.values()].map(p => p.employee).filter(Boolean)
                    )];
                    phase = "preload shift context";
                    employeeArea.text(
                        "Phase: " + phase + "… ("
                        + matchedEmployees.length + " employees)"
                    );
                    const tPre = performance.now();
                    await preloadContext(matchedEmployees);
                    state.timings.preloadMs = Math.round(performance.now() - tPre);

                    renderEmployees();
                    paintStats();

                    state.busy = false;
                    phase = "preview";
                    await refreshPreview();

                } catch (e) {
                    state.busy = false;
                    console.error(e);

                    employeeArea.html(
                        "<div class='text-danger' style='padding:8px'>"
                        + "<b>Excel error</b> (phase: " + esc(phase) + ")<br>"
                        + esc(e.message || e)
                        + "</div>"
                    );
                    previewArea.empty();
                }
            });

                // Resolve fields from actual Attendance metadata; never assume custom fieldnames.
        function fieldName(label, candidates = []) {
            const meta = frappe.get_meta("Attendance");
            const all = [...(meta.fields || [])];
            for (const candidate of candidates) {
                if (all.some(f => f.fieldname === candidate)) return candidate;
            }
            const matches = all.filter(f => norm(f.label).toLowerCase() === norm(label).toLowerCase());
            return matches.length === 1 ? matches[0].fieldname : null;
        }

        function setHours(doc, label, value, candidates = []) {
            const f = fieldName(label, candidates);
            if (!f) throw Error("Missing Attendance numeric field: " + label +
                " (candidates: " + candidates.join(", ") + ")");
            const df = frappe.get_meta("Attendance").fields.find(x => x.fieldname === f);
            if (!["Float", "Currency", "Int", "Percent"].includes(df.fieldtype))
                throw Error(`${label}: ${f} is ${df.fieldtype}, not a numeric hours field`);
            doc[f] = hours(Math.max(0, value));
            return true;
        }

        function setCheck(doc, label, value, candidates = []) {
            const f = fieldName(label, candidates);
            if (!f) throw Error("Missing Attendance checkbox: " + label);
            const df = frappe.get_meta("Attendance").fields.find(x => x.fieldname === f);
            if (df.fieldtype !== "Check")
                throw Error(label + " must be a Check field; found " + df.fieldtype);
            doc[f] = value ? 1 : 0;
        }

        function weekday(date) {
            return new Date(date + "T12:00:00Z").getUTCDay();
        }

        function addDay(date) {
            const x = new Date(date + "T00:00:00Z");
            x.setUTCDate(x.getUTCDate() + 1);
            return x.toISOString().slice(0, 10);
        }

        // Historical back-projection is used ONLY for selected Excel rows.
        // It never creates Shift Assignments or Attendance outside the Excel dates.
        async function resolveSchedule(r) {
            const ctx = state.context || {};
            let assignments = (ctx.assignments && ctx.assignments[r.employee]) || [];
            if (!assignments.length) {
                // Fallback for employees added after preload.
                assignments = await frappe.db.get_list("Shift Assignment", {
                    filters: { employee: r.employee, docstatus: 1, status: "Active" },
                    fields: ["name", "shift_type", "start_date", "end_date", "shift_schedule_assignment"],
                    order_by: "start_date asc",
                    limit_page_length: 500
                });
            }
            if (assignments.length === 500)
                throw Error("Too many Shift Assignments; cannot safely resolve historical shift");

            const effective = assignments.filter(a =>
                a.start_date <= r.date && (!a.end_date || a.end_date >= r.date));
            let a;
            let historical = false;
            if (effective.length) {
                const keys = new Set(effective.map(x =>
                    x.shift_type + "|" + (x.shift_schedule_assignment || "")));
                if (keys.size !== 1) throw Error("Conflicting effective Shift Assignments");
                a = effective[0];
            } else {
                // Do not bridge an expired historical assignment or choose a later
                // assignment if there are competing assignments in the future.
                const future = assignments.filter(x => x.start_date > r.date);
                if (!future.length)
                    throw Error("No Shift Assignment available for historical rotation");
                const firstStart = future[0].start_date;
                const nearest = future.filter(x => x.start_date === firstStart);
                const keys = new Set(nearest.map(x =>
                    x.shift_type + "|" + (x.shift_schedule_assignment || "")));
                if (keys.size !== 1)
                    throw Error("Conflicting future Shift Assignments; historical rotation is ambiguous");
                if (assignments.some(x => x.start_date <= r.date))
                    throw Error("Historical assignment gap; refusing to infer rotation");
                a = nearest[0];
                historical = true;
            }

            let shiftName = a.shift_type;
            let scheduled = true;
            let rotationDayType = null;
            let isCustom = false;
            if (a.shift_schedule_assignment) {
                let ssa = (ctx.schedule_assignments || {})[a.shift_schedule_assignment];
                if (!ssa) {
                    ssa = await api("frappe.client.get", {
                        doctype: "Shift Schedule Assignment", name: a.shift_schedule_assignment
                    });
                }
                if (!ssa.shift_schedule) throw Error("Shift Schedule Assignment has no schedule");
                let schedule = (ctx.schedules || {})[ssa.shift_schedule];
                if (!schedule) {
                    schedule = await api("frappe.client.get", {
                        doctype: "Shift Schedule", name: ssa.shift_schedule
                    });
                }
                isCustom = Number(schedule.custom_use_custom_rotation || 0) !== 0;
                if (historical && !isCustom)
                    throw Error("Historical import requires a custom rotation schedule");
                if (isCustom) {
                    const rotation = schedule.custom_rotation_type || "Weekly";
                    const repeat = Number(schedule.custom_repeat_every || 0);
                    const anchor = ssa.create_shifts_after;
                    const dayName = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][weekday(r.date)];
                    const rows = schedule.custom_repeat_on_days_copy || [];
                    let sequence, weekNo;
                    if (rotation !== "Weekly") {
                        if (!anchor || !Number.isInteger(repeat) || repeat <= 0)
                            throw Error("Rotation anchor/repeat missing or invalid");
                        const offset = Math.round((Date.parse(r.date + "T00:00:00Z") -
                            Date.parse(anchor + "T00:00:00Z")) / 86400000);
                        if (!Number.isFinite(offset)) throw Error("Invalid rotation date");
                        if (rotation === "Daily Sequence")
                            sequence = ((offset % repeat) + repeat) % repeat + 1;
                        else if (rotation === "Weekly Sequence")
                            weekNo = ((Math.floor(offset / 7) % repeat) + repeat) % repeat + 1;
                        else throw Error("Unsupported rotation " + rotation);
                    }
                    const matching = rows.filter(x => rotation === "Daily Sequence"
                        ? Number(x.custom_sequence) === sequence
                        : rotation === "Weekly Sequence"
                            ? Number(x.custom_week_no) === weekNo && x.day === dayName
                            : x.day === dayName);
                    if (rotation === "Weekly" && matching.length === 0) {
                        scheduled = false;
                        rotationDayType = "Rest";
                    } else {
                        if (matching.length !== 1) throw Error("Missing or ambiguous rotation row");
                        const row = matching[0];
                        rotationDayType = row.custom_day_type || "Work";
                        if (!["Work", "Rest"].includes(rotationDayType))
                            throw Error("Invalid rotation Work/Rest");
                        scheduled = rotationDayType === "Work";
                        if (scheduled) {
                            if (!row.custom_shift_type)
                                throw Error("Work rotation row missing child Shift Type");
                            shiftName = row.custom_shift_type;
                        }
                    }
                } else {
                    const rows = schedule.repeat_on_days || [];
                    const dayName = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][weekday(r.date)];
                    scheduled = rows.some(x => x.day === dayName);
                }
            } else if (historical) {
                throw Error("Historical import requires Shift Schedule Assignment");
            }

            // For Rest, mother shift is context only, not a scheduled child shift.
            let st = (ctx.shift_types || {})[shiftName];
            if (!st) {
                st = await api("frappe.client.get", { doctype: "Shift Type", name: shiftName });
            }
            const start = String(st.start_time || "00:00:00");
            const end = String(st.end_time || "00:00:00");
            let shiftStart = r.date + " " + start;
            let shiftEnd = r.date + " " + end;
            if (dateTimeMs(shiftEnd) <= dateTimeMs(shiftStart))
                shiftEnd = addDay(r.date) + " " + end;
            const required = scheduled ? minutesBetween(shiftStart, shiftEnd) : 0;

            // Mirror the Checkin Resolver: official holiday suppression applies
            // only when enabled on the selected child Shift Type.
            let isHoliday = false;
            if (isCustom && Number(st.custom_apply_official_holidays || 0) && st.holiday_list) {
                // Holiday is a child DocType; direct get_list may return 403.
                // Read the parent Holiday List with normal document permissions.
                let holidayDates = state.holidayCache.get(st.holiday_list);
                if (!holidayDates) {
                    const holidayList = await api("frappe.client.get", {
                        doctype: "Holiday List",
                        name: st.holiday_list
                    });
                    holidayDates = new Set(
                        (holidayList.holidays || [])
                            .map(x => x.holiday_date)
                            .filter(Boolean)
                    );
                    state.holidayCache.set(st.holiday_list, holidayDates);
                }
                isHoliday = holidayDates.has(r.date);
                if (isHoliday) scheduled = false;
            }
            return {
                shiftName: scheduled ? shiftName : null,
                shiftStart, shiftEnd,
                required: scheduled ? required : 0,
                scheduled,
                rest: !scheduled,
                holiday: isHoliday || weekday(r.date) === 5,
                rotationDayType,
                historical,
                assignment: a.name
            };
        }

        // Derive duty hours from the actual Attendance Shift Type, not Excel.
        // Keep a confirmed rest/holiday day at zero required hours.
        async function alignDutyToAttendanceShift(doc, r, schedule) {
            const shift = doc.shift || schedule.shiftName;
            if (!shift) {
                schedule.required = 0;
                return;
            }
            const ctxAlign = state.context || {};
            let st = (ctxAlign.shift_types || {})[shift];
            if (!st) {
                st = await api("frappe.client.get", {
                    doctype: "Shift Type", name: shift
                });
            }
            const parseTime = (v) => {
                const m = String(v ?? "").match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?/);
                if (!m || +m[1] > 23 || +m[2] > 59 || +(m[3] || 0) > 59)
                    throw Error("Invalid Shift Type time for " + shift + ": " + v);
                return (+m[1] * 60 + +m[2] + +(m[3] || 0) / 60);
            };
            const start = parseTime(st.start_time);
            const end = parseTime(st.end_time);
            const duration = ((end - start + 1440) % 1440);
            if (!duration) throw Error("Zero-length Shift Type: " + shift);
            const fmt = (mins) => {
                const hh = Math.floor(mins / 60);
                const mm = Math.floor(mins % 60);
                const ss = Math.round((mins % 1) * 60);
                return [hh, mm, ss].map(x => String(x).padStart(2, "0")).join(":");
            };
            schedule.shiftName = shift;
            schedule.shiftStart = r.date + " " + fmt(start);
            schedule.shiftEnd = (end <= start ? addDay(r.date) : r.date) + " " + fmt(end);
            schedule.required = schedule.scheduled ? duration : 0;
            if (schedule.scheduled && schedule.required <= 0)
                throw Error("Scheduled shift has no standard working hours: " + shift);
        }

        // Actual night presence within 22:00-06:00, across calendar days.
        // Uses punches, never the Excel night-work total or planned shift duration.
        function actualNightMinutes(inTime, outTime) {
            if (!inTime || !outTime) return null;
            const startMs = dateTimeMs(inTime);
            const endMs = dateTimeMs(outTime);
            if (!Number.isFinite(startMs) || !Number.isFinite(endMs) ||
                endMs <= startMs || endMs - startMs > 48 * 3600000)
                throw Error("Invalid or over-48h IN/OUT interval for night-work calculation");

            let total = 0;
            const dayMs = 86400000;
            const firstDay = Math.floor(startMs / dayMs) - 1;
            const lastDay = Math.floor(endMs / dayMs);
            for (let day = firstDay; day <= lastDay; day++) {
                const nightStart = day * dayMs + 22 * 3600000;
                const nightEnd = (day + 1) * dayMs + 6 * 3600000;
                total += Math.max(0,
                    Math.min(endMs, nightEnd) - Math.max(startMs, nightStart)
                ) / 60000;
            }
            return total;
        }

        function applyValues(doc, r, schedule) {
            const d = r.details;
            // Excel-authoritative presence (حضور). Do not substitute punch interval.
            const presence = Math.max(0, d.presence || 0);
            // Preserve On Leave / Half Day set by HRMS on an existing document.
            const priorStatus = doc.status;
            if (priorStatus === "On Leave" || priorStatus === "Half Day") {
                // Keep HRMS leave status; Excel rules do not override Leave docs
                // (those rows are classified Review and normally not written).
            } else {
                doc.status = excelImportStatus(d, r.inTime, r.outTime);
            }
            // Only write a complete IN/OUT pair. Asymmetric punches on mission
            // days must not block Present/mission hours, and must not persist
            // a one-sided punch that HRMS / calc hooks would misread.
            if (r.inTime && r.outTime) {
                if (state.fields.has("in_time")) doc.in_time = r.inTime;
                if (state.fields.has("out_time")) doc.out_time = r.outTime;
            } else {
                if (state.fields.has("in_time")) doc.in_time = null;
                if (state.fields.has("out_time")) doc.out_time = null;
            }
            if (state.fields.has("shift")) doc.shift = schedule.shiftName || null;

            // Explicitly map visible attendance flags. A Rest day is not scheduled.
            setCheck(doc, "Scheduled Day", schedule.scheduled,
                ["custom_scheduled_day"]);
            setCheck(doc, "Is Holiday", schedule.holiday,
                ["custom_is_holiday"]);
            if (state.fields.has("late_entry"))
                doc.late_entry = d.late > 0 ? 1 : 0;
            if (state.fields.has("early_exit"))
                doc.early_exit = d.early > 0 ? 1 : 0;

            // Standard / shift-window fields remain schedule-derived (not in Excel mapping table).
            setHours(doc, "Standard Working Hours", schedule.required, ["standard_working_hours"]);
            // Rule: حضور → working_hours (Excel authoritative, including explicit zero).
            setHours(doc, "Working Hours", presence, ["working_hours"]);
            if (state.fields.has("working_hours"))
                doc.working_hours = hours(presence);
            const inside = r.inTime && r.outTime && schedule.scheduled
                ? Math.max(0, (Math.min(dateTimeMs(r.outTime), dateTimeMs(schedule.shiftEnd)) -
                    Math.max(dateTimeMs(r.inTime), dateTimeMs(schedule.shiftStart))) / 60000)
                : Math.min(presence, schedule.required);
            setHours(doc, "Shift Work Hours", inside, ["custom_shift_work_hours"]);
            setHours(doc, "Presence Shortage Hours (raw)",
                Math.max(0, schedule.required - inside),
                ["custom_presence_shortage_hours"]);

            // Rule 1: Work Shortage = خروج غیرمجاز + تعجیل + تأخیر (not shift math, not absence).
            const shortageMinutes = (r.workShortage != null)
                ? r.workShortage
                : (d.unauthorizedExit + d.early + d.late);
            setHours(doc, "Work Shortage Hours", shortageMinutes, ["custom_work_shortage_hours"]);
            if (state.fields.has("custom_work_shortage_hours"))
                doc.custom_work_shortage_hours = hours(Math.max(0, shortageMinutes));

            // Friday remains schedule/weekday derived (no dedicated Excel Friday column).
            const friday = weekday(r.date) === 5 ? presence : 0;
            setHours(doc, "Friday Work Hours", friday, ["custom_friday_work_hours"]);

            // تعطیل مجاز → Holiday Work Hours (Excel approved holiday OT column).
            setHours(doc, "Holiday Work Hours", d.holidayOTApproved,
                ["custom_holiday_work_hours"]);
            if (state.fields.has("custom_holiday_work_hours"))
                doc.custom_holiday_work_hours = hours(Math.max(0, d.holidayOTApproved));

            setHours(doc, "Mission Work Hours", d.mission, ["custom_mission_work_hours"]);
            if (state.fields.has("custom_mission_work_hours"))
                doc.custom_mission_work_hours = hours(Math.max(0, d.mission));

            // شبکاری کل → Night Work Hours (Excel authoritative; not punch overlap).
            setHours(doc, "Night Work Hours", d.nightTotal, ["custom_night_work_hours"]);
            if (state.fields.has("custom_night_work_hours"))
                doc.custom_night_work_hours = hours(Math.max(0, d.nightTotal));

            setHours(doc, "Total Leave Hours", d.paidLeave + d.unpaidLeave,
                ["custom_total_leave_hours", "custom_leave_hours"]);
            setHours(doc, "Paid Leave Hours", d.paidLeave, ["custom_paid_leave_hours"]);
            if (state.fields.has("custom_paid_leave_hours"))
                doc.custom_paid_leave_hours = hours(Math.max(0, d.paidLeave));
            setHours(doc, "Unpaid Leave Hours", d.unpaidLeave, ["custom_unpaid_leave_hours"]);
            if (state.fields.has("custom_unpaid_leave_hours"))
                doc.custom_unpaid_leave_hours = hours(Math.max(0, d.unpaidLeave));

            // Rule 2: عادی غیرمجاز → BOTH custom_regular_work_hours AND custom_extra_presence_hours.
            const unauthorizedNormal = (r.unauthorizedNormalOT != null)
                ? r.unauthorizedNormalOT
                : d.normalOTRegular;
            setHours(doc, "Regular Work Hours", unauthorizedNormal,
                ["custom_regular_work_hours"]);
            setHours(doc, "Extra Presence Hours", unauthorizedNormal,
                ["custom_extra_presence_hours"]);
            if (state.fields.has("custom_regular_work_hours"))
                doc.custom_regular_work_hours = hours(Math.max(0, unauthorizedNormal));
            if (state.fields.has("custom_extra_presence_hours"))
                doc.custom_extra_presence_hours = hours(Math.max(0, unauthorizedNormal));
            // Legacy overtime field: keep in sync with the same unauthorized-normal value.
            if (state.fields.has("custom_regular_overtime_hours"))
                doc.custom_regular_overtime_hours = hours(Math.max(0, unauthorizedNormal));

            // Rule 4: Approved OT = K+M+O+Q approved minutes → hours (never unauthorized).
            setHours(doc, "Approved Overtime Hours", r.approvedOT,
                ["custom_approved_overtime_hours"]);
            if (state.fields.has(CONFIG.approvedOT))
                doc[CONFIG.approvedOT] = hours(Math.max(0, r.approvedOT));

            if (state.fields.has("custom_missing_checkin")) {
                doc.custom_missing_checkin = r.inTime && r.outTime ? "None" :
                    r.inTime ? "OUT" : r.outTime ? "IN" : "Both";
            }
            if (state.fields.has("custom_calculation_details")) {
                doc.custom_calculation_details = JSON.stringify({
                    source: "Attendance Excel Import",
                    mapping_version: "V2.0.8",
                    source_file: state.fileName,
                    excel_row: r.row, personnel_code: r.code,
                    raw_excel_date: r.rawDate,
                    weekday: r.weekday, work_group: r.workGroup,
                    durations_minutes: d,
                    unauthorized_normal_ot_minutes: unauthorizedNormal,
                    work_shortage_minutes: shortageMinutes,
                    approved_overtime_minutes: r.approvedOT,
                    approved_overtime_components: {
                        holiday_approved_minutes: d.holidayOTApproved,
                        normal_approved_minutes: d.normalOTApproved,
                        excluded_night_approved_minutes: d.nightOTApproved,
                        excluded_mission_approved_minutes: d.missionOTApproved
                    },
                    night_work_source: "Excel شبکاری کل",
                    holiday_work_source: "Excel تعطیل مجاز",
                    mission_work_source: "Excel کل مأموریت",
                    regular_and_extra_presence_source: "Excel عادی غیرمجاز (identical)",
                    work_shortage_source: "Excel خروج غیرمجاز + تعجیل + تأخیر",
                    status_rule: (d.mission > 0 && d.absence > 0)
                        ? "Conflict: mission + absence (Review)"
                        : (d.absence > 0)
                            ? "Excel غیبت > 0 → Absent"
                            : (d.mission > 0)
                                ? "Excel کل مأموریت > 0 → Present (mission hours mapped)"
                                : "Present unless Excel absence > 0 (missing punches do not force Absent)",
                    resolved_shift: schedule.shiftName, scheduled_day: schedule.scheduled,
                    holiday_or_rest: schedule.rest,
                    note: "Other Paid Leave Hours not overwritten: source Excel has no separate column"
                }, null, 2);
            }
            return doc;
        }

        async function importRecords() {
            if (
                state.busy ||
                state.importing ||
                !state.previewReady ||
                Number(dialog.get_value("confirm") || 0) !== 1
            ) return;

            state.busy = true;
            state.importing = true;
            state.allowClose = false;
            state.previewReady = false;
            primary.prop("disabled", true).text("Importing…");
            progressArea.find("[data-espad-att-result]").removeClass("visible").empty();
            progressArea.find("[data-espad-att-term]").empty().removeClass("visible");
            state.logLines = [];

            let created = 0;
            let updated = 0;
            const failed = [];
            const skipped = [];
            const tImport = performance.now();
            let tick = null;

            try {
                const rows = selectedRows();
                if (!rows.length) throw Error("Select at least one employee");
                const selectedEmployees = new Map();
                for (const r of rows) {
                    const p = state.people.get(r.code);
                    if (!p || !p.included || p.employee !== r.employee)
                        throw Error("Selection changed; refresh preview");
                    if (selectedEmployees.has(r.employee) && selectedEmployees.get(r.employee) !== r.code)
                        throw Error("Multiple personnel codes mapped to " + r.employee);
                    selectedEmployees.set(r.employee, r.code);
                }
                const existing = await fetchExisting(rows);
                const duplicates = duplicateKeys(rows);

                const work = [];

                for (const r of rows) {
                    const [action, reason] =
                        classify(r, existing, duplicates);

                    if (action === "Create" ||
                        action === "Update") {
                        work.push(r);
                    } else {
                        skipped.push(
                            `Row ${r.row}: ${reason}`
                        );
                    }
                }

                const BATCH = 50;
                const prepareTotal = Math.max(1, work.length);

                state.progress = {
                    heading: "Import Progress",
                    processed: 0,
                    total: prepareTotal,
                    created: 0,
                    updated: 0,
                    skipped: skipped.length,
                    failed: 0,
                    batch: 0,
                    batches: 0,
                    elapsed_sec: 0,
                    rate: 0,
                    eta_sec: 0,
                    status: "Preparing",
                    operation: "Preparing Attendance rows…",
                    done: false
                };
                updateProgressDom();
                termAppend([
                    { cls: "info", text: `[INFO] Import started — ${rows.length} selected rows, ${work.length} to create/update, ${skipped.length} pre-skipped` }
                ]);

                tick = setInterval(() => {
                    if (!state.progress || state.progress.done) return;
                    stampRate(state.progress.processed, state.progress.total, tImport);
                    updateProgressDom();
                }, 400);

                const ALLOWED_SUBMITTED = [
                    "status", "shift", "in_time", "out_time",
                    "late_entry", "early_exit", "working_hours",
                    "standard_working_hours", "custom_scheduled_day",
                    "custom_is_holiday", "custom_shift_work_hours",
                    "custom_presence_shortage_hours", "custom_work_shortage_hours",
                    "custom_friday_work_hours", "custom_holiday_work_hours",
                    "custom_mission_work_hours", "custom_night_work_hours",
                    "custom_total_leave_hours", "custom_leave_hours",
                    "custom_paid_leave_hours", "custom_unpaid_leave_hours",
                    "custom_regular_work_hours", "custom_regular_overtime_hours",
                    "custom_extra_presence_hours", "custom_approved_overtime_hours",
                    "custom_missing_checkin", "custom_calculation_details"
                ];
                const prepared = [];
                for (let i = 0; i < work.length; i++) {
                    const r = work[i];
                    if (i === 0 || (i + 1) % 25 === 0 || i + 1 === work.length) {
                        state.progress.processed = i + 1;
                        state.progress.failed = failed.length;
                        state.progress.skipped = skipped.length;
                        state.progress.status = "Preparing";
                        state.progress.operation =
                            `Preparing ${i + 1} / ${work.length}`;
                        stampRate(i + 1, prepareTotal, tImport);
                        updateProgressDom();
                        await yieldUi();
                    }
                    try {
                        const key = r.employee + "|" + r.date;
                        const current = existing.get(key);
                        if (current?.duplicate)
                            throw Error("Duplicate Attendance in ERP");
                        if (current?.status === "On Leave")
                            throw Error("Existing Leave Attendance");

                        const company =
                            (state.context?.employees?.[r.employee]?.company) || null;
                        if (!current && !company)
                            throw Error("Employee company missing");

                        const doc = {
                            doctype: "Attendance",
                            name: current?.name,
                            employee: r.employee,
                            attendance_date: r.date,
                            company: company || undefined,
                            status: "Absent",
                            shift: current?.shift || null,
                            custom_scheduled_day: current?.custom_scheduled_day
                        };

                        const schedule = await resolveSchedule(r);
                        if (current && doc.shift) {
                            schedule.shiftName = doc.shift;
                            if (doc.custom_scheduled_day !== undefined &&
                                doc.custom_scheduled_day !== null) {
                                schedule.scheduled = Number(doc.custom_scheduled_day) === 1;
                                schedule.rest = !schedule.scheduled;
                            }
                        }
                        await alignDutyToAttendanceShift(doc, r, schedule);
                        applyValues(doc, r, schedule);
                        if (!Number.isFinite(Number(doc.standard_working_hours)))
                            throw Error("Invalid Standard Working Hours");
                        if (doc.employee !== r.employee || doc.attendance_date !== r.date)
                            throw Error("Attendance employee/date scope changed");

                        const isSubmitted = current && Number(current.docstatus) === 1;
                        const values = {};
                        if (isSubmitted) {
                            if (!state.fields.has("standard_working_hours"))
                                throw Error("Attendance standard_working_hours field missing");
                            for (const field of ALLOWED_SUBMITTED) {
                                if (state.fields.has(field) &&
                                    Object.prototype.hasOwnProperty.call(doc, field))
                                    values[field] = doc[field];
                            }
                            values.standard_working_hours = doc.standard_working_hours;
                            // Excel-authoritative fields must always be present on submitted updates.
                            const mustSend = [
                                "status",
                                "working_hours",
                                "custom_night_work_hours",
                                "custom_work_shortage_hours",
                                "custom_mission_work_hours",
                                "custom_paid_leave_hours",
                                "custom_unpaid_leave_hours",
                                "custom_holiday_work_hours",
                                "custom_regular_work_hours",
                                "custom_extra_presence_hours",
                                "custom_approved_overtime_hours",
                            ];
                            for (const field of mustSend) {
                                if (!state.fields.has(field)) continue;
                                if (!Object.prototype.hasOwnProperty.call(doc, field) ||
                                    (typeof doc[field] === "number" && !Number.isFinite(doc[field])))
                                    throw Error("Excel-authoritative field missing: " + field);
                                values[field] = doc[field];
                            }
                            // Dual-field unauthorized OT must stay identical.
                            if (state.fields.has("custom_regular_work_hours") &&
                                state.fields.has("custom_extra_presence_hours") &&
                                Number(values.custom_regular_work_hours) !==
                                Number(values.custom_extra_presence_hours)) {
                                throw Error(
                                    "Regular Work Hours and Extra Presence Hours must match " +
                                    "(Excel عادی غیرمجاز)"
                                );
                            }
                        } else {
                            for (const field of Object.keys(doc)) {
                                if (["doctype", "name", "employee", "attendance_date", "company"].includes(field))
                                    continue;
                                if (state.fields.has(field) || field === "status" || field === "shift" ||
                                    field === "in_time" || field === "out_time" ||
                                    field === "late_entry" || field === "early_exit" ||
                                    field === "working_hours" || field === "standard_working_hours")
                                    values[field] = doc[field];
                            }
                        }

                        prepared.push({
                            employee: r.employee,
                            attendance_date: r.date,
                            company,
                            name: current?.name,
                            mode: isSubmitted ? "update_submitted" :
                                (current ? "update_draft" : "create"),
                            values,
                            row: r.row
                        });
                    } catch (e) {
                        failed.push({
                            row: r.row,
                            employee: r.employee,
                            date: r.date,
                            error: e.message || String(e)
                        });
                        termAppend([{
                            cls: "err",
                            text: `[FAIL] Row ${r.row} ${r.employee} ${r.date}: ${e.message || e}`
                        }]);
                    }
                }

                const saveTotal = Math.max(prepared.length, 1);
                const saveBatches = Math.max(1, Math.ceil(prepared.length / BATCH) || 1);
                state.progress.total = prepared.length || 1;
                state.progress.batches = prepared.length ? saveBatches : 0;
                state.progress.processed = 0;
                state.progress.failed = failed.length;
                state.progress.skipped = skipped.length;
                state.progress.status = "Saving";
                state.progress.operation = prepared.length
                    ? `Saving batch 1 / ${saveBatches}`
                    : "Nothing to save";
                updateProgressDom();
                termAppend([{
                    cls: "info",
                    text: `[INFO] Prepared ${prepared.length}; prepare-failed ${failed.length}; saving in ${saveBatches} batch(es) of ${BATCH}`
                }]);

                let savedProcessed = 0;
                for (let i = 0; i < prepared.length; i += BATCH) {
                    const chunk = prepared.slice(i, i + BATCH);
                    const batchNo = Math.floor(i / BATCH) + 1;
                    state.progress.batch = batchNo;
                    state.progress.status = "Saving";
                    state.progress.operation =
                        `Saving batch ${batchNo} / ${saveBatches}`;
                    stampRate(savedProcessed, saveTotal, tImport);
                    updateProgressDom();
                    await yieldUi();

                    try {
                        const result = await api(
                            "erpnext_extensions.extentionhrms.attendance_excel_import.import_attendance_details",
                            { records: chunk }
                        );
                        const batchCreated = (result.created || []).length;
                        const batchUpdated = (result.updated || []).length;
                        created += batchCreated;
                        updated += batchUpdated;
                        for (const row of (result.failed || [])) {
                            failed.push({
                                row: row.row,
                                employee: row.employee,
                                date: row.attendance_date,
                                error: row.error || "Import failed"
                            });
                            termAppend([{
                                cls: "err",
                                text: `[FAIL] Row ${row.row} ${row.employee} ${row.attendance_date}: ${row.error || "Import failed"}`
                            }]);
                        }
                        for (const row of (result.skipped || [])) {
                            skipped.push(`Row ${row.row || "?"}: ${row.reason || "skipped"}`);
                        }
                        termAppend([{
                            cls: "ok",
                            text: `[OK] Batch ${batchNo}/${saveBatches}: +${batchCreated} created, +${batchUpdated} updated`
                        }]);
                    } catch (e) {
                        for (const item of chunk) {
                            failed.push({
                                row: item.row,
                                employee: item.employee,
                                date: item.attendance_date,
                                error: e.message || String(e)
                            });
                        }
                        termAppend([{
                            cls: "err",
                            text: `[ERROR] Batch ${batchNo} failed: ${e.message || e}`
                        }]);
                    }

                    savedProcessed += chunk.length;
                    state.progress.processed = savedProcessed;
                    state.progress.created = created;
                    state.progress.updated = updated;
                    state.progress.failed = failed.length;
                    state.progress.skipped = skipped.length;
                    stampRate(savedProcessed, saveTotal, tImport);
                    updateProgressDom();
                }

                state.timings = state.timings || {};
                state.timings.importMs = Math.round(performance.now() - tImport);
                const importSecs = Math.max(0.001, state.timings.importMs / 1000);
                const savedCount = created + updated;
                const rate = Math.round(savedCount / importSecs);

                state.progress.processed = prepared.length;
                state.progress.total = Math.max(prepared.length, 1);
                state.progress.created = created;
                state.progress.updated = updated;
                state.progress.failed = failed.length;
                state.progress.skipped = skipped.length;
                state.progress.done = true;
                state.progress.eta_sec = 0;
                state.progress.heading = failed.length
                    ? "Import Completed With Errors"
                    : "Import Completed";
                state.progress.status = failed.length ? "COMPLETED WITH ERRORS" : "COMPLETED";
                state.progress.operation = `Finished in ${state.timings.importMs} ms (${rate} records/s)`;
                state.progress.rate = rate;
                updateProgressDom();

                showInlineResult({
                    created,
                    updated,
                    skipped,
                    failed,
                    durationMs: state.timings.importMs,
                    rate
                });
                termAppend([{
                    cls: failed.length ? "warn" : "ok",
                    text: `[DONE] Created ${created}, Updated ${updated}, Skipped ${skipped.length}, Failed ${failed.length}`
                }]);

                if (window.cur_list?.doctype === "Attendance")
                    cur_list.refresh();

            } catch (e) {
                console.error(e);
                termAppend([{
                    cls: "err",
                    text: `[ERROR] ${e.message || e}`
                }]);
                if (state.progress) {
                    state.progress.status = "FAILED";
                    state.progress.operation = String(e.message || e);
                    state.progress.done = true;
                    updateProgressDom();
                }
                frappe.msgprint({
                    title: "Import Error",
                    indicator: "red",
                    message: esc(e.message || String(e))
                });
            } finally {
                if (tick) clearInterval(tick);
                state.busy = false;
                state.importing = false;
                state.allowClose = true;
                primary.prop("disabled", true).text("Import Selected Attendance");
            }
        }
    };

    // Register the button on the Attendance List.
    // Keep existing List View settings.
    const previous =
        frappe.listview_settings["Attendance"] || {};

    const previousOnload = previous.onload;

    frappe.listview_settings["Attendance"] = {
        ...previous,

        onload(listview) {
            if (typeof previousOnload === "function")
                previousOnload.call(this, listview);

            listview.page.add_inner_button(
                "Import Attendance Details (Excel)",
                () => NS.open()
            );
        }
    };

})();

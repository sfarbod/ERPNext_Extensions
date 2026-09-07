# Release 5.1.5 — Account Explorer Export / Prepared Queue Isolation (P0)

## Scope

P0 concurrency hotfix only:

- Background **export** must not starve Account Explorer **prepared** summaries
- Account / Voucher remain usable while a large export runs
- Large exports remain asynchronous (not on the web request)

No unrelated accounting measure changes.

## Queue policy

| Workload | Queue | Notes |
|----------|-------|-------|
| Prepared summary (Account / Voucher) | **`short`** | Interactive; `timeout=900`; `at_front=True` |
| Large export (> threshold) | **`long`** | Bulk GL materialization |

**Before (5.1.4 and earlier):** both used `long` → export monopolized long workers → prepared stayed `Queued` → UI poll spun.

**After (5.1.5):** isolated by construction on the existing **3 long + 4 short** topology. No custom RQ queue names and no worker-count increase required.

## UI

- Large export enqueue does **not** use Desk `freeze`
- Export busy state is local (`_export_enqueue_inflight` + export button)
- Export never claims summary loading / refresh tail
- Prepared poll is bounded (2 minutes); timeout/error clears loading
- Axis switch respects generation guards; stale polls cannot retain the spinner

## Deployment impact

None required for queue consumers: production already runs short + long workers.

Optional ops note: short workers now also run AE prepared builds; keep at least one healthy short worker.

## Background export finalization (v5.1.5)

Root cause of "export failed" with valid files: `_send_export_ready_email(..., now=True)`
registered SMTP send on RQ job `after_commit`. Broken Email Account encryption keys
marked successful file jobs as **Failed**.

Fix (Account Explorer-local):

1. Save File and `frappe.db.commit()` first
2. Notify via realtime + Notification Log
3. Optional email with `now=False` / `delayed=True` only (never SMTP on this commit)
4. Job returns `{ok, file_url, filename, ...}` and UI polls / listens for download

## Large voucher export performance (v5.1.5)

Before: export reused UI page builder (`build_voucher_summary` per page) with OFFSET
pagination + party/title enrichment → ~12k SQL / ~95s for 143k rows.

After: lean `iter_voucher_export_batches` (one temp GROUP BY + keyset batches, export
columns only). Measured on this site:

- 143k CSV ≈ **6.5s** (target ≤30s)
- 143k XLSX ≈ **9s** (target ≤45s)
- SQL count ≈ **19** (constant/batched, not O(rows))

UI: "Export is being prepared." → status strip → **Download Export** (no Background Jobs navigation).

Queue isolation unchanged: prepared → `short`, large export → `long`.

## Fingerprint / version

App version **5.1.5**.

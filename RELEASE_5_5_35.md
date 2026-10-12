# erpnext_extensions v5.5.35

**Release Type:** Bug Fix / Account Explorer Prepared Artifact Recovery  
**Release Date:** 2026-10-12

---

## 1. Overview

Account Explorer can fail after a **database-only restore** (or any loss of
private File bytes) when Prepared Result rows still point at missing or
corrupt gzip JSON artifacts. Desk then surfaces `FileNotFoundError` and
“Account Explorer could not load this view.”

v5.5.35 soft-fails unreadable artifacts, invalidates **only** the affected
prepared row, and rebuilds on demand through the existing RQ short queue
(with the existing inline fallback when workers are unavailable).

**This release does NOT claim to fix the unresolved Production account
balance discrepancy** for `11100409 - بانک اقتصاد نوین - E` (native CoA
≈ 994B IRR vs Production Account Explorer ≈ 3.06T IRR). That issue needs a
separate read-only Production investigation (see Known Limitations).

---

## 2. Root Cause

After restoring a Production SQL dump onto another site without the matching
`private/files` tree:

1. `Account Explorer Prepared Result` rows remain `Completed` with
   `result_file` URLs.
2. Physical files under `sites/<site>/private/files/` are absent.
3. `_load_artifact()` called `File.get_content()`, which raised
   `FileNotFoundError` into the Desk API response.

The same failure mode applies to missing File documents, corrupt gzip, or
invalid JSON inside an otherwise “Completed” prepared row.

---

## 3. Previous Behavior

- Missing / corrupt artifacts raised `FileNotFoundError` to the browser.
- Account Explorer showed a permanent failure / could-not-load state.
- Rebuilding could collide on fingerprint-only filenames left on disk.
- Concurrent Desk polls could race on the same Completed row.

---

## 4. New Behavior

- `_load_artifact()` soft-fails restore/lifecycle failures (missing File,
  missing bytes, corrupt gzip, invalid JSON) and returns `None`.
- Unexpected loader errors still raise (not silently swallowed).
- Unreadable Completed/Error rows are invalidated for rebuild only for that
  fingerprint/doc (`FOR UPDATE` + deadlock retry).
- Rebuild uses collision-safe filenames:
  `{fingerprint}:{prepared_doc_name}.json.gz`.
- Concurrent callers share one prepared row / one effective rebuild.
- Valid readable artifacts are unchanged.
- Unrelated File / prepared rows are not deleted in bulk.

---

## 5. Changed Files

| File | Change |
|---|---|
| `erpnext_extensions/__init__.py` | Version **5.5.34 → 5.5.35** |
| `iran_accounting/account_explorer/prepared_report.py` | Soft-load, invalidate, unique store names, concurrency-safe rebuild |
| `iran_accounting/tests/test_account_explorer_prepared_result.py` | Regression coverage for missing/corrupt/concurrent paths |
| `iran_accounting/e2e/playwright_account_explorer_artifact_recovery.mjs` | Desk + RQ recovery Playwright gate |
| `RELEASE_5_5_35.md` | This document |

No GL / SLE / hierarchy / opening / Case A–B engine formula changes.

---

## 6. Prepared Artifact Recovery

Handled cases:

- Missing physical artifact after DB-only restore
- Missing File document
- Corrupt gzip
- Invalid JSON
- Stale Completed metadata without `result_file`
- Concurrent invalidation of the same row
- Worker restart during preparation (poll re-ensures job)

Not handled by broad wipe: valid peers remain; no global prepared purge.

---

## 7. Concurrency Protection

- Row lock (`SELECT … FOR UPDATE`) before invalidate
- Retry on MariaDB deadlock / “record changed” (1020)
- `_create_or_reuse_job` reuses only Queued/Started (not Completed without proof)
- Verified on development with 1 / 5 / 10 / 25 / 50 simultaneous requests:
  one prepared row, one job id, final `ready`

---

## 8. Worker Compatibility

Uses existing policy (`queue_policy.py`):

- Prepared interactive builds → **short** (`at_front=True`, long timeout)
- Large exports → **long**

Verified on development topology **4 short + 3 long** with real RQ registry
evidence (job id, queue, worker name, timestamps). Inline fallback remains
when `workers_available()` is false; it is not removed.

---

## 9. Accounting Safety

No changes to:

- GL Entry / SLE posting or aggregation formulas
- Account hierarchy construction
- Opening balance policy semantics
- Case A / Case B engine selection
- Inventory attribution math
- Fiscal year resolution

On the restored Production database on `development.localhost`, account
`11100409 - بانک اقتصاد نوین - E` (FY1405, opening included) reconciles to
**994,353,195,185 IRR** across independent GL, native CoA, AE live, AE
prepared, and Desk UI (Δ = 0).

---

## 10. Regression Tests

- Unit: `test_account_explorer_prepared_result` (9 tests) — missing file,
  missing File doc, corrupt gzip/JSON, concurrency, valid path, unexpected
  error re-raise, miss/hit/invalidation.
- Playwright: `playwright_account_explorer_artifact_recovery.mjs` — Group /
  GL / SL / L4 / L5, party, dimension (`cost_center`), currency, item group,
  item, voucher, refresh, hard refresh, concurrent refresh, no FNF /
  permanent spinner.
- Manual RQ gates: short-queue prepared rebuild; long-queue voucher export
  (≥5000 rows) while browsing; concurrency storms.

Playwright harness uses `browser.newContext({ locale: "en-US" })` because
Chromium’s default `navigator.language` (`en-US@posix`) crashes Frappe’s
`AltShortcutGroup` via `Intl.Locale`. That is a **test-harness** fix, not an
application Intl monkey-patch.

---

## 11. Deployment Instructions

1. Deploy / update `erpnext_extensions` to **5.5.35** on the target site.
2. Restart web workers / gunicorn so they load the new Python code.
3. Restart RQ workers (short and long) so background prepared/export jobs
   run the new code.
4. `bench build --app erpnext_extensions` if assets are rebuilt in your
   pipeline (this release is primarily Python; build still recommended).
5. **`bench migrate`:** not required for this release (no DocType / patch).
6. Do **not** mass-delete Prepared Results. Missing artifacts rebuild on
   first request.
7. Confirm Account Explorer loads after deploy; optionally open a view that
   previously failed after restore.

---

## 12. Rollback Instructions

1. Reinstall / checkout `erpnext_extensions` **5.5.34**.
2. Restart web + RQ workers.
3. Rollback does not restore deleted corrupt local files; Prepared rows may
   again raise `FileNotFoundError` if artifacts are still missing.

---

## 13. Known Limitations

1. **Lazy rebuild:** After DB-only restore, most Completed prepared rows may
   still lack disk files until each fingerprint is requested once.
2. **Production numerical discrepancy (open):** On Production
   (`erp.espadpharmed.com`), Account Explorer has been observed showing
   **≈ 3,064,765,748,904 IRR** for `11100409` while native Chart of Accounts
   and the restored-DB development AE path show **994,353,195,185 IRR**.
   v5.5.35 does **not** claim this is fixed. Requires read-only Production
   diagnostics (runtime version, worker code identity, prepared artifact
   payload vs live API, filters, hierarchy) before any calculation change.
3. **Worker registry health:** Stale RQ processes can leave
   `workers_available=False` even when PIDs exist; prefer verifying RQ
   registry, not process list alone.
4. **Playwright locale:** Use an explicit BCP-47 locale in automated Desk
   browsers; do not patch `Intl` globally in production JS.

# RELEASE 5.6.3 — Facility Management Cancelled JE Reconciliation

**App:** `erpnext_extensions`  
**Version:** `5.6.3`  
**Branch:** `feature/5.6.3-facility-je-reconciliation`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 (unchanged)

---

## 1. Release overview

Facility Management could keep pointing at **cancelled** receipt (and, in edge cases, repayment) Journal Entries. That left Facilities in `Active` with non-null `receipt_journal_entry`, blocked replacement receipts, and left paid/received amounts inconsistent with submitted accounting documents.

This release provides:

1. A production-ready **historical scanner + repair utility**
2. A permanent **Journal Entry cancel lifecycle** for Facility receipt/repayment JEs
3. **Duplicate prevention** and dependency protection
4. Regression tests covering the receipt/repayment cancel–reissue lifecycle

---

## 2. Root cause

Receipt creation (`create_receipt_journal_entry` in `facility.py`) sets:

- `receipt_journal_entry`
- `received_amount`
- `status = Active`

There was **no** `Journal Entry` `before_cancel` / `on_cancel` hook for Facility Management. Cancelling the receipt JE (Desk → Journal Entry → Cancel) reversed GL but left the Facility unchanged.

Consequently:

| Symptom | Cause |
|---------|--------|
| Stale `receipt_journal_entry` | No cancel-path clear |
| Status stayed `Active` | No status restore |
| `received_amount` stayed set | No amount restore |
| Replacement receipt blocked | `create_and_submit_receipt_je` rejected any non-empty receipt field |
| Repayment JE cancelled outside Facility Repayment | Would leave submitted repayment counting in paid totals (blocked going forward) |

**Key files (before fix):**

- `facility_management/doctype/facility/facility.py` — `create_receipt_journal_entry`
- `facility_management/facility_accounting.py` — `create_and_submit_receipt_je`, `cancel_journal_entry`
- `facility_management/doctype/facility_repayment/facility_repayment.py` — `on_cancel` (clears JE only when repayment is cancelled)
- `hooks.py` — Journal Entry events had no Facility handlers

**Data model note:** `Facility Repayment Schedule` is a contractual child table (`due_date`, amounts, remarks) and does **not** store Journal Entry links. Active JE references live on `Facility.receipt_journal_entry` and `Facility Repayment.journal_entry`.

---

## 3. Historical repair

Utility module:

`erpnext_extensions/facility_management/facility_je_reconciliation.py`

Capabilities:

| Function | Purpose |
|----------|---------|
| `run_global_scan(dry_run=True)` | Scan all Facilities / repayments |
| `scan_facility` / `scan_repayment` | Single-document scan |
| `repair_facility` / `repair_repayment` | Safe repair (`confirm=True` required when `dry_run=False`) |
| `repair_batch` | Per-facility savepoints |
| `verify_facility` | Post-repair eligibility check |

Classifications: `SAFE_AUTO_REPAIR`, `MANUAL_REVIEW_REQUIRED`, `BLOCKED_BY_DEPENDENCIES`, `NO_CHANGE_REQUIRED`.

Categories A–H cover cancelled receipt/repayment refs, invalid refs, status/amount/schedule issues, replacements, and downstream dependencies.

---

## 4. Cancelled receipt reconciliation

Safe repair when JE `docstatus=2` and no submitted repayments:

1. Clear `receipt_journal_entry`
2. Set `received_amount = 0`
3. Set `status = Draft` (non-opening)
4. Refresh paid/remaining from submitted repayments
5. Preserve the cancelled Journal Entry (no delete, no GL rewrite)

If exactly one submitted replacement JE exists (`cheque_no` = Facility name), safe path **relinks** instead of clearing.

If submitted repayments exist without a replacement → `BLOCKED_BY_DEPENDENCIES`.

**Target repaired on development.localhost:**

| Field | Before | After |
|-------|--------|-------|
| Facility | FAC-2026-11810 | FAC-2026-11810 |
| Receipt JE | ACC-JV-1405-146350 (cancelled) | `NULL` |
| Status | Active | Draft |
| Received amount | 432,264,132,834 | 0 |
| Replacement eligible | No | Yes |

---

## 5. Cancelled repayment reconciliation

Scanner detects submitted (or cancelled) Facility Repayments whose `journal_entry` is cancelled/missing.

Safe repair cancels the Facility Repayment (existing `on_cancel` clears the JE pointer and refreshes Facility paid fields), preserving other valid submitted repayments (partial-payment model).

---

## 6. Repayment Schedule reconciliation

Schedule rows are contractual. Scanner reports total principal/profit mismatches as `MANUAL_REVIEW_REQUIRED` and does **not** regenerate schedules or invent JE links on schedule rows.

---

## 7. Permanent cancellation handling

New hooks:

`erpnext_extensions/facility_management/journal_entry_hooks.py`

Registered on `Journal Entry` in `hooks.py`:

- **before_cancel**
  - Block cancelling a receipt JE while the Facility has submitted repayments
  - Block cancelling a repayment JE directly; user must cancel the Facility Repayment (unless cancel is owned by `FacilityRepayment.on_cancel`)
- **on_cancel**
  - Clear Facility receipt reference, zero `received_amount`, restore `Draft` when safe
  - Fail closed if Facility rollback cannot complete (cancels with the JE transaction)

`FacilityRepayment.on_cancel` sets `frappe.flags.in_facility_repayment_je_cancel` so JE cancel from repayment is not blocked.

---

## 8. Duplicate prevention

Receipt:

- Submitted receipt JE → block create
- Stale cancelled/missing pointer → block create until reconciled
- Multiple submitted receipt-like JEs → block / manual review
- Single unlinked submitted candidate → block create; relink via utility

Repayment:

- Existing remaining-amount validation unchanged
- Fully paid Facility → further principal/profit blocked by balance checks
- Direct JE cancel blocked while repayment submitted

---

## 9. Accounting safety controls

- No automatic JE create/cancel by the repair utility
- No GL balance manipulation
- No delete of historical Journal Entries
- No automatic cancel of dependent repayments/settlements from receipt cancel (blocked instead)
- Repair uses narrow `db_set` + `refresh_facility_paid_fields`
- Batch repair uses savepoints per Facility

---

## 10. Production repair utility

### Backup

```bash
bench --site <site> backup --with-files
# or mysqldump fallback if bench backup fails
```

### Preflight

```bash
bench --site <site> doctor
bench --site <site> list-apps
# Confirm no conflicting HRC / valuation jobs
```

### Commands (explicit `confirm=True` for live writes)

```bash
# 1) Global dry-run
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.run_global_scan \
  --kwargs '{"dry_run": True}'

# 2) Single Facility dry-run
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.repair_facility \
  --kwargs '{"facility": "FAC-XXXX", "dry_run": True}'

# 3) Single repayment dry-run
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.repair_repayment \
  --kwargs '{"repayment": "FREP-XXXX", "dry_run": True}'

# 4) Single Facility repair
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.repair_facility \
  --kwargs '{"facility": "FAC-XXXX", "dry_run": False, "confirm": True}'

# 5) Single repayment repair
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.repair_repayment \
  --kwargs '{"repayment": "FREP-XXXX", "dry_run": False, "confirm": True}'

# 6) Approved batch (SAFE only)
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.repair_batch \
  --kwargs '{"facilities": ["FAC-A","FAC-B"], "dry_run": False, "confirm": True, "only_safe": True}'

# 7) Post-repair verification
bench --site <site> execute \
  erpnext_extensions.facility_management.facility_je_reconciliation.verify_facility \
  --kwargs '{"facility": "FAC-XXXX"}'
```

### Rollback

Restore the pre-repair database backup. Facility field changes are reversible via backup; cancelled JEs are never deleted by this utility.

---

## 11. Deployment instructions

1. Deploy `erpnext_extensions` 5.6.3 code
2. `bench --site <site> clear-cache` (hooks change)
3. **`bench migrate` is NOT required** (no DocType/schema changes)
4. Run global dry-run scan; review MANUAL / BLOCKED
5. Repair approved SAFE candidates one-by-one or via confirmed batch
6. Verify target Facilities can create replacement receipts where business rules allow

---

## 12. Tests and results

Module:

`erpnext_extensions.facility_management.tests.test_facility_je_reconciliation`

```bash
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.facility_management.tests.test_facility_je_reconciliation
```

**Result (development.localhost):** `Ran 19 tests … OK`

Coverage includes: receipt create/submit/cancel/restore/replace/duplicate; repayment create/submit/cancel/replace/partial; schedule model; blocked receipt cancel with repayments; blocked direct repayment JE cancel; missing JE; unrelated JE; repair idempotency; unrelated Facility isolation; scanner dry-run; repeated cancel/reissue.

---

## 13. Known limitations

- Schedule contractual mismatches are reported only (no auto-regenerate)
- Shared receipt JE across Facilities → manual review
- Multiple candidate replacement JEs → manual review
- Opening / migrated Facilities are not forced to zero `received_amount` solely for missing receipt JE
- Amount-field drift on Facilities without cancelled JE refs may be refreshed only when an explicit repair path runs `refresh_facility_paid_fields`
- Historical sites may still have SAFE candidates beyond the repaired target; do not batch-repair without review

---

## 14. Rollback considerations

- Code rollback: redeploy previous app version and `clear-cache`
- Data rollback: restore DB backup taken before repair
- Permanent hooks are additive; rolling back code re-exposes the cancel gap for future JE cancels

---

## Migration required

**NO**

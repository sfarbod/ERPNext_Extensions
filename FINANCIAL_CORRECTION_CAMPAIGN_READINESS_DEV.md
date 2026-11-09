# Financial correction campaign — readiness (Development only)

**Version:** `erpnext_extensions` **5.7.18** (`__version__` unchanged)  
**Site:** `development.localhost`  
**Git:** history preserved; **no commit / push**  
**Data mutation / RIV / HRC:** **not executed**  
**JE `ACC-JV-1405-169046`:** denylisted; unmodified  

---

## Code changes (local working tree)

| File | Change |
|------|--------|
| `historical_stock/manufacture_native.py` | IRR integer `write_sle_rate` / SED rates; amount-authoritative Class A residual; SVD/qty consume rate integerized |
| `historical_stock/manufacture_scrap_fg.py` | Integer scrap/FG SED+SLE rates under IRR; SVD/qty consume integerized |
| `domain/stock_entry_sync.py` | SVD/qty → `round_monetary_rate`; `enforce_irr_sle_unit_rates_integer` fail-safe on sync exits |
| `financial_correction_campaign/*` | Cohort A/B dry-run APIs, denylist, approved Asset PR map, outsider reject |
| `asset_purchase_receipt_correction/*` | Existing per-PR API (still apply-pending) |
| `tests/test_ste_integer_rate_hardening_v5718.py` | Regression coverage |

## Tests

```text
python -m unittest erpnext_extensions.iran_accounting.tests.test_ste_integer_rate_hardening_v5718
→ 8 tests OK
```

Also retained: `test_asset_pr_correction_v5720` (account resolution / FX scope).

Representative before/after (calculation only — **ledger not mutated**):

| Case | Before IR | Expected int IR | Class A residual |
|------|-----------|-----------------|------------------|
| PO-JOB shape SVD 4,400,547,964 / qty 3000 | 1,466,849.32133… | **1,466,849** | **964** |
| MFG FG amount 2,040,161,462.72 / 300 | 6,800,538.209… | **6,800,538** | **62.72** (pre-amount round) |
| Repack 1,087,591,494.88 / 659 | 1,650,366.456… | **1,650,366** | **~300.88** |

---

## Cohort A — Asset PR dry-run

Manifest: `CAMPAIGN_A_ASSET_PR_DRYRUN_DEV.json`  
API: `dry_run_asset_purchase_receipt_cohort`  
**8/8 success** dry-run; all still **PENDING apply**.

| PR | Target account | Land debit |
|----|----------------|------------|
| MAT-PRE-2026-01907 | 121013 …کامپیوتری | 6,630,000 |
| MAT-PRE-2026-00953 | 121013 | 1,500,000,000 |
| MAT-PRE-2026-01311 | 121006 | 1,290,000,000 |
| MAT-PRE-2026-00747 | 121006 | 1,290,000,000 |
| MAT-PRE-2026-01413 | 121003 | 11,781,250,000 |
| MAT-PRE-2026-00880 | 121006 | 158,000,000 |
| MAT-PRE-2026-01241 | 121003 | 1,030,000,000 |
| MAT-PRE-2026-01266 | 121006 | 1,160,000,000 |

RIV plan per PR: Transaction + `repost_only_accounting_ledgers=1` **after** source `expense_account` correction (RIV alone insufficient).

## Cohort B — STE fractional dry-run

Manifest: `CAMPAIGN_B_STE_FRACTIONAL_DRYRUN_DEV.json`  
API: `dry_run_stock_entry_fractional_cohort`  

| Metric | Count |
|--------|-------|
| Frac SLE | 512 |
| Vouchers | 387 |
| Identities | 73 |

**Blocker:** `MAT-STE-2026-29903` (Repack) has **0 GL rows** — must investigate before accepting Repack reconciliation.

---

## Future controlled execution order (when authorized)

```bash
# 0) Confirm version
bench --site development.localhost execute erpnext_extensions.__version__
# expect 5.7.18

# 1) Dry-runs (safe)
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.financial_correction_campaign.api.dry_run_asset_purchase_receipt_cohort
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.financial_correction_campaign.api.dry_run_stock_entry_fractional_cohort

# 2) Cohort A — one PR at a time (NOT run in this task)
# apply_asset_purchase_receipt_correction(purchase_receipts=[...], dry_run=0,
#   confirm='APPLY_ASSET_PR_CORRECTION', fingerprints={...})
# then verify GL / Asset / GRNI gates; never touch ACC-JV-1405-169046

# 3) Cohort B — one identity per category first (NOT run in this task)
# Item+Warehouse RIV from earliest posting_date; verify integer SED/SLE + SVD/GL
# Investigate MAT-STE-2026-29903 missing GL before Repack sign-off
```

Confirm token strings (future only):  
`EXECUTE_ASSET_PR_COHORT` / `EXECUTE_STE_FRACTIONAL_COHORT` / `APPLY_ASSET_PR_CORRECTION`

---

## Readiness & blockers

| Item | Status |
|------|--------|
| Code fixes for integer IRR STE paths | Ready (local, uncommitted) |
| Unit tests | Pass |
| Asset PR dry-run manifest | Ready — apply **not** authorized |
| STE fractional dry-run manifest | Ready — RIV **not** authorized |
| `ACC-JV-1405-169046` | Protected / unchanged |
| `MAT-STE-2026-29903` missing GL | **Blocker** for Repack gate |
| Production | Out of scope on this bench |
| Company-wide rebuild | Forbidden / not prepared |

**Stop point honored:** no voucher mutation, no RIV/HRC execution, no commit/push.

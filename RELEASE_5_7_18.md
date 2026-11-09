# Release 5.7.18 — Integer IRR Valuation Rate + Inventory / Asset PR Campaign Readiness

**Version:** **5.7.18** (package `__version__`; do **not** treat any earlier draft as a separate release)  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting IRR valuation, residual classification, Stock Entry hardening, Asset PR / FA GL correction, financial correction campaign dry-run APIs  
**Deployment status:** Development-validated work. Production apply is separate and gated.  
**Denylist:** Never modify / cancel / reverse `ACC-JV-1405-169046` (Finance-owned).

This document consolidates everything previously recorded under the 5.7.18 ULP-skip draft notes and the integer-VR work that was briefly labeled “5.7.19” in Git history. The **effective package version is only 5.7.18**. Commit `7bd1b1d` implements integer VR; it is **not** a published 5.7.19 package.

---

## A. Chronology within 5.7.18

### A.1 Earlier ULP-skip draft (superseded — do not deploy as policy)

Originally documented when commit `bb6e710` accepted ±1 DECIMAL(30,9) quantum / IEEE ULP drift for historical UVR floats.

**Problem it targeted**

Historical Rebuild Campaign `HRC-00014` fail-fast during sequential RIV:

> `IRR rate-rounding residual rejected: valuation inconsistency (Class B)`  
> Purchase Receipt `MAT-PRE-2026-02045` / item `15010547`  
> `qty=46`, `valuation_rate=1737714.673913044`, `amount=79934875`,  
> `expected_rate=1737715`, `residual=-15`, `reason=non_integer_rate_under_irr_contract`

RIV: `8tt4rqd11g` — identity `15010547` @ `انبار ملزومات مصرفی اسپاد`.  
A second company case: `MAT-PRE-2026-02094` / `15010666` qty=3.

**Root cause (as analyzed for the draft)**

PI discount produced amount-preserving fractional `valuation_rate = auth/qty`. Sibling rows on the same PR reconstructed exactly and skipped. This row’s stored VR was **one DECIMAL(30,9) quantum (1e-9)** above `flt(auth/qty)` while `qty × VR` still equalled acquisition auth. The legacy UVR detector required exact equality, so classification fell through to Class B and invented a −15 residual against `round(rate)`. SLE already matched auth (`SVD = 79,934,875`). Not SLE/GL drift.

**Draft fix (no longer accepted policy)**

`is_legacy_uvr_float_valuation_rate` accepted ±1 DECIMAL(30,9) quantum and IEEE ULP drift vs `auth/qty`, still gated by non-integer rate under IRR and `ROUND_HALF_UP(rate) == integer_valuation_rate_from_amount(auth, qty)`. Economically reconstructing fractional VRs were **skipped** (`amount_preserving_fractional_vr_zero_residual`), which would have **kept** rates like `1,737,714.673913044` in the ledger path.

**HRC follow-up note from the draft**

After any valuation-policy change on Development, re-run / resume sequential RIV for the blocked identity and confirm Class B no longer fires incorrectly. Do not treat campaign status alone as success — verify RIV Completed and GL balance.

### A.2 Current 5.7.18 policy — mandatory integer IRR valuation rate

**Valuation Rate must always be an integer under IRR** (company currency). Fractional rates are not permitted — including historical UVR `auth/qty` floats and ±1e-9 persistence drift. The ULP-skip draft above is **not** accepted.

1. **Write path** (`_align_purchase_valuation_rate_amount_preserving`): always `integer_valuation_rate_from_amount(auth, qty)` for IRR company-currency VR on FX, domestic, LCV, PI `amount_difference`, and header-discount acquisitions. FX commercial `rate` keeps transaction-currency precision; only company-currency `valuation_rate` / `base_*` are integerized. Non-IRR companies are unchanged.
2. **Classify path**: fractional VR with `|auth − qty×VR| ≤ 0.51` (or exact legacy UVR reconstruction) → **Class A** coerce to integer VR; residual `auth − qty×integer_VR` posts via Stock Adjustment (not Round Off / manual JE substitutes).
3. **ULP tolerance removed** from the legacy UVR detector (exact reconstruction only).
4. True non-reconstructing fractional rates remain **Class B** fail-closed.
5. Replaces v5.6.25 fractional amount-preserving VR skip.

Example (`MAT-PRE-2026-02045` / `15010547` qty=46, auth=79,934,875):

| | Fractional (invalid) | Integer (required) |
|--|--|--|
| VR | 1,737,714.673913044 | **1,737,715** |
| Inventory | 79,934,875 | 79,934,890 |
| Residual | (must not skip) | **−15** Class A |

Gap vs acquisition amount posts as **Class A** Stock Adjustment residual. Do **not** hide residuals via Round Off or manual Journal Entries.

---

## B. Stock Entry integer IRR hardening (Cohort B readiness)

- `domain/stock_entry_sync.py` — SVD/qty via `round_monetary_rate`; `enforce_irr_sle_unit_rates_integer` fail-safe on sync exits (preserves SVD amount authority).
- `historical_stock/manufacture_native.py` — IRR integer `write_sle_rate` / SED rates; amount-authoritative Class A residual; SVD/qty consume rate integerized.
- `historical_stock/manufacture_scrap_fg.py` — Integer scrap/FG SED+SLE rates under IRR; SVD/qty consume integerized.
- Suite: `tests/test_ste_integer_rate_hardening_v5718.py`.

Evidence / dry-run (read-only manifests; no campaign execution claimed by this release file alone):

- `STE_FRACTIONAL_IRR_INVESTIGATION_DEV.md` / `.json` — 512 fractional STE SLE rows / 387 vouchers.
- `CAMPAIGN_B_STE_FRACTIONAL_DRYRUN_DEV.json` — Cohort B dry-run manifest.
- `ZERO_GL_STE_INVESTIGATION_DEV.md` / `.json` / `ZERO_GL_STE_DEEP_DIVES_DEV.json` — 1,387 zero-GL STE classification (1,384 expected; 3 unexpected MTfM).
- `INVENTORY_WIDE_CAMPAIGN_READINESS_AUDIT_DEV.md` / `.json` — inventory-wide readiness.
- `FINANCIAL_CORRECTION_CAMPAIGN_READINESS_DEV.md` — campaign API readiness.

---

## C. Asset Purchase Receipt / Fixed Asset GL (Cohort A)

### C.1 Land / expense-account correction API

Module: `erpnext_extensions.iran_accounting.asset_purchase_receipt_correction`

- Per-PR dry-run / apply via Transaction RIV (`repost_only_accounting_ledgers`).
- Per-row destination account from asset category — **no blanket Land→Computer**.
- Dry-run success is **not** completed correction by itself.
- Evidence: `ASSET_PR_LAND_CORRECTION_DRYRUN_DEV.json`, `ASSET_PR_DEPRECIATION_IMPACT_DEV.md` / `.json`, `CAMPAIGN_A_ASSET_PR_DRYRUN_DEV.json`, `CAMPAIGN_A_ASSET_PR_EXECUTION_DEV.md` / `.json`, `ASSET_PR_COHORT_A_READINESS_V5718.md`.

Asset PRs have **no SLE**. Rebuild original PR GL via RIV `based_on=Transaction` + `repost_only_accounting_ledgers` after correcting `expense_account` on the PR item — **not** via manual Journal Entries.

### C.2 Fixed Asset PR GL amount-difference patch

With `Buying Settings.set_landed_cost_based_on_purchase_invoice_rate = 1`, core updates PR `amount_difference_with_purchase_invoice` and `valuation_rate` from the linked PI, but Fixed Asset `PurchaseReceipt.make_item_gl_entries` (ERPNext 16.53) omitted `amt_diff` on the FA debit while GRNI received it → imbalance.

- Extension: `integration/fa_pr_gl_amount_difference.py` (wired in `monkey_patches.py`).
- Evidence: `ASSET_PR_FA_GL_FIX_V5718.md`, `ASSET_PR_FA_GL_FIX_TESTS_V5718.json`.
- Suite: `tests/test_fa_pr_gl_amount_difference_v5718.py`.

---

## D. Financial Correction Campaign APIs

Package: `erpnext_extensions.iran_accounting.financial_correction_campaign`

- Cohort A Asset PR dry-run and Cohort B STE fractional dry-run whitelisted APIs.
- Journal denylist includes `ACC-JV-1405-169046` at every entry point.
- Confirm tokens required for any future execute path; dry-run does not mutate ledgers.

---

## E. Production decimal-VR HRC validation (priority)

Must run on the Production bench hosting `erp.espadpharmed.com` (not this Development bench):

1. Confirm installed app version is **5.7.18** with this consolidated policy present.
2. Fresh Production DB + files backup.
3. Identify decimal-rate Purchase Receipts / STE identities.
4. Controlled per-identity / per-voucher RIV (not company-wide).
5. Verify SLE / SVD / GL; no fractional IRR VR remaining; Class A residuals explicit.
6. Report before/after VR and residuals.
7. Never touch `ACC-JV-1405-169046`.

---

## F. Tests

- `test_integer_vr_enforcement_v5719.py` — integer VR / Class A residual (suite filename historical; policy is 5.7.18)
- `test_pr_uvr_ulp_drift_v5718.py` — aliases integer suite (ULP skip removed)
- Updated `test_pr_lcv_amount_preserving_vr_v5625.py`, `test_pr_legacy_uvr_float_v5528.py`
- `test_ste_integer_rate_hardening_v5718.py`
- `test_asset_pr_correction_v5720.py`
- `test_fa_pr_gl_amount_difference_v5718.py`

---

## G. Related Dev evidence retained with this release line

- `CONTROLLED_RIV_BATCH_DEV.json`, `CONTROLLED_RIV_15010666_DEV.json` — controlled Dev RIV batch evidence.
- `DEV_FRACTIONAL_IRR_CHAIN_EVIDENCE.json` — decimal PR chain verification on Development.

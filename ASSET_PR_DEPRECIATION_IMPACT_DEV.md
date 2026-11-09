# Cohort A — Asset & Depreciation Impact (Read-Only Dev)

**Generated from:** `ASSET_PR_DEPRECIATION_IMPACT_DEV.json`  
**Mutated:** No  
**App version:** 5.7.18  

Core methods reviewed (not executed): `erpnext.assets.doctype.asset.depreciation.reset_depreciation_schedule`, `reschedule_depreciation`, Asset Depreciation Schedule `modify_depreciation_dchedule`. Blind use of `reset_depreciation_schedule` is **not safe** when posted Depreciation Entries / JE links exist — it can reschedule around history and must not auto-cancel posted GL.

---

## Summary

| Purchase Receipt | Linked Assets | PI amt_diff | Rebuild required? | Notes |
|---|---:|---:|---|---|
| MAT-PRE-2026-01907 | 1 (`4026`) | 0 | **No** (Case D) | Cost matches; 0 posted depr rows |
| MAT-PRE-2026-00953 | 3 | 0 | **No** (Case D) | Costs match per-asset VR×qty; posted depr exists — preserve |
| MAT-PRE-2026-01311 | 10 (`3736`–`3745`) | 0 | **Yes / Case E** | Asset `purchase_amount` 122,550,000 vs PR VR×qty 129,000,000; **posted depr present** — no auto rebuild |
| MAT-PRE-2026-00747 | **0** | -64,500,000 | **No** (Case A) | No Asset docs; FA GL fix enables future RIV only |
| MAT-PRE-2026-01413 | 1 (`3750`) | 0 | **No** (Case D) | Posted depr — preserve |
| MAT-PRE-2026-00880 | 1 (`3766`) | 0 | **No** (Case D) | Posted depr — preserve |
| MAT-PRE-2026-01241 | 1 (`3833`) | 0 | **No** (Case D) | No posted depr |
| MAT-PRE-2026-01266 | 1 (`3838`) | 0 | **No** (Case D) | Posted depr — preserve |

`gross_purchase_amount` is **0** on most of these Assets; comparison uses `purchase_amount` / `net_purchase_amount` fallbacks.

---

## Per-PR detail

### MAT-PRE-2026-01907 → target 121013 (already reconciled on Dev)

| Field | Asset `4026` |
|---|---|
| docstatus / status | 1 / Submitted |
| purchase_amount (current) | 6,630,000 |
| corrected expectation | 6,630,000 |
| posted depr entries | 0 |
| rebuild | **Not required** |
| safe standard rebuild | n/a |

### MAT-PRE-2026-00953 → 121013 (already reconciled)

Assets `3670`, `3699`, `3703` — Case D (costs match allocated VR×qty). Posted depreciation present on all three → **preserve historical JE/GL**; no schedule rebuild from Cohort A.

### MAT-PRE-2026-01311 → 121006 (already reconciled)

Ten Assets `3736`–`3745`, status Partially Depreciated.

| Metric | Value |
|---|---|
| Current acquisition (each) | 122,550,000 |
| PR valuation × qty (each) | 129,000,000 |
| Delta | +6,450,000 per asset |
| Posted depr rows (each) | 5 (non-zero amounts) |
| Decision | **Case E** — rebuild conceptually needed for cost alignment, but **`safe_standard_rebuild = false`** |
| Action | Separate Finance-approved design for future-only adjustment; **do not** cancel/reverse posted Depreciation Entries |

Note: This cost gap is **independent** of the Land→121006 GL account correction already done on Dev. Account correction ≠ Asset cost update.

### MAT-PRE-2026-00747 → 121006 (failed RIV; FA GL fix ready)

- **Case A** — no linked Asset.  
- PI-aligned acquisition for a future Asset would be **1,225,500,000**.  
- No depreciation schedule work.

### MAT-PRE-2026-01413 / 00880 / 01241 / 01266 (not executed)

All Case D for acquisition vs PR valuation. Several have posted depreciation — preserve. Future Cohort A execute is account/GL only unless Finance separately authorizes Asset cost work.

---

## Rebuild policy (binding)

1. Successful accounting RIV does **not** update Asset purchase amounts or schedules.  
2. Case E with posted depreciation → **block auto rebuild**.  
3. Never delete/rewrite submitted schedule rows or Depreciation Entry GL from this campaign.  
4. Draft Assets only: recommend field update before submit (none in this cohort).

# Cohort A — Asset Purchase Receipt Execution Complete (Development)

**Site:** `development.localhost` only  
**App version:** `5.7.18`  
**ERPNext:** 16.53.0  
**FA GL patch:** installed (`fa_pr_gl_amount_difference`)  
**Cohort B:** not executed · **Production:** not touched · **Git:** no commit/push  

Evidence JSON: `CAMPAIGN_A_ASSET_PR_EXECUTION_DEV.json`

---

## Verdict

**All eight Asset Purchase Receipts are financially reconciled on Development.**

`ACC-JV-1405-169046` unchanged (`modified=2026-10-19 14:35:53.838699`, `total_debit=18,215,880,000`).

Bidirectional PI/PR amount-difference support verified:

| Scenario | Meaning | Seen in cohort |
|---|---|---|
| A — PI higher | `amt_diff` > 0 → FA debit increases | Not present in remaining set |
| B — PI lower | `amt_diff` < 0 → FA debit decreases | **`MAT-PRE-2026-00747`** (−64,500,000) |
| C — equal | `amt_diff` = 0 → core debit unchanged | `01413`, `00880`, `01241`, `01266` (+ prior successes) |

Campaign does **not** require PR amount = PI amount. Invoice-based valuation (`Buying Settings.set_landed_cost_based_on_purchase_invoice_rate=1`) drives FA debit = `base_net + tax + LCV + amt_diff`.

---

## Per-voucher results

| Purchase Receipt | Target | Source | RIV | Scenario | FA debit after | GRNI credit | Result |
|---|---|---|---|---|---:|---:|---|
| MAT-PRE-2026-01907 | 121013 | prior | prior | C | 6,630,000 | 6,630,000 | **Preserved OK** |
| MAT-PRE-2026-00953 | 121013 | prior | prior | C | 1,518,000,000 (+ other FA rows) | balanced | **Preserved OK** |
| MAT-PRE-2026-01311 | 121006 | prior | prior | C | 1,290,000,000 | 1,290,000,000 | **Preserved OK** |
| MAT-PRE-2026-00747 | 121006 | already 121006 | `avk6q3n6iq` Completed (RIV-only) | **B** | **1,225,500,000** | 1,225,500,000 | **OK** |
| MAT-PRE-2026-01413 | 121003 | applied | `avmggq2j59` Completed | C | 11,781,250,000 | 11,781,250,000 | **OK** |
| MAT-PRE-2026-00880 | 121006 | applied | `avmks58of9` Completed | C | 158,000,000 | 158,000,000 | **OK** |
| MAT-PRE-2026-01241 | 121003 | applied | `avnq5l95ln` Completed | C | 1,030,000,000 | 1,030,000,000 | **OK** |
| MAT-PRE-2026-01266 | 121006 | applied | `avokdlv8vh` Completed | C | 1,160,000,000 | 1,160,000,000 | **OK** |

Credit clearing account: `211230 - معلق خرید دارایی ثابت - E`.

---

## `00747` detail (Scenario B)

| Field | Value |
|---|---|
| Source expense before this run | `121006` (already corrected; Land GL residual) |
| Path | RIV-only recovery (no re-write of commercial rates) |
| `amount_difference_with_purchase_invoice` | −64,500,000 |
| PR base_net | 1,290,000,000 |
| Expected / actual FA debit | **1,225,500,000** |
| Linked Assets | none — no Asset cost / depr update |

---

## Asset / depreciation follow-ups (not auto-mutated)

| PR | Assets | Rebuild auto? |
|---|---|---|
| 00747 | none | N/A |
| 01311 | 10 assets @ purchase_amount 122.55M vs PR VR 129M; posted depr | **No** — Case E, Finance design required |
| Others | Case D or matched | No auto rebuild |

Successful RIV does **not** update Asset acquisition cost or depreciation schedules.

---

## Explicit non-actions

- No Cohort B  
- No company-wide RIV/HRC  
- No Production  
- No touch of `ACC-JV-1405-169046`  
- No compensating JE / Round Off / Stock Adjustment  
- No git commit or push  
- Version remains `5.7.18`

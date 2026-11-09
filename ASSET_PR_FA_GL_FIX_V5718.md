# Fixed Asset Purchase Receipt GL Fix — v5.7.18

**Site:** `development.localhost`  
**ERPNext:** 16.53.0 · **Frappe:** 16.54.0 · **erpnext_extensions:** **5.7.18**  
**Scope:** Extension-only implementation + tests + read-only evidence. **No** document/ledger mutation, RIV, HRC, commit, or push.

---

## Root cause

With `Buying Settings.set_landed_cost_based_on_purchase_invoice_rate = 1`, core updates PR `amount_difference_with_purchase_invoice` and `valuation_rate` from the linked PI, but **Fixed Asset** `PurchaseReceipt.make_item_gl_entries` (ERPNext 16.53.0) still computes the FA debit as:

```text
base_net_amount + item_tax_amount + landed_cost_voucher_amount
```

while `make_amount_difference_entry` posts `amount_difference_with_purchase_invoice` onto Asset Received But Not Billed (GRNI), and `make_divisional_loss_gl_entry` **returns early for FA**. Result: GL imbalance = `|amt_diff|`.

Concrete case `MAT-PRE-2026-00747` / `ACC-PINV-2026-00331`: imbalance **64,500,000** IRR.

---

## Core vs Extension

| Behavior | Core 16.53.0 | Extension 5.7.18 |
|---|---|---|
| Overwrite PR commercial rate/amount from PI | No | No (unchanged) |
| Update `billed_amt` / `per_billed` | Yes | Unchanged |
| Set `amount_difference` + VR when flag on | Yes | Unchanged (+ #59476 compat / IRR UVR) |
| FA debit includes `amt_diff` | **No** | **Yes** (this fix) |
| Stock-item GL / SLE path | Unchanged | Unchanged (FA-only adjust) |
| Separate price-variance JE | No | No |

---

## Exact files / functions

| File | Change |
|---|---|
| `iran_accounting/integration/fa_pr_gl_amount_difference.py` | **New** — FA debit adjust + idempotent install |
| `iran_accounting/integration/monkey_patches.py` | Calls `_patch_fa_pr_gl_amount_difference()` from `apply_monkey_patches` |
| `iran_accounting/asset_purchase_receipt_correction/asset_impact.py` | **New** — read-only Asset/depr impact decisions |
| `iran_accounting/financial_correction_campaign/cohort.py` | Dry-run enriched with PI diffs, asset impact, blockers, JE denylist |
| `iran_accounting/tests/test_fa_pr_gl_amount_difference_v5718.py` | **New** regression tests |

**Patched method:** `PurchaseReceipt.make_item_gl_entries` — wrap original, then adjust in-memory FA debit rows.

**Formula (no double-count):**

```text
FA debit = base_net + item_tax + LCV + amount_difference_with_purchase_invoice
```

`amt_diff` is **not** part of base_net, item_tax, or LCV. GRNI rows from core (including `make_amount_difference_entry`) are left untouched. Tax/LCV credits remain as core posts them.

**Guards:**

- Only `is_fixed_asset` items  
- Only when invoice-based valuation setting is enabled  
- No-op when `amt_diff ≈ 0`  
- Install marker `_iran_fa_pr_gl_amt_diff` — install exactly once  

---

## Live read-only proof (`MAT-PRE-2026-00747`)

After bootstrap (no save/repost), `get_gl_entries()`:

| Account | Debit | Credit |
|---|---:|---:|
| `121006 - اثاثیه ومنصوبات - E` | 1,225,500,000 | — |
| `211230 - معلق خرید دارایی ثابت - E` | — | 1,225,500,000 |
| **Total** | **1,225,500,000** | **1,225,500,000** |

Posted GL on disk is **unchanged** by this task (still Land 1.29B until a future accounting RIV).

---

## Compatibility

- Coexists with `#59476` PR/PI landed-cost compat wrap  
- Coexists with IRR `update_regional_item_valuation_rate`  
- Does not wrap Stock Entry / stock PR GL paths  
- JE denylist `ACC-JV-1405-169046` remains in campaign + asset-correction APIs  

---

## Explicit non-actions (this task)

No source correction, Asset update, depreciation rebuild, RIV, HRC, SLE/GL write, commit, or push.

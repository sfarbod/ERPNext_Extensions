# erpnext_extensions 5.7.57

## Fix — Subcontracting Receipt Clearing Account Balance (Iran Accounting)

### Summary

Version **5.7.57** fixes a systematic General Ledger imbalance on the
subcontracting/consignment clearing account
(`111421 - واسط کالای امانی (برونسپاری)`) for IRR Subcontracting Receipts
when Iran integer / SABE valuation raises finished-goods SLE
`stock_value_difference` above the document float `amount`.

### Root cause

Core `SubcontractingReceipt.make_item_gl_entries`:

1. Debits FG inventory from SLE SVD.
2. Credits expense/clearing with **SVD − service_cost** (already embeds +Δ).
3. Credits subcontracting service expense with `service_cost`.
4. Posts `divisional_loss = amount − SVD` (= −Δ) to Stock Adjustment **and**
   mirrors that signed amount onto the same expense/clearing account.

When Iran integer valuation sets **SVD = amount + Δ**:

| Leg | Effect on clearing |
|---|---|
| Expense credit (step 2) | +(amount − service) **+ Δ** |
| Divisional mirror (step 4) | another **+Δ** credit (negative debit toggled) |
| RM consumption debit | +(amount − service) |

Net clearing imbalance = **2Δ**. Overall the voucher still balances because that
2Δ clearing credit funds both INV's +Δ and Core's SA debit +Δ.

Reference DEV transaction `MAT-SCR-2026-00112` (qty 1,962, fixed service
1,230,000 IRR): Δ = 174 → clearing discrepancy **348 IRR**, SA debit **174**.

Forcing clearing to net zero while leaving Core's SA debit would break voucher
balance by 2Δ. The economically correct residual for an SLE integer **increase**
is a Stock Adjustment **credit** (valuation gain), not a debit.

### Fix

| Layer | Change |
|---|---|
| Domain | `iran_accounting/domain/subcontracting_receipt_gl_balance.py` |
| Integration | `StockController.make_gl_entries` (IRR) calls the normalizer before residual / SABE / aggregate |

Corrected accounting rule (IRR, non-return SCR):

- Clearing **credit** = Σ(`item.amount − service_cost`) from the document.
- Clearing **debit** = Σ supplied RM amounts (same expense account) → net **0**.
- Inventory and service expense legs unchanged (INV stays at SLE SVD; service
  fixed-total unchanged).
- Stock Adjustment rewritten to the single residual **Σ(amount − SVD)**:
  - SVD > amount → **credit** Δ (valuation gain)
  - SVD < amount → **debit** \|Δ\| (valuation loss)
- Idempotent under repost (`doc.flags.irr_scr_clearing_balanced`).

Preserved:

- Fixed-total subcontracting service cost allocation (unchanged).
- SLE inventory valuation (unchanged).
- Standard ERPNext SCR behaviour for non-IRR companies and returns.

### Regression tests

`iran_accounting/tests/test_scr_clearing_balance_v5757.py`:

- A — Exact integer valuation → clearing balanced, SA = 0
- B — Fractional service (1,962 × 1,230,000) → debit = credit, SA credit = Δ, voucher balanced
- C — IRR rounding difference on SVD
- D — Stock Adjustment magnitude/sign correct without clearing imbalance
- E — Repost rebuild stays balanced
- F — Repeated normalize is idempotent (no duplicate legs)
- G — Positive and negative Δ both balance

### Verification (DEV)

Repost `MAT-SCR-2026-00112` via supported reverse + `make_gl_entries`. Accept only when:

- Account 111421 debit = credit (net 0)
- Service expense credit = 1,230,000
- Stock Adjustment credit = 174 (valuation gain for Δ)
- Total GL debit = total GL credit
- No duplicate SLE/GL from the repost

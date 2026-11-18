# erpnext_extensions 5.7.57

## Fix — Subcontracting Receipt Clearing Account Balance (Iran Accounting)

### Summary

Version **5.7.57** fixes a systematic General Ledger imbalance on the
subcontracting/consignment clearing account
(`111421 - واسط کالای امانی (برونسپاری)`) for IRR Subcontracting Receipts
when Iran integer / SABE valuation raises finished-goods SLE
`stock_value_difference` above the document float `amount`.

**Mandatory rule:** account 111421 must always net to zero. When there is no
net accounting movement (including equal debit and credit), **no GL row** is
persisted for that account — reusing the established
`drop_net_zero_merged_gl_rows` convention from Manufacture / Repack.

### Root cause

Core `SubcontractingReceipt.make_item_gl_entries`:

1. Debits FG inventory from SLE SVD.
2. Credits expense/clearing with **SVD − service_cost** (already embeds +Δ).
3. Credits subcontracting service expense with `service_cost`.
4. Posts `divisional_loss = amount − SVD` (= −Δ) to Stock Adjustment **and**
   mirrors that signed amount onto the same expense/clearing account.

When Iran integer valuation sets **SVD = amount + Δ**, clearing shows a
**2Δ** net credit while Stock Adjustment shows **Δ**. Overall the voucher
still balances because that 2Δ clearing credit funds both INV's +Δ and Core's
SA debit +Δ.

Reference DEV transaction `MAT-SCR-2026-00112` (qty 1,962, fixed service
1,230,000 IRR): Δ = 174 → clearing discrepancy **348 IRR**.

### Fix

| Layer | Change |
|---|---|
| Domain | `iran_accounting/domain/subcontracting_receipt_gl_balance.py` |
| Reuse | `drop_net_zero_merged_gl_rows` from `manufacture_expense_account.py` |
| Integration | `StockController.make_gl_entries` (IRR) calls the normalizer before residual / SABE / aggregate |

Corrected accounting rule (IRR, non-return SCR):

1. Align clearing debit/credit to document RM / (`amount − service`).
2. Rewrite Stock Adjustment to residual **Σ(amount − SVD)**  
   (credit Δ when SVD > amount; debit \|Δ\| when SVD < amount).
3. **Drop** balanced clearing rows (net zero / equal debit=credit / both zero)
   via `_drop_balanced_clearing_accounts` + `drop_net_zero_merged_gl_rows`.
4. Leave inventory (SLE SVD) and fixed-total service expense unchanged.
5. Idempotent under repost (`doc.flags.irr_scr_clearing_balanced`).

### Regression tests

`iran_accounting/tests/test_scr_clearing_balance_v5757.py`:

1. Debit = credit → net zero, **no clearing GL rows**
2. Zero debit/credit → no GL rows
3. Equal nonzero debit/credit dropped (Iran convention)
4. Positive IRR rounding Δ
5. Negative IRR rounding Δ
6. Fractional service cost (1,962 × 1,230,000)
7. Repeated repost idempotent
8. Reversal / reconstruction preserves rule
9. Existing zero-value rows not persisted
10. Overall voucher remains balanced

### Verification (DEV)

Repost `MAT-SCR-2026-00112` via reverse + patched `make_gl_entries`. Accept when:

- **No** active GL Entry for account 111421 (or debit = credit with net 0 and
  rows dropped)
- Service expense credit = 1,230,000
- Stock Adjustment credit = 174 (valuation gain for Δ)
- Total GL debit = total GL credit
- SLE inventory valuation unchanged

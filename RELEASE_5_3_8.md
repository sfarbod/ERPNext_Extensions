# Release 5.3.8 — IRR Stock Entry one-quantum round-off + RIV out-of-scope sync

## Problem

1. **MAT-STE-2026-30470 / RIV `Difference is 1.0`**  
   IRR currency precision is 0. ERPNext Stock Entry debit/credit allowance is hardcoded
   `0.5`. Iran `process_debit_credit_difference` absorbed only `|diff| < 1`, so
   `|diff| == 1` threw before `make_round_off_gle` (dead zone: round-off requires
   `|diff| >= 1` and `|diff| <= 0.5` — impossible).

2. **MAT-STE-2026-28696 RIV aborted on unrelated Manufacture I2**  
   - `sync_irr_sle_from_stock_entry_row` threw I2 without RIV scope gating.  
   - SE-level integrity treated “voucher contains target item” as fully blocking,
     so a poison FG row on a Manufacture that also consumes the target aborted the
     target-item RIV.

## Fix (general)

| Area | Change |
|------|--------|
| Iran `process_debit_credit_difference` | For currency precision 0, raise non-JE/PE allowance to **one quantum (1.0)** so `|diff|==1` reaches `Company.round_off_account` via `make_round_off_gle`. Larger imbalances still throw. |
| `transfer_valuation` | Material Transfer `previous_healthy_source_sle` promoted to **EXACT** (proven warehouse MA tip; not sibling invention). |
| `transfer_convergence` | Fix `replay_from` NameError (`from_dt`). |
| `stock_entry_sync` | Out-of-scope ValuationIntegrityError during RIV → log + skip Iran mirror. |
| `riv_valuation_scope` | SE-level asserts scope to exception `item_code=`, not voucher membership alone. |

## Classification (30470 1 IRR)

**CUSTOM_PRECISION_BUG** (Iran/ERPNext SE allowance dead zone) over a **LEGITIMATE_ROUNDING** residual. Original 30470 GL was balanced (D=C=9,026,708) with 8 IRR stock adjustment. The 1 IRR appears at RIV-time float→`flt(.,0)` and must post to round-off, not be absorbed by inventing SLE rates.

## Tests

- `test_irr_se_quantum_round_off_v538`
- `test_riv_valuation_scope` (SE offending-item scope)

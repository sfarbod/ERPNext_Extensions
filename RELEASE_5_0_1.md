# Release 5.0.1 — Purchase Invoice distributed-discount GL balance

## Summary

- **Bug:** Iran Accounting `_align_po_pi_si_row` rebuilt `net_amount` as `qty × net_rate` after ERPNext allocated document additional discount onto items (`distributed_discount_amount`). That wiped the allocation and produced unbalanced Purchase Invoice GL (example: `ACC-PINV-2026-00327`, difference **105**).
- **Fix:** When `distributed_discount_amount` is present, preserve and round the ERPNext `net_amount` / `base_net_amount` instead of recomputing from rate. Gross `amount` remains rate-first.
- **Not causal:** 5.0.0 canonical rounding ownership refactor (same align logic existed before).

## Policy

- Do **not** post 105 to Round Off — it is a destroyed discount allocation, not an IRR residual.
- Fail-closed GL validation unchanged.

## Compatibility

- ERPNext 16.33.x / Frappe 16.32.x
- Applies to Purchase Invoice and Sales Invoice (shared align path)

## Files

- `iran_accounting/domain/qty_rate_amount.py`
- tests for align + live PI pattern
- `RELEASE_5_0_1.md`

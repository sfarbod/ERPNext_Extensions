# Release 5.0.1 — Preserve ERPNext distributed-discount net amounts

## Summary

- **Bug:** Iran Accounting `_align_po_pi_si_row` rebuilt `net_amount` / `base_net_amount` as `qty × net_rate` after ERPNext allocated document additional discount (`distributed_discount_amount`). That wiped the allocation and unbalanced Purchase Invoice GL (example: `ACC-PINV-2026-00327`, difference **105**).
- **Fix:** When `distributed_discount_amount` is non-zero, preserve ERPNext post-discount `net_amount` / `base_net_amount` (round in-place only). Gross `amount` remains rate-first. Shared helper applies to **Purchase Invoice**, **Sales Invoice**, and **Purchase Order**.
- **Not causal:** 5.0.0 canonical rounding ownership refactor (same align arithmetic existed before).

## Policy

- Do **not** post the destroyed-discount delta to Round Off or Stock Adjustment — it is not a rounding residual.
- Fail-closed GL validation unchanged; true imbalances still raise.
- No historical repair / no schema change.

## Compatibility

- ERPNext 16.33.x / Frappe 16.31.x–16.32.x

## Files

- `iran_accounting/domain/qty_rate_amount.py`
- `tests/test_invoice_distributed_discount_align.py`
- `tests/test_purchase_invoice_distributed_discount_gl.py`
- `RELEASE_5_0_1.md`

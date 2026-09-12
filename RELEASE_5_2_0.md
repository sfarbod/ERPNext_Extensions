# Release 5.2.0 — Manufacture RIV Valuation Safety + SLE Sign Integrity

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.1.9**

## Summary

ERPNext 16.34.2 still revalues bomless/costed-out Manufacture secondary items from live warehouse/batch average during `calculate_rate_and_amount` (including RIV). When that scrap value exceeds the consumed RM pool, FG `basic_rate` / `amount` become negative.

Iran Accounting 5.1.9 then applied `abs(row.amount)` for incoming SLE movement, turning an invalid negative FG amount into a **positive** `stock_value_difference`. RIV can persist that SLE/Bin poison before GL rebuild later fail-closes.

**5.2.0** keeps Iran DC fail-closed and adds:

1. **Manufacture output valuation parity** between normal submit and RIV
2. **Component scrap issued-rate** handling when warehouse/batch valuation would poison FG
3. **FG residual restoration** after component-only scrap reprice
4. **SLE sign integrity** — `row.amount` is a non-negative magnitude; never `abs()`-repair
5. **RIV pre-persist safety guards** (L1 recalculate wrap, L2 `process_sle` before vanilla, L3 persist)
6. Dedicated **`ValuationIntegrityError`** diagnostics (`Stock valuation integrity`)

Negative incoming Manufacture valuation is **not** a rounding residual. It is blocked before persistence.

## Explicit non-goals

- No historical SLE / Bin / Stock Entry repair
- No generic ±1 GL absorb
- Transfer / MTfM value-neutral policy unchanged (no scrap-pool, no Stock Adjustment workaround)
- Iran DC fail-closed unchanged
- No Round Off on Manufacture residuals
- No silent clamp of negative values to zero
- Historical data repair is **not** included and remains a separate release

## Behavior

### Manufacture output contract (submit + RIV)

After ERPNext `calculate_rate_and_amount`:

- Product reject: existing absorbed-cost pool split (unchanged)
- Component scrap: issued rate from this voucher when ERPNext warehouse/batch valuation would make FG negative or exceed the pool; then FG is restored as residual
- Independent by-product: ERPNext warehouse/manual/BOM valuation only if FG stays ≥ 0; otherwise fail closed (I5)
- Healthy 25720-class warehouse/batch scrap is not rewritten to issued rate

### SLE sign contract

```
magnitude < 0 → ValuationIntegrityError
actual_qty > 0 → SVD = +magnitude
actual_qty < 0 → SVD = −magnitude
actual_qty = 0 → SVD = 0
```

### Guards

| Layer | Where | Role |
|-------|--------|------|
| L1 | wrap `recalculate_amounts_in_stock_entry` | Iran contract + I1–I5 **before** SE `db_update` |
| L2 | `process_sle` before vanilla | block already-invalid row/SLE from vanilla write |
| L3 | sync / persist helpers | refuse negative magnitude, negative incoming rate, inverted SVD, leftover value at qty 0 |

Unsupported ERPNext versions remain fail-closed (fingerprint allow-list; vanilla original is fingerprinted, never the wrapper).

## Live-safe verification (development.localhost, in-memory, rolled back)

| Voucher | ERPNext calc | 5.2.0 contract | DB/SLE/Bin |
|---------|--------------|----------------|------------|
| MAT-STE-2026-25333 | negative FG | FG restored ≥ 0 (issued scrap) | unchanged |
| MAT-STE-2026-25304 | negative FG | FG restored ≥ 0 (reject + issued component) | unchanged |
| MAT-STE-2026-25720 | healthy | warehouse scrap kept; FG ≥ 0 | unchanged |

No persistent RIV was run on poisoned historical vouchers.

## Version

- `erpnext_extensions.__version__` = `5.2.0`

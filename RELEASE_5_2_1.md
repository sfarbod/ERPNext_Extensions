# Release 5.2.1 — Production Stock Posting-Order Prevention + Historical Repair

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.0**

## Summary

ERPNext 16.34.2 processes Stock Ledger Entries by `(posting_datetime ASC, creation ASC)`. When a prerequisite inbound and a dependent outbound share the same posting second, the outbound SLE can be processed first if it was created earlier (typical of backdated production). Running quantity can go temporarily negative and then recover.

**5.2.1** adds:

1. **Production stock posting-order prevention** for automatic Job Card / Work Order / against-Stock-Entry dependents
2. **Historical same-time ordering repair** on the Historical Repair Desk page (EXACT dependencies only)
3. **Minimum 1-second** dependent-document ordering (`ensure_dependent_stock_posting_after`)
4. **Batch/SABB-aware** pairing (different warehouse or batch is not paired)
5. **Dry-run and audit trail** (`Production Posting Order Repair Log`)
6. **Valuation / GL impact preview** before write; healthy GL is not rebuilt unless required
7. **5.2.0 Manufacture valuation behavior unchanged**
8. **No global Stock Entry timestamp monkey patch**

## Explicit non-goals

- No change to 5.2.0 Manufacture valuation policy, component scrap costing, FG residual, IRR sign rules, RIV guards, Transfer/MTfM policy, or GL ±1 handling
- No global shift of Stock Entry times
- No pairing of unrelated documents
- Auto-repair is EXACT only; LIKELY is preview/manual; AMBIGUOUS fails closed
- Midnight `23:59:59 + 1s` does not silently change posting date (manual review)
- Real insufficient stock is not hidden by timestamp changes

## Behavior

### Prevention

On Stock Entry `before_validate` / `before_submit`, for proven production dependents:

```
dependent posting_datetime > prerequisite posting_datetime
minimum_seconds = 1
```

Row locks are limited to the prerequisite Stock Entry, Job Card, and Work Order. Unrelated documents at T+1 are not moved; the dependent uses the next free second.

### Historical repair

Scan same-datetime inbound/outbound production SLEs. Repair only when:

- confidence is EXACT
- current min running qty < 0 due to ordering
- proposed min qty ≥ 0
- final qty unchanged

Dry Run is required. Optimistic locking aborts if the document changed since preview.

## Live-safe verification (development.localhost)

| Item | Result |
|------|--------|
| Root cause | `ORDER BY posting_datetime, creation` — not insertion order |
| Real same-second example | `18000007` / WIP / batch `5673-18000007-J100260042` at `2026-06-21 18:01:20` (cross-WO, LIKELY, not auto-repaired) |
| Exact chain repaired | `MAT-STE-2026-37452` (MTfM 14:00:00) → `MAT-STE-2026-37451` (Manufacture 14:00:01); min qty -10 → 0; final qty unchanged |
| Gate `full_stress=1` | PASS |
| 5.2.0 unit/RIV/scrap/transfer | PASS |

## Version

- `erpnext_extensions.__version__` = `5.2.1`

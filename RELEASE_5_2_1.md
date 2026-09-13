# Release 5.2.1 — Production Stock Posting-Order Prevention + Historical Repair

## Purpose

Stop same-second production stock movements from processing in the wrong order.

ERPNext 16.34.2 sorts Stock Ledger Entries by `(posting_datetime ASC, creation ASC)`. When a prerequisite inbound and a dependent outbound share the same posting second, the outbound SLE can run first if it was created earlier. Running quantity can go temporarily negative and then recover, even when final quantity is correct.

5.2.1 prevents that on new automatic production documents, and offers a conservative historical repair for EXACT same-time groups that actually go negative.

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.0**
- `erpnext_extensions.__version__` = **5.2.1**

## Architecture

Package: `erpnext_extensions.iran_accounting.stock_posting_order`

| Module | Role |
|--------|------|
| `ordering.py` | SLE sort key matching ERPNext 16.34.2 |
| `dependency.py` | EXACT / LIKELY / AMBIGUOUS pairing |
| `prevention.py` | Future documents: proven prerequisite → dependent chronology |
| `optimizer.py` | Historical min-seconds search (only if current sequence goes negative) |
| `batch_identity.py` | Canonical batch from `SLE.batch_no` or single-batch SABB |
| `scanner.py` | Full-history same-time groups with true opening qty |
| `replay.py` | After time repair: `qty_after_transaction`, `stock_value`, SVD, `valuation_rate`, Bin |
| `repair.py` / `api.py` | Dry-run and EXACT apply with optimistic lock |
| `dag.py` | Topological offsets for prevention / proven chains |
| `simulation.py` | Quantity / valuation-impact preview |
| `integrity.py` | SE↔SLE, GL D=C, Bin vs last SLE |

Desk page: **Historical Repair** (`historical-repair`).

Audit: `Production Posting Order Repair Log` + child `Production Posting Order Repair Entry`.

No global Stock Entry timestamp monkey-patch.

## Prevention

On Stock Entry `before_validate` / `before_submit`, for proven Job Card / Work Order / against-Stock-Entry dependents:

```
dependent posting_datetime > prerequisite posting_datetime
minimum_seconds = 1
```

Proven economic dependency gets explicit chronology even if opening stock would cover a same-second inversion. Unrelated documents at T+1 are not moved; the dependent uses the next free second. Midnight date rollover is refused (`MANUAL_REVIEW_MIDNIGHT`).

Row locks are limited to the prerequisite Stock Entry, Job Card, and Work Order.

## Historical Repair

Group by **item + warehouse + canonical batch (SABB-aware) + posting date + posting time**.

True opening quantity is the sum of prior `actual_qty` for that identity (date filters do not clip opening).

Simulate current ERPNext order. Then:

| Result | Action |
|--------|--------|
| `min_running ≥ 0` | **NO_REPAIR_NEEDED** — do nothing |
| A voucher-level reorder keeps `running_qty ≥ 0` and final qty unchanged | **REPAIRABLE_SECONDS** — minimum seconds only |
| Even inward-first stays negative | **REAL_STOCK_SHORTAGE** — do not change time |
| Proven dependency reversed | **DEPENDENCY_CONFLICT** |
| Moving a parent voucher breaks another item/batch | **CROSS_ITEM_CONFLICT** |
| Offset would change posting date | **MIDNIGHT_REVIEW** |

Auto-apply is **EXACT only**. LIKELY is preview/manual. AMBIGUOUS fails closed.

Minimize, in order: documents whose time changes, total seconds shifted, maximum seconds shifted, original relative order. Do not force +1 second when the current sequence is already safe.

## Dry Run

Historical Repair Desk: Scan → Dry Run → Repair Selected.

Preview shows item, warehouse, batch, posting date, opening qty, row-by-row current vs proposed time and running qty, **Minimum Seconds Required** (`Repair unnecessary` / `No change` / `+N second(s)`), valuation/GL impact, and confidence.

Default apply API is dry-run. Optimistic locking aborts if the document changed since preview.

## Replay

After a timestamp change, rebuild only the affected item/warehouse SLE sequence:

- `qty_after_transaction`
- `stock_value`
- `stock_value_difference`
- `valuation_rate`
- Bin `actual_qty` / `stock_value` / `valuation_rate`

If that window already contains 5.2.0 valuation poison, apply stops (`VALUATION_POISON_DEPENDENCY`). Do not mix posting-order repair with historical valuation repair.

Quantity-only repairs leave healthy GL alone and verify D = C. RIV/GL rebuild runs only when valuation actually changes. No new Round Off or Stock Adjustment from ordering alone.

## Performance

Full-history dry-run on development.localhost (2026-09-13):

| | |
|--|--|
| SLE scanned | 94,647 |
| Same-time groups | 662 |
| Elapsed | 3.39 s |
| Groups/sec | ~195 |
| SLE/sec | ~27,930 |
| Production estimate (same size) | ~5–7 s at 1.5×–2× |

## Known limitations

- Auto-repair is EXACT only
- Same-voucher multi-row movements (e.g. Repack) cannot be reordered by parent posting time
- Multi-batch SABB is not mixed into another batch group
- Apply aborts on 5.2.0 valuation-poisoned warehouse chains
- Offset search is combinatorial up to 3 vouchers; larger groups use a greedy schedule
- Midnight `23:59:59 + 1s` does not silently change posting date
- Real insufficient stock is never hidden by timestamp changes
- No bulk production historical repair is claimed; this dump had **zero remaining operator EXACT negatives**

## Explicit non-goals

- No change to 5.2.0 Manufacture valuation policy, component scrap costing, FG residual, IRR sign rules, RIV guards, Transfer/MTfM policy, or GL ±1 handling
- No global shift of Stock Entry times
- No pairing of unrelated documents

## Migration notes

1. `bench --site <site> migrate` (run twice)
2. New DocTypes: Production Posting Order Repair Log / Repair Entry (`seconds_shifted` on the child)
3. New Desk page: Historical Repair
4. `bench build --app erpnext_extensions`
5. No data rewrite on migrate. Historical timestamps change only when an administrator runs Repair Selected after Dry Run.

## Regression coverage

| Area | Tests |
|------|--------|
| Ordering tie-break, DAG, prevention helper | `test_stock_posting_order` |
| Optimizer +1 / +2 / no-repair / shortage / batch / SABB / cross-item / midnight | `test_stock_posting_order` |
| Inverted MTfM→Manufacture repair, value+Bin replay, shortage refuse | `test_stock_posting_order_integration` |
| Historical Repair Desk | Playwright `historical-repair-posting-order.spec.ts` |
| 5.2.0 valuation / scrap / rounding / transfer / RIV | existing 5.2.0 modules (unchanged contract) |
| 25333-class scrap vs FG | `test_riv_valuation_integrity`, `test_scrap_absorbed_costing` |
| Local gate | `gate_release_383_local.run_gate(full_stress=1)` |

## Live-safe verification (development.localhost)

| Item | Result |
|------|--------|
| Root cause | `ORDER BY posting_datetime, creation` — not insertion order |
| Operator 18:01 example | `18000007` / WIP / SABB batch `5673-18000007-J100260042` at `2026-06-21 18:01:20`: opening 1602.27, min 1601.63 → **Repair unnecessary** (LIKELY, not auto-repaired) |
| Operator EXACT negatives remaining | **0** |
| Fixture EXACT +1 chains | leftover inverted test items only; not used as production proof |
| Gate `full_stress=1` | PASS |
| 5.2.0 unit/RIV/scrap/transfer | PASS |

## Deployment steps

1. Install/update `erpnext_extensions` 5.2.1 on a clone of production (do not push/tag from this workspace).
2. `bench build --app erpnext_extensions`
3. `bench --site <site> migrate` twice
4. Run unit + integration + Playwright + `run_gate(full_stress=1)`
5. Production **dry-run only** first (Historical Repair → Dry Run). Review EXACT rows.
6. If any EXACT repairable operator chain remains: backup database, repair one chain, verify SLE/Bin/GL, then continue.

## Rollback

- **Prevention:** disable by not deploying the Stock Entry hooks / app version; already-submitted documents keep their posting times.
- **Historical repair:** restore the pre-repair database backup. Do not reverse timestamps by hand without a SLE replay.
- App rollback: check out previous tag/commit (`5.2.0`) and migrate/build as usual.

## Version

- `erpnext_extensions.__version__` = `5.2.1`

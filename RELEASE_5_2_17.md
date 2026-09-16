# Historical Repair v5.2.17 — Warehouse Engine

## Why Warehouse Engine was required
Six Posting Order cases were blocked as `WAREHOUSE_ESCALATION_REQUIRED` because ERPNext SLE identity is **Item + Warehouse**: reordering a batch-local inversion rewrites other-batch `qty_after` / `stock_value` / `valuation_rate` via Moving Average. Batch-only repair cannot preserve warehouse valuation.

## Architecture
Package: `historical_stock/warehouse_engine/`

| Module | Role |
|--------|------|
| `scope_analyzer.py` | Smallest safe scope ladder |
| `dependency.py` | MA / batch / WO / cross-WH graph |
| `simulator.py` | Read-only `replay_series` MA sim |
| `validator.py` | Ten safety gates → planner status |
| `planner.py` | `READY_WAREHOUSE_REPLAY` pipeline |
| `replay.py` | Controlled apply (timestamps + identity write + selective GL) |

Reuses existing `replay_series` / posting-order primitives. Does **not** invent a new valuation algorithm. Does **not** use Bin as truth. RIV is never invoked.

## Scope escalation model
```
LOCAL_VOUCHER → BATCH_SCOPED → WORK_ORDER_SCOPED
  → IDENTITY_SCOPED → WAREHOUSE_VALUATION_SCOPED → UNSAFE_GLOBAL_DEPENDENCY
```
Never jump to warehouse unless simulation proves other-batch MA rewrite.

## Safety rules (READY only if all pass)
1. Final quantity unchanged  
2. No unexplained negative qty  
3. No negative incoming rate  
4. No exploded valuation  
5. MA deterministic / idempotent  
6. Bin rebuildable from last SLE  
7. GL impact known (selective)  
8. No unrelated item changed  
9. Cross-warehouse documented (transfers value-neutral at apply)  
10. Re-sim identical  

Otherwise: `WAREHOUSE_REAL_SHORTAGE` / `WAREHOUSE_POISONED_OPENING` / `WAREHOUSE_AMBIGUOUS` / `UNSAFE_GLOBAL_DEPENDENCY`.

## Controlled proofs (development.localhost)
1. **Proof 1 SUCCESS** — item `30300020`, 1 other-batch MA dependency → Posting Order −1, I4 −1, Bin/GL stable.  
2. **Remaining 5 escalations** — correctly classified `WAREHOUSE_REAL_SHORTAGE` (`proposed_min` stays largely negative). Not forced.

## Supported / unsupported
**Supported:** READY_WAREHOUSE_REPLAY posting-order inversions with proven MA rewrite window.  
**Unsupported / honest MANUAL:** REAL_STOCK_SHORTAGE, I4 QTY_CHAIN_BREAK, I4 POISONED_OPENING, ambiguous Zero Rate, Wrong Rate / RIV / GL Phase 2.

## Tests
- `test_warehouse_engine_v5217` — scope mapping, READY gates, shortage refusal, MA edges  
- Prior Phase 1 campaign / I4 / KPI tests remain green  

## Production guidance
- Dev-only proofs so far (`LIMITED` live proof count = 1 mathematically READY case on this DB).  
- Never bulk remaining REAL_STOCK_SHORTAGE.  
- No push/tag/merge from this workstream without explicit approval.

## Rollback
- Per-root savepoints; failed apply rolls back that root only.  
- Restore from `20260915_133438-database.sql.gz` for full session reset.

## Verdict
**PHASE 1 LIMITED — WAREHOUSE ENGINE PROVEN, MANUAL CASES REMAIN**

# Historical Repair v5.2.15

## Scope
- Environment: `development.localhost` only
- Database: `20260915_133438-database.sql.gz`
- No push / tag / merge / production

## Delivered
1. **Master Repair Plan** rebuilt per class (counts, READY/WAITING/MANUAL/AMBIGUOUS, PZ, depths, cross deps, estimates, risk, priority, graph)
2. **Campaign framework**: SAFE_GROUP / SAFE_SEQUENTIAL_GROUP / UNSAFE_GROUP + savepoint grouped repair
3. **Campaign 1 Zero Rate**: independent clusters → smallest SAFE_GROUP (9 roots) → dry run → apply → residual verify → Validate Dashboard **PASS** → `PRODUCTION_PROVEN_SMALL_CLUSTER`
4. **Idempotency fix**: `classify_zero_row` returns `RATE_REBUILD_COMPLETE` when basic_rate already non-zero
5. Scaffolds: Wrong Rate / Failed RIV / GL classification campaigns + Warehouse Dependency Engine
6. UI: Campaign Wizard + Cluster Explorer; Master Plan table view
7. Unit tests for cluster taxonomy

## Campaign 1 result
| Metric | Before | After | Δ |
|--------|--------|-------|---|
| Zero Rate | 614 | 605 | **-9** |
| Validate Dashboard | — | all_pass | PASS |

- Repairs: 9/9 verified cleared
- Failures: 0
- Warnings: Wrong Incoming/Outgoing rose slightly after replay; Patient Zero +1 newly exposed

## Explicitly NOT done
- Global repair campaign
- Bulk Zero Rate
- Wrong Rate apply
- Failed RIV retry campaign
- GL rebuild campaign
- Warehouse-wide replay

## Next
1. Next Zero Rate SAFE_GROUP (~10–20) with same discipline
2. Wrong Rate: own Patient Zero graph → small SAFE_GROUP only
3. Failed RIV SAFE_TO_RETRY only (currently 0 on this DB)
4. GL after SLE healthy
5. Warehouse engine before any warehouse-wide scope

# Historical Repair v5.2.16 — Phase 1 stabilization

## Scope
- Environment: `development.localhost` only
- Database: `20260915_133438-database.sql.gz`
- No push / tag / merge / production
- Phase 2 (Wrong Rate / RIV / GL / Warehouse) **not started**

## Objective
Finish and stabilize Phase 1 core classes (I4, Zero Rate, Posting Order) with SAFE_GROUP discipline — correctness over volume.

## Engine improvements
1. **I4 Patient Zero** — readiness now uses earliest remaining `qty_after≈0 / stock_value≠0` leftover, not generic poison (fixed WAITING stuck on already-cleared non-I4 PZs).
2. **Zero Rate clustering** — shared-PZ unsafe groups promote the PZ root into SAFE_GROUP; sequential fallback when packing empty.
3. **Campaign runbook** — `v5216_phase1_runbook.py` with KPI gates between Zero Rate rounds, savepoint-per-root, checkpoint every 10.
4. **Posting Order taxonomy** — TRUE_REPAIRABLE / WAITING_DEPENDENCY / WAREHOUSE_ESCALATION / REAL_STOCK_SHORTAGE / NO_REPAIR_PATH / MANUAL / AMBIGUOUS.

## Campaign results (session)

| KPI | Before | After | Δ |
|-----|-------:|------:|--:|
| I4 Leftover | 112 | 89 | **-23** |
| Zero Rate | 605 | 575 | **-30** |
| Posting Order | 520 | 520 | 0 |
| Patient Zero | 159 | 138 | **-21** |
| Integrity Score | 32 | 33 | +1 |
| Broken Bin / GL / Failed RIV | — | stable | 0 |
| Validate Dashboard | — | **PASS 24/24** | |

- I4: READY queue driven to empty (then re-promotions); remaining **MANUAL 24 + WAITING 66** (all WAITING depend on MANUAL PZs).
- Zero Rate: EXACT READY exhausted (0); SAFE_GROUP/PZ-promotion campaigns verified ~30 roots.
- Posting Order: **TRUE_REPAIRABLE = 0**. Dominant: REAL_STOCK_SHORTAGE 289; ELIGIBLE rows are WAREHOUSE_ESCALATION (Phase 2).

## Remaining (not auto-repairable yet)
- **I4 MANUAL**: mostly OUTBOUND leftovers where honest replay cannot recreate stored `qty_after=0` (quantity chain break) — correctly blocked.
- **Zero Rate**: MANUAL / WAITING / AMBIGUOUS — need better rate sources / dependency conversion (engine evolution, not bulk).
- **Posting Order**: no seconds-EXACT READY left; warehouse MA escalation forbidden until Warehouse Dependency Engine.

## Tests
- `test_i4_earliest_pz_v5216` — PASS
- `test_campaign_framework_v5215` (incl. shared-PZ promote) — PASS

## Phase 1 verdict
**NOT complete.** Auto-repairable queues for I4 READY and Zero Rate EXACT are empty; counters are not ≈0. Do **not** begin Phase 2.

## Next (still Phase 1)
1. I4: investigate MANUAL outbound qty-chain breaks — only add repair if mathematically proven (not force).
2. Zero Rate: convert WAITING/AMBIGUOUS with new dependency classes via planner→sim→tests→resume.
3. Posting Order: keep TRUE_REPAIRABLE at 0; document shortage/midnight as MANUAL.

Artifacts: `.local-backups/restore_20260915_133438/campaigns_v5216/PHASE1_REPORT_v5216.json`

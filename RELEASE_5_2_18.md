# RELEASE 5.2.18 — Historical Repair framework (publish candidate)

**Version:** `5.2.18`  
**Branch:** `repair`  
**Environment validated:** `development.localhost`  
**Scope:** Historical Stock / Historical Repair engines, planner, campaigns, assisted recovery, warehouse engine, posting-order unlocks, selective GL / Failed RIV classification, dashboard consistency.

This release consolidates Phase 1–2 Historical Repair work plus Warehouse Engine, Assisted Recovery, multi-move Posting Order, and SVD residue repair into one reviewable package.

Do **not** claim full automatic recovery of every dashboard anomaly.

---

## Purpose

Provide a **permission-gated, savepoint-aware, identity-scoped** Historical Repair framework that can:

1. Scan and classify stock/valuation/GL defects  
2. Plan dependency-ordered repairs (Patient Zero first)  
3. Dry-run / impact without writes  
4. Apply only READY / proven-safe classes  
5. Replay selectively, rebuild Bin/SABB as needed, and selectively repair GL  
6. Leave AMBIGUOUS / MANUAL / UNKNOWN / real shortage to operators  

---

## Architecture (single coherent flow)

```
Scan → Classify → Patient Zero / Dependency → Planner
  → Dry Run → Impact → Repair → Replay → SABB/SBE → Bin
  → Selective GL → Failed RIV classification → Integrity → Audit
```

Shared primitives (prefer these; do not fork):

| Concern | Module |
|---------|--------|
| Scan / dashboard | `historical_stock/scan.py` |
| Planner statuses | `historical_stock/planner.py` |
| Master plan | `historical_stock/master_plan.py` |
| Moving Average / reconstruct | `historical_stock/reconstruct.py`, `replay.py` |
| Posting Order | `stock_posting_order/` (+ `multi_move.py`) |
| Warehouse Engine | `historical_stock/warehouse_engine/` |
| Wrong Rate | `historical_stock/wrong_rate.py` + `wrong_rate_engine/` |
| Zero Rate | `historical_stock/zero_rate.py` |
| I4 | `historical_stock/i4_repair.py` |
| Selective GL | `historical_stock/gl_integrity.py` + `gl_campaign.py` |
| Failed RIV | `historical_stock/failed_riv.py` |
| Assisted Recovery | `historical_stock/assisted_recovery/` |
| Company / artifacts | `historical_stock/util.resolve_company`, `dump_artifact` |

**Company resolution:** never hard-codes a tenant. Explicit company → user default → Global Defaults → throw.  
**Artifact dumps:** only when `HISTORICAL_REPAIR_ARTIFACT_DIR` is set (no app-tree / `.local-backups` writes by default).

Operator research scripts under `historical_stock/_validation/` may still hard-code a lab company; they are **not** product APIs.

---

## Supported / proven repair classes

| Class | Maturity | Notes |
|-------|----------|-------|
| I4 leftover | **PRODUCTION_PROVEN** (at scale on lab) | Gated READY_I4; identity replay |
| Zero Rate EXACT READY | **PROVEN** (queues may be exhausted) | Version/evidence based |
| Wrong Rate (EXACT / SAFE_GROUP) | **PRODUCTION_PROVEN_SMALL_CLUSTER** | Re-assert after identity replay; SVD residue path for matched-rate leftovers |
| Selective GL (G1 / healthy map) | **LIMITED_PROVEN** | No empty-map false success; Material Transfer net-zero maps not auto |
| Posting Order (CROSS_TIME / PRE / multi-move) | **LIMITED_PROVEN → expanding** | External PRE anchors; joint multi-item; multi-move after inbound |
| Warehouse Engine (SAFE_GROUP / global shared voucher) | **LIMITED_PROVEN** | Expand only when simulation proves joint clear |
| Assisted Recovery (≥95% unique evidence) | **LIMITED_PROVEN** | AUTO only for unique; ASSISTED for operator confirm |
| Failed RIV | **Classified** | `SAFE_TO_RETRY` may be zero; UNKNOWN never blindly retried |

---

## Limited-proven / operator classes

- Wrong Rate AMBIGUOUS / MANUAL / WAITING_PATIENT_ZERO  
- Failed RIV `NEGATIVE_STOCK`, `VALUATION_INTEGRITY`, `UNKNOWN`  
- GL G2_MISSING with empty expected map  
- Posting Order `REAL_STOCK_SHORTAGE`, multi-warehouse shortages  
- Assisted `OPERATOR` / `NO_EVIDENCE` / true shortage  

---

## Assisted recovery

- Evidence package + weighted confidence + ambiguity resolver  
- Unique external inbound promotion when mathematically unique  
- Master Assisted Plan ranks by unlock fan-out  
- AUTO never applies AMBIGUOUS/MANUAL  

---

## Warehouse Engine

- Multi-pair dependency graph → simulator → validator → campaign apply with savepoints  
- Global shared-voucher solver when one voucher blocks many identities  
- No warehouse-wide replay without proof  

---

## Posting Order

- Optimizer statuses: CROSS_TIME, MULTI_MOVE_REPAIRABLE, MIDNIGHT_REVIEW, REAL_STOCK_SHORTAGE, LATER_INBOUND_UNRELATED, NO_REPAIR_NEEDED  
- KPI counts optimizer status only (not planner `NO_REPAIR_PATH` noise)  
- Timestamp moves must preserve final qty/valuation integrity under simulation  

---

## Zero Rate / Wrong Rate / I4

- Reconstruction never uses live Bin as truth  
- Disagree / multi-source → AMBIGUOUS  
- Wrong Rate apply: write SLE rates → identity replay → re-assert → selective GL → residual verify  
- SVD residue: selective correction when rate matches but SVD does not  

---

## Selective GL / Failed RIV

- No global GL rebuild; no global RIV retry  
- Failed RIV UNKNOWN is never blindly retried  
- Upstream WAITING_* must clear before SAFE_TO_RETRY  

---

## Permission model

| Level | Roles | Capabilities |
|-------|-------|----------------|
| Read | Stock / Manufacturing / Accounts Manager, System Manager, Administrator | Scan, Dry Run, Impact, Dashboard, Plans, Export |
| Write | System Manager, Administrator | Repair, replay, selective GL, READY apply |
| Experimental | Administrator (+ Advanced Mode) | Resume/cancel/rollback/benchmark/developer tools |

Page roles: Stock Manager, Manufacturing Manager, Accounts Manager, System Manager, Administrator.  
API methods call `require_read` / `require_repair` / `require_admin` — no guest bypass.

---

## Safety controls (invariants)

1. Scan never writes  
2. Dry Run never writes  
3. Impact never writes  
4. AMBIGUOUS never auto-writes  
5. MANUAL never auto-writes  
6. No global RIV  
7. No global GL rebuild  
8. No blind global Bin rebuild  
9. No live Bin as reconstruction truth  
10. No negative-stock enabling  
11. No silent “success” with zero writes  
12. Complete only if defect gone after rescan  
13. Savepoint / rollback on apply paths  
14. Idempotent second execution  
15. Warehouse expand only when simulation requires  
16. Selective repost identity/voucher scoped  
17. Failed RIV UNKNOWN never blindly retried  
18. Posting-order timestamp changes preserve final qty/valuation under simulation  

---

## Known limitations

- Not every dashboard KPI is auto-repairable  
- Large residual Wrong Rate / Failed RIV / shortage populations remain MANUAL or shortage  
- Campaign `_validation` runners are lab-oriented; use site company explicitly  
- Playwright / full stress may be environment-dependent  
- Warehouse “WAREHOUSE_WIDE” wizard topic remains scaffold (use Warehouse Engine APIs)  

---

## Migration

- Navigation patches only: `ensure_historical_repair_navigation` (+ v2/v3 wrappers)  
- Idempotent metadata (page roles, Stock workspace link, sidebars)  
- **No** stock/SLE/GL mutation during migrate  
- **No** Historical Repair campaign during migrate  

Deploy: `bench --site <site> migrate` (safe to run twice).

---

## Deployment

1. Install / update app to `5.2.18` on a **backup-first** site  
2. `bench migrate`  
3. Confirm page `/app/historical-repair` opens for permitted roles  
4. Read-only: Scan All → Validate Dashboard → Integrity → Master Repair Plan  
5. Apply repairs only under System Manager / Administrator, class-by-class, with backup  

---

## Rollback

1. Restore DB backup taken before migrate/repairs  
2. Revert app checkout to prior tag/commit  
3. `bench migrate` if schema/metadata differed (navigation patches are additive)  

---

## Production operating procedure

1. Backup  
2. Scan All (read-only)  
3. Validate Dashboard  
4. Master Repair Plan — follow dependency order  
5. Dry Run + Impact on READY roots only  
6. Apply one class / SAFE_GROUP at a time  
7. Rescan; do not force UNKNOWN / AMBIGUOUS  
8. Stop when READY queues empty or only operator/shortage remain  

---

## Explicit non-claims

- Does **not** auto-clear all residual KPIs  
- Does **not** enable negative stock  
- Does **not** run global replay / global GL / global RIV  
- Does **not** treat live Bin as valuation truth  

---

## Tests (release matrix)

Unit: historical stock, posting order, warehouse engine, I4, zero/wrong rate, assisted recovery, failed RIV, GL root, multi-move/SVD, resolve_company, dashboard consistency.  

Integration / Playwright / `run_gate(full_stress=0)` — run on target site before publish.

---

## Git

Branch `repair`, version `__version__ = "5.2.18"`.  
`.local-backups/` is gitignored — never commit campaign artifacts.

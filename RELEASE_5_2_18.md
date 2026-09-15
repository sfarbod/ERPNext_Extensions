# RELEASE 5.2.18 — Phase 2 Wrong Rate / Failed RIV / GL Root

**Environment:** development.localhost only  
**Branch:** `repair`  
**Baseline:** v5.2.17 (Warehouse Engine LIMITED_PROVEN; I4 / Zero Rate EXACT READY / Posting Order auto queues exhausted)

## Verdict

**PHASE 2 PARTIAL — SOME CLASSES PROVEN**

| Class | Maturity |
|-------|----------|
| Wrong Rate Engine | **PRODUCTION_PROVEN_SMALL_CLUSTER** (2 distinct proofs + SAFE_GROUP of 8) |
| Failed RIV | **Classified** — `SAFE_TO_RETRY = 0`; upstream WAITING_* / NEGATIVE / VALUATION dominate |
| GL Root | **LIMITED_PROVEN** (2 distinct G1 selective rebuilds; balanced; dims preserved) |
| Warehouse Engine | No redesign; reused for MA escalation only |

Do **not** push / merge / tag / publish / touch production.

## Wrong Rate architecture

- Taxonomy + planner states: `READY_WRONG_RATE`, `WAITING_PATIENT_ZERO`, `WAITING_RATE_DEPENDENCY`, `RATE_*` (no generic BLOCKED for rate rows)
- Reconstruction priority includes `implied_svd`; **disagree → AMBIGUOUS**; **never Bin**
- Controlled apply: write SLE txn rates → identity replay → **re-assert expected rate** → selective GL → residual verify + idempotency
- Campaign ladder: single → second distinct → SAFE_GROUP ≤10 → checkpoint

### Proofs

1. `MAT-STE-2026-03112` / `03119` — EXACT implied_svd outgoing; after_rate == expected; replay OK; GL G0 preserved  
2. `MAT-STE-2026-03120` — second distinct root; idempotent  
3. SAFE_GROUP (8): `03129, 03130, 03132, 03133, 03137, 03139, 03145, 03146` — all cleared  

Promotion: `PRODUCTION_PROVEN_SMALL_CLUSTER` (no bulk beyond this pass)

## Failed RIV

Statuses: `SAFE_TO_RETRY`, `WAITING_RATE/SLE/GL/PATIENT_ZERO/REPLAY`, `NEGATIVE_STOCK`, `RAW_MATERIAL_COST`, `VALUATION_INTEGRITY`, `DEADLOCK`, `TIMEOUT`, `UNKNOWN`, `PERMANENTLY_UNSAFE`

Live sample (limit 500): SAFE=0; WAITING_RATE=15, WAITING_SLE=6, WAITING_PATIENT_ZERO=38; NEGATIVE_STOCK=274; VALUATION_INTEGRITY=35; UNKNOWN=132  

**No force retry.** Upstream Wrong Rate / SLE / Patient Zero must clear first.

## GL Root

- Roles: `GL_ROOT` vs `WAITING_SLE`; G4 not eligible  
- Rebuild via Stock Entry GL map + `make_gl_entries` (refuse empty map / G2-still-missing false success)  
- Proofs: `MAT-STE-2026-36677`, `MAT-STE-2026-36688` (G1→balanced Debit=Credit, cost centers preserved, no unexpected SA/Round Off)  
- Promotion: **LIMITED_PROVEN**

## Dashboard / Root Cause

New clickable KPIs: Wrong Rate READY/WAITING/MANUAL, RIV SAFE/WAITING/UNSAFE, GL READY/WAITING/MANUAL  
Root Cause Explorer: `phase2_repair_plan` narrative (RIV → WAITING_RATE → Wrong Rate PZ → Replay → GL → Retry RIV)  
Validate Dashboard: **24/24 PASS**

## Tests

- `test_wrong_rate_engine_v5218` — 16 OK  
- `test_failed_riv_v5218` — 7 OK  
- `test_gl_root_v5218` — 5 OK  

## KPI snapshot (post Phase 2 Scan All)

- Wrong Rate READY: 97 (capped scan 2000)  
- RIV SAFE: 0  
- GL READY: 63  
- READY_I4 / Zero Rate EXACT READY / Posting READY: 0 (Phase 1 hold)

## Remaining manual / unsafe

- Wrong Rate MANUAL / AMBIGUOUS / WAITING_PATIENT_ZERO (majority of capped scan)  
- Failed RIV NEGATIVE_STOCK / UNKNOWN / VALUATION_INTEGRITY  
- GL G2_MISSING with empty expected GL map (Material Transfer net-zero maps) — not auto-rebuilt  
- G4 poisoned SLE — WAITING_SLE

## Commit history (v5.2.17 → v5.2.18)

See `git log 28f631a..HEAD` on branch `repair`.

Out of scope (left uncommitted): `petty_management.json` timestamp noise.

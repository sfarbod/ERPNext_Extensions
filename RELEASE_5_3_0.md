# Release 5.3.0 — Historical Repair Master Plan V2

Package version: **5.3.0**.

This is a **significant Historical Repair architecture upgrade**, not a
patch on 5.2.x. All approved work in this cut belongs to **5.3.0**
(no intermediate 5.2.29).

Backward-safe with Historical Repair behaviour from **v5.2.28**. Existing
integrity protections are **not** removed or weakened merely to make more
cases repairable.

---

## Summary

| Area | v5.3.0 behaviour |
|------|------------------|
| Master Plan | **V2** — root-cause graph, root-vs-downstream grouping, safety banner |
| RIV | Preflight + dependency-closure + repost impact preview before queue |
| Wrong / Zero Rate | Authoritative-rate validation; refuse false `RATE_REBUILD_COMPLETE` |
| Manufacture | Deterministic contract → **EXACT** when AFTER rates are healthy (even if BEFORE FG was negative) |
| I1 | Dependency-cycle detection; route self-poison to Manufacture EXACT |
| Expected GL | IRR align **before** postability gate (classify ↔ apply consistency) |
| Failed RIV | Current-ledger reconciliation (`HISTORICAL_ONLY` / `SUPERSEDED` / `SAFE_TO_RETRY` / `BLOCKED_*`) |
| Integrity Score | Includes I1 pressure; version tag `5.3.0` |

---

## Historical Repair Master Plan V2

`build_master_repair_plan` now reports:

- `version: "5.3.0"`, `master_plan: "V2"`
- `root_cause_graph` via `root_graph.build_root_cause_graph`
- per-class `sample_rows` for root/downstream grouping
- `safety` flags: no global RIV, riv preflight required, false-complete refused, manufacture EXACT-on-healthy-AFTER, IRR align before expected-GL gate

Recommended repair order is unchanged from v5.2.x (Warehouse → I1 → Posting Order → Zero → Wrong → I4 → GL → SLE_GL_DRIFT → Failed RIV).

---

## RIV preflight & dependency-closure

New module: `historical_stock/riv_preflight.py`

| API | Purpose |
|-----|---------|
| `preview_repost_impact` | Read-only impact preview for Item+Warehouse scope |
| `analyze_dependency_closure` | Co-occurring vouchers/items (dependant-expansion proxy) |
| `riv_preflight_gate` | Hard gate used by controlled `preview_repost_selected` |
| `classify_failed_riv_current_impact` | Failed RIV ↔ current ledger reconciliation |

**Refuses RIV** when the closure contains exploded/negative rates or expected GL is not postable after IRR alignment — the damage class seen when RIV rewrites SLE then fails on ±1 IRR GL.

Whitelisted: `riv_preflight_api`, `preview_repost_impact_api`.

---

## Wrong Rate — authoritative-rate validation

New module: `historical_stock/authoritative_rate.py`

- `is_poison_rate` / `is_authoritative_healthy_rate` / `rate_integrity_reason`
- `refuse_false_rate_rebuild_complete` / `reclassify_false_complete_row`

### Prevention of false `RATE_REBUILD_COMPLETE`

Incident class: SE Detail and SLE **agree** on a poisoned rate (e.g. FG
`-5.8e9`). Prior logic treated matching non-zero rates as
`already_valued` / `RATE_REBUILD_COMPLETE`, which blocked I1 forever.

v5.3.0:

- `zero_rate.classify_zero_row` reclassifies poisoned “already valued”
- `planner._evaluate_rate` refuses `PLAN_RATE_REPAIR_COMPLETE` for poison
- `_rate_patient_cleared` does not clear WAITING on poisoned COMPLETE roots

---

## Manufacture — deterministic replay / confidence

`preview_manufacture_voucher`:

- If Iran manufacture output contract yields **healthy AFTER** rates,
  confidence is **EXACT** and status **RECONSTRUCTABLE** even when BEFORE
  FG was negative (`fg_negative`).
- Source of truth label: `5.3.0_manufacture_output_contract`
- Field `after_rates_healthy` exposed for operators / I1 routing

Does **not** bypass zero-RM dependency gates.

---

## I1 — dependency-cycle detection / resolution

When WAITING because replay is blocked by this voucher’s own
negative/exploded rates, and manufacture preview is EXACT:

- `dependency_cycle: true`
- `required_action`: Repair Manufacture `<voucher>` first
- Explicit message: do not wait on Wrong Rate false COMPLETE

---

## Posting Order / Zero / I4

- Posting Order planner path unchanged; Master Plan V2 groups roots vs downstream
  and exposes **8 campaign phases** (negative-rate → … → residual manual).
- Zero Rate:
  - **Rule 1** — receipt into Scrap/Reject/Waste warehouse with rate≈0 →
    `LEGITIMATE_SCRAP_ZERO_RATE` / `NO_ACTION_REQUIRED` (not corruption).
  - **Rule 6** — actionable zeros carry precise `zero_reason`
    (`TRUE_ZERO_RATE_CORRUPTION`, `MISSING_SOURCE_RATE`, `UPSTREAM_POISONED`, …).
  - Dashboard exposes **Zero Rate** (actionable), **Zero Rate Raw**,
    **Legitimate Scrap Zero Rate**.
- Wrong Rate: `MATCHED_BUT_CORRUPT` when SE Detail rate == SLE rate but both
  disagree with independently reconstructed expected rate.
- I4: unchanged apply guards; benefits from healthier upstream Wrong/Manufacture classification.
- Negative Stock Root Report (`negative_stock_report.py`) for operator investigation.

---

## SLE_GL_DRIFT — classifier / apply consistency

`planner._gl_expected_map_state` now runs
`align_irr_gl_map_to_currency_precision` **before** the postability check
(same path `make_gl_entries` uses).

- Does **not** widen debit/credit allowance
- Still refuses true imbalances after alignment
- Sets `irr_aligned: true` on the map state

This addresses classify=`READY_GL_ONLY` vs apply=`UNBALANCED_EXPECTED_GL`
skew for the known **±1 IRR** arithmetic class without forcing unbalanced GL.

---

## Failed RIV reconciliation

`classify_failed_riv` attaches:

| `riv_reconcile_status` | Meaning |
|------------------------|---------|
| `HISTORICAL_ONLY` | Chain healthy now (often prior 1IRR GL failure) |
| `SUPERSEDED_BY_SUCCESSFUL_REPAIR` | Later Completed RIV exists |
| `SAFE_TO_RETRY` | Preflight passes |
| `BLOCKED_POISON_CLOSURE` / `BLOCKED_UNBALANCED_EXPECTED_GL` / … | Preflight refuses |
| `CURRENT_LEDGER_IMPACT` | Identity still unhealthy |

Never mass-promotes historical Failed rows to SAFE.

---

## Integrity Score

- Penalty now includes **I1** weight (`×1.25`)
- Dashboard field `Integrity Score Version: "5.3.0"`

---

## Safety / blocked-state semantics

| Rule | Behaviour |
|------|-----------|
| No global RIV | Unchanged |
| No weaken I1/I4/poison | Unchanged |
| No force unbalanced GL | Unchanged |
| RIV preflight | New hard refuse on poison closure / unpostable expected GL |
| False COMPLETE | Reclassified; not treated as healthy root |
| Manufacture EXACT | Only when AFTER rates pass authoritative checks |
| Legitimate scrap zero | `NO_ACTION_REQUIRED` — does not inflate actionable Zero KPI |
| MATCHED_BUT_CORRUPT | SE==SLE is not health proof vs native expected |
| RIV preflight on scrap/FG poison | Refuses cascade seen in 13100134 incident class |

**Upgrading to v5.3.0 does not repair any vouchers.** Operators must run
explicit dry-run → controlled apply after backup.

---

## APIs (additive)

- `master_repair_plan_api` → V2 payload
- `riv_preflight_api`
- `preview_repost_impact_api`

Existing scan/repair APIs remain.

---

## Tests

`erpnext_extensions.iran_accounting.tests.test_historical_repair_v530` covers:

- authoritative rate / poison / false COMPLETE / MATCHED_BUT_CORRUPT
- manufacture EXACT on healthy AFTER despite fg_neg BEFORE
- planner refuses poison COMPLETE
- root-vs-downstream + cycle detection
- RIV preflight poison + unbalanced GL blocks
- GL map state calls IRR align
- I1 cycle → Manufacture routing
- Master Plan V2 shape / safety flags / 8 phases
- legitimate Scrap/Reject/Waste zero → NO_ACTION_REQUIRED
- normal incoming zero remains actionable
- input-material vs manufacture scrap valuation roles
- negative-stock chain classification helpers

Also retain v5.2.28 `test_sle_gl_drift_v5228` regression suite.

---

## Business rules (restore review addendum)

Validated against restored pre-deep-pass DB (`20260919_221747`):

1. **Scrap/Reject/Waste zero** → `LEGITIMATE_SCRAP_ZERO_RATE` / `NO_ACTION_REQUIRED`
   (`scrap_warehouse.py`). Six company warehouses discovered by name tokens.
2. **Input-material vs manufacture scrap roles** distinguished (Rule 2).
3. **Manufacture FG** reconstructs via native Iran output contract; negative FG
   + healthy AFTER → EXACT even when another RM row is zero (Rule 3).
4. **MATCHED_BUT_CORRUPT** when SE==SLE but both disagree with expected.
5. **Negative Stock Root Report** for operator investigation.
6. **Failed RIV reconcile** skips expensive preflight when SLE identity is
   already unhealthy (scan performance + HISTORICAL_ONLY KPI semantics).
7. Dashboard KPI: actionable Zero Rate excludes legitimate scrap zeros.

Master Plan V2 exposes **8 campaign phases** derived from dependency order.

### User Action Required / Blockers (campaign addendum)

- DocType **`Historical Repair Blocker`** with lanes:
  - `USER_ACTION_REQUIRED` — genuine business blockers
  - `TOOL_LIMIT` — deterministic gaps in code (never mixed into user blockers)
- Historical Repair page tab **User Action Required** + Recheck / Open List
- APIs: `scan_blockers_api`, `recheck_blocker_api`, `sync_blockers_api`
- Workflow: OPEN → UNDER_REVIEW → USER_FIXED → READY_TO_RECHECK → RESOLVED

### Manufacture SE→SLE sync (campaign discovery)

`write_sle_transaction_rates` alone cannot heal poisoned SLE after SE repair
because it derives rate from existing SLE SVD. v5.3.0 adds
`sync_sle_from_stock_entry_detail` and manufacture repair now:

1. applies Iran manufacture contract to SE (when needed)
2. pushes SE Detail → SLE rates/SVD
3. replays affected FG/scrap identities
4. re-asserts SE→SLE after replay

Also repairs the case where SE is already healthy but SLE still disagrees
(`sle_se_drift`).

### Posting Order — LIKELY→EXACT stamp + dry-run preview

Campaign finding: optimizer may label chronology `LIKELY` while quantity sim
clears (`min_after >= 0`). Planner already promoted these to auto-repairable
via `promoted_likely_sim_cleared`, but `attach_plan` left row `confidence` as
`LIKELY`, so campaigns/blocker sync treated READY rows as TOOL_LIMIT.

v5.3.0 now:

1. stamps effective `confidence=EXACT` (+ `raw_confidence`) on attach_plan
2. returns apply-shaped `applied`/`blocked` from posting-order dry_run
3. skips promoted/READY PO rows from blocker TOOL_LIMIT lane
4. batch apply skips outbounds already timestamp-shifted as collision
   multi-move collateral (`already_moved_as_collateral`) so a single-scan
   SAFE_GROUP run does not false-fail later roots as STALE_PREVIEW

True EXACT+`REAL_STOCK_SHORTAGE` / warehouse-still-negative remain blockers
(USER or further warehouse-campaign TOOL_LIMIT), not silent auto-apply.

### Posting Order — multi-move pre-window injection

Campaign finding (`MAT-STE-2026-36772`): multi-move proposals shift Stock Entries
that currently sit *before* the repair `from_dt` into the window. Plan-time
`_identity_window(from_dt)` never saw those rows, while apply-time assert saw
them after timestamp rewrite and double-counted their qty against
`opening_qty` → false `batch running qty still negative`.

v5.3.0:

1. `_identity_window_for_moves` unions moved vouchers into the sim series
2. reverses injected qty out of `opening_qty` before proposed/assert running
3. apply pre-write gate refuses proposals that still go negative under this sim


| Incident | v5.3.0 fix |
|----------|------------|
| RIV cascade into manufacture then 1IRR GL fail | Preflight + closure poison/GL gate |
| Wrong Rate `RATE_REBUILD_COMPLETE` on matched poison | Authoritative-rate refusal |
| Manufacture MANUAL despite correct preview | EXACT when AFTER healthy |
| I1 WAITING circular on Wrong COMPLETE | Cycle resolution → Manufacture |
| READY_GL_ONLY vs UNBALANCED apply skew | IRR align before expected-GL gate |
| Promoted LIKELY READY mislabeled as TOOL_LIMIT | stamp EXACT confidence on attach_plan |
| Batch STALE_PREVIEW after sibling multi-move | skip collateral-moved outbounds |
| Multi-move apply false-fail (pre-window SE) | inject moves into window + opening adjust |

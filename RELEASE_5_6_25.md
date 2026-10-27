# erpnext_extensions 5.6.25

## Purchase Valuation, Landed Cost / GRNI Integrity, Historical Detection & Reposting

### Summary
Fixes three interlocking purchase-valuation defects on IRR companies:

1. **Purchase Invoice #59476 compatibility wrapper** incorrectly forced Core
   over-billing validation when invoice-based Purchase Receipt valuation was
   enabled, blocking legitimate PI rate differences.
2. **Fractional Landed Cost** integerized valuation rates early, then multiplied
   by quantity, overvaluing inventory and parking residuals in purchase GRNI.
3. **Material Transfer destination** could invent inventory when Core
   `recalculate_amounts_in_stock_entry` rewrote SED as integer `rate × qty`
   after an amount-preserving source leg.

Historical Rebuild Campaign detection is extended (dry-run only) for Categories
A/B/C so affected PRs/LCVs/RIVs can be classified before repair via standard RIV.

**Local merge into `develop` after quality gate. No push / publish / production deploy.**

---

### Business problems corrected
| Problem | Impact |
|---------|--------|
| PI submit failed with Over Billing when PI rate ≫ PR rate under invoice-based valuation | Blocked legitimate landed-cost-from-invoice workflows |
| LCV fractional allocations (e.g. 1,500 IRR on qty 800) overvalued stock | Inventory +100 IRR; purchase GRNI residual |
| Downstream Issue/Transfer retained provisional rates until patched RIV | Consumption / transfer valuation stale after PI/LCV |
| Transfer destination SVD ≠ −source SVD after integer rate rewrite | Phantom inventory on destination warehouse |

### Technical root causes
1. `pr_pi_landed_cost_compat.update_billing_percentage_with_landed_cost` called
   Core `orig(..., adjust_incoming_rate=False)`, taking the over-billing branch
   instead of the #59476 adjust path.
2. Iran `authoritative_acquisition_incoming_rate` / PR VR align rounded IRR rates
   before SLE movement; Core divisional + residual partner then used GRNI.
3. Transfer inward leg used SED after Core rate×qty recalculation instead of the
   outward SLE `stock_value_difference` magnitude.
4. Forced `bench execute` RIV without `apply_monkey_patches()` left sticky
   outgoing rates (site had no RQ workers) — operational note for repairs.

---

### Implementation summary

| Area | Files |
|------|-------|
| PI compat control flow | `integration/pr_pi_landed_cost_compat.py` |
| Amount-preserving PR/LCV VR | `domain/qty_rate_amount.py`, `domain/stock_ledger_deterministic.py` |
| GRNI residual isolation | `domain/irr_residual_classification.py`, `domain/irr_rounding_residual.py` |
| Transfer pair conservation | `domain/stock_entry_sync.py` (`conserve_transfer_inward_from_outward_sle`) |
| HRC detection Categories A/B/C | `historical_rebuild/purchase_valuation_detection.py`, scanner wire-in |
| Tests | `test_pr_pi_compat_control_flow_v5625.py`, `test_pr_lcv_amount_preserving_vr_v5625.py`, `test_transfer_inward_conservation_v5625.py`, `test_purchase_valuation_detection_v5625.py`, updated `test_pr_legacy_uvr_float_v5528.py` |

### Accounting behavior changes
- Acquisition SLE `incoming_rate` may be **fractional** when qty×rate must equal
  the authoritative amount (base + PI adjustment + LCV) at currency precision.
- Representational `valuation_rate` on balances may still IRR-round for display;
  **movement totals** are amount-authoritative.
- Purchase GRNI is **forbidden** as a residual partner for Class A rounding.
- Material Issue may post a small Stock Adjustment (e.g. 150 IRR) when integer
  expense rate×qty differs from amount-preserving SLE SVD — **not** GRNI.
- Material Transfer destination SVD must equal −source SVD (+ additional_cost).

### GRNI reconciliation — guarantees and limitations
**Guarantee (fully invoiced PR + PI + applicable LCV):** purchase GRNI net on the
closed voucher set = 0 after corrected acquisition SLE/GL.

**Limitations:**
- Unbilled / partially billed PRs legitimately hold GRNI — detection excludes them.
- Unrelated open GRNI on other vouchers is out of scope.
- Site-wide Queued RIVs without workers do not clear Category C until drained
  with Iran monkey patches applied.

### Landed Cost precision
Amount-preserving VR: `valuation_rate = (base + PI diff + LCV) / qty` without
premature integerization. Residual skip when qty×VR reconstructs the amount.
Validated LCV E2E: qty 800, LCV 1,500 → SLE 800,001,500; GRNI 0.

### Reposting behavior
- Corrected PR/PI/LCV queue standard Item+Warehouse RIV.
- **Automatic patch initialization (pickle-safe):**
  - `hooks.before_request` / `hooks.before_job` call `integration.bootstrap.apply`.
  - Public + private RIV executors are owned by module-level
    `integration.riv_execute_guard.execute_reposting_entry` (and private wrap),
    which calls `_ensure_iran_runtime_for_riv` → `bootstrap.apply` on every run.
  - Nested locals wraps of `execute_reposting_entry` are **not** used — they
    break RQ pickling and the execute-guard contract (regression caught by
    `test_riv_execute_guard_v5523`).
- Real RQ worker validation (temporary `bench worker` on short/default/long):
  enqueue via `enqueue_reposting_entry` completed without a manual
  `apply_monkey_patches()`; worker probe reported `sle_patched`,
  `riv_is_guard`, `recalculate_wrapper_active`, `not_nested_wrap`.
- Idempotent re-RIV with patches preserves corrected totals.

### Historical campaign functionality
`dry_run_purchase_valuation_scan(company, ...)` and `run_full_scan` (scanner
version 5.6.25) classify:

| Category | Code | Meaning |
|----------|------|---------|
| A | `A_INVOICE_BASED_VALUATION` | Fully/partially billed PR SLE ≠ expected acquisition |
| B | `B_LANDED_COST_ROUNDING` | LCV integer VR inflation / GRNI residual / transfer pair |
| C | `C_REPOSTING_INCONSISTENCY` | Failed / Skipped / Queued RIV |

#### Repair orchestration (final gate)
Module: `historical_rebuild/purchase_valuation_repair.py`

| Question | Result |
|----------|--------|
| Does **RIV alone** fix Category A when VR still provisional? | **No** — `amount_difference` may already be correct while `valuation_rate`/SLE lag |
| Does **RIV alone** fix Category B integer VR inflation? | **No** — document VR must be realigned first |
| Prerequisite | Core `adjust_incoming_rate_for_pr` (recalculates VR from base+PI diff+LCV) |
| If SLE correct but PR GL stale | Core `make_reverse_gl_entries` + `make_gl_entries(from_repost=True)` |
| Then | Scoped Item+Warehouse RIV via `execute_job` (triggers `before_job` bootstrap) |

API: `repair_one(pr, execute=False|True)`, `repair_from_scan(company, ...)`.
Dry-run by default. Explicit `execute=True` required. Idempotent re-run →
`idempotent_skip` when clean.

#### Proven gate evidence
| Scenario | Detected | Diff | Repair | Final SLE | GRNI |
|----------|----------|------|--------|-----------|------|
| A PI 1000×26,900,332 | Category A | −25,900,332,000 | adjust → GL regen → RIV | 26,900,332,000 | 0 |
| B LCV 800×1,500 | Category B + GRNI | +100 SLE / −100 GRNI | adjust → RIV | 800,001,500 | 0 |
| Valid control | none | — | — | — | — |

Note: this environment’s Category B GRNI residual was **100 IRR** (not the
historical 200 IRR sample); both SLE and GRNI cleared after repair.

#### Unsupported / blocked
- Ambiguous unbilled PRs (never auto-flagged as GRNI defects).
- Unrelated site-wide Queued RIVs for the same item (repair scopes to the PR’s
  warehouses / newly spawned RIVs only).
- Cases where `adjust_incoming_rate_for_pr` or GL regenerate throws — marked
  `blocked` with reason; no silent skip.

### Combined E2E evidence (development.localhost)
Documents: `MAT-PRE-2026-02150`, `MAT-STE-2026-42854` (Issue 300),
`MAT-STE-2026-42855` (Transfer 200), `ACC-PINV-2026-02187`, `MAT-LCV-2026-00010`.

| Component | IRR |
|-----------|----:|
| Purchase Invoice | 26,900,332,000 |
| PI price difference | 25,900,332,000 |
| Landed cost | 1,500 |
| PR SLE acquisition | 26,900,333,500 |
| Issue SLE | −8,070,100,050 |
| Transfer src / dst | −5,380,066,700 / +5,380,066,700 |
| Remaining inventory (src+dst bins) | 18,830,233,450 |
| Purchase GRNI residual | **0** |

Invariant check: issue + remaining = acquisition ✓

### Regression / test results (this package)
Focused critical Iran Accounting suites (purchase / LCV / inventory / RIV /
manufacture / HRC): **572 passed**, **3 failed**, **1 error** across 576 tests.

| Area | Result |
|------|--------|
| Purchase / #59476 / LCV amount-preserving / residual / detection / repair | PASS |
| Inventory transfer conservation / SLE-GL / posting order / I4 lifecycle | PASS |
| RIV integrity / propagate / guard / scope / rate-first / campaign / sequential | PASS |
| Manufacture residual / output / scrap / fractional / reject | PASS |
| Historical rebuild campaign / HRC progress | PASS |
| Real RQ worker RIV enqueue + idempotency | **PASS** |
| Historical gate E2E (bootstrap / control / A / B) | **PASS** |
| Playwright desk smoke (PR/PI/LCV/SLE/GL/HRC list) | **7/7 PASS** |

Pre-existing baseline failures (unchanged on this branch; not 5.6.25 regressions):
- `test_transfer_convergence_v530` — `NO_ACTION` vs `WAITING_UPSTREAM` (0 file diff vs develop)
- `test_manufacture_rounding` — mock `_SteDoc` missing `docstatus`
- `test_manufacture_valuation_v533_integrity` — site docs already carry contract version fields

### Known limitations
- Keep at least one RQ worker running in environments that enqueue RIV; without
  workers, jobs remain Queued until drained.
- Issue GL may show Stock Adjustment vs expense for integer rate×qty vs SLE
  amount (explained residual, not GRNI).
- Broad historical repair of production-like data is **detection-ready**; mass
  apply remains an operator-controlled HRC sequential session.

### Deployment considerations
1. Deploy `erpnext_extensions` 5.6.25 only (no Frappe/ERPNext core edits).
2. Keep RQ workers running so `before_job` bootstrap applies on standard RIV jobs.
3. Dry-run `dry_run_purchase_valuation_scan` / `repair_one(..., execute=False)` first.
4. Execute allowlisted `repair_one(..., execute=True)`; verify SLE/GL/GRNI; re-scan.

### Historical repair instructions
1. Dry-run: `dry_run_purchase_valuation_scan(company)` or `repair_one(pr, execute=False)`.
2. Review eligibility / `riv_alone_sufficient` / `recommended_action`.
3. Execute: `repair_one(pr, execute=True)` (or allowlisted `repair_from_scan`).
4. Verify: SLE = base+PI diff+LCV; active PR inventory GL = SLE; GRNI=0 if fully
   billed; transfer src+dst = 0 when applicable.
5. Re-run dry-run / `repair_one` — expect `idempotent_skip`.
6. Category C: drain Failed/Queued RIVs via job path; never mark success without
   SLE/GL checks.

### Rollback
Revert to 5.6.21 app revision. Accounting already posted under 5.6.25 remains;
re-RIV may be needed if rolling back mid-repair. Do not reverse via balancing JE.

### Final quality gate (merge readiness)
| Gate | Result |
|------|--------|
| Critical regression (purchase/LCV/inventory/RIV/HRC) | **PASS** (baseline-only failures noted above) |
| Real RQ worker RIV + patch bootstrap | **PASS** |
| Campaign detection A/B + repair + idempotency | **PASS** |
| Financial invariants (PI 26.9B / LCV 800,001,500 / GRNI 0) | **PASS** |
| Playwright desk smoke | **PASS** (7/7) |
| Version remains 5.6.25 | **PASS** |

### Version
`erpnext_extensions.__version__` → **5.6.25** (unchanged)

### Merge note
Feature branch merged into local `develop` after the gates above. **Do not push
or publish until manually approved.**

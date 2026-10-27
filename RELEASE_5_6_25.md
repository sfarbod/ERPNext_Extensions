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

**No production deploy, merge, or push in this release package.**

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
- Repairs and verification **must** call `apply_monkey_patches()` (or run under
  request/worker bootstrap). Bare `execute_reposting_entry` without patches
  leaves sticky Material Issue/Transfer outgoing rates.
- Idempotent re-RIV with patches preserves corrected totals.

### Historical campaign functionality
`dry_run_purchase_valuation_scan(company, ...)` and `run_full_scan` (scanner
version 5.6.25) classify:

| Category | Code | Meaning |
|----------|------|---------|
| A | `A_INVOICE_BASED_VALUATION` | Fully/partially billed PR SLE ≠ expected acquisition |
| B | `B_LANDED_COST_ROUNDING` | LCV integer VR inflation / GRNI residual / transfer pair |
| C | `C_REPOSTING_INCONSISTENCY` | Failed / Skipped / Queued RIV |

Repair remains standard RIV + verification — no manual JE balancing.

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
| Suite | Result |
|-------|--------|
| `test_pr_pi_compat_control_flow_v5625` (8) | PASS |
| `test_pr_lcv_amount_preserving_vr_v5625` (7) | PASS |
| `test_pr_legacy_uvr_float_v5528` (11) | PASS |
| `test_transfer_inward_conservation_v5625` (3) | PASS |
| `test_purchase_valuation_detection_v5625` (5) | PASS |
| LCV E2E FIX4 (800 / 1,500) | PASS |
| Combined PR→Issue→Transfer→PI→LCV | PASS (after patched RIV) |
| Playwright UI | **NOT EXECUTED** — Python `playwright` package unavailable in this environment (`npx playwright` CLI present only) |
| Full Iran regression suite | Not fully re-run in this session; focused purchase/LCV/transfer suites green |

### Known limitations
- Development site often has **no RQ workers**; RIV stays Queued until executed
  synchronously with patches.
- Issue GL may show Stock Adjustment vs expense for integer rate×qty vs SLE
  amount (explained residual, not GRNI).
- Playwright UI workflows not validated in this environment.
- Broad historical repair of production-like data is **detection-ready**; mass
  apply remains an operator-controlled HRC sequential session.

### Deployment considerations
1. Deploy `erpnext_extensions` 5.6.25 only (no Frappe/ERPNext core edits).
2. Ensure worker processes load Iran monkey patches (existing hooks).
3. Dry-run `dry_run_purchase_valuation_scan` before any campaign apply.
4. For each repair: RIV from PR posting time → verify SLE/GL/GRNI → re-scan.

### Historical repair instructions
1. Dry-run detection; export findings (root cause, related PI/LCV, eligibility).
2. For Category A/B: `apply_monkey_patches()` then RIV Item+Warehouse from PR.
3. Verify: acquisition SLE = base+PI diff+LCV; GRNI=0 if fully billed; transfer
   src+dst = 0; no new GRNI residual.
4. Category C: execute or clear Failed/Queued RIVs; never mark success without
   SLE/GL checks.
5. Re-run dry-run — Category A/B for repaired vouchers should clear.

### Rollback
Revert to 5.6.21 app revision. Accounting already posted under 5.6.25 remains;
re-RIV may be needed if rolling back mid-repair. Do not reverse via balancing JE.

### Version
`erpnext_extensions.__version__` → **5.6.25**

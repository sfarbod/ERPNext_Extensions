# erpnext_extensions 5.5.30

## Dynamic Manufacture valuation (Job Card repair + native RIV)

5.5.29 remains the prior local release candidate. This release finalizes the
experimental Job Card Manufacture repair path for **dynamic** consumption and
finished-good valuation under Iran Accounting, with fail-closed native RIV.

It does **not** automatically unlock every historical Manufacture document.
FG unlock applies when Job Card repair plans set `unlock_main_fg` (value-changing
historical consumption correction). Documents that retain
`set_basic_rate_manually=1` on MAIN_FG stay frozen until unlocked by that path
(or an equivalent explicit unlock).

### FEATURES

- **Dynamic Manufacture valuation during RIV** — When the Iran recalculate
  wrapper is active, native RIV recalculates the corrected Manufacture itself
  (`MANUFACTURE_PLUS_DOWNSTREAM`), not only downstream vouchers.
- **Automatic FG costing recalculation** — Unlocked MAIN_FG rates follow material
  consumption valuation changes during Iran-wrapped RIV recalculate.
- **Multilevel Manufacture valuation propagation** — Upstream item/warehouse rate
  changes can propagate A → transfer → B when FG is unlocked (verified
  PO-JOB08136 → PO-JOB08137 after a controlled LCV shock).
- **Legitimate zero consumption rates** — Temporary or authoritative zero SLE /
  CONSUME rates are allowed during upstream historical reposting; they are not
  treated as automatic corruption.

### FIXES

- **Iran Accounting wrapper enforcement during native RIV** — Bootstrap +
  `ensure_iran_riv_recalculate_wrapper_active` on enqueue/execute; Core-only
  Manufacture RIV is refused (`BLOCKED_WRAPPER_INACTIVE` /
  `IRAN_RIV_WRAPPER_INACTIVE`).
- **Multi-FG double-pool prevention** — Iran SAME_ITEM_MULTI_FG closes the
  material pool once; verification rejects FG Σ ≈ 2× material pool.
- **Removal of unintended FG valuation locks (repair path)** — Value-changing
  repairs clear MAIN_FG `set_basic_rate_manually` via `unlock_main_fg` so RIV can
  reprice FG. Scrap Manual rows are left alone.
- **Repair valuation status mapping** — Queued Apply maps
  `APPLY_PASS_VALUATION_PENDING` → `COMMITTED_VALUATION_PENDING` and
  `APPLY_PASS_VALUATION_FAILED` → `COMMITTED_VALUATION_FAILED` instead of
  mis-reporting `FAILED` / full `COMMITTED` while RIV is outstanding.

### IMPROVEMENTS

- Manufacture + logistics vouchers in post-Commit native RIV scope.
- Whole-IRR FG rate checks when rates are nonzero.
- RIV idempotency / convergence (repeated RIV produces no additional drift on
  verified chains).
- Independent post-RIV verification (`VALUATION_PENDING` /
  `VALUATION_VERIFIED` / `VALUATION_FAILED`) with Golden Rule scan.
- Fail-closed behavior when the Iran wrapper is unavailable.

### TESTING

Verified on disposable clone `jc-riv-accept.localhost` (and prior dyn clone):

| Scenario | Result |
|----------|--------|
| PO-JOB08604 dynamic Manufacture RIV | PASS — consume/FG updated; zero-rate row allowed; Multi-FG closed; no artificial Additional Cost / Stock Adjustment |
| PO-JOB08136 → PO-JOB08137 multilevel propagation | PASS — +5% upstream LCV on producer of consume item; A and B cons/FG moved; SLE/GL balanced; repeated RIV converge |
| Zero-rate scenarios | PASS (policy) — legitimate zeros retained |
| Multi-FG allocation | PASS under Iran wrapper; fail-closed without wrapper |
| Worker-based RIV | PASS — `enqueue_reposting_entry` → `riv_execute_guard` → Completed, zero drift |
| Repeated RIV convergence | PASS on multilevel chain |
| Repair status transitions | PASS — pending while upstream/later RIV Queued; map statuses unit-tested |

Unit / module tests added or retained:

- `test_dynamic_manufacture_riv_v5530` — status map, zero-rate verify allowance, double-pool signal
- `test_multi_fg_riv_wrapper_v5529` — wrapper active / fail-closed / Multi-FG pool

### Known limitations / blocked regressions

- **Preferred chain PO-JOB08469 → PO-JOB08608** is not SLE-linked (different FG
  batches); multilevel proof used PO-JOB08136 → PO-JOB08137 instead.
- **Scrap Manual residuals** — Unlocked MAIN_FG updates; Manual scrap can retain
  pre-shock amounts, leaving small `value_difference` and FG Σ ≠ material pool
  in `assert_multi_fg_pool_closed` even when GL is balanced. No artificial
  Additional Cost or Stock Adjustment is invented to force-close that gap.
- **Job Card Apply regressions (blocked, not bypassed):**
  - PO-JOB08631 — disposition required for `13200475` remaining 2961; Nothing to repair
  - PO-JOB08773 — disposition required for `13200091` remaining 303; Nothing to repair
  - PO-JOB08760 / PO-JOB09259 — Nothing to repair
- **Historical Manufactures with frozen MAIN_FG** do not reprice on RIV until
  repair unlock (or equivalent) clears `set_basic_rate_manually`.
- Stock Reconciliation rate-only shocks on batch items can trip Iran SLE
  integrity (I3); acceptance used Landed Cost Voucher for the upstream shock.

### Production

Not deployed. Not pushed. Not tagged. Experimental branch only until merge review.

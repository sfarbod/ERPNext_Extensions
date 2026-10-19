# RELEASE 5.6.1 — Historical Valuation Rebuild (Phase 2 + Phase 3)

**App:** `erpnext_extensions`  
**Version:** `5.6.1`  
**Branch:** `develop` (merged from `feature/5.6.1-historical-valuation-rebuild`)  
**Policy:** `ACTIVE_HISTORICAL_VALUATION_GUARD = TRANSITIONAL_NON_BLOCKING` (unchanged)

---

## HISTORICAL REBUILD STATUS: PAUSED / INCOMPLETE

**HRC-00001 has not completed its 15,968-entry historical rebuild.**

| Field | Value |
|-------|-------|
| Campaign | `HRC-00001` |
| Status | **Paused** |
| Session | `20261019000943` |
| Session status | **Paused** |
| Manifest entries | 15,968 |
| Distinct identities (approx.) | 4,517 |
| Session RIV completed | 2,338 |
| Session RIV failed | 0 |
| Direct verified | 65 |
| Covered verified | 4,512 |
| Pending verification | 11,391 |

The development database contains **partial** historical SLE/GL recalculation
from this sequential session. Full SLE/GL/TVA reconciliation and the full-scope
integrity scan have **not** passed. Do **not** treat development.localhost as a
clean or fully reconciled accounting baseline.

Strict historical guard activation remains deferred:

- `ACTIVE_HISTORICAL_VALUATION_GUARD = TRANSITIONAL_NON_BLOCKING`
- `TODO(HISTORICAL-REBUILD-STRICT-GUARD)` preserved
- I3/I4 remain enabled

Remaining rebuild work will continue in a **future development task**. Do not
resume the campaign unless that task explicitly authorizes it.

### Resume later (do not run now)

```bash
# Only when a future task authorizes resume of the same session:
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.restore_session_from_file \
  --kwargs '{"session_id":"20261019000943"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.clear_sequential_stop

# Clear pause_requested on HRC-00001 if set, then:
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.run_sequential \
  --kwargs '{"campaign":"HRC-00001","max_identities":None,"stop_on_failure":True,"mark_every":25}'
```

Resuming the **existing** session is supported: `identities_done` / session
`riv_completed` skip already-finished identities. Requires code ≥ 5.6.1 on
`develop` after this merge. Do **not** create a new session unless a future
task explicitly resets the campaign.

---

## Summary

This release corrects false **Skipped Covered** decisions in the Historical Rebuild
Campaign (HRC), adds a full historical stock valuation integrity scanner, and
introduces a **no-skip sequential** historical valuation rebuild path for
development/test. The campaign itself remains paused/incomplete (see above).

### Execution strategy (updated)

Mass pre-creation / parallel RIV queues are **rejected**. The supported path is:

- Restore a clean development baseline from the authorized Production backup.
- Delete **all** development `Repost Item Valuation` rows (RIV only — not SLE/GL).
- Reset every campaign entry to `REPOST_PLANNED`.
- Process Item+Warehouse identities in chronological order.
- **`MAX_ACTIVE_RIV = 1`** — create → execute sync (`continue_reposting=False`) →
  verify SLE continuity → then next identity.
- Empty RQ queues between steps; no uncontrolled background flood.
- Final success classes only:
  - `DIRECT_REPOSTED_AND_VERIFIED`
  - `COVERED_BY_REPOSTED_AND_VERIFIED`
- Inspect-only `VERIFIED_CLEAN` and historical Production Completed RIVs are
  **never** final success.

Module: `historical_rebuild/sequential_rebuild.py`

### Multi-row Material Transfer absorb fix

`_lookup_previous_stock_value` previously excluded the entire current voucher when
resolving the previous stock layer for zero-qty residual absorb. On multi-row
Material Transfers (e.g. `MAT-STE-2026-25926` / item `13100028`) that skipped the
same-voucher prior SLE and re-poisoned a correct BatchNoValuation SVD against the
pre-voucher balance. Lookup now uses chronological prior SLE on the identity
(same voucher allowed).

### Out-of-scope Manufacture FAIL_CLOSED soft-skip

Document-authority zero-qty `FAIL_CLOSED` (e.g. manufacture ghost above the auto
SA ceiling) raised during Item+WH RIV cascades on unrelated FG/WIP rows and
aborted the target identity. The throw is now inside the same out-of-scope
soft-skip handler as other integrity errors.

### Fail-fast sequential halt

`run_sequential` stops scheduling after any non-ok RIV (`stop_on_failure=True`)
and honors `request_sequential_stop()` so an operator can halt after the current
RIV reaches a terminal state without killing an in-flight DB transaction.
Failure evidence is stored on the session JSON (`last_failure`).

### CASE A I3 guard (MAT-STE-2026-40942 / item 13200244)

Full Item+WH RIV on WIP `13200244` / پایکار failed at Material Transfer for
Manufacture `MAT-STE-2026-40942` with outbound `stock_value_difference > 0` (I3).
Root chain:

1. Mid-chain Manufacture SED stamps left `qty=1` with a sticky negative layer
   (~−95.9M).
2. Soft-skip / dependant expansion desynced `wh_data` to a future balance
   (qty≈15721) before the chronological previous SLE (qty=1).
3. Zero-qty absorb preferred the poisoned negative DB previous and set
   `effective_movement = −value_before` → positive SVD on an outbound SLE.

Fixes (I3/I4 remain enabled; no artificial Stock Adjustment):

- Reseed desynced `wh_data` from the chronological previous SLE before vanilla
  (`sle_persistence.reseed_wh_data_from_chronological_previous`); refuse to
  install a negative previous layer.
- Restore `wh_data.prev_stock_value` on out-of-scope soft-skip.
- Refuse document-authority SED stamps that would leave `qty>0` with negative
  `stock_value` (keep vanilla MA).
- Absorb must not adopt a negative DB previous that would flip outbound SVD
  positive; `apply_zero_qty_movement_contract` skips that absorb candidate.

### CASE D nonnegative remaining-layer reconstruction (item 30100023)

Manufacture SED rates can exceed the capitalized prior layer by a few IRR/unit
(e.g. Material Transfer incoming 608890 vs Manufacture consume 608892 on
`MAT-STE-2026-24815`). Stamping the full SED then leaves `qty_after > 0` with
`stock_value < 0` (`NEGATIVE_STOCK_VALUE` / `CONFIRMED_CORRUPTION`).

Reconstruction (I3/I4 kept):

- Available layer = `stock_value − stock_value_difference` (floored at 0).
- Outbound movement expenses at most that available layer.
- Remaining positive qty keeps `stock_value = 0` (same pattern as clean sibling
  `MAT-STE-2026-24813`).
- SED is realigned to the reconstructed movement.

Also: when post-RIV SLE chain verify finds confirmed corruption, the sequential
executor now marks that RIV `Failed` so DB counters match session fail-fast
(avoids Completed-in-DB / Failed-in-session disagreement).

### Campaign ↔ sequential session source of truth

`HRC-00001` is the canonical campaign. Sequential sessions are execution
attempts (`SEQ_SESSION=…` on campaign remarks + session JSON). `run_sequential`
sets campaign status Running / Failed / Paused / Completed, updates
`last_activity` / current RIV identity, records fail-fast in campaign logs, and
enforces a single-executor lock. `campaign.status` reports active session id,
session counters, last failure, elapsed/avg/ETA, and verified coverage without
counting old-session Completed RIVs as current coverage.

Production historical reposting remains a separately controlled operation.
This release does **not** activate `STRICT_AFTER_HISTORICAL_REBUILD`.

---

## Root causes addressed

1. **False Skipped Covered** — `fast_cover.py` / `find_covering_riv` treated any
   Completed Item+Warehouse RIV with earlier/equal posting as proof of coverage.
2. **covers[0] arbitrary association** — first matching RIV stored without
   verifying identity integrity after repost.
3. **Weak post-checks** — `verify_voucher_basic` (SLE/GL presence) was insufficient
   for valuation continuity, I3/I4, Bin, or TVA evidence.
4. **Historical residuals hidden** — identities with mid-chain corruption (e.g.
   positive qty / negative stock value, continuity breaks) were marked clean
   because a later Completed RIV existed.

---

## Campaign coverage corrections / no-skip policy

**Absolute rule:** no campaign entry may be skipped merely because current SLE/GL
looks healthy. Coverage means **actual recalculation** by a RIV created in the
current controlled rebuild session (durable JSON registry under
`private/files/hrc_full_rebuild/`).

Final classifications:

| Class | Meaning |
|-------|---------|
| `DIRECT_REPOSTED_AND_VERIFIED` | Entry itself had no SLE (FA/non-stock) or was directly linked after session rebuild + verify |
| `COVERED_BY_REPOSTED_AND_VERIFIED` | Every SLE identity has a **session** Completed RIV at ≤ posting, chain clean after anchor |

A Completed RIV from the restored Production backup alone is **rejected**.

Modules:

- `historical_rebuild/full_rebuild.py` — reset → session RIV seed/execute → mark cover  
- `historical_rebuild/coverage_verify.py` — evidence engine  
- `historical_rebuild/fast_cover.py` — bulk skip uses evidence only  
- `historical_rebuild/coverage_audit.py` — re-audit Skipped Covered  
- `historical_rebuild/processor.py` — refuses false skip after process  
- `historical_rebuild/reevaluate.py` — integrity re-evaluation (not final success alone)  

---

## Historical integrity scanner

`historical_rebuild/integrity_scanner.py` detects:

| Check | Code |
|-------|------|
| Zero qty residual | `ZERO_QTY_RESIDUAL` |
| Invalid outgoing (I3) | `INVALID_OUTGOING_VALUATION` |
| Invalid incoming | `INVALID_INCOMING_VALUATION` |
| Positive qty / negative SV | `NEGATIVE_STOCK_VALUE` |
| Valuation continuity | `VALUATION_CONTINUITY` |
| Qty continuity | `QTY_CONTINUITY` |
| SLE / GL sample | `SLE_GL_INTEGRITY` |
| Open TVA | `TVA_UNRESOLVED` |
| Failed RIV | `RIV_FAILURE` |
| Bin mismatch | `BIN_INCONSISTENCY` |

Classifications: `CONFIRMED_CORRUPTION`, `SUSPECTED_CORRUPTION`,
`LEGITIMATE_ACCOUNTING_BEHAVIOR`, `NEEDS_MANUAL_REVIEW`, `REPAIRED_AND_VERIFIED`.

Bench:

```bash
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.scan_integrity \
  --kwargs "{'campaign':'HRC-00001'}"
```

---

## RIV identity matching

- Chronological Completed RIV lookup remains available for **deduplicating**
  RIV creation (`find_covering_riv` / `find_completed_covering_riv`).
- **Accounting coverage** uses `verify_identity_coverage` /
  `verify_voucher_coverage` — wrong Item, wrong Warehouse, dirty SLE, Failed RIV,
  or open TVA → not covered.

---

## Reposting dependency strategy

`historical_rebuild/executor.py`:

- Deterministic order: posting_date, posting_time, ordinal  
- Non-conflicting identity filter within a batch  
- Backpressure via `MAX_OUTSTANDING_RIV`  
- Pre-verify idempotency (skip if already evidence-clean)  
- Post-RIV accounting verification (Completed ≠ Verified)  
- Pilot batch then careful drain  

Mid-chain anchors (e.g. `13200244` / `2026-08-22 23:40:12`) remain the preferred
repair entry when full-history RIV fails I3; suffix-only repair must not be
claimed as full historical integrity.

---

## TVA integration

- Open / SUBMITTED / REPOST_PENDING / MANUAL_REVIEW TVA blocks coverage.  
- `tva_reconcile` still supersedes Phase-1 TRACE rows after successful rebuild.  
- No silent deletion of Stock Adjustment GL history.

---

## Financial reconciliation

Verification is account- and voucher-level (SLE continuity + Bin + optional
SLE/GL sample). Net-zero GL alone is not accepted as proof.

---

## Performance

- Set-oriented candidate SQL for fast_cover pre-filter  
- Dirty-identity set from scanner used as fast path in re-evaluate  
- Bounded SLE/GL sample scan  
- Streaming campaign batches; no full ledger load into memory  

---

## Migration requirements

- DocType **Historical Rebuild Campaign Item** gains:
  - `verification_class` (Select)
  - `verification_detail` (Long Text)
- Run `bench --site <site> migrate` after deploy.
- **No automatic Production historical repost** is performed by migrate.

---

## Known limitations

- Incoming negative SVD is classified **SUSPECTED** until purpose-level review.  
- Mega Manufacture residuals (~3.3e9 IRR class, e.g. `MAT-STE-2026-40436`) remain
  fail-closed / manual review — no arbitrary Stock Adjustment to force completion.  
- Full Phase 3 on Production is out of scope for automatic deploy.  
- Scanner SLE/GL check is a sample of Stock Entry vouchers, not every GL line.

---

## Production deployment precautions

1. Deploy code + migrate only.  
2. Keep `TRANSITIONAL_NON_BLOCKING`.  
3. Do **not** start Production HRC / RIV flood without change control.  
4. Re-evaluate and Phase 3 on a restored non-Production site first.  
5. Export RIV audit snapshots before any RIV cleanup.  
6. Never point development workers at Production Redis/DB.

---

## Strict Guard future TODO

```
TODO(HISTORICAL-REBUILD-STRICT-GUARD)
```

Remain in `historical_valuation_residual.py`. After HRC verification completes
and historical SLE/GL inconsistencies are reconciled, set:

`ACTIVE_HISTORICAL_VALUATION_GUARD = STRICT_AFTER_HISTORICAL_REBUILD`

Do **not** activate strict mode in this release.

---

## Tests

- `tests/test_historical_valuation_rebuild_v561.py`  
- Updated `tests/test_historical_rebuild_phase2_complete_v5547.py`  
- Re-run `tests/test_historical_residual_v5551.py` (guard preserved)

---

## Bench helpers (development)

```bash
# Scanner
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.scan_integrity

# Re-evaluate false Skipped Covered
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.reevaluate \
  --kwargs "{'campaign':'HRC-00001'}"

# Phase 3 drain (dev only)
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.execute_phase3 \
  --kwargs "{'campaign':'HRC-00001','batch_size':10,'max_batches':50}"
```

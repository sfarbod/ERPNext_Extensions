# erpnext_extensions 5.6.11

## HRC-00004 — Historical fractional SABE pool blocks Physical Two-Slice

### Symptom (Production `HRC-00004` / session `20261024195815`)
Failed RIV `arpd4l8e70` for Item `13100012` @ packaging approved warehouse:

`AllocationError: historical fractional pool_value=…; repair required before allocate`

Call chain: Core RIV → `update_serial_batch_no_valuation` →
`rewrite_outgoing_bundle_after_core` → `allocate_physical_two_slice`.

Development reproduction (morning backup `20261024_085950`):

`historical fractional pool_value=3898315893.4397836`

### Root cause
1. **Historical / Core MA float on Serial and Batch Entry** for FG batches
   (example: Material Transfer `MAT-STE-2026-33527`, batch
   `7060-20100006-MR265001D01`, SABE rate `250653.505702364` while SED rate is
   integer `250654`). Pool aggregates become non-integer.
2. **RIV dependency expansion**: Core queues dependent Item/Warehouse pairs
   from vouchers that touch the seed identity. Packaging-material RIVs therefore
   revalue FG Delivery Notes (`MAT-DN-2026-00073` / `00074`) that hit Physical
   Two-Slice.
3. **5.6.10 RIV skip**: For non-DN/SI vouchers during RIV the fractional SABE
   guard returned early, so Core MA **reintroduced** float rates on Stock Entry
   SABE mid-campaign, then DN two-slice fail-closed on the poisoned pool.

### Fix
1. **`historical_fractional_sabe_repair`**: explicit ROUND_HALF_UP integerization
   of SABE rate / `stock_value_difference` with audit JSON under
   `private/files/iran_fractional_sabe_repair/`. Repair uses a DB savepoint;
   audit is written only after savepoint release (caller owns outer commit).
2. During RIV, after Core MA on non-DN/SI bundles: `integerize_bundle_sabe` +
   `assert_bundle_integer_irr` and refresh in-memory `doc` totals (no longer
   skip silently).
3. Before two-slice allocate: if pool value is fractional, run batch@warehouse
   repair, re-read pool; still fail closed if residual fraction remains.

### RIV short-pool behavior (hardened, not weakened)
When Physical Two-Slice sees `out_qty > visible pool_qty` **during RIV only**:

- Live submit: still raises `AllocationError` (fail-closed).
- RIV: writes `riv_short_pool_fallthrough` audit JSON, returns `None` from the
  two-slice rewriter, then the monkey-patch path runs Core MA +
  `integerize_bundle_sabe` + `assert_bundle_integer_irr`.
- Fractional IRR rates/amounts cannot persist after integerize; residual
  fractions throw.
- Does not invent Stock Adjustment / GL balancing; does not bypass live
  integrity guards; does not silence empty/short pool on live submit.

### Accounting safeguards
- Integer IRR valuation rates (and SVD amounts) after repair / integerize.
- Fractional quantities remain supported.
- Idempotent row repair (already-integral matching qty×rate → no-op).
- Savepoint atomicity for batch-pool repair (mid-failure rolls back repair only).
- Scope limited to the named item/batch/warehouse SABE rows.
- Audit records original/corrected values, batch, voucher, reason, timestamp,
  and best-effort RIV/session context. Audit does not claim DB commit success
  (`committed_by_caller: true`).

### Failure 2 — I5 (`mnqsl58oke` / `13100060`)
**Not reproduced** on morning Production backup `20261024_085950` with current
code (`13100060` Quarantine RIV Completed in scoped Dev E2E). Likely
state-dependent after partial evening HRC mutation. **Not claimed fixed.**
Does not block 5.6.11 solely on non-reproduction; requires evening evidence
if it recurs after install.

### Scoped allowlist hardening
`create_scoped_campaign` always embeds `SCOPED_HRC_TEST:` even when custom
remarks are supplied, so sequential allowlist filtering cannot be silently
disabled (SEQ_SESSION may prepend later).

### Tests
- `test_historical_fractional_sabe_repair_v5611` — RHU math, allocate refuse,
  idempotency, savepoint rollback, audit-after-release, short-pool live vs RIV.
- Existing Physical Two-Slice / IRR / manufacture / RIV / MariaDB 1020 suites
  (see release validation matrix).
- Independent Dev E2E after morning restore `20261024_085950`: scoped
  `HRC-00005` / session `20261024214108` — seeds `13100012` approved +
  `13100060` Quarantine — 2/2 RIV Completed (`3qpkgk0v7h`, `3tkobtr2n6`),
  42/42 `COVERED_BY_REPOSTED_AND_VERIFIED`, `gates_ok=true`, fractional SABE
  on batch `7060-20100006-MR265001D01` = 0.

### Regression matrix (Development validation)
Executed for this release (exact counts):

| Suite | Result |
| --- | --- |
| `test_physical_two_slice` | PASS 4 |
| `test_historical_fractional_sabe_repair_v5611` | PASS 13 |
| `test_scoped_campaign_v569` | PASS 6 |
| `test_sequential_riv_1020_retry_v569` | PASS 2 |
| `test_mariadb_1020_concurrency_v569` | PASS 2 |
| `test_irr_rate_first_rounding` | PASS 5 |
| `test_manufacture_fg_pool_residual_v5525` | PASS 13 |
| `test_transfer_valuation_v530` | PASS 5 |
| `test_riv_valuation_integrity` | PASS 33 |
| `test_hrc_coverage_time_normalize_v568` | PASS 6 |
| `test_manufacture_irr_residual_v5334` | PASS 43 |
| `test_manufacture_residual_submit_v5330` | PASS 14 |
| `test_stage_output_equivalent_costing` | PASS 33 |
| `test_irr_rounding_residual` | PASS 19 |
| `test_co_product_residual_align_v5518` | FAIL 1 / PASS 9 — `test_09_fractional_equivalent_factors` (`1002 != 1000`); **develop baseline**, unchanged by 5.6.11 |
| `test_stock_reconciliation_cancel` | FAIL env — Cost Center missing on restored Dev fixtures |
| `test_repost_determinism` | FAIL env — Cost Center missing on restored Dev fixtures |

Not executed in this gate (do not treat as PASS): Delivery Note COGS / Sales Invoice full E2E UI suites beyond integrity coverage already exercised via scoped RIV DN dependents; Serial/Batch Bundle live-submit beyond unit + RIV path.

### Compatibility / migration
- Application version: **5.6.11** (`erpnext_extensions/__init__.py`).
- Compatible with Frappe 16.52.0 / ERPNext 16.51.1.
- No schema migration. First RIV that touches a fractional batch writes audit
  files on the site under `private/files/iran_fractional_sabe_repair/`.

### Production recovery (operator-executed; not automated here)
Do **not** delete `HRC-00004`. After publishing 5.6.11:

1. Clear stop flag / stale Redis session + executor lock **only if no executor
   is running**.
2. Restore session `20261024195815` from persisted JSON if Redis cache is empty.
3. Resume sequential rebuild — `execute_one_identity` skips
   `identities_done` (31 completed preserved); failed identity
   `arpd4l8e70` / `13100012` is retried without replaying completed RIVs.
4. Preserve Manifest checksum, fail-fast, `MAX_ACTIVE_RIV=1`.
5. Cancel/clear only the failed in-flight RIV if it remains Queued/Failed before
   resume, if Core would otherwise conflict — do not wipe completed session RIVs.

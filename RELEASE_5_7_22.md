# Release 5.7.22 — HRC-00015 Negative Pool Two-Slice Floor + Restricted Campaign Validation

**Version:** **5.7.22**  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting Physical Two-Slice allocator; Historical Rebuild Campaign sequential RIV  
**Deployment status:** Local Development validated. **No Production deploy. No push.**

## Objective

Investigate and permanently fix Historical Rebuild Campaign `HRC-00015` fail-fast on
Repost Item Valuation `cuaaqspcvm` (identity `13200112` @
`انبار approved اقلام بسته بندی ثانویه اسپاد`), then validate with a **new**
restricted campaign containing only the demonstrated risk records.

## Production backup baseline

| | |
|--|--|
| Backup | `20261110_141333-erp_espadpharmed_com-database.sql.gz` (498 MB) |
| Integrity | `gzip -t` OK; MariaDB dump / Frappe 16.54.0 metadata |
| Pre-restore Dev snapshot | `sites/preserved_dev_backups/20261110_pre_20261110_141333_prod_restore/` |
| Restore target | `development.localhost` only |
| Isolation | scheduler muted; `host_name` Dev; Production untouched |

## HRC-00015 root cause

### Recorded failure

```
AllocationError: n_high out of range: out_v=-1947079 R=-1947078 oq=1.0 n=-1
```

Stack: RIV `repost` → `update_serial_batch_no_valuation` →
`rewrite_outgoing_bundle_after_core` → `allocate_physical_two_slice`.

Banner about `Allow Zero Valuation Rate` / item `13200412` is Core `msgprint`
noise prepended via `message_log` (intentional scrap zero on
`MAT-STE-2026-36814`) — **not** the allocator root cause.

### Primary defect (Critical)

`allocate_physical_two_slice` used:

```python
R = int(pv // pq)  # intended floor(avg)
```

For Python **`int`**, `//` is floor toward −∞. For **`decimal.Decimal`**, `//`
follows `divmod` remainder-sign rules and **truncates toward zero**.

Exact reproduction of the production vector:

| | |
|--|--|
| pool_qty | 2 |
| pool_value | −3,894,157 |
| out_qty | 1 |
| `Decimal //` (bug) | **−1,947,078** |
| true floor | −1,947,079 |
| `ROUND_HALF_UP` share | −1,947,079 |
| `n_high` | −1 → AllocationError |

### Secondary defect (Critical)

Slice-sum guard compared `abs(total)` to a **signed** `out_v`, so some negative-pool
plans that passed `n_high` still failed the identity check.

### Campaign context

| | |
|--|--|
| Manifest | 17,973 docs (full history) |
| Session RIVs completed before fail-fast | 233 |
| Verified docs at failure | 394 Skipped Covered |
| Failed identity | `13200112` @ approved packaging WH |

## Risk classification

| ID | Severity | Issue |
|----|----------|-------|
| R1 | Critical | Decimal `//` ≠ mathematical floor → `n_high out of range` |
| R2 | Critical | Signed `out_v` vs `abs(total)` slice-sum check |
| R3 | High | Pre-existing negative SABE pools (qty>0, value<0) elsewhere |
| R4 | Medium | msgprint / `message_log` noise on RIV error_log |
| R5 | Low | Intentional `allow_zero` scrap on `13200412` |

## Fixes implemented

1. **`_floor_div`** — mathematical floor via `ROUND_FLOOR` for rate base `R`
   (whole-qty and fractional-qty paths).
2. **`out_value_abs`** — always magnitude; slice-sum compares magnitudes.
3. **`simulate_sequential`** — updates remaining value from signed slice amounts
   (correct for negative pools).
4. **RIV negative-pool fallthrough** — live submit fail-closed on
   `pool_value < 0` with `pool_qty > 0`; during RIV, audit + fall through to
   integer SABE policy (mirrors short-pool policy).

### Files

| Path | Change |
|------|--------|
| `domain/physical_two_slice.py` | `_floor_div`; signed/`abs` out-value; sequential remainder |
| `domain/physical_two_slice_bundle.py` | RIV negative-pool fallthrough |
| `tests/test_physical_two_slice.py` | HRC-00015 vector + Decimal `//` guard |
| `tests/test_hrc00015_negative_pool_v5722.py` | Allocator + live/RIV rewrite behaviour |
| `__init__.py` | **5.7.22** |
| `HRC_00015_RCA_5_7_22.md` | Full RCA |
| `RELEASE_5_7_22.md` | This document |

## Restricted campaign validation (HRC-00016)

**Do not reuse HRC-00015.**

| | |
|--|--|
| Campaign | **HRC-00016** |
| Scope label | `HRC-00015-risk-13200112-v5722` |
| Vouchers | **54** exact (1 Stock Reconciliation + 53 Stock Entry) — only SLE
vouchers for the failed identity |
| Identities | **1** — `13200112` @ approved packaging WH |
| Preflight | PASS |
| Execution | Sequential, stop-on-failure, sync |
| Result | **Completed** |
| Session RIV | `cu35drueub` **Completed** |
| Manifest coverage | 54/54 Skipped Covered |
| Failed session RIVs | 0 |

Preview artifact:
`sites/development.localhost/private/files/hrc_00015_restricted_preview_v5722.json`

### Inclusion rule

Every voucher has an SLE for `13200112` @ the failed warehouse (direct dependency
on the HRC-00015 fail-fast identity). Unrelated historical documents excluded
(17,973 → 54).

### Post-execution integrity (identity)

| Check | Result |
|-------|--------|
| RIV status | Completed |
| SLE qty / value | 36,291 / 8,413,831,198 |
| Fractional SLE `valuation_rate` | 0 |
| Fractional SABE `incoming_rate` | 0 |
| Open Queued/In Progress RIV for identity | 0 |
| Campaign pending/failed items | 0 |

Note: historical Failed RIV `cuaaqspcvm` from HRC-00015 remains as forensic
evidence on the restored baseline; it is not part of HRC-00016.

## Tests

```
test_hrc00015_negative_pool_v5722   — 4 OK
test_physical_two_slice             — 7 OK
test_physical_two_slice_pick_list_pool_v5716 — 5 OK
```

## Version

`erpnext_extensions.__version__` → **5.7.22**

## Explicit non-actions

- Production not modified, not pushed, not deployed.
- HRC-00015 not resumed / overwritten.
- No silent Stock Adjustment to hide negative pools on live submit.
- No broadening of Physical Two-Slice voucher scope beyond DN/SI.

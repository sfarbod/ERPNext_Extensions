# Release 5.2.28 — SLE↔GL Drift Historical Repair

Package version: **5.2.28**.

## Summary

Adds Historical Repair mode **`SLE_GL_DRIFT`** for the case where:

> SLE is healthy and authoritative, but posted GL is stale relative to the GL
> ERPNext currently generates from that SLE state.

This is the safe recovery path after a Failed RIV that committed SLE recalculation
before the GL phase failed (e.g. `l7fhvbp01r`).

| Area | Behavior |
|------|----------|
| Discovery | Semantic expected-vs-posted GL compare — **does not trust false `G0_HEALTHY`** |
| Integrity gate | Refuses GL rebuild when SLE fails I1 / I4 / poison / non-finite checks |
| Execution | GL-only via `apply_gl_rebuild_from_current_sle` / ERPNext `make_gl_entries` |
| SLE | **Immutable** — fingerprint before/after must match |
| RIV | Never starts, resumes, or relies on full Item+Warehouse RIV |

---

## Mode: `SLE_GL_DRIFT`

### Outcomes

| Status | Meaning |
|--------|---------|
| `READY_GL_ONLY` | SLE healthy; expected GL valid; posted GL differs → eligible |
| `NO_DRIFT` | Posted GL already matches expected |
| `WAITING_SLE_REPAIR` | SLE integrity unhealthy (I1 / I4 / valuation integrity / non-finite) |
| `EXPECTED_GL_ERROR` | Cannot generate expected GL |
| `UNBALANCED_EXPECTED_GL` | Expected GL not postable / unbalanced |
| `CONFLICTING_RIV` | Active Queued/In Progress RIV intersects voucher scope |
| `MANUAL_REVIEW` | Not submitted / not a Stock Entry / other |

### False G0 fix

Classic GL classification treats many Material Transfer vouchers as `G0_HEALTHY`
even when account nets differ from expected GL. `SLE_GL_DRIFT` ignores that gate
and uses expected-vs-posted equality as the decisive condition.

### Integrity refusal

GL-only repair is **blocked** when SLE is untrustworthy. Blocker tags include:

- `I1` — negative incoming rate on inbound qty
- `I4` — qty_after≈0 with leftover stock_value
- `VALUATION_INTEGRITY` — sign-inverted SVD / established `exploded_rate` poison (≥1e12)
- `NON_FINITE` — NaN/Inf valuation fields

No arbitrary “high IRR rate” threshold is used beyond the existing poison contract.

### APIs

- `scan_sle_gl_drift_api`
- `classify_sle_gl_drift_api`
- `dry_run_sle_gl_drift_api`
- `repair_sle_gl_drift_selected_api` (default `dry_run=True`)

### Ordering / batching

Deterministic order: `posting_date, posting_time, creation, name`.

Supports `batch_size`, `resume_cursor`, `stop_on_error` (default stop on failure).

---

## Operational safety

**Upgrading to v5.2.28 does not repair any historical SLE↔GL mismatches.**

Existing drift remains until an operator runs an explicit `SLE_GL_DRIFT` dry-run and then a controlled apply (with backup). Class D / `WAITING_SLE_REPAIR` vouchers require SLE integrity repair first and must not be GL-rebuilt from poisoned SLE.

Checkpoint / resume / audit use the existing Historical Stock Repair Log. GL rebuild reuses ERPNext/`make_gl_entries` so **v5.2.27 IRR precision alignment** remains active on every rebuild.

---

## Development dry-run baseline (read-only)

Against the `13100134` target-chain mismatch set (103 Stock Entries):

| Classification | Count |
|----------------|------:|
| `READY_GL_ONLY` | 91 |
| `WAITING_SLE_REPAIR` | 12 |

`MAT-STE-2026-34391` (outside that item set): `READY_GL_ONLY` under v5.2.27 precision alignment.

**No production/development vouchers were repaired in this release.** These counts are validation facts only — a separate controlled repair scenario is required after upgrade.

---

## Tests

`erpnext_extensions.iran_accounting.tests.test_sle_gl_drift_v5228` covers:

- healthy SLE + stale GL → READY
- matching GL → NO_DRIFT
- false G0 transfer → READY
- I1 / I4 → WAITING_SLE_REPAIR
- large legitimate IRR rate not blocked
- expected GL error / unbalanced / conflicting RIV
- dry-run no write; fingerprint preserve; post-repair mismatch fail
- v5.2.27 1-IRR arithmetic eligibility
- RIV scope + I1/I4 module regression imports

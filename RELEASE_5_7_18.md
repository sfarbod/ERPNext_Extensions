# Release 5.7.18 — UVR ±1e-9 drift is amount-preserving (HRC-00014)

**Version:** 5.7.18  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting IRR residual classification (Purchase Receipt)  
**Deployment status:** Local Development fix. **No Production deploy until validated.**

## Problem

Historical Rebuild Campaign `HRC-00014` fail-fast during sequential RIV:

> `IRR rate-rounding residual rejected: valuation inconsistency (Class B)`  
> Purchase Receipt `MAT-PRE-2026-02045` / item `15010547`  
> `qty=46`, `valuation_rate=1737714.673913044`, `amount=79934875`,  
> `expected_rate=1737715`, `residual=-15`, `reason=non_integer_rate_under_irr_contract`

RIV: `8tt4rqd11g` — identity `15010547` @ `انبار ملزومات مصرفی اسپاد`.

## Root cause

PI discount produced amount-preserving fractional `valuation_rate = auth/qty`.
Sibling rows on the same PR reconstructed exactly and skipped. This row’s stored
VR was **one DECIMAL(30,9) quantum (1e-9)** above `flt(auth/qty)` while
`qty × VR` still equalled acquisition auth. The legacy UVR detector required
exact equality, so classification fell through to Class B and invented a −15
residual against `round(rate)`.

SLE already matched auth (`SVD = 79,934,875`). Not SLE/GL drift. Not a need to
absorb into Round Off / Stock Adjustment.

A second company case: `MAT-PRE-2026-02094` / `15010666` qty=3.

## Fix

`is_legacy_uvr_float_valuation_rate` accepts ±1 DECIMAL(30,9) quantum and IEEE
ULP drift vs `auth/qty`, still gated by:

- non-integer rate under IRR contract
- `ROUND_HALF_UP(rate) == integer_valuation_rate_from_amount(auth, qty)`

True non-reconstructing floats remain Class B. No Round Off / SA absorption
change. No ledger SQL mutation.

## Tests

- `test_pr_uvr_ulp_drift_v5718.py` — HRC-00014 shapes skip; true mismatch Class B
- Existing `test_pr_legacy_uvr_float_v5528.py` / residual classification suites

## HRC follow-up

After this fix on Development, re-run / resume sequential RIV for the blocked
identity and confirm Class B no longer fires on the two PRs. Do not treat
campaign status alone as success — verify RIV Completed and GL balance.

# erpnext_extensions 5.5.28

## Historical Purchase Receipt UVR float rates (Class B → Class A)

5.5.27 remains the prior local release candidate. This patch does not add
tolerance, does not silence assertions, does not invent invoice linkage, and
does not mutate historical Purchase Receipts / SLE / GL.

### Root cause

The second 1000-voucher pilot left **Failed = 4** Class B RIVs on two
economically valid Purchase Receipts:

- `MAT-PRE-2026-00780` (IRR) — UVR stored `valuation_rate = base_net / stock_qty`
  as IEEE float (e.g. `32584473.066666666`).
- `MAT-PRE-2026-00707` (EUR→IRR + LCV) — same shape persisted at
  `DECIMAL(30,9)` (`89660.277177778`).

Iran Accounting requires integer IRR rates. Classifier
`non_integer_rate_under_irr_contract` rejected the float before the
amount-authoritative Class A path could absorb the integer-division residual
via Company Round Off.

Saved SLE already used the integer rate (`ROUND_HALF_UP(auth/qty)`); the
document float is storage shape only.

### Economic owner

**Document stock valuation amount** (UVR numerator: `base_net_amount` + tax +
LCV + …) is authoritative. Rate for residual math is
`integer_valuation_rate_from_amount(auth, stock_qty)`. Round Off absorbs the
path-bounded remainder (`|residual| < |qty|`). Stock Adjustment must not.

### Fix

`is_legacy_uvr_float_valuation_rate` — exact reconstruction of auth÷qty via
`flt(auth/qty)` or DECIMAL(30,9) ROUND_HALF_UP / ROUND_DOWN / ROUND_HALF_EVEN.
Matching floats coerce to the integer amount-derived rate inside
`classify_amount_rate_residual` (classification only; document unchanged).
Non-matching non-integer rates stay fail-closed Class B.

### Prevalence (full restored DB, read-only)

2 Purchase Receipts / 8 item rows (family exclusive to these two documents in
the scanned IRR PR set). Pilot RIV failures: 4. Full-campaign estimate: the
same family only — RIVs whose GL rebuild visits these PRs.

### Tests

`test_pr_legacy_uvr_float_v5528` reproduces both real PR shapes (IRR multi-row
and EUR+LCV large qty), Class B mismatch retention, RIV-safe decision, and
idempotent classification.

### Production

Not deployed. Not pushed.

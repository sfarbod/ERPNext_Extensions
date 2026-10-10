# erpnext_extensions 5.5.27

## RIV ±1 IRR GL residual (historical 1000-voucher pilot)

5.5.26 remains the prior local release candidate. This patch does not weaken
GL balance validation, does not add `abs(diff) <= 1` tolerance, and does not
silence Debit/Credit failures.

### Root cause

The LOCAL 1000-voucher chronological pilot (7107 RIV, 203 Failed) ran
`_execute_reposting_entry` without Iran runtime bootstrap. Vanilla ERPNext
then:

1. wrote `Stock Entry Detail.basic_rate = |SLE.stock_value_difference| / qty`
   (float) during RIV rate updates;
2. built fractional inventory GL legs;
3. quantized each account with IRR precision 0;
4. produced Debit/Credit Difference ±1;
5. threw because Stock Entry allowance is hard-coded at 0.5.

All 203 failures were that signature (+1: 112, −1: 91) across 89 Stock Entries
(Manufacture 69, Material Transfer for Manufacture 19, Material Issue 1).

### Economic owner

A proven one-quantum IRR stock-valuation residual is owned by
`Company.stock_adjustment_account` — the existing Iran precision-residual /
TYPE-C stock-valuation contract. Round Off must not absorb it. Larger gaps
still fail closed.

### Fix

- RIV public and private executors always call Iran bootstrap before work
  (closes the console/campaign gap when `before_job` did not run).
- `repost()` re-asserts bootstrap as defense in depth.
- Material Issue stays rate-first (not in RIV-propagated rate purposes), so
  vanilla `|SVD|/qty` cannot rewrite submitted integer rates once patches run.

### Tests

`test_riv_pm1_gl_residual_v5527` covers +1/−1 Stock Adjustment ownership,
idempotent second align, Material Issue rate-first, and bootstrap-safe private
executor installation.

### Production

Not deployed. Not pushed.

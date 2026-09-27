# RELEASE 5.3.9 — Out-of-scope RIV must not create new I1

## Verdict context
MAT-STE-2026-30470 1 IRR = legitimate precision-0 residual (quantum → round-off account).
MAT-STE-2026-28696 TGT RIV previously Completed but raised I1 11→19 by rewriting unrelated FG SLEs.

## Root cause
`run_integrity_assert_in_riv_scope` logged out-of-scope I1/I2 as non-blocking, then vanilla
`process_sle` still persisted newly computed negative incoming rates on Manufacture FG rows
reached via dependant hops from packaging-material RIV.

## Fix (general)
- Soft-skip now returns `False`.
- `process_sle`: skip vanilla when before-assert soft-skips; after soft-skip restore
  pre-vanilla SLE + `wh_data` snapshot, then persist restored economics.
- Do not `db.commit()` from out-of-scope audit logging during active RIV.

## Tests
- `test_riv_valuation_scope` asserts soft-skip returns False.

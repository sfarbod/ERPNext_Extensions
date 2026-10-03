# 5.3.46 — PENDING_PURCHASE_INVOICE_VALUATION RIV-safe

## Why
Native RIV on zero-rate Purchase Receipts with pending Purchase Invoice was
failing I3: ERPNext MA uses `qty * valuation_rate` and drops leftover
`stock_value`, producing false negative incoming SVD. IRR deterministic
rounding then invented `incoming_rate` from prev_value/prev_qty.

## What
- Generic `PENDING_PURCHASE_INVOICE_VALUATION` classifier (unchanged contract)
- MA override preserves warehouse `stock_value` and dilutes rate (never invents a rate)
- Post-vanilla economics re-assert `incoming_rate=0`, `svd=0`
- FULL_REPOST_PREFLIGHT treats `LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING` as non-blocking
- I3 remains fail-closed for unexplained negative incoming SVD
- No SQL-Skip, no continue-on-Failed, no PR rate amendment

## Regression
`test_pending_purchase_invoice_valuation_v5345` (A–H + MA leftover preserve + preflight)
Canary: MAT-PRE-2026-00793-1 / 15010444

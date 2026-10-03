# v5.3.45 — PENDING_PURCHASE_INVOICE_VALUATION

Generic business classification for submitted Purchase Receipts with temporary
document rate 0 while Purchase Invoice is not yet posted.

- Native RIV honors zero inbound (no invented rate) until PI finalizes
- Stale SLE incoming_rate cleared to document 0 in-memory during process_sle
- I3 remains fail-closed for unexplained negative incoming SVD
- No PR amendment, no SQL-Skip, no continue-on-Failed
- Canary: MAT-PRE-2026-00793-1 / 15010444

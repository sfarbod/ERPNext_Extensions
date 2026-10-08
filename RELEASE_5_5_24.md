# erpnext_extensions 5.5.24

## RIV × Iran Accounting — leftover-MA gate + dependant rate propagation

### Summary

Two local-proven fixes for Repost Item Valuation under Iran Accounting:

**FIX A — leftover-MA eligibility gate**

`restore_leftover_ma_after_riv()` now enforces the existing
`leftover_ma_receipt_eligible` / manufacture-flow contract before any
patient-zero stamp or `update_entries_after` replay. Paykar / MTfM /
Manufacture patient zeros no longer cause an 85× duplicate walk that
exceeded the 1800s RQ timeout (e.g. `oua59fnjrf`).

**FIX B — RIV-propagated rate × rate-first sync**

Rate-first remains the submit-time and idempotent-RIV contract. During
RIV, when Core `outgoing_rate` legitimately diverges from Stock Entry
`basic_rate` on transfer / manufacture / repack dependants, Iran now:

1. IRR-rounds the Core rate
2. Writes it to the document
3. Forces recalculate (vanilla skips this when `dependant_sle_voucher_detail_no` is set)
4. Applies the existing Iran manufacture / residual contract
5. Lets `sync_irr_sle_from_stock_entry_row` mirror the **updated** document

Soft-skip (5.3.9), VALUED_SOURCE_ZERO_OUTGOING, Document Rebuild, and the
5.5.23 pickle-safe RIV guard are unchanged.

### Local acceptance

- Controlled receipt 100→250 → MTfM / Manufacture / FG 500 / GL 2500 with Iran on
- `oua59fnjrf` completion-only (~0.4s) and full re-walk (~107s, 9013 SLE) Completed
- leftover-MA replay count 0 for Paykar / MTfM
- Document Rebuild not invoked

### Production

Not deployed by this release cut. Prefer a single-RIV canary after backup.

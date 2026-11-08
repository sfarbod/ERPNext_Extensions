# Release 5.7.15 — Same-account Repack net-zero GL (HRC-00013)

**Version:** 5.7.15  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting normalized Manufacture / Repack GL  
**Deployment status:** Local Development fix only. **No Production deploy. No push.**

## Problem

Historical Rebuild Campaign sequential RIV failed during GL repost with:

> `ValidationError: Incorrect number of General Ledger Entries found`

Production reference: HRC-00013 / RIV `4cbi8ss2dd`. Dev reproduction on
identity `13100001` @ `انبار کالاهای FOR OTHER PURPOSE اسپاد` failed on
Repack vouchers `MAT-STE-2026-37328` and `MAT-STE-2026-37329`.

## Root cause

`build_normalized_manufacture_gl_entries` posts inventory debit and credit for
same-account Repack (batch↔batch or item↔item, equal value). Core
`process_gl_map` / `merge_similar_entries` collapses those legs onto **one** GL
row with `debit == credit`. Core `make_gl_entries` accepts only an empty map or
`len(gl_map) > 1`, so a single net-zero row throws.

The same-account Material Transfer guard (`_should_skip_balanced_transfer_gl_pair`)
does not apply to Repack (normalized Manufacture path).

## Fix

1. **`drop_net_zero_merged_gl_rows`** — after merge, drop rows where
   ROUND_HALF_UP debit equals credit (or both near-zero). Preserves one-sided
   residuals.
2. **`finalize_normalized_manufacture_gl_map`** — `collapse_sa` then drop
   net-zero rows.
3. **`build_normalized_manufacture_gl_entries`** — returns finalize output.
4. **`StockController.make_gl_entries` monkey patch** — uses finalize for
   Manufacture/Repack so provided `gl_entries` (RIV `from_repost`) are cleaned
   the same way.

Does **not** invent clearing accounts, change Department handling, alter the
Material Transfer same-account guard, or change Manufacture residual-only SA
behaviour from v5.7.12.

## Tests

- `test_repack_net_zero_gl_v5715.py` — equal same-account Repack → 0 GL;
  unequal → balanced inventory + residual; multi-account Repack; Manufacture
  residual unchanged; Material Transfer skip guard; `from_repost` consistency;
  assert never single-row for `make_gl_entries`.
- Existing `test_manufacture_expense_account_v5712.py` expectations unchanged.

## Verification

Fresh local scoped campaign for the 10-voucher reproduction identity; sequential
RIV must complete. Do not resume Production HRC-00013.

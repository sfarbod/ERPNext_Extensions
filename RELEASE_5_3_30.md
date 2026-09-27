# Release 5.3.30 — Iran Accounting Manufacture residual before ledger contract

Package version: **5.3.30**.

## Fixed

Iran Accounting Manufacture Stock Entry could fail after ERPNext successfully
created balanced SLE/GL because post-SLE rate-first normalization rewrote the
Finished Good amount and caused the Iran ledger contract to reject its own
changed state.

Regression reference (development canary, rolled back): `MAT-STE-2026-37736`.

Observed class of failure:

- Outgoing consume + component scrap leave a valid Manufacture FG residual.
- Additional Cost is capitalized on the FG (e.g. 5 IRR).
- ERPNext posts SLE/GL from the residual-correct FG amount.
- Iran `on_submit` then ran rate-first `align_stock_entry_item_amounts` **after**
  ledger creation, rewriting FG to `qty × integer rate + additional_cost`.
- `enforce_stock_entry_ledger_contract` then aborted submit on SLE ↔ row mismatch
  (example residual destroyed: 73 IRR).

## Root Cause

Ordering mismatch between validation and submit processing, plus interaction of:

1. **Rate-first integer IRR normalization** — composes
   `amount = qty × integer rate + additional_cost + LCV`.
2. **Manufacture FG residual** — when that product cannot reproduce the economic
   pool, the single FG must absorb the residual so
   `FG + component scrap = consume + capitalized additional costs`.
3. **Component Scrap** — reduces the FG material residual.
4. **Additional Cost** — must stay capitalized exactly once and must not skip
   residual restoration merely because capitalization exceeds IRR tolerance.
5. **SLE mirror enforcement** — after submit, row economics must still match the
   already-posted SLE movements.

Validate already ran: align → Manufacture output contract → residual restore.
Submit previously re-aligned rate-first **after** SLE without restoring residual,
so the safety gate failed on Iran’s own rewrite.

Additionally, residual restore previously skipped the pool path when
`capitalized > residual tolerance`, which incorrectly blocked restoration for
Additional Cost cases such as `add_cost = 5`. Composition verification also
required `basic_amount == qty × rate` even for the single Manufacture FG that
owns a valid pool residual.

## Resolution

Combination of:

1. **Lifecycle / execution-order correction** — shared
   `apply_irr_manufacture_economic_finalize()` runs
   `align_stock_entry_item_amounts` → `apply_iran_manufacture_output_contract` →
   `align_manufacture_finished_good_residual`. Used by `on_submit_stock_entry`
   and Manufacture persist paths so post-SLE finalize cannot destroy residual.
2. **Residual logic correction** — when FG capitalized cost
   `>=` IRR tolerance, restore
   `FG.amount = outgoing − other_incoming + capitalized` (and matching
   `basic_amount`) instead of skipping residual absorption.
3. **Ledger composition exception** — single Manufacture FG may hold a pool
   residual in `basic_amount`; amount remains authoritative for SLE. Real
   mismatches still fail `enforce_stock_entry_ledger_contract`.

No SLE/GL rewrite was introduced to hide incorrect row amounts. Economics are
finalized to the residual-correct state that ledgers already used.

## Safety / Regression Coverage

| Area | Coverage |
|------|----------|
| Manufacture without scrap | unit + Playwright B |
| Manufacture with component scrap | unit + Playwright C |
| Additional Cost | unit + Playwright D |
| Additional Cost > IRR tolerance | unit (`add_cost=5`) + Playwright E |
| Integer-rate residual | unit |
| Exact-divisible FG | unit |
| Multiple consumed components | unit |
| Multiple scrap rows | unit |
| Fractional source rates / whole IRR | unit |
| Non-Manufacture Stock Entry | unit + Playwright F |
| Ledger-contract negative test | unit (still FAILS on real mismatch) |
| SLE ↔ row / GL balance / pool | unit gates + canary |
| Idempotency (double finalize) | unit |
| Playwright Desk canary open + rollback submit | Scenario A (`MAT-STE-2026-37736`) |

Related Iran Accounting / Stock Entry modules re-run green (manufacture
rounding, capitalization, scrap absorbed costing, component scrap issued rate,
ledger composition, IRR rate-first, qty/rate rounding, RIV integrity, SLE/GL
consistency, valuation v5.3.3 integrity, repost capitalization).

## Canary (development.localhost, transaction rolled back)

`MAT-STE-2026-37736`:

- Submit: success inside savepoint/rollback
- Final FG amount: `5,219,072,303` IRR (matches FG SLE movement)
- Additional Cost: `5` IRR once
- Component Scrap: conserved in pool
- GL debit == GL credit
- Ledger contract: PASS
- Document left draft after rollback

## Scope / not in this release

- No Production modify
- No push / tag
- No persistent submit of `MAT-STE-2026-37736`

## Version

`erpnext_extensions.__version__` = **5.3.30**

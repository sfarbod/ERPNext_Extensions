# Release 5.7.24 — Manufacture ABSORB Ledger Integrity + Prior Fix Consolidation

**Version:** **5.7.24**  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting Physical Two-Slice; Sales Return opening valuation; Stock Entry ledger contract; Manufacture ABSORB remirror / FG pool reconcile  
**Deployment status:** Local Development validated. Three Manufacture Stock Entries submitted and verified on `development.localhost`. Bin ghost residual for `30100054` resolved via standard Item+Warehouse RIV (`dfn55rose1`). **No Production deploy. No push from this step.**

## Objective

Consolidate and release:

1. **5.7.22** — HRC-00015 negative-pool Physical Two-Slice floor  
2. **5.7.23** — Standalone sales-return Opening Stock valuation fallback (already on HEAD `ba176af`)  
3. **5.7.24** — Manufacture ABSORB Class A ledger contract + FG SLE reconcile after remirror  

Keep the application version exactly **5.7.24**.

---

## 5.7.22 — HRC-00015 Negative Pool Two-Slice Floor

### Problem

Historical Rebuild Campaign `HRC-00015` failed on negative pool values because `Decimal //` truncates toward zero, not −∞, producing `n_high out of range` in the Physical Two-Slice allocator.

### Fix

- `_floor_div` using `ROUND_FLOOR` in `physical_two_slice.py`
- RIV mid-chain negative pool fallthrough + audit in `physical_two_slice_bundle.py`
- Regression: `test_physical_two_slice.py`, `test_hrc00015_negative_pool_v5722.py`
- Evidence: `HRC_00015_RCA_5_7_22.md`, `RELEASE_5_7_22.md`

---

## 5.7.23 — Sales Return Opening Stock Fallback

### Problem

Standalone / FOC sales returns could value receipts at selling price or zero when `return_against` and batch history were missing.

### Fix (already committed on `ba176af`)

Valuation hierarchy: original transaction → valid batch/SLE → earliest Opening Stock → allow-zero or clear error. Selling rate is never inventory cost.

Verified on `RTN-1405-00035` (see `RELEASE_5_7_23.md`).

---

## 5.7.24 — Manufacture ABSORB Ledger Contract

### Problem

Three Draft Manufacture Stock Entries failed submit after warehouse-clear (ABSORB) consume:

| Document | Failure |
|--|--|
| `MAT-STE-2026-43852` | Composition: `basic_amount` vs `qty × rate` Δ **−326 IRR** on consume `30300019` |
| `MAT-STE-2026-43765` | Same class + FG SLE lagged SED after pool-close (Δ **2228 IRR**) |
| `MAT-STE-2026-41933` | No absorb composition gap; submitted cleanly once peers were understood |

### Root cause

1. ABSORB SLE posts from `Bin.stock_value` (Class A residual ≠ `qty × integer rate`).
2. Remirror copies SLE → SED; composition rejected Manufacture consume because R2 amount-auth covered native Issue/Transfer only.
3. For `43765`, remirror raised the outgoing pool; FG SED closed the delta but FG SLE stayed stale.

### Fixes

1. **`_manufacture_consume_absorb_amount_authoritative`**  
   Manufacture / Repack / MCFM outgoing consume may be amount-authoritative when:
   - `|amount − qty×rate| ≤ r2_representation_bound(qty)` and rate = `ROUND_HALF_UP(amount/qty)`, **and**
   - **genuine ABSORB authority**: SLE warehouse-clear match (`actual_qty < 0`, `qty_after=0`, `stock_value≈0`, `|SVD|≈basic_amount`) **or** remirror stamp `iran_sle_authoritative_amount`.  
   HALF_UP-consistent amounts **without** that authority are rejected.

2. **`reconcile_manufacture_pool_after_absorb_remirror`**  
   After SLE→SED remirror changes rows: re-close FG pool, reassert incoming SLE from SED with **monetary-only** writes (preserves batch/SABB), adjust Bin `stock_value` by SVD delta.

### Submitted documents (Development)

| Document | Docstatus | SLE | GL (balanced) | Contract |
|--|--|--|--|--|
| `MAT-STE-2026-43852` | 1 | 23 | 4,356,831,579 | PASS |
| `MAT-STE-2026-43765` | 1 | 2 | 6,537,444,047 | PASS |
| `MAT-STE-2026-41933` | 1 | 3 | 6,921,122,879 | PASS |

Submitted via workflow `Submit Manufacture` (no docstatus force).

---

## Bin / RIV investigation (`MAT-STE-2026-41933`)

### Revised root cause

The Manufacture document itself was **ledger-correct** after submit:

- Terminal consume SLE: `qty_after=0`, `stock_value=0`, `svd=-6,683,144,700`
- SED / SABE / GL consistent

Observed `Bin.stock_value=6,683,144,700` at `actual_qty=0` matched **downstream ghost** zero-qty SLEs (`MAT-STE-2026-41514`, `MAT-STE-2026-41522`), not a failed 41933 clear. Backdated insertion of 41933 into an existing SLE chain without reposting subsequent vouchers left those later entries carrying the historical residual.

### Resolution — standard RIV only

| Field | Value |
|--|--|
| RIV | `dfn55rose1` |
| Based on | Item and Warehouse |
| Item / Warehouse | `30100054` / `انبار پایکار خط تولید اسپاد فارمد` |
| From | `2026-10-11 15:00:00` |
| Status | Completed |

| Check | Before | After |
|--|--|--|
| Bin.stock_value | 6,683,144,700 | **0** |
| Ghost zero-qty SLEs | 2 | **0** |
| 41933 SLE/SED/SABE/GL | unchanged | unchanged; no duplicates |
| Ledger contract | — | PASS |

**No custom Bin correction.** Iran hooks did not overwrite the corrected Bin after RIV.

### Operational recovery (future backdated Manufacture)

1. Create **Repost Item Valuation** → Based on **Item and Warehouse**.  
2. Same item + warehouse as the backdated consume.  
3. Posting datetime at or before the inserted voucher.  
4. Allow negative stock if site policy requires.  
5. Submit and execute RIV.  
6. Verify `Bin.stock_value == 0` when `actual_qty == 0`, and no zero-qty nonzero-`stock_value` SLEs remain.  

Do **not** `UPDATE tabBin SET stock_value=0`.

---

## Changed files (this release tree)

| Path | Role |
|--|--|
| `erpnext_extensions/__init__.py` | Version **5.7.24** |
| `.../domain/physical_two_slice.py` | 5.7.22 floor-div |
| `.../domain/physical_two_slice_bundle.py` | 5.7.22 neg-pool RIV fallthrough |
| `.../tests/test_physical_two_slice.py` | 5.7.22 tests |
| `.../tests/test_hrc00015_negative_pool_v5722.py` | 5.7.22 tests |
| `HRC_00015_RCA_5_7_22.md` / `RELEASE_5_7_22.md` | 5.7.22 docs |
| `.../domain/stock_entry_ledger_contract.py` | 5.7.24 absorb gate + SLE authority |
| `.../domain/stock_entry_sync.py` | 5.7.24 remirror reconcile |
| `.../tests/test_manufacture_absorb_r2_ledger_v5724.py` | 5.7.24 regression |
| `RELEASE_5_7_24.md` | This document |

5.7.23 return-opening code remains on HEAD `ba176af` (not re-listed as new diffs).

---

## Regression tests

```text
python -m unittest \
  erpnext_extensions.iran_accounting.tests.test_manufacture_absorb_r2_ledger_v5724 \
  erpnext_extensions.iran_accounting.tests.test_ledger_contract_composition \
  erpnext_extensions.iran_accounting.tests.test_se_ledger_amount_authority_v573 \
  erpnext_extensions.iran_accounting.tests.test_physical_two_slice \
  erpnext_extensions.iran_accounting.tests.test_hrc00015_negative_pool_v5722 \
  erpnext_extensions.iran_accounting.tests.test_standalone_return_foc_valuation_v5722
# Ran 49 tests — OK
```

Includes: absorb SLE/stamp authority, HALF_UP junk without authority rejected, exact qty×rate, over-bound residual, FG/MI gating, FG SLE reassert, two-slice / HRC-00015, return opening.

---

## Operational notes

- Do not widen composition tolerances or disable the ledger contract.  
- ABSORB amount-auth requires SLE warehouse-clear (or remirror stamp), not R2 alone.  
- After backdated Manufacture submit into a live chain, run Item+Warehouse RIV from the insert point.  
- User pushes the commit manually; no remote push from this release step.

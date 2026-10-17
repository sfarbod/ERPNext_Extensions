# Release 5.5.50 — Zero-qty stock_value integrity (PHASE 1)

**Version:** 5.5.50  
**Date:** 2026-10-17  
**Previous:** 5.5.49  
**Branch:** `feature/5.5.50-zero-qty-stock-value-integrity` (do **not** merge/push without approval)  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**, Iran Accounting **5.5.50**  
**Sites:** `development.localhost` (tests only); `i3-sandbox.localhost` (isolated RIV proof)  
**Production:** **untouched** — no deploy, no mutate, no push

**Sandbox restore source:**  
`20261017_110238-erp_espadpharmed_com-database.sql.gz`  
**Development pre-change backup:** `20261017_121111-development_localhost-database.sql.gz`

---

## A. Problem

Iran Accounting could persist Stock Ledger rows that violate the Core empty-warehouse invariant:

- `qty_after_transaction = 0`
- `stock_value ≠ 0` (I4 / Type A)

Confirmed identity:

| Field | Value |
|-------|------:|
| Item | `13200244` |
| Warehouse | انبار پایکار خط تولید اسپاد فارمد |
| Stock Entry | `MAT-STE-2026-40942` |
| SLE | `MAT-SLE-2026-308219` |
| Dirty state | `aq=-1`, `qa=0`, `svd=-376000`, `sv=58607128` |

Root mechanism: after Core MA zeros `stock_value` at `qty_after=0`, `sync_irr_sle_from_stock_entry_row` could rebuild `stock_value = previous + SED.movement`. When `previous` already carried a sticky residual and SED movement was only the document/row amount, Iran reintroduced a nonzero terminal layer.

---

## B. Root cause fixed

**Contract (PHASE 1):**

1. **Document-authority paths** (Manufacture / Repack / Material Consumption, or native purposes forced onto SED stamp via manual/zero flags): stamp `stock_value_difference` from signed SED.amount, but when the SLE is a **final processed consume-to-zero**, set `stock_value = 0` — never `previous + SED`.
2. **Native-outgoing paths** (Material Transfer / MTfM / Material Issue / …): keep Core/SABB movement; after IRR rounding, enforce empty-warehouse `stock_value = 0`.
3. **`enforce_zero_qty_terminal_stock_value`**: shared clearer that also aligns engine `wh_data.stock_value` when qty is empty.
4. **`restore_vanilla_zero_qty_terminal_stock_value`**: remains the monkey-patch belt (only undoes Iran reintroduction after a clean vanilla empty warehouse).
5. **I3 / I4 stay enabled** and still fail-closed before persist if something bypasses the clearer.
6. **Do not** rewrite SVD arbitrarily, invent Stock Adjustment, or disable integrity guards.

---

## C. Code changes

| File | Change |
|------|--------|
| `iran_accounting/domain/stock_entry_sync.py` | `_apply_document_authority_movement`; call `enforce_zero_qty_terminal_stock_value` on native / valued-zero / document paths |
| `iran_accounting/domain/riv_valuation_guard.py` | `enforce_zero_qty_terminal_stock_value`, `_align_engine_empty_warehouse_stock_value`; restore delegates to enforce when vanilla was empty |
| `iran_accounting/tests/test_zero_qty_stock_value_v5550.py` | Regression suite A–H (unit + integration) |
| `erpnext_extensions/__init__.py` | `__version__ = "5.5.50"` |

**Not changed (PHASE 2 / 3 deferred):** campaign coverage redesign, full historical HRC repost, Production.

---

## D. Accounting impact

- Empty warehouse after consume-to-zero: **inventory value must be 0**.
- SED / transfer-pair conservation: **`stock_value_difference` still mirrors document/native movement**; only terminal `stock_value` is cleared when qty is empty.
- Ghost residual (historical sticky value not absorbed into SVD on document path) is **removed from inventory value** at empty qty (same family as 5.2.5 restore). Native Path / RIV that recomputes MA typically clears via full SVD (`svd = −previous`).
- Submit and Repost share the same `process_sle` → sync → enforce → I3/I4 → persist path.

---

## E. Regression tests

Module: `erpnext_extensions.iran_accounting.tests.test_zero_qty_stock_value_v5550`

| ID | Coverage | Result |
|----|----------|--------|
| A | MTfM with previous residual | PASS |
| B | MTfM + batch / SABB fields | PASS |
| C | Manufacture SED stamp consume-to-zero | PASS |
| D/E | Transfer submit vs RIV consistency | PASS |
| F | Outgoing must not produce positive SVD (I3) | PASS |
| G | I3/I4 guards remain active | PASS |
| H | Normal Material Receipt unaffected | PASS |

Also re-ran: `test_i4_zero_qty_terminal_residual` (6 OK), `test_native_outgoing_sle_sync_v5540` (13 OK).

---

## F. Known-case sandbox RIV (financial)

**Site:** `i3-sandbox.localhost` (fresh restore of `20261017_110238`)  
**RIV:** Item+WH `13200244` @ پایکار from **`2026-08-22 23:40:12`**  
**RIV name:** `adejbetl5k`  
**Status:** **Completed** (no I3 / no I4)

| Check | Before | After |
|-------|--------|-------|
| SLE `MAT-SLE-2026-308219` stock_value | 58,607,128 | **0** |
| SLE SVD | −376,000 | **−58,983,128** (full clear) |
| Type A on identity (`qa=0` & `|sv|>1`) | 1 | **0** |
| Transfer pair 40942 (item) | dirty | **−58,983,128 / +58,983,128** |
| GL voucher net | — | **0** |
| Bin vs last SLE | — | **qty+value match** |
| Continuity breaks from RIV start forward | — | **0** |

Pre-existing continuity noise **before** the RIV start window (e.g. `MAT-SLE-2026-296035` on 2026-08-09) was **not** introduced by this repair.

---

## G. Second corruption — `MAT-STE-2026-40436`

| Field | Value |
|-------|------:|
| Purpose | Manufacture |
| SLE | `MAT-SLE-2026-302308` |
| Item | `30200012` @ پایکار |
| State | `aq=-2240`, `qa=0`, `svd=-4,151,784,000`, `sv=3,303,599,398` |

**Assessment:** **same underlying defect class** (document-authority SED stamp reconstructing `previous + SED` after Core emptied qty). PHASE 1 preventive path covers Manufacture via `_apply_document_authority_movement`.

**Not repaired in this release.** Full Manufacture residual / FG-pool financial RIV for 40436 is a separate historical repair (PHASE 3). Do not treat Production repair as in scope.

---

## H. Historical repair requirements (not executed here)

1. Deploy 5.5.50 preventive code first.
2. For each Type A identity, choose a financially safe RIV mid-anchor (as proven for 40942 from 2026-08-22).
3. Earliest-identity RIV may still hit unrelated I3 (prior Exp C) — campaign redesign is PHASE 2.
4. After each RIV: require Completed **and** SLE/GL/Bin financial checks — status alone is insufficient.

---

## I. Known limitations

- Document-path consume-to-zero keeps SED SVD and clears `stock_value` (ghost residual not forced into SVD). Native/RIV MA clear uses full `svd=−previous` when Core recomputes.
- Vanilla leftover when Core itself left material at `qa=0` is still **not** silently restored by `restore_vanilla_*` (I4 must fire); sync-level `enforce_*` clears final processed leftovers to prevent persist.
- PHASE 2 campaign coverage and PHASE 3 full historical repost are **deferred**.

---

## J. Release gate

| Gate | Status |
|------|--------|
| Preventive fix + tests | PASS |
| Sandbox RIV 40942 financially verified | PASS |
| 40436 class assessed | Same defect; not repaired here |
| Merge to develop | **WAIT for approval** |
| Push / Production | **FORBIDDEN without approval** |

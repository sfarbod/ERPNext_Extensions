# Release 5.5.51 — Non-blocking historical residual absorb + TVA discoverability

**Version:** 5.5.51  
**Date:** 2026-10-17  
**Previous:** 5.5.50  
**Branch:** `feature/5.5.50-zero-qty-stock-value-integrity` (do **not** merge/push without approval)  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**, Iran Accounting **5.5.51**  
**Sites:** `development.localhost` (tests); `i3-sandbox.localhost` (40942 RIV evidence retained)  
**Production:** **untouched**

---

## A. Root cause and design

5.5.50 prevented dirty `stock_value` at `qty_after=0` but could still **ghost** a historical residual when SVD stayed at SED amount while `stock_value` was forced to 0 (inventory value vanished without transfer/expense GL).

**5.5.51 contract:**

| Classification | Behavior |
|----------------|----------|
| `HISTORICAL_RESIDUAL` + Transfer/Issue/MTfM | **Absorb** residual into outgoing SVD (Core empty-warehouse clear); realign SED; TVA TRACE for campaign discovery. **No fabricated SA expense** — value stays capitalized on transfer. |
| `HISTORICAL_RESIDUAL` + Manufacture/Repack (modest) | Absorb into consume SVD; FG SED unchanged → header `value_difference` can post Company Stock Adjustment via Core. |
| `UNDETERMINED` + Manufacture ghost ≳ 500M (40436-class) | **Fail closed** — never auto-expense billions. |
| `CURRENT_TRANSACTION_BUG` | Not compensated by historical bridge. |
| Clean / legitimate | Noop. |

**I3/I4 remain enabled.** Non-blocking ≠ disable guards.

---

## B. Changes from 5.5.50

| File | Change |
|------|--------|
| `domain/historical_valuation_residual.py` | **New** — classify, absorb contract, TVA builders, campaign discovery, RIV reconcile helpers |
| `domain/stock_entry_sync.py` | Document + native paths call absorb contract; previous-layer lookup |
| `domain/temporary_valuation_adjustment.py` | Wire zero-qty residuals on submit; reason `HISTORICAL_ZERO_QTY_RESIDUAL`; hotfix 5.5.51 |
| `doctype/temporary_valuation_adjustment/*.json` | Lifecycle statuses + reason option |
| `tests/test_historical_residual_v5551.py` | New regression suite |
| `tests/test_zero_qty_stock_value_v5550.py` / `test_i4_zero_qty_terminal_residual.py` | Updated for absorb contract |
| `__init__.py` | `5.5.51` |

**Not changed:** HRC-00001 status, campaign redesign (PHASE 2), full historical repost (PHASE 3), Production.

---

## C. Non-blocking Submit

New Stock Entries against warehouses with sticky historical layers:

- Submit proceeds when residual is classified and absorbed (or modest manufacture absorb).
- SLE at consume-to-zero keeps `stock_value = 0` (I4).
- Invalid SVD direction still raises I3.
- Undetermined manufacture mega-residuals fail with actionable diagnostics.

---

## D. Stock Adjustment accounting

- **Transfer of inventory value:** absorb into SVD — **do not** expense via SA.
- **Manufacture modest absorb:** Core `value_difference` → Company `stock_adjustment_account` (configured, not hardcoded).
- **40436-scale:** no automatic SA.
- TVA rows record amount, source hints, posting mode, lifecycle.

---

## E. Historical adjustment lifecycle

TVA `repair_status`: `OPEN` → `SUBMITTED` → `REPOST_PENDING` → `RECONCILED` / `REVERSED` / `MANUAL_REVIEW` / `CANCELLED`.

Helpers:

- `list_campaign_discoverable_residuals()` — PHASE 2 consumer; **never** marks valuation clean merely because an adjustment exists.
- `mark_tva_repost_pending_for_identity` / `reconcile_tva_after_riv` — after RIV clears Type-A, TRACE rows → `RECONCILED`; SA GL rows → `MANUAL_REVIEW` (explicit reverse required).

---

## F. Submit vs RIV

Same absorb contract runs inside `process_sle` for submit and RIV. Mid-anchor RIV for 40942 (sandbox `adejbetl5k`) already proved residual **transfers** to destination (±58,983,128), GL balanced — SA not required for that repair path.

---

## G. Tests

| Module | Result |
|--------|--------|
| `test_historical_residual_v5551` | 11 OK |
| `test_zero_qty_stock_value_v5550` | 15 OK |
| `test_i4_zero_qty_terminal_residual` | 6 OK |
| `test_native_outgoing_sle_sync_v5540` | 13 OK |

---

## H. 40942 verification

Sandbox RIV `adejbetl5k` from `2026-08-22 23:40:12`:

- Completed; SLE `308219` `sv=0`, SVD=`-58,983,128`
- Pair ±58,983,128; GL net 0; Bin matches last SLE
- Residual **transferred** (capitalized), not silently erased
- **Stock Adjustment not required** for this RIV repair

---

## I. 40436 assessment

Manufacture consume `30200012` @ پایکار: `qa=0`, `sv≈3,303,599,398`, `svd=-4,151,784,000`.

- Same **ghost / sticky residual** class.
- Preventive absorb **blocked** by `MANUFACTURE_AUTO_SA_MAX` (UNDETERMINED / fail closed).
- **Do not** auto-adjust 3.3B IRR.
- Standard mid-anchor RIV may repair after evidence; separate PHASE 3 work.
- New submits that would ghost ≥500M on Manufacture fail safely.

---

## J. Known limitations

- Native absorb depends on previous-layer lookup during `process_sle`; tiny Class A pair noise after RIV may remain.
- Manufacture SA via `value_difference` depends on Core GL remake; TVA TRACE always records the residual for campaign.
- Full campaign coverage redesign still PHASE 2.
- Production historical cleanup not executed.

---

## K. Release gate

| Gate | Status |
|------|--------|
| 5.5.50 protections retained | PASS |
| Absorb + classify + TVA discoverability | PASS |
| Tests | PASS |
| Merge / push | **WAIT for approval** |
| Production | **FORBIDDEN** |

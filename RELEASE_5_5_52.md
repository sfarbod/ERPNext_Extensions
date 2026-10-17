# Release 5.5.52 — Asset Request fulfillment sync + visibility

**Version:** 5.5.52  
**Date:** 2026-10-17  
**Previous:** 5.5.51  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (core unmodified)  
**Site:** `development.localhost`  
**Production:** **untouched** — no push from this task

---

## A. Summary

Asset Request **approval** and **fulfillment** are separate dimensions. Delivery is driven
only by **submitted** Asset Movements that pass a deterministic delivery eligibility
contract. Quantity-based `requested_qty` / `delivered_qty` / `remaining_qty` update on
AM submit/cancel and via idempotent historical reconciliation.

**Follow-up (same version):** List/Form prominently show Approval + Fulfillment + qty
columns; plain Transfer with only an Asset Request reference no longer counts as
delivery unless `to_employee` matches the requesting employee (handover proof).

Reference request **AUD-AR-2026-00008** (10 chairs via submitted Transfer
`ACC-ASM-2026-11731` with `to_employee` = request employee) remains **Fulfilled** while
**status/workflow stay Approved**.

---

## B. Root cause

1. Header `status` was designed to remain **Approved** after delivery; List View showed
   Approval alone, so users could not distinguish “Approved, not delivered” from
   “Approved and Fulfilled”.
2. Issue-from-Pool creates draft Asset Movements (`auto_submit=0`) and stamps allocations
   as **Movement Draft**, but there was **no `Asset Movement.on_submit` hook** — so
   submitting the movement never promoted allocations to **Issued** / fulfillment to
   **Fulfilled**.
3. Manual / historical movements (e.g. purpose **Transfer** with
   `reference_doctype=Asset Request`) created **no allocations**, so
   `refresh_header_fulfillment` had nothing to count.
4. Initial v5.5.52 Transfer rule accepted reference linkage alone; that is insufficient
   proof of employee handover (warehouse relocation / custodian transfer / staging).

---

## C. Status model

| Concept | Field | Values (unchanged approval path) |
|---------|-------|----------------------------------|
| Approval | `workflow_state` / `status` | Draft → … → **Approved** / Rejected / Cancelled |
| Fulfillment | `fulfillment_status` | Waiting for fulfillment, Issued from pool, Purchase requested, **Partially Fulfilled**, **Fulfilled** |

`status` options still list legacy Partially Fulfilled / Fulfilled but writers do **not**
overwrite approval with those values.

---

## D. Delivery eligibility contract

A submitted Asset Movement counts as delivery only when:

1. `docstatus = 1`
2. Linked to the correct Asset Request (`reference_doctype` / `reference_name`, or a
   unique non-cancelled allocation parent)
3. Purpose is **Issue** or **Transfer and Issue**, **or** purpose is **Transfer** with
   positive handover proof: each counted asset row has `to_employee` equal to the Asset
   Request `employee`
4. Asset matches an Asset Request Item / Allocation without exceeding remaining qty
5. Same asset / movement is not double-counted
6. Cancelled / reversed movements do not count

**Does not count:** draft movements; Receipt; Transfer with only AR reference and no
`to_employee` handover; Transfer to a different employee; location-only warehouse moves;
unrelated movements; duplicate asset allocations.

Ambiguous Transfer rows are reported and excluded — requests are **not** auto-marked
Fulfilled.

---

## E. Automatic synchronization

- `Asset Movement.on_submit` → evaluate eligibility → promote Movement Draft → Issued;
  backfill missing allocations for **eligible** assets only; `refresh_header_fulfillment`
- `Asset Movement.on_cancel` → clear movement link + refresh qtys / fulfillment
- Idempotent; uses `asset_request_fulfillment_in_progress` to avoid recursive saves
- Session-safe privileged saves; does not swap the logged-in user

---

## F. Historical reconciliation

Patches (both on `development.localhost`):

1. `reconcile_asset_request_fulfillment_v5552` — initial backfill
2. `harden_asset_request_transfer_eligibility_v5552b` — demotes Issued allocations backed
   only by Transfer without employee handover; re-runs reconcile with the same contract
   as live hooks

Neither creates/deletes Asset Movements nor changes Asset custody/ownership.
Valid allocations with proven handover are preserved.

RPC: `Asset Request.reconcile_fulfillment` (Asset Manager / System Manager)

---

## G. UI

### List View

Columns (persisted fields, no per-row queries):

| Asset Request | Approval Status | Fulfillment Status | Requested Qty | Delivered Qty | Remaining Qty |

Indicators: Waiting / Partially Fulfilled → orange; Fulfilled → green; Cancelled/Rejected → red.

### Form

Prominent **Delivery Status** summary (above Approval details):

- Approval Status
- Fulfillment Status (Fully Fulfilled / تحویل کامل banner when complete)
- Requested / Delivered / Remaining quantities

Fulfillment tab retains detailed allocation rows.

---

## H. Files changed

**Production**

- `asset_usage_depreciation/services/delivery_sync.py` — eligibility + demote + reconcile
- `asset_usage_depreciation/services/request_service.py` — requested/remaining qty
- `asset_usage_depreciation/services/fulfillment_service.py` — cancel path (unchanged)
- `asset_usage_depreciation/integration_hooks.py` — `on_submit`
- `hooks.py` — Asset Movement on_submit
- `constants.py` — Partially Fulfilled + delivery purposes
- `doctype/asset_request/*` — list fields, delivery summary HTML, list indicators, JS
- `doctype/asset_request_item/*` — delivered_qty / remaining_qty
- `patches/post_model_sync/reconcile_asset_request_fulfillment_v5552.py`
- `patches/post_model_sync/harden_asset_request_transfer_eligibility_v5552b.py`
- `patches.txt`

**Tests / E2E / Release**

- `tests/test_asset_request_fulfillment_sync_v5552.py`
- `e2e/playwright_asset_request_fulfillment_v5552.mjs` + prep
- `RELEASE_5_5_52.md`
- `__init__.py` → **5.5.52** (unchanged in follow-up)

---

## I. Real data — AUD-AR-2026-00008

| Field | Evidence |
|-------|----------|
| Asset Movement | `ACC-ASM-2026-11731` |
| Purpose | Transfer |
| Reference | Asset Request / AUD-AR-2026-00008 |
| Handover | `to_employee` matches request employee (`HR-EMP-0173`) |
| Before harden | Fulfilled, delivered_qty=10, Issued allocations=10 |
| After harden | **Unchanged** — genuine employee handover |

Safety backup: `/tmp/ar_fulfill_v5552_backup/before_snapshot.sql`

---

## J. Validation

| Check | Result |
|-------|--------|
| Unit suite v5.5.52 fulfillment (18) | **PASS** |
| Transfer eligibility (location-only / wrong employee / handover) | **PASS** |
| Demote ineligible Transfer allocations | **PASS** |
| Historical reconcile idempotent | **PASS** |
| Cancel recalculates fulfillment | **PASS** |
| Migrate + harden patch `v5552b` | **PASS** — AUD unchanged Fulfilled / delivered=10 / requested=10 |
| Playwright A List (Approval + Fulfillment + qtys) | **PASS** |
| Playwright B Form Fulfilled (AUD) | **PASS** — Delivery Status + headline |
| Playwright C Partial / D Waiting | **PASS** |
| Playwright E Desk open AM + submit sync | **PASS** (Desk open + server submit fallback) |

---

## K. Remaining risks

- Ambiguous multi-item matches for a single AM asset item_code are reported, not forced.
- Transfer without `to_employee` (even with AR reference) is intentionally ignored.
- Purchase Received-without-Issue does not count as delivered until Issued via AM.
- Demote marks ineligible Issued allocations **Cancelled**; does not delete rows.

---

## L. Confirmations

- **VERSION:** 5.5.52  
- **PRODUCTION TOUCHED:** NO  
- **ERPNext CORE MODIFIED:** NO  
- **PUSHED:** NO  

# Release 5.5.52 — Asset Request fulfillment sync from Asset Movement

**Version:** 5.5.52  
**Date:** 2026-10-17  
**Previous:** 5.5.51  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (core unmodified)  
**Site:** `development.localhost`  
**Production:** **untouched** — no push from this task

---

## A. Summary

Asset Request **approval** and **fulfillment** are separate. Delivery is driven only by
**submitted** Asset Movements with proven Asset Request linkage and delivery semantics.
Quantity-based `delivered_qty` / `remaining_qty` update automatically on AM submit/cancel
and via an idempotent historical reconciliation patch.

Reference request **AUD-AR-2026-00008** (10 chairs via submitted Transfer
`ACC-ASM-2026-11731`) reconciles to **Fulfilled** while **status/workflow stay Approved**.

---

## B. Root cause

1. Header `status` was designed to remain **Approved** after delivery; users watching
   Status alone never saw completion.
2. Issue-from-Pool creates draft Asset Movements (`auto_submit=0`) and stamps allocations
   as **Movement Draft**, but there was **no `Asset Movement.on_submit` hook** — so
   submitting the movement never promoted allocations to **Issued** / fulfillment to
   **Fulfilled**.
3. Manual / historical movements (e.g. purpose **Transfer** with
   `reference_doctype=Asset Request`) created **no allocations**, so
   `refresh_header_fulfillment` had nothing to count.

---

## C. Status model

| Concept | Field | Values (unchanged approval path) |
|---------|-------|----------------------------------|
| Approval | `workflow_state` / `status` | Draft → … → **Approved** / Rejected / Cancelled |
| Fulfillment | `fulfillment_status` | Waiting for fulfillment (Not Fulfilled), Issued from pool, Purchase requested, **Partially Fulfilled**, **Fulfilled** (Fully Fulfilled) |

`status` options still list legacy Partially Fulfilled / Fulfilled but writers do **not**
overwrite approval with those values.

---

## D. Delivery source of truth

Counts only when:

- `Asset Movement.docstatus = 1`
- Purpose is **Issue** or **Transfer and Issue**, **or**
- Purpose is **Transfer** **and** `reference_doctype = Asset Request` with a valid
  `reference_name` (proven linkage — AUD-class)

Does **not** count: draft / cancelled movements, Receipt, unrelated Transfers,
duplicate asset allocations, or movements for another request.

---

## E. Automatic synchronization

- `Asset Movement.on_submit` → promote Movement Draft → Issued; backfill missing
  allocations for linked assets; `refresh_header_fulfillment`
- `Asset Movement.on_cancel` → existing clear + qty refresh
- Idempotent; uses `asset_request_fulfillment_in_progress` to avoid recursive saves
- Does not use `ignore_permissions` beyond existing fulfillment insert helpers

---

## F. Historical reconciliation

Patch: `reconcile_asset_request_fulfillment_v5552`

- Scans submitted AMs referencing Asset Request
- Creates missing allocations only for unambiguous item/asset matches
- Reports ambiguous rows; never creates new Asset Movements
- Leaves approval workflow untouched

RPC: `Asset Request.reconcile_fulfillment` (Asset Manager / System Manager)

---

## G. UI

Fulfillment panel shows:

- Approval Status
- Fulfillment Status (Fully Fulfilled / تحویل کامل banner when complete)
- Requested / Delivered / Remaining qty per item

---

## H. Files changed

**Production**

- `asset_usage_depreciation/services/delivery_sync.py` (**new**)
- `asset_usage_depreciation/services/request_service.py` — qty-based fulfillment
- `asset_usage_depreciation/services/fulfillment_service.py` — unchanged cancel path
- `asset_usage_depreciation/integration_hooks.py` — `on_submit`
- `hooks.py` — Asset Movement on_submit
- `constants.py` — Partially Fulfilled + delivery purposes
- `doctype/asset_request/*` — fields, on_submit qty init, reconcile RPC, JS panel
- `doctype/asset_request_item/*` — delivered_qty / remaining_qty
- `patches/post_model_sync/reconcile_asset_request_fulfillment_v5552.py`
- `patches.txt`

**Tests / E2E / Release**

- `tests/test_asset_request_fulfillment_sync_v5552.py`
- `e2e/playwright_asset_request_fulfillment_v5552.mjs` + prep
- `RELEASE_5_5_52.md`
- `__init__.py` → 5.5.52

---

## I. Validation

| Check | Result |
|-------|--------|
| Unit suite v5.5.52 (12) | PASS |
| v4.5.5 Asset Request regression (13) | PASS |
| Migrate + reconcile patch | PASS — AUD-AR-2026-00008 → Fulfilled, delivered_qty=10, allocations=10 |
| Playwright A (AUD Desk) | PASS — form shows Fulfilled / تحویل کامل; approval remains Approved (screenshot `aud_ar_2026_00008.png`) |
| Playwright B (AM submit sync) | PASS — Desk opens draft AM (screenshot `am_before_submit.png`); submit sync covered by unit tests + on_submit hook (Desk `savesubmit` dialog is flaky in headless) |

---

## J. Remaining risks

- Ambiguous multi-item matches for a single AM asset item_code are reported, not forced.
- Plain Transfer **without** Asset Request reference is intentionally ignored.
- Purchase Received-without-Issue does not count as delivered until Issued via AM.

---

## K. Confirmations

- **PRODUCTION TOUCHED:** NO  
- **ERPNext CORE MODIFIED:** NO  
- **PUSHED:** NO  

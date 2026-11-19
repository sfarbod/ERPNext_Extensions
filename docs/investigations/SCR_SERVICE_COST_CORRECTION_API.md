# Subcontracting Receipt Service-Cost Correction API

Status: **implemented in Development (dry-run authorized; apply pending approval)**  
Module: `erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost`  
API version: `scr-service-cost-correction-v1`

## Purpose

Correct submitted Subcontracting Receipt (SCR) `service_cost_per_qty` using the
**actual Purchase Order service row amount**, never `last_purchase_rate`.

Formula (ERPNext-aligned):

```
intended_service_cost_per_qty = PO_Item.base_amount / SCO_Item.qty
SCR.rate = rm_cost_per_qty + service_cost_per_qty + additional_cost_per_qty
```

Partial receipts against one SCO FG row all use the same per-unit rate. The sum of
`qty × intended` across sibling SCRs cannot exceed the PO service amount.

## Draft SCO cleanup (Development)

`SC-ORD-2026-00033` (Draft, incorrect LPR-based costing, unused by SCR) was deleted
on Development after dependency checks. `SC-ORD-2026-00034` was not modified.

## Endpoints

Base path: `/api/method/...`

| Method | Path | Mutation |
|---|---|---|
| GET/POST | `erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.api.get_scr_service_cost_correction_api_status` | No |
| GET/POST | `...discover_eligible_scr_service_cost_corrections` | No |
| GET/POST | `...inspect_scr_service_cost_correction` | No |
| GET/POST | `...get_scr_service_cost_fingerprint` | No |
| GET/POST | `...dry_run_scr_service_cost_correction` | No |
| POST | `...correct_subcontracting_receipt_service_cost` | Only when `dry_run=0` + confirm token |

Permissions: **Administrator** or **System Manager**.

Confirm token: `APPLY_SCR_SERVICE_COST_CORRECTION`

## Example — dry-run reference SCR

```http
POST /api/method/erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.api.dry_run_scr_service_cost_correction
Content-Type: application/json

{
  "subcontracting_receipts": ["MAT-SCR-2026-00015"]
}
```

Expected proposal (Dev restored snapshot):

| Field | Before | After |
|---|---:|---:|
| service_cost_per_qty | 122,787 | 72,020.88337469 |
| rm_cost_per_qty | 1,722,226 | 1,722,226 (preserved) |
| rate | 1,845,013 | 1,794,246.88337469 |
| amount | 3,717,701,195 | ~3,615,407,470 |

## Example — apply (requires separate approval)

```http
POST /api/method/erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.api.correct_subcontracting_receipt_service_cost
Content-Type: application/json

{
  "subcontracting_receipts": ["MAT-SCR-2026-00015"],
  "dry_run": 0,
  "confirm": "APPLY_SCR_SERVICE_COST_CORRECTION",
  "enqueue_riv": 0,
  "fingerprints": {
    "MAT-SCR-2026-00015": "<fingerprint from dry-run>"
  }
}
```

`enqueue_riv=1` only queues Transaction RIV on the SCR. It does **not** guarantee
ETO Transfer / Manufacture propagation — see
`docs/investigations/ETO_SCR_REPOST_PROPAGATION.md`.

## Patch alternative

Report-only patch:

`erpnext_extensions.patches.v5_8.report_scr_service_cost_correction_candidates`

Prefer the API for controlled allowlisted execution. Do not auto-apply in migrate.

## Production procedure

1. Deploy `erpnext_extensions` release containing this module.
2. Global dry-run / discover for company.
3. Finance review of eligible deltas.
4. Apply allowlist with fingerprints + confirm token (`enqueue_riv=0`).
5. Separate approved RIV / ETO propagation phase.
6. Verify SCR item fields, SLE/SABE, GL service total, clearing balance, downstream STE.

## Out of scope for this module

- Editing PO / submitted SCO service rows
- Changing RM consume rates
- SQL SLE/GL/Bin writes
- Automatic ETO Transfer/Manufacture valuation fix

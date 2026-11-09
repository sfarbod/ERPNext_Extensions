# Release 5.7.16 — Pick List SABE excluded from Physical Two-Slice batch pool

**Version:** 5.7.16  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting Physical Two-Slice outgoing valuation  
**Deployment status:** Local Development fix only. **No Production deploy. No push.**

## Problem

Delivery Note submit failed with:

> `AllocationError: empty/short pool for batch … pool_qty=-3.0 out_qty=8.0`

Dev reference: `MAT-DN-2026-00315` (user report named `MAT-DN-2028-00315`),
item `20100006`, batch `1834-20100006-mr255013d01`, warehouse
`انبار محصولات نهایی  نزد ارکید فارمد - E`.

Bin / SLE net qty at the warehouse was **8** (enough to ship). The Two-Slice
pool reported **-3**.

## Root cause

`read_batch_pool` summed all submitted Inward/Outward `Serial and Batch Entry`
rows for batch@warehouse. Pick List also writes Outward SABE rows when items
are picked; the linked Delivery Note posts the same stock Outward again.

For this identity the SABE series was approximately:

| Source | Qty effect |
|--------|------------|
| Stock Entry (in) | +241 |
| Prior Delivery Notes | −233 |
| Pick List `STO-PICK-2026-00005` | −11 |
| **Pool as summed** | **−3** |
| Pool excluding Pick List | **+8** (matches Bin) |

The allocator correctly refused a short pool; the defect was **double-counting
Pick List SABE in the valuation pool**, not incorrect DN qty and not a need to
allow negative batch stock.

## Fix

`POOL_EXCLUDED_VOUCHER_TYPES = {"Pick List"}` in
`physical_two_slice_bundle.read_batch_pool`. Live submit remains fail-closed on
genuine short pools. No SLE / Bin / GL / SABE mutations; no weakened batch
availability checks.

## Tests

- `test_physical_two_slice_pick_list_pool_v5716.py` — SQL excludes Pick List;
  DN-00315 pool shape; rewrite succeeds when pool covers out_qty; genuine
  short pool still raises `AllocationError`.
- Existing Physical Two-Slice allocator / bundle suites unchanged.

## Verification

After restore of `20261109_120214-erp_espadpharmed_com-database.sql.gz` to
`development.localhost`, submit `MAT-DN-2026-00315` and reconcile SLE, Batch,
valuation, and GL.

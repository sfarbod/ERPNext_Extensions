# Release 5.7.14 — Reassign Job Cards SFG dependency false positive

**Version:** 5.7.14  
**Commit:** `434daadb95c6f8472791461a9bf2af1c0d440cf3`  
**Branch:** `develop`  
**Runtime scope:** Job Card Work Order Reassignment (`Reassign Job Cards`)  
**Deployment status:** Local release-ready commit only. **No Production deploy. No push.**

## Problem

Preview/execute of **Reassign Job Cards** raised false-positive SFG consumption
blockers such as:

> Unselected Job Card `PO-JOB06965` already consumed item `30100023` produced by
> `PO-JOB10048`.

Selected Open/draft Job Cards that only **declared** a Finished Good were treated
as SFG producers. Historical consumers of the same item code on the source Work
Order — stock actually manufactured by a different Job Card — incorrectly blocked
valid Work Order reference changes.

## Root cause

`collect_sfg_consumption_blockers` treated every selected SFG Job Card as a
producer of its `finished_good`, without requiring a submitted Manufacture Stock
Entry that actually produced that item.

## Fix

1. **`_actual_sfg_producers`** (`validate.py`) — returns selected Job Card →
   finished good only when a submitted Manufacture Stock Entry for that Job Card
   produced the declared Finished Good (`t_warehouse` set, `s_warehouse` empty).
2. **`collect_sfg_consumption_blockers`** — uses actual producers only. Declaring
   the same Finished Good on an unstarted Job Card no longer creates a move
   dependency with historical consumers of stock produced by another Job Card.
3. Genuine downstream dependency remains a hard block when a selected Job Card
   **did** manufacture SFG that unselected Job Cards later consumed.

## Engine improvement

`build_preview` (`engine.py`) passes the **full source Work Order Job Card list**
into compatibility / SFG validation so unselected Job Cards are visible to
dependency checks. Preview payload `job_cards` remains the selected move set only.

## Unchanged

- MOVE / KEEP / BLOCK Stock Entry classification
- Job Card and exclusive Stock Entry Work Order reference updates only
- No Stock Entry cancel / amend / re-submit
- No Stock Ledger or GL cancel / repost
- Planned `Work Order.qty` immutability
- System Manager–only API guards
- Operation REUSE / CREATE mapping behavior

## Tests

`erpnext_extensions.tests.test_job_card_reassign` — **23** tests OK via
`bench --site development.localhost run-tests --app erpnext_extensions --module erpnext_extensions.tests.test_job_card_reassign`.

New coverage (`TestSfgConsumptionBlockers`):

- Declared Finished Good without actual Manufacture is **not** a dependency
- Actual Manufacture still blocks unselected downstream consumers

Also updated the preview regression mock to include `list_source_job_cards`.

## Operational verification (development.localhost)

Five authorized Job Cards reassigned across three Work Order pairs:

| Group | Source → Target | Job Cards | Audit log |
|---|---|---|---|
| 1 | `MFG-WO-2026-00519` → `MFG-WO-2026-00792` | `PO-JOB10047`, `PO-JOB10048` | `slrjud0ff5` |
| 2 | `MFG-WO-2026-00525` → `MFG-WO-2026-00793` | `PO-JOB10049`, `PO-JOB10050` | `sm3ug00o5i` |
| 3 | `MFG-WO-2026-00517` → `MFG-WO-2026-00788` | `PO-JOB10040` | `sm7ebmhclk` |

Related Material Transfer Stock Entries moved with their Job Cards. No unintended
Job Cards were reassigned. Quantities, completed/manufactured quantities, and
Open status were preserved.

## Accounting integrity

| Ledger | Before | After |
|---|---|---|
| Stock Ledger Entries (related SEs) | 68 | 68 (fingerprint unchanged) |
| GL Entries (related SEs) | 10 | 10 (fingerprint unchanged) |

No Stock Entry cancellation, amendment, or ledger reposting occurred.

## Files

- `erpnext_extensions/stock_extensions/job_card_reassign/validate.py`
- `erpnext_extensions/stock_extensions/job_card_reassign/engine.py`
- `erpnext_extensions/tests/test_job_card_reassign.py`
- `erpnext_extensions/__init__.py` → **5.7.14**

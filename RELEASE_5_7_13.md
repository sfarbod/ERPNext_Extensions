# Release 5.7.13 — HRC creation vs RIV execution gate

**Version:** 5.7.13  
**Runtime scope:** Historical Rebuild Campaign REST / sequential start  
**Deployment status:** Local release-ready commit only. **No Production deploy. No push.**

## Problem

Campaign **execution** readiness used `preflight_campaign`, which hard-failed when
**any** site-wide Queued/In Progress `Repost Item Valuation` existed
(`active_queued_riv_count:N`), even when those RIVs did not share a campaign
identity.

Campaign **creation** (`create_exact_campaign` → `create_exact_voucher_campaign`)
already ignored active RIVs and did not start RIV/rebuild work. That path is
unchanged.

## Required behavior (confirmed)

| Stage | Active Queued/In Progress RIV on campaign identities | Unrelated site-wide RIV |
|---|---|---|
| Create exact campaign | Allowed | Allowed |
| `preflight_campaign` / `start_sequential_campaign` | **Blocked** with exact overlap details | Allowed (informational count only) |

## Fix

1. **`collect_identity_riv_overlaps`** — returns overlapping RIV id, status,
   item_code, warehouse, posting_date, and identity pair.
2. **`preflight_campaign`** — no longer appends `active_queued_riv_count:N` as a
   blocking reason. Site-wide count remains in the payload for operators.
   Identity overlap remains a hard block.
3. **`assert_execution_riv_ready`** — fresh live overlap check used immediately
   before `start_sequential_session` to shrink the race window after preflight.
4. Blocked starts return overlap details and leave the campaign **Ready**; RIVs
   are not cancelled, skipped, or bypassed.

## Unchanged

- Exact voucher allowlist / checksum / duplicate-identity creation guards
- DocType `HistoricalRebuildCampaign` (still no validate RIV gate)
- Residual-only Manufacture GL (5.7.12)
- No Production create/start of HRC-00012 in this release task

## Tests

`test_hrc_create_vs_riv_execution_gate_v5713.py` covers create-with-active-RIV,
no side effects on create, start blocked for Queued/In Progress overlap,
unrelated RIV pass-through, post-clear start, race recheck, checksum and
duplicate-identity guards, and Ready status after a blocked start.

## Production note (Espad Exact-6 / HRC-00012)

After this release is **separately** installed on Production:

1. Create Exact-6 with `create_exact_campaign` (allowed even while overlapping
   RIVs remain Queued/In Progress).
2. Do **not** start until `preflight_campaign` returns PASS (no identity overlap).
3. Retry `start_sequential_campaign` after conflicts clear.

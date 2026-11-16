# erpnext_extensions 5.7.51

## Enable Authorized Production Staggered HRC + Safe Sequential Migration

### Summary

Version **5.7.51** makes the v5.7.50 staggered Historical Rebuild Campaign path
**safely usable on Production** (`erp.espadpharmed.com`) through an explicit
authorization gate, Sequential↔Staggered mutual exclusion, and identity-level
RIV adoption so Stage A cannot duplicate RIVs already created by
`run_sequential_rebuild`.

Actual Production authorization and HRC-00017 migration remain **operator-gated**
after code review — this release only makes that path possible.

### Production authorization

| Setting | Requirement |
|---|---|
| `hrc_stagger_allow_production` | Must be exactly `1` |
| `hrc_stagger_production_site` | Must **exactly** equal `frappe.local.site` (no wildcards) |
| Operator confirm | `START_STAGGERED_PRODUCTION` |

- Development still uses confirm `START_STAGGERED` plus existing Dev markers.
- `developer_mode` is **never** accepted as Production authorization.
- Fail-closed when settings are absent, malformed, mismatched, or wildcards.
- Read-only status / audit probes remain available without enabling execution.
- Campaign clone remains Development/restored-only.

### Sequential → Staggered migration (no duplicates)

Stage A now resolves every plan identity before create:

| Disposition | Behavior |
|---|---|
| Adopted Completed | Ledger terminal; proven Item/WH + posting + voucher/covering evidence |
| Adopted Queued | Ledger pending; Stage B may enqueue once |
| Adopted In Progress | Ledger dispatched; monitor only — no re-enqueue / no cancel |
| Failed needs recovery | Ledger blocked; no automatic replacement RIV |
| Needs review | Ledger blocked; ambiguous coverage — no auto-create |
| Newly created | Normal Stage A create for truly missing identities |
| Ledger reused | Idempotent resume |

Stage A reconciliation must account for **every** plan identity.

### Mutual exclusion

Campaign-level atomic lock (`hrc_campaign_exec_lock:<campaign>`) carries
`mode` (`sequential`|`staggered`), `session_id`, and `owner_token`.

- Stage A/B refuse while Sequential is active.
- Sequential refuses while Staggered owns the campaign.
- Owners never release another session’s lock.
- Stale recovery requires `confirm=RECOVER_CAMPAIGN_EXEC_LOCK` and ownership match.

### Stage B

Unchanged conflict model (Item/Warehouse, Item, SLE voucher-overlap).
Default Production concurrency remains:

`hrc_stagger_max_global_concurrent_riv = 1`

Native Parallel Reposting must stay **OFF**.

### API

- `start_staggered_campaign` — Dev: `START_STAGGERED`; Prod: `START_STAGGERED_PRODUCTION`
- `get_staggered_status` / `get_campaign_status` — include authorization + sequential probes
- `request_staggered_stop` / `request_campaign_stop` — unchanged semantics

### Files

- `historical_rebuild/staggered_riv_auth.py` (new)
- `historical_rebuild/staggered_riv_campaign_lock.py` (new)
- `historical_rebuild/staggered_riv_adopt.py` (new)
- `historical_rebuild/staggered_riv.py`
- `historical_rebuild/api.py` / `api_service.py`
- `historical_rebuild/sequential_rebuild.py` (campaign lock integrate)
- `tests/test_hrc_staggered_riv_v5750.py` (auth patch updates)
- `tests/test_hrc_staggered_riv_v5751.py` (new)

### Safety

- Does not execute Stage A/B on Production as part of this release task
- Does not modify SLE, Bin, or GL
- Does not treat Skipped / Needs Review as Completed
- Does not cancel in-flight RIVs on stop

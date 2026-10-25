# erpnext_extensions 5.6.17

## Historical Rebuild Campaign — Status Progress Sync

### Summary
Fixes operator confusion where Sequential session progress advanced while
Campaign `counters.completed` / `riv_*` stayed at 0. Root cause: **intentional
scope separation that was poorly exposed**, not a stuck sync of verified
coverage.

Sequential marks verified manifest documents as **Skipped Covered** (via
`mark_campaign_coverage`) and tracks RIVs in **Session JSON**. Campaign
`counters.riv_*` come only from Item.`riv_names` (legacy/processor path) and
stay 0 under Sequential-only runs. Campaign `completed` is the processor
`item_status=Completed` path — Sequential does not use it.

### Changes
- `status()` is **read-only**: uses `_compute_campaign_counters` (no
  `_persist` / no `db.commit`). Safe to poll while Production Sequential runs.
- New `progress` block with explicit scopes:
  - Document verified = `completed + skipped_covered` (never RIV count)
  - Sequential RIV metrics from current Session
  - Manifest-linked RIV metrics from Item.`riv_names`
- `counter_semantics` documents ownership of every counter family
- Status text labels Sequential vs Manifest-linked RIV clearly
- Executor persists `plan_size` on the Session so identity % / ETA need no
  heavy `chronological_identities()` scan on every status poll
- Session records `current_riv` / `current_item_code` / `current_warehouse`

### Non-goals / Safety
- Does **not** fake Pending by subtracting completed RIV counts
- Does **not** mutate SLE / GL / SABE / Batch / RIV from status
- Does **not** merge Session RIV counts into Campaign `riv_*`
- Does **not** stop or restart running campaigns
- Manifest checksum unchanged by status

### Version note
Requested target label was 5.6.15; that version (and 5.6.16) already shipped
for Payroll Correction APIs. This fix ships as **5.6.17**.

### Tests
- `test_hrc_campaign_progress_sync_v5617.py`
- Regression: `test_sequential_campaign_status_v561.py`

### Migration
No schema change. Version **5.6.17**.

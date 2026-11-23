# erpnext_extensions 5.8.7

## MCA Option A — Serialized standard Transaction RIV

### Architecture decision

**Selected: Option A** (serialized standard ERPNext `Repost Item Valuation` per MCA).

Core ERPNext v16 Transaction RIV (`repost_future_sle` / `update_entries_after`) replays
future SLEs for the voucher’s item–warehouse identities and pulls cross-warehouse
transfer inbounds via `dependant_sle_voucher_detail_no` / `get_sle_by_voucher_detail_no`.
That is sufficient for MCA manufacturing valuation propagation, including quarantine →
approved transfers.

**Option B (HRC)** remains available for multi-identity historical rebuild campaigns,
not for per-MCA submit orchestration.

### Serialization gate

- Server-side `before_submit` gate: `assert_mca_serialization_gate(company)`.
- MariaDB `GET_LOCK('mca_serial:{company}')` serializes concurrent Submit API requests.
- Blocks while any company MCA is Queued / Pending RIV / In-flight / **Failed**.
- Next MCA allowed only after genuine Core RIV terminal success **and** MCA
  `Processing Status = Completed` (RQ Job OK alone is never success).

### RIV execution hardening

- `_kick_riv_execution` always enqueues on standard Frappe `long` (no sync on web/API).
- Orphan heal never marks RIV Completed; only re-enqueues when idle ≥120s with no live
  worker PID (survives MySQL 1020 / deadlock Job-OK leftovers).
- `on_riv_change` nudges MCA finalize only on terminal RIV statuses (Completed / Failed /
  Skipped / Cancelled) — not on every In Progress heartbeat (avoids 1020 storms).
- Stale RQ `started` jobs with dead worker PIDs are cleared before re-enqueue
  (`deduplicate` safety).

### Web Service API

- `get_mca_api_status` / `get_serialization_status` / `get_mca_status`
- `generate_mcas_from_persian_period` (idempotent)
- `submit_mca` (confirm=`SUBMIT_MCA`)
- `retry_mca_processing` (confirm=`RETRY_MCA`) — Failed / Pending RIV only

### Queues

- MCA orchestration + RIV execution: Frappe `long` only.
- Dev: `workers.long.concurrency = 1` (serialization gate is independent of worker count).
- Prod recommendation: keep `long` concurrency ≥1; MCA gate still enforces one active
  valuation chain per company.

### Farvardin 1405

Processed strictly one MCA at a time via Web Service API after restore of
`20261123_131558-erp_espadpharmed_com-database.sql.gz`. **15/15 Completed.**

Known case (`MCA-2026-00012` / `MAT-STE-2026-25933-2` → `MAT-STE-2026-25934`,
item `20100042`): manufacture and transfer qty -102 rates converge at
**35,496,780 IRR** (not the stale ~3,705,004 basic chain). `known_case_final.ok = true`.

### Tests

- `test_mca_serialization_gate_v586`
- `test_mca_false_riv_completion_v586` (no false-Complete; idle orphan nudge; terminal hooks)
- `test_mca_api_v586` / queue / lock / accounting suites as applicable

### Safety

- ERPNext Core untouched.
- No push / tag / deploy from this release commit.
- Development site only for operational validation.

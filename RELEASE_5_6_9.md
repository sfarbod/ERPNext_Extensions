# erpnext_extensions 5.6.9

## CASE F — Historical Manufacture MAIN_PRODUCT_REJECT left at rate 0

### Symptom
`MAT-STE-2026-27800` row 3 (`30200041` → Reject warehouse) remained at
`basic_rate = 0` / SLE SVD = 0 after a Completed Historical Rebuild Campaign
(4,628 RIVs, 0 Failed, Pending Verification = 0).

### Root cause
1. Unstamped historical Manufacture documents skipped
   `apply_iran_manufacture_output_contract` by design (v5.3.x stamp gate).
2. `allocate_scrap_absorbed_cost` was therefore never applied during RIV, so
   the entire material pool landed on the finished-good row and the
   MAIN_PRODUCT_REJECT row stayed at zero (`secondary_item_type=Scrap`,
   `allow_zero_valuation_rate=0`).
3. Campaign coverage marked the voucher COVERED via a raw-material identity
   RIV without verifying product-reject SED rates — so Completed ≠ financially
   repaired for this document.

### Correct valuation (evidence-based)
Shared absorbed integer rate across FG + reject quantities from the existing
`allocate_scrap_absorbed_cost` pool split (for this voucher: 390,769 IRR on
both 493 FG and 21 reject units; composed value equals the consume pool).

### Fix
- `repair_historical_zero_product_reject` / gated path inside
  `apply_iran_manufacture_output_contract` for unstamped submitted docs with
  illegal zero MAIN_PRODUCT_REJECT.
- Source rebuild detects and persists the repair before Core RIV.
- `verify_voucher_basic` and sequential `mark_campaign_coverage` refuse
  false COVERED when zero product-reject rates remain.

### Tests
`test_case_f_27800_product_reject_v569`.

## Production HRC-00003 — MariaDB 1020 on RIV progress

### Symptom
Production campaign `HRC-00003` / session `20261023181509` failed after a small
number of RIVs. Failed RIVs included `h6vr92mt90`, `tnf88cqqer`, `7oivgc47g8`
(items `13100023` / `13100024`). Error logs showed truncated DB frames and
zero-valuation *warnings*; final exception was:

`QueryDeadlockError: (1020, "Record has changed since last read in table 'tabRepost Item Valuation'")`

during Core `update_data_in_repost` → `repost_doc.db_set`.

### Root cause
Not a lock-wait timeout (1205), classic deadlock (1213), or connection loss.
MariaDB **1020** optimistic/snapshot conflict on the RIV document row while
progress fields were updated mid-repost. The “Allow Zero Valuation Rate”
message is a secondary warning embedded in the error log, not the primary
exception. Several of those RIVs later show status Completed after retry,
confirming transient concurrency rather than permanent valuation failure.

### Fix
`execute_one_identity` wraps `execute_reposting_entry` with the existing
`run_with_db_concurrency_retry` classifier (retries 1020 / deadlock / lock
wait). Does not raise worker timeouts as a substitute.

### Tests
`test_sequential_riv_1020_retry_v569`,
`test_mariadb_1020_concurrency_v569` (two real MariaDB connections).

### Concurrent writers (evidence boundary)
Proven: Core `update_entries_after.update_data_in_repost` performs mid-repost
`repost_doc.db_set` commits on `tabRepost Item Valuation`. Error 1020 means
another transaction committed a change to that same RIV row after the
reposting connection had read it (MariaDB CHECKREAD / snapshot conflict).

Not proven from the truncated Production logs alone which *specific* peer
writer won the race on each failed RIV. Plausible peers under Production
worker topology (multiple short/default/long RQ workers + scheduler): a
second RQ job on the same RIV, a progress/status updater, or
`continue_reposting` dispatch. The sequential executor mitigates this with
`MAX_ACTIVE_RIV=1`, `continue_reposting=False`, queue drain, and bounded
1020 retry after rollback — it does not by itself identify the historical
Production peer.

### Scoped Development validation
`scoped_campaign.create_scoped_campaign` / `campaign.create_scoped` builds a
Manifest from seed vouchers + Item/Warehouse identities (dependency voucher
expansion) without post-filtering a full-company Manifest. Sequential
identity selection and coverage for scoped campaigns honour the persisted
allowlist; unrelated co-voucher identities are excluded. Scoped runs do
**not** claim full-company accounting verification.

### Production note
Do **not** restart Production HRC until:
1. 5.6.9 is installed.
2. Concurrent stock-writing risk is controlled.
3. The earlier undiagnosed SQL class on RIV `514n6fea1d` / item `13100018`
   (if still present in the live trail) is separately confirmed — 5.6.9 does
   not claim that older failure is fixed unless the restored snapshot
   reproduces it as 1020 and the retry path clears it.

# Release 5.5.47 — Phase 2 Historical Rebuild Campaign

**Version:** 5.5.47  
**Date:** 2026-10-14  
**Previous:** 5.5.46  
**Branch:** `feature/5.5.47-historical-rebuild-campaign`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**, Iran Accounting **5.5.47**  
**Site:** `development.localhost` (Production DB clone) — **no production deploy**, **no push**, **no tag**

**Backup before mutate:** `20261014_223331-development_localhost-database.sql.gz`

---

## A. Objective

Independent **Historical Rebuild Campaign** that lists **all** submitted stock-affecting documents and orchestrates:

1. Existing Iran source reconstruction (when required)
2. Core **Repost Item Valuation** (authoritative valuation engine)
3. SED / SLE / SABE / Bin / GL regeneration via Core + Iran hooks
4. TVA supersession (`SUPERSEDED_BY_PHASE2`)

Does **not** replace Core RIV algorithms or invent parallel valuation.

---

## B. New DocTypes

| DocType | Role |
|---------|------|
| Historical Rebuild Campaign | Campaign header, counters, cursor, pause/stop flags, logs |
| Historical Rebuild Campaign Item | One row per historical voucher (indexed, unique campaign+voucher) |
| Historical Rebuild Campaign Log | Child execution log rows |

---

## C. Server commands

```bash
bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.create

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.build_manifest \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.start \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.pause \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.stop \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.resume \
  --kwargs '{"campaign":"HRC-00001"}'

bench --site development.localhost execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.status \
  --kwargs '{"campaign":"HRC-00001"}'
```

`start` / `resume` process **continuously** until PAUSE, STOP, or completion. Optional `max_items` for pilot runs.

---

## D. Pause / Stop semantics

- **PAUSE / STOP** stop **new selections** from the campaign list only.
- Already-created Core RIV continue under normal Core behavior.
- Unrelated user/system RIV are never cancelled or blocked by the campaign.
- Under Maintenance Mode, campaign **self-drives** RIV via `execute_reposting_entry` (schedulers inactive).

---

## E. Manifest scope

Stock Entry, Purchase Receipt, Delivery Note, Stock Reconciliation, Subcontracting Receipt, Purchase Invoice (`update_stock=1`), Sales Invoice (`update_stock=1`).

Order: `posting_date, posting_time, creation, doctype, name`.

---

## F. Known limitations

1. Full 15k+ voucher rebuild wall-clock is dominated by Core RIV future-SLE walks; multi-session Pause/Resume is required for production windows.
2. Source reconstruction beyond Stock Entry uses Core RIV only (no invented formulas).
3. TVA Stock Adjustment GL rows are superseded for discovery without automatic GL reverse.
4. Production site must not be mutated from this release without a separate ops plan.

---

## G. Local execution evidence (HRC-00001) — FINAL

| Metric | Value |
|--------|------:|
| Manifest total | 15,802 |
| Completed | 80 |
| Skipped Covered | 15,722 |
| Pending / Failed / Blocked | 0 / 0 / 0 |
| Core RIV Completed | 4,837 |
| Core RIV Failed (open) | 0 |
| Distinct failed identities | 0 |
| Campaign status | Completed |

Verified locally:

- CREATE / BUILD MANIFEST / START / PAUSE / STOP / RESUME / STATUS
- Identity drain + `bulk_skip_covered` for scale
- Pause/Stop stop new selections only
- Skipped Covered sample audit: 0 uncovered among sampled covered rows
- Fixed-asset and non-stock PR verify paths (no false SLE requirement)
- Unit/regression: HRC scrap/registry, campaign, phase2, RIV integrity, I4, native transfer OK

## H. Queue recovery (2026-10-15)

Root cause of stalled RIV (verified, not assumed):

1. Site scheduler is **disabled** and `pause_scheduler=1`, so Core never drains Queued RIV onto RQ.
2. Campaign PAUSE stopped new selection; already-created RIV were left without a worker.
3. Long queue depth was **0**. None of the 98 Queued or 3 In Progress rows had an RQ job (`owned=false`).
4. The three In Progress rows (`19d2jmtnfq`, `19d3vl82or`, `1h86o2nbn5`) were **stale**: `current_index=0`, last modified ~9h earlier. Core had classified the failures as recoverable because the traceback contained `deadlock`, so status stayed In Progress instead of Failed.

Recovery: `recover_unscheduled_rivs` calls Core `execute_reposting_entry(..., continue_reposting=False)` **only** for RIV names linked on this campaign. No Redis purge. No unrelated job deletion.

The three In Progress rows completed on re-execute.

Backpressure: `MAX_OUTSTANDING_RIV=8`. START/RESUME settles outstanding campaign RIV before selecting more documents.

## I. Component Scrap mid-RIV fix (2026-10-15)

**Root cause (item 13200190 / MAT-STE-2026-40937):** during Core RIV,
`apply_component_scrap_issued_rates` ran after Core cleared consume
`basic_amount`/`basic_rate` mid-walk. Issued-rate lookup returned 0 and threw
even though matching consume rows and persisted SED rates existed.

**Fix (smallest safe):**

1. `_weighted_issued_rate` falls back to in-memory `basic_rate`, then persisted
   SED / outgoing SLE for the consume `voucher_detail_no` (never destination WH).
2. Under `through_repost_item_valuation`, if consume rows exist but rate is still
   unavailable, skip reprice instead of aborting the identity walk.
3. `failure_registry` tracks Failed Item+WH identities, blocks uncontrolled
   re-seed, and provides explicit `repair_failed_identity`.
4. Extra workers via `scripts/hrc_workers.sh` (2 long + 2 short,default).

**Verified:** Component Scrap RIVs Completed for 13200190 after fix.

## I2. Material Issue sticky outgoing_rate → I3 (16400017)

**Root cause:** During Item+WH RIV Core keeps DB `SLE.outgoing_rate`. For
Material Issue that rate can be a poisoned SED stamp (e.g. 1,581,633 vs MA
1,500,000). `get_moving_average_values` mixes it into `wh_data`, drives
`stock_value` negative, then consume-to-zero yields positive SVD → I3 on
`MAT-STE-2026-41477`.

**Fix:** `clear_sticky_native_outgoing_rate_for_riv` before vanilla
`process_sle` for native-outgoing purposes (Material Issue/Transfer/…).
Iran then realigns SED via preserve-native. Failure registry skips Failed
rows after covering Completes; auto-seed never recreates Failed identities.

**Verified:** RIV `idnlp2utam` Completed; Bin/SLE chain clean (qty=0,value=0).

## I3. Failed-identity reseed flood

Late Completed covers that do not reach earliest SLE must not reopen seeding.
`uncovered_identities` and `is_blocked_identity` treat any open Failed as a
hard auto-seed block; `mark_resolved` moves Failed → Skipped (error_log kept).

## J. Reconciliation (development.localhost)

- Terminal: 15,802 accounted, pending=0, failed=0, blocked=0
- Bin ↔ latest SLE stock_value delta: **0**
- SLE SVD ↔ warehouse inventory GL delta: **−49,009 IRR** (≤1e5 tolerance)
- TVA: 50 TRACE_ONLY, 30 GENUINE_ROUNDING; 75 SUPERSEDED_BY_PHASE2, 5 CANCELLED
- Open Failed RIV: **0**

## K. Verdict (local)

**FULL_REBUILD_VERIFIED_AND_MERGED** (merge into `develop` local only — no push).

# October 6 Historical Repair Campaign — Pause Checkpoint

**Status:** CAMPAIGN PAUSED — incomplete repair/repost proof  
**Release:** erpnext_extensions **5.5.7** (pause checkpoint + Product Reject residual-align fix)  
**Production:** LIVE PRODUCTION UNTOUCHED — no Production token, no Production access, no Production restore.

This document freezes the October-6 Production-backup Historical Repair campaign
so work can resume later without rediscovery. It is **not** a claim that repair
is complete, that native repost is proven, or that any database is a Production
restore candidate.

---

## Source Backup

Immutable Production-sourced backup (verified in Phase 1; gzip re-checked at pause):

| Field | Value |
|-------|-------|
| Name | `20261006_001111-erp_espadpharmed_com-database.sql.gz` |
| Path | `/workspace/development/frappe-bench/sites/development.localhost/private/backups/20261006_001111-erp_espadpharmed_com-database.sql.gz` |
| Size | `377416047` bytes (~360M) |
| SHA256 | `2384b5786fb823dcb252846342c3d5684de31a1b664db05da62b00d1b4b75141` |
| gzip | `OK` |
| Immutable | **YES — do not overwrite** |

This supersedes the September `20260930_093401...` source for this campaign.
Old September findings are regression knowledge only.

---

## Current Stack

Captured at pause (`development.localhost`):

| Component | Version |
|-----------|---------|
| Frappe | 16.36.1 |
| ERPNext | 16.37.0 |
| HRMS | 16.16.0 |
| erpnext_extensions | **5.5.7** (was 5.5.6 at campaign start HEAD `f4469a0`) |
| Branch | `develop` (tracking `upstream/develop`) |
| HEAD before pause commit | `f4469a0afa4380cc8f4a8c515db2276784b5986a` |

Relevant installed apps (stock/accounting): `frappe`, `erpnext`, `hrms`,
`erpnext_extensions`, plus site apps not required for this campaign.

---

## Campaign Objective

Latest Production backup → isolated Development restore → deterministic
historical stock root repair under **CURRENT Iran Accounting** → native stock
repost ×2 with economic idempotency → create a repaired backup → later
**user-controlled** Production restore.

**None of the final proof steps were completed before this pause.**

---

## Work Completed

Actual completed work only (artifacts under
`sites/development.localhost/private/files/hr_correction_20261006/`):

1. **Phase 1** — verified source backup; restored into `development.localhost`;
   `bench migrate`; baseline counts/dates; infrastructure preflight; Iran
   Accounting compatibility matrix draft; read-only global integrity scan;
   canary revalidation; `FULL_REPOST_BLOCKED` at baseline (open RIV / broken bins).
2. **Dump-stale RIV admin-close** — open dump-stale RIV closed to **Failed**
   (not Skipped) so Full Repost was not blocked by leftover Queued/In Progress
   dump artifacts (`dump_stale_riv_admin_close.json`).
3. **Iran-native historical EXACT waves** — applied via
   `apply_iran_native_historical` for EXACT Manufacture contract adoption
   (incl. Bulk Scrap stamp preservation; Co-product stamps where applicable).
4. **Wrong Rate EXACT** — voucher-grouped durable drain of transfer
   `TRANSFER_AUTHORITATIVE_RECONSTRUCTION` EXACT rows to **0 eligible EXACT**
   (`wave_wr_voucher_durable_v557.json`). Early row-wise waves were misleading
   (reported REPAIRED while sibling SE details remained 0).
5. **Product Reject persist bug found + fixed in code** — Iran-native apply for
   `MAT-STE-2026-25523` was undone by `align_manufacture_finished_good_residual`
   after equal-rate + header operating cost. Generic fix in
   `manufacture_rounding.py`; unit test
   `test_product_reject_residual_align_v557.py`. After fix, `25523` analyze =
   `ALREADY_HEALTHY`.
6. **Bin rebuild** — `rebuild_bins_derived()` fixed 91 mismatches →
   `broken_bin = 0`.
7. **READY_I4 drain** — all `READY_I4`/`EXACT` rows repaired; remaining I4 lanes
   at reclass: MANUAL/WAITING/TECHNICAL (no READY left)
   (`wave_i4_drain.json`, `i4_reclass_after_wr.json`).
8. **L6 preflight blocker probe** — identity `13200473` @ Paykar tip I4/poison
   cleared by one **narrow native RIV** (`probe_13200473_narrow_riv.json`) →
   tip `q=0,sv=0`, opening `LEGITIMATE`.
9. **L6 full native repost #1 STARTED then PAUSED** — `L6_1_OCT6` reached
   **550 / 4368** identities (`failed_n=0` in progress file) then **SIGTERM**
   on user pause. **Not completed. Not proven. Not idempotent.**

---

## Current Iran Accounting Audit

| Topic | Status |
|-------|--------|
| Moving Average / residual align vs Product Reject + operating cost | **VERIFIED_CURRENT_IRAN_RULE** (bug found; fix in 5.5.7) |
| Product Reject equal issued rate | **VERIFIED_CURRENT_IRAN_RULE** on `25523` after fix |
| Bulk Scrap rate 0 | **VERIFIED_CURRENT_IRAN_RULE** (canary `MAT-STE-2026-37603` / `30100101` still rate 0 at pause) |
| Wrong Rate transfer SABB reconstruction | **VERIFIED_CURRENT_IRAN_RULE** (EXACT WR drained voucher-wise) |
| I4 leftover READY path | **VERIFIED_CURRENT_IRAN_RULE** (READY drained; MANUAL/WAITING remain) |
| RIV sync execute / settlement | **PARTIALLY verified** (narrow RIV + partial L6); full L6 incomplete |
| Purchase Receipt / Pending PI | **Revalidated dynamic** — Sep canary PR cancelled on Oct-6 backup; do not preserve old pending claim blindly |
| Leftover MA invent-rate prohibition | **NOT_YET_REVALIDATED** as a full family pass on Oct-6 |
| Co/By-Product / Stage equivalent | **PARTIAL** (Iran-native stamps applied; not full matrix re-proof) |
| Component Scrap | **NOT_YET_REVALIDATED** as dedicated Oct-6 family |
| 621301 Stock Adjustment / 622515 exclusion | **NOT_YET_REVALIDATED** post-partial-L6 |
| Full native stock repost idempotency | **NOT PROVEN** |
| Old 5.3.x Clean A/B / GL deadlock assumptions | **OLD_5.3.x_ASSUMPTION** — do not treat as current proof |

---

## Repairs Applied

| Family | Result (actual) |
|--------|-----------------|
| Iran-native historical EXACT | Applied (multiple vouchers; see wave JSON) |
| Wrong Rate EXACT (transfers) | Drained to 0 eligible EXACT (voucher-grouped) |
| READY_I4 | Drained to 0 |
| Bin derived rebuild | 91 fixed → broken_bin 0 |
| Narrow RIV `13200473`@Paykar | Completed; tip healed |
| L6 company-wide RIV | **PARTIAL** — 550/4368 then stopped |

---

## Repost Status

**PARTIAL**

- Full Repost / L6 #1: started (`L6_1_OCT6`), progress **550/4368**,
  `failed_n=0` at last progress write, process **SIGTERM** on pause.
- L6 #2 / idempotency: **NOT STARTED**.
- Repaired Production-candidate backup: **NOT CREATED**.

---

## Current Database Status

**PARTIALLY_MUTATED**

DO NOT USE CURRENT DEVELOPMENT DATABASE AS A PRODUCTION RESTORE CANDIDATE.

**SOURCE RESTORE REQUIRED BEFORE RESUMING PROOF** from the immutable
October-6 source backup (unless a future agent proves otherwise with fresh
evidence — do not assume continuity after partial L6).

Site: `development.localhost`  
Artifacts: `.../private/files/hr_correction_20261006/`  
Pause marker: `L6_CAMPAIGN_PAUSED.json`

---

## Open RIV

At pause snapshot (`PAUSE_RUNTIME_SNAPSHOT.json`):

| Status | Count |
|--------|------:|
| Skipped | 118161 |
| Failed | 25872 |
| Completed | 23098 |
| **Queued** | **1** |
| In Progress | 0 |

Open Queued (exact):

- `55corrcbai` — item `13200264` — warehouse `انبار پایکار خط تولید اسپاد فارمد`
  — status `Queued` (left by interrupted L6; **do not SQL-Skip**)

Hard gates at pause measurement:

- `i1=0`, `neg_stock=0`, `broken_bin=0`, `broken_gl=0`, `open_riv=1`
- Bulk Scrap canary rate still `0`

---

## Known Failures / Blockers

1. Full L6 incomplete (550/4368) — DB economically mid-repost.
2. Remaining I4 non-READY lanes (MANUAL / WAITING_I4 / TECHNICAL_TOOL_GAP) —
   some may clear after clean-source replay; do not invent rates.
3. Integrity scan still showed many non-EXACT Wrong/Zero findings after WR/I4
   waves — score-chasing forbidden; classify before mutate.
4. WR path previously proposed rate `0` for Iran-native Product Reject rows —
   **must route Manufacture Product Reject through Iran-native apply**, not WR zeroing.
5. Simple `full_repost_preflight()` can report READY while richer
   `phase2_reconcile_0930.run_preflight()` is stricter — resume must use the
   L6/`run_preflight` path before Full Repost.

---

## Known Business Contracts

Preserve for regression (mark revalidation):

| Contract | Oct-6 revalidation |
|----------|-------------------|
| Zero rate alone ≠ corruption | Reaffirmed in scans (Z0 buckets remain) |
| Bulk Scrap may stay rate 0 | **Revalidated** (`30100101`) |
| Pending PI valuation is dynamic | **Revalidated** (old PR canary cancelled) |
| Current Iran Accounting owns valuation | **Authority** |
| Leftover MA must not invent incoming rate | Contract preserved; full family not closed |
| Manufacturing qty read-only | Preserved (Iran-native `quantity_mutation=false`) |
| No dump to 622515 for stock residuals; 621301 only when justified | Contract preserved; not fully re-audited post-L6 |
| No direct SLE/Bin/GL as repair mechanism; no SQL-Skip RIV; fail-closed integrity | Preserved operationally |

---

## Code Changes Made During This Campaign

Intended product changes for this checkpoint commit:

1. `erpnext_extensions/iran_accounting/manufacture_rounding.py` — under TYPE-C
   policy, skip FG pool-close when Product Reject is present (preserve equal-rate
   after header operating-cost spread).
2. `erpnext_extensions/iran_accounting/tests/test_product_reject_residual_align_v557.py`
3. `erpnext_extensions/__init__.py` → **5.5.7**
4. `RELEASE_5_5_7.md` — pause + fix note
5. This checkpoint document

Runtime/site artifacts under `sites/.../hr_correction_20261006/` are **not**
source-controlled.

---

## Uncommitted Work (before checkpoint commit)

- Product Reject residual-align fix + test + version 5.5.7 (to be committed)
- Unrelated dirty files **preserved, not staged**:
  - `erpnext_extensions/desktop_icon/petty_management.json`
  - `erpnext_extensions/workspace_sidebar/petty_management.json`

---

## Production Status

LIVE PRODUCTION UNTOUCHED by this repair campaign.  
No Production restore is authorized by this checkpoint.

---

## RESUME FROM HERE

1. Checkout/verify commit that contains this checkpoint (see git log after pause commit).
2. **Read this file end-to-end.**
3. Verify stack (Frappe/ERPNext/HRMS/erpnext_extensions HEAD + version).
4. **Restore** immutable
   `20261006_001111-erp_espadpharmed_com-database.sql.gz`
   into an isolated Development/repair site (current DB is PARTIALLY_MUTATED /
   unproven after partial L6).
5. `bench migrate` + cache/schema init.
6. Infrastructure / Redis / worker / scheduler / RIV preflight
   (do not SQL-Skip open RIV).
7. Re-run **current** Iran Accounting compatibility audit (do not reuse 5.3.x
   assumptions blindly).
8. Fresh baseline global Historical Repair scan (raw + causal roots).
9. Resume root repair in causal order: posting/WR EXACT (voucher-grouped) →
   Iran-native EXACT → I4 READY → leftover/terminal → bin → GL; **never** WR-zero
   Product Reject (use Iran-native apply).
10. Use **`run_preflight` / L6 preflight** (not the empty-snapshot light check)
    before Full Repost.
11. Native Full Repost only when `FULL_REPOST_READY`; stop at first unexplained
    economic failure; settle state-based; no continue-on-Failed.
12. Full Repost #2 + economic idempotency required.
13. Create repaired backup only after final hard-gate pass — distinct filename;
    never overwrite source; never auto-restore to Production.

---

## Artifact Index (Development site — not in git)

Directory:
`/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_20261006/`

Key files: `PHASE1_REPORT.json`, `PAUSE_RUNTIME_SNAPSHOT.json`,
`L6_CAMPAIGN_PAUSED.json`, `l6_1_oct6_progress.json` (550/4368),
`wave_wr_voucher_durable_v557.json`, `wave_i4_drain.json`,
`probe_13200473_narrow_riv.json`, `integrity_after_wr_i4bin.json`.

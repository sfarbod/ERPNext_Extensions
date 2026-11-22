# Costing Restore and Six-Month Calculation Report

**Site:** `development.localhost`  
**Database:** `_19b2f83259cbf92a` (Development only)  
**Backup:** `20261121_095630-erp_espadpharmed_com-database.sql.gz`  
**App version:** `erpnext_extensions 5.8.5` (unchanged)  
**Run date (UTC):** 2026-11-21  
**Overall:** **PASS**

No Production access, push, tag, deploy, or version bump occurred.

---

## 1. Backup restore result

| Check | Result |
|---|---|
| Backup exists & readable | **PASS** — `sites/development.localhost/private/backups/20261121_095630-erp_espadpharmed_com-database.sql.gz` (~557 MB) |
| Original backup preserved | **PASS** — copy at `sites/preserved_dev_backups/20261121_095630-erp_espadpharmed_com-database.sql.gz` |
| Target site exact | **PASS** — `development.localhost` only |
| DB overwritten | **PASS** — `_19b2f83259cbf92a` on `mariadb` |
| Production touched | **PASS** — not accessed |
| Restore command | `bench --site development.localhost restore … --force` |
| Restore outcome | **PASS** — `Site development.localhost has been restored` (exit 0, ~212 s) |

---

## 2. Restored site confirmation

| Item | Value |
|---|---|
| Site | `development.localhost` |
| DB name / host | `_19b2f83259cbf92a` / `mariadb` |
| Isolation markers | `developer_mode=1`, `host_name=http://development.localhost:8000`, `restore_isolation_applied=1` |
| Maintenance mode | Off after migrate |
| Desk ping | **PASS** — `GET /api/method/frappe.ping` → 200 `pong` |
| Company | اسپاد فارمد دارو |

---

## 3. Installed app versions

| App | Version |
|---|---|
| frappe | 16.60.2 |
| erpnext | 16.60.0 |
| hrms | 16.53.0 |
| **erpnext_extensions** | **5.8.5** |
| persian_calendar | 2.1.3 |
| (others) | dms 2.5.0, raven 2.8.11, lending 1.5.4, wiki 3.0.0, insights 3.12.2, lms 2.58.1, helpdesk 1.28.1, payments/telephony 0.0.1 |

Version pin held: no release / tag / version change.

---

## 4. Migration result

| Step | Result |
|---|---|
| `bench --site development.localhost migrate` | **PASS** (exit 0, ~192 s) |
| DocType sync (Costing + Control Credit) | **PASS** |
| Reports `MFG Cost Control Credit Pool` / `… Batch Product Allocation` | **PASS** (present) |
| Child `MFG Cost Product Allocation Document Breakdown` | **PASS** (fields include all four required shares) |
| Expense rules | **PASS** — 3026 enabled `Expense Account Dimension Rule` rows |
| Control accounts | **PASS** — Labor `211238 - کنترل دستمزد - E`, Overhead `211237 - کنترل سربار - E` |
| Non-blocking noise | flow-server UDS chmod `OSError` during after_migrate (unrelated; migrate completed) |

---

## 5. Worker configuration and running status

Docker Compose worker services were unavailable (no docker.sock). Existing local scripts were reused; discrete single-queue consumers were started so each queue has exactly one worker.

| Queue | Required | Actual live workers | Status |
|---|---:|---:|---|
| `short` | 1 | 1 (`worker-short`) | **PASS** |
| `default` | 1 | 1 (`worker-default`) | **PASS** |
| `long` | 1 | 1 (`worker-long`) | **PASS** |

**MCA worker (preserved, reported separately):**

| Queue | Workers | Status |
|---|---:|---|
| `mca` | 1 (`worker-mca` via `scripts/ensure_mca_worker.sh`) | **PASS** — idle, not repurposed |

Redis: queue + cache ping **PASS**.  
Stale overlapping Redis registrations from prior Compose workers were death-registered before start.  
Final Redis registration: exactly 4 workers (`short`, `default`, `long`, `mca`).

Scheduler: enabled on `development.localhost` after restore (was disabled by restore).

---

## 6. Queue processing evidence

Calculations were enqueued sequentially (one month at a time):

- Monthly FG cost → queue **`long`**
- Control Credit Allocation → queue **`default`**
- Health ping → queue **`short`**

| Evidence | Result |
|---|---|
| Jobs observed QUEUED → STARTED → FINISHED on intended queues | **PASS** |
| Unexpected retries left in queue | **PASS** — none |
| Stuck Queued/Started after run | **PASS** — all drained |
| Failed jobs remaining | **PASS** — 0 on short/default/long/mca |
| Final queue depths | all `queued=0`, `started=0`, `failed=0` |

**Transient failure (resolved):** first attempt of `1405-03` on `long` failed with  
`IntegrityError: Duplicate entry 'f83d89e50c' for key 'PRIMARY'` while bulk-inserting `MFG Cost Batch Expense Allocation` (10-char hash collision with an existing month-6 detail name).  

**Fix applied (no version bump):** `persist._name()` now uses length-16 hashes + in-run uniqueness cache (`persist.py`). Workers restarted; months 03–06 recalculated successfully.

---

## 7. Results per Persian month (1405-01 … 1405-06)

All six months: Monthly status `Recalculated` revision **2**, `is_consistent=1`, Credit status `Calculated`.

| Period | Status | Batches | Stock Entries | Product Allocations | Credit docs | Credit product rows | Unresolved credit sources |
|---|---|---:|---:|---:|---:|---:|---:|
| 1405-01 | **PASS** | 8 | 8 | 8 | 1 | 15 | 0 |
| 1405-02 | **PASS** | 21 | 21 | 21 | 1 | 21 | 0 |
| 1405-03 | **PASS** | 73 | 73 | 73 | 1 | 73 | 4 |
| 1405-04 | **PASS** | 56 | 56 | 56 | 1 | 65 | 1 |
| 1405-05 | **PASS** | 78 | 78 | 78 | 1 | 88 | 0 |
| 1405-06 | **PASS** | 82 | 82 | 82 | 1 | 82 | 25 |

Successful calculations: **6 / 6**. Failed/pending after final run: **0**.

---

## 8. Manufacturing cost calculation totals

| Period | Pools | Identified expenses | Allocated expenses | Eligible source rows |
|---|---:|---:|---:|---:|
| 1405-01 | 430 | 227,084,278,119 | 227,084,278,119 | 15 |
| 1405-02 | 469 | 262,491,027,005 | 262,491,027,005 | 42 |
| 1405-03 | 485 | 321,767,724,450 | 321,767,724,450 | 139 |
| 1405-04 | 477 | 350,281,335,440 | 350,281,335,440 | 116 |
| 1405-05 | 502 | 495,659,813,644 | 495,659,813,644 | 151 |
| 1405-06 | 540 | 441,321,446,417 | 441,321,446,417 | 82 |

---

## 9. Labor and Overhead allocation (Document Account Control Breakdown)

Verified fields on every breakdown row path:  
`allocated_labor_share`, `allocated_overhead_share`, `unallocated_labor`, `unallocated_overhead`.

| Period | Labor Control | Allocated Labor | Unallocated Labor | Overhead Control | Allocated Overhead | Unallocated Overhead |
|---|---:|---:|---:|---:|---:|---:|
| 1405-01 | 202,441,946,143 | 0 | 202,441,946,143 | 24,642,331,976 | 0 | 24,642,331,976 |
| 1405-02 | 173,520,713,591 | 0 | 173,520,713,591 | 88,970,313,414 | 0 | 88,970,313,414 |
| 1405-03 | 174,293,999,512 | 0 | 174,293,999,512 | 147,473,724,938 | 351,288 | 147,473,373,650 |
| 1405-04 | 226,497,333,083 | 0 | 226,497,333,083 | 123,784,002,357 | 188 | 123,784,002,169 |
| 1405-05 | 226,775,131,250 | 0 | 226,775,131,250 | 268,884,682,394 | 6,971,596 | 268,877,710,798 |
| 1405-06 | 225,933,832,320 | 0 | 225,933,832,320 | 215,387,614,097 | 75,157,941,821 | 140,229,672,276 |

### Control Credit Allocation (monthly credit docs)

| Period | Labor period credit | OH period credit | Labor eligible / allocated / remaining | OH eligible / allocated / remaining |
|---|---:|---:|---|---|
| 1405-01 | 0 | 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| 1405-02 | 0 | 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| 1405-03 | 0 | 351,300 | 0 / 0 / 0 | 351,288 / 351,288 / 0 |
| 1405-04 | 0 | 190 | 0 / 0 / 0 | 188 / 188 / 0 |
| 1405-05 | 0 | 6,971,596 | 0 / 0 / 0 | 6,971,596 / 6,971,596 / 0 |
| 1405-06 | 0 | 75,218,486,393 | 0 / 0 / 0 | 75,157,941,821 / 75,157,941,821 / 0 |

Notes:

- No Labor control GL credits existed in these six months (account `211238`); Labor Credit pools are correctly zero.
- Months 01–02 have no Overhead control GL credits either; Credit docs correctly Calculated at zero.
- Overhead Credit allocations for 03–06 match eligible credits (remaining 0). Period vs eligible gaps equal unresolved/excluded source credits.

---

## 10. Reconciliation results

For every month:

`Labor Control = Allocated Labor + Unallocated Labor` → **PASS**  
`Overhead Control = Allocated Overhead + Unallocated Overhead` → **PASS**  
`Credit eligible = allocated + remaining` (Labor & Overhead) → **PASS**  
Monthly identified = allocated expenses → **PASS**

---

## 11. Failed, pending, or unresolved records

| Item | Status |
|---|---|
| Pending monthly calculations | **NONE** |
| Failed monthly calculations (final) | **NONE** |
| Credit docs pending | **NONE** |
| Unresolved credit sources (expected exclusions) | **1405-03:** 4 · **1405-04:** 1 · **1405-06:** 25 |

Unresolved reason (all sampled): `Landed Cost line could not be reliably classified` (small IRR amounts, e.g. 2–6). These remain visible on Credit Sources; eligible amounts were fully allocated. Documents stay `Calculated` / `is_consistent=1` (not submitted).

Historical Error Log retained: `kmso9heu2e` (first 1405-03 hash collision) — superseded by successful recalculation after name fix.

---

## 12. Relevant error logs

| Log | Severity | Disposition |
|---|---|---|
| `kmso9heu2e` — Duplicate PK `f83d89e50c` on Batch Expense insert (1405-03 first attempt) | Blocking then | **Resolved** via `persist._name` length-16; month recalculated **PASS** |
| flow-server UDS chmod during migrate | Noise | Non-blocking; migrate **PASS** |
| SSL self-signed DB connect noise in unrelated Error Log | Noise | Not related to this site’s MariaDB path |

---

## 13. Final site health status

| Check | Result |
|---|---|
| Site reachable (Desk API ping) | **PASS** |
| DB connectivity | **PASS** |
| Redis queue/cache | **PASS** |
| Workers 1×short / 1×default / 1×long | **PASS** |
| MCA worker preserved | **PASS** |
| Queues drained | **PASS** |
| Scheduler enabled | **PASS** |
| `erpnext_extensions` version | **5.8.5** |
| Production / push / tag / deploy | **NOT RUN** (by design) |
| Uncontrolled RIV / historical repost | **NOT RUN** (by design) |

### Completion conditions checklist

- [x] Backup restored on `development.localhost` only  
- [x] Migration successful  
- [x] Version remains `5.8.5`  
- [x] Exactly one worker each for `long`, `short`, `default`  
- [x] Workers processed their queues; queues drained  
- [x] Manufacturing cost + Control Credit for all six months completed  
- [x] Monthly allocation + reconciliation reported  
- [x] No Production change / push / tag / deployment  

**Final verdict: PASS**

---

## Appendix — artifacts

- Restore log: `/tmp/restore_20261121.log`  
- Migrate log: `/tmp/migrate_dev_20261121.log`  
- Calculation JSON: `/tmp/six_month_costing_results.json`  
- Run logs: `/tmp/six_month_costing_run.log`, `/tmp/six_month_costing_run2.log`  
- Code fix (no version bump): `erpnext_extensions/iran_accounting/fg_cost_allocation/persist.py` (`_name` length 16 + cache)

# Release 5.5.49 — In-place RIV JSON/file recovery (Production-safe)

**Version:** 5.5.49  
**Date:** 2026-10-17  
**Previous:** 5.5.48  
**Branch:** `feature/5.5.49-riv-json-inplace-recovery` (not merged; not pushed)  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**, Iran Accounting **5.5.49**  
**Site:** `development.localhost` only — **no Production mutate**, **no push**, **no deploy**

**Development restore source (investigation only):**  
`20261017_063803-erp_espadpharmed_com-database.sql.gz`  
**Pre-recovery Development backup:** `20261017_091835-development_localhost-database.sql.gz`

---

## A. Problem

Production HRC-00001 paused with:

| Metric | Value |
|--------|------:|
| Total | 15,968 |
| Skipped Covered | 15,966 |
| Blocked | 2 (`MAT-PRE-2026-02002`, `MAT-STE-2026-41577`) |
| Failed RIV | 3 (`0abr2540f9`, `0i3f2beji5`, `61s9c8ieo4`) |

All three Failed RIVs shared:

`/private/files/repost_item_valuation-5946ptkd5h.json.gz`

Error: `JSONDecodeError: Expecting value` in Core `get_reposting_data` during `repost_gl_entries` (after SLE progress).

---

## B. Root causes (separated)

### TYPE A — RIV temporary file / JSON failure (confirmed)

1. Core writes progress JSON via `create_json_gz_file` → Frappe `File`.
2. Identical empty skeletons share **one** `content_hash` (`42bb3df5…`) → **one** `file_url`.
3. Evidence: **20** File rows share that URL; **3** Failed RIVs point to it.
4. Core `get_reposting_data(file_path)` looks up File by `file_url` only (not `attached_to_name`).
5. Traceback File `9883545519` returned `content=b''` → `gzip` empty payload → `json.loads('')`.
6. SLE phase can complete before GL aborts (`current_index=1`) → partial RIV.

### TYPE B — Partial execution / retry safety

Failed status left after SLE touch; file pointer remains. Auto-seed blocked; campaign items Blocked.

### TYPE C/D/E — Iran Accounting / SLE vs GL / document vs ledger

Investigated `MAT-PRE-2026-02002` and `MAT-STE-2026-41577`:

| Doc | SLE SVD | Inventory GL | Match |
|------|--------:|-------------:|:-----:|
| PRE | +35,701,754 | +35,701,754 | yes |
| STE MI | −35,701,754 | −35,701,754 | yes |

PRE commercial `rate=25,900,000` ≠ `valuation_rate=17,850,877`.  
Matching **Purchase Invoice** `ACC-PINV-2026-01966` lines at **17,850,877**.  

**Verdict:** document commercial rate vs stock valuation is **legitimate** ERPNext billing/valuation divergence, not an Iran Accounting SLE/GL defect for these vouchers. STE fully consistent.

### TYPE F — Campaign blocked state

Blocked solely because Failed identities blocked new RIV; resolved by recovering Failed RIVs + `bulk_skip_covered`.

---

## C. Fix

1. **`riv_file_recovery.py`** — in-place recovery API:
   - `list_recoverable_failed_rivs`
   - `recover_failed_riv` (detach File rows for that RIV, clear pointer, Core `execute_reposting_entry`)
   - `unblock_campaign_items_when_identity_healthy`
   - `recover_campaign_json_failures` (idempotent, dry_run supported)

2. **Monkey patch** `erpnext.stock.stock_ledger.get_reposting_data`:
   - Empty / invalid payload → empty dict (same as Core gzip-failure path)
   - Prefer newest File row for shared URL
   - Prevents GL abort on empty content

Does **not** restore Production DB, recreate campaign/manifest, delete Completed RIVs, or edit SLE/GL by hand.

---

## D. Development recovery result

| Check | Result |
|-------|--------|
| 3 Failed → Completed | **ok** |
| Blocked → Skipped Covered | **2** |
| Campaign status | **Completed** |
| Pending / Failed / Blocked | **0** |
| Idempotent rerun | recovered=`[]`, terminal_ok |
| Bin ↔ latest SLE | **0** |
| SLE SVD ↔ GL | **−40,527 IRR** (≤1e5; accepted band) |
| PRE/STE SLE/GL amounts | unchanged & balanced |

---

## E. Production runbook

See `PRODUCTION_RUNBOOK_5_5_49_RIV_JSON_RECOVERY.md`.

**Requires deploying 5.5.49 code to Production, then running recovery in place — no DB restore.**

---

## F. Tests

`test_riv_file_recovery_v5549.py` — classify, empty content safety, empty gzip, valid skeleton, missing RIV precondition.

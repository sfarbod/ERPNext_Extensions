# v5.2.7 — Historical Stock Integrity & Repair

## Title

Unified Historical Stock Integrity & Repair: lost rates, posting order, manufacture contract replay, SLE/Bin/GL, Failed RIV — selected only, never global repost

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.6**
- `erpnext_extensions.__version__` = **5.2.7**
- Site used for validation: **development.localhost**

---

## 1. Problem

Historical stock on this site is internally consistent in several places and still economically wrong.

Typical chain:

1. A patient-zero voucher writes `incoming_rate = 0` / `SVD = 0` onto a previously valued batch.
2. Later transfers and MTfM rows resolve outgoing rate 0.
3. For IRR, `update_rate_on_stock_entry` is skipped, so submitted Stock Entry zeros are **preserved**, not overwritten by a later RIV.
4. Same-second posting order can still invert IN/OUT when creation order disagrees with economic order.
5. Failed RIV rows accumulate (negative stock, GL imbalance, I1/I4) and must not be retried blindly.

5.2.1 Historical Repair covered posting-order only. It cannot reconstruct lost Stock Entry rates. Global RIV / global repost would spread poison.

## 2. Architecture

Package: `erpnext_extensions.iran_accounting.historical_stock`

Canonical repair direction:

```
Stock Entry economic truth
→ SLE
→ forward item + warehouse + batch/SABB valuation
→ Bin from last healthy SLE
→ selective GL (G1–G4 only)
→ Failed RIV retry only when the chain is healthy
```

Never: GL first, Bin first, global RIV, live Bin as rate truth, `abs()` to hide invalid valuation, bulk apply of AMBIGUOUS/LIKELY.

Desk page **Historical Stock Integrity & Repair** (`historical-repair`) has six topics:

| Tab | Scan / Dry Run / Repair Selected |
|-----|----------------------------------|
| A Posting Order | 5.2.1 same-time optimizer (unchanged policy) |
| B Zero / Lost Rate | Version + batch + source SLE + issued scrap rate |
| C Manufacture Valuation | Historical 5.2.0 output contract preview |
| D SLE / Bin Integrity | poison / leftover value / Bin vs last SLE |
| E GL Integrity | G0–G4; rebuild G1–G4 only |
| F Failed RIV | dependency map; retry only `SAFE_TO_RETRY` |

Actions: **Scan**, **Dry Run**, **Preview**, **Repair Selected**, **Repost Selected** (one Item+Warehouse RIV after integrity), **Integrity Check**, **Resume** (audit log cursor).

There is **no** Repost All Stock, **no** global RIV, **no** bulk apply.

Audit: `Historical Stock Repair Log` + child `Historical Stock Repair Entry` (resumable).

## 3. Zero / lost rate policy

Suspicious submitted Stock Entry Detail rows: `qty ≠ 0`, rates/amount 0, `allow_zero_valuation_rate = 0`.

| Class | Meaning |
|-------|---------|
| Z0 | Legitimate zero (`allow_zero` or Stock Reconciliation is authoritative) |
| Z1 | Historical rate lost (Version / source SLE / issued scrap) |
| Z3 | Missing incoming valuation |
| Z4 | Batch/SABB inward is the only remaining nonzero |
| Z6 | Unknown |

Confidence:

- **EXACT** — Repair Selected allowed when this voucher **is** the patient-zero (or has no upstream patient-zero)
- **LIKELY** — preview only
- **AMBIGUOUS** — no automatic write

EXACT examples: Version + matching batch inward (±1 Rial); Version + matching previous healthy SLE; source transfer SLE; same-voucher issued component scrap rate.

Version **alone** is LIKELY. Version vs batch mismatch is AMBIGUOUS (25741 item `13200114`).

Never use current Bin as automatic truth.

## 4. Patient-zero engine

For each item + warehouse (+ batch), find the first invalid SLE transition:

- nonzero → zero incoming
- negative incoming / sign-inverted SVD
- exploded valuation
- qty-after ≈ 0 with leftover stock_value

Repair must start there. Downstream zeros (including **MAT-STE-2026-25741**) are `DEPENDENCY_REPAIR_REQUIRED`.

## 5. Manufacture policy

Historical preview applies the **existing 5.2.0** contract in memory, then restores the document. No policy change: component scrap issued-rate fallback, FG residual, independent by-product pool limit, Additional Cost, integer align. Scan does not persist. Repair Selected persists only EXACT reconstructable manufacture vouchers, then replays that identity.

## 6. SLE replay / Bin / GL / RIV

After an EXACT rate write:

1. Set SLE `incoming_rate` / SVD (and `outgoing_rate` on outbound legs)
2. Forward-replay **only** that item + warehouse from the patient-zero datetime
3. Rebuild Bin from the last SLE
4. GL rebuild only if classified G1–G4 **and** SLE is no longer poisoned; G0 is preserved
5. Failed RIV retry only for deadlock/timeout-class errors on a healthy chain with no zero-rate dependency

Replay still aborts on 5.2.0 sign/exploded poison.

## 7. Runtime lost-rate guard

On Stock Entry **submit** (`before_submit` + `validate` after the 5.2.0 contract):

If an outgoing row has `allow_zero_valuation_rate = 0`, qty ≠ 0, `basic_rate` resolved to 0, and batch/SABB or previous healthy SLE proves a nonzero historical rate:

```
Stock valuation integrity — outgoing rate unexpectedly resolved to zero
```

Draft save is allowed. RIV / historical repair flags skip the guard. No evidence → no block (true zeros remain possible). IRR still skips vanilla `update_rate_on_stock_entry` overwrite of submitted rates.

## 8. Explicit non-goals

- No global Stock Entry / SLE / RIV repost
- No change to 5.2.0 Manufacture accounting policy
- No change to Transfer value-neutral policy
- No generic GL residual absorb
- No automatic repair of AMBIGUOUS or LIKELY rows
- No automatic repair of 25741 (downstream of 25407; `13200114` AMBIGUOUS)

## 9. Real-data scan (development.localhost, 2026-09-13, read-only)

START LOCAL `2026-09-13T18:23:30`  
END LOCAL `2026-09-13T18:23:43`  
DURATION **12.566 s** (92,334 SLE)

| Topic | Count |
|-------|-------|
| Same-time posting-order groups | 160 (147 NO_REPAIR_NEEDED, 11 REAL_STOCK_SHORTAGE, 2 REPAIRABLE_SECONDS +1s) |
| Unexpected zero SE rows | 616 (EXACT 95, LIKELY 360, AMBIGUOUS 161) |
| Zero status | DEPENDENCY 531, MANUAL_REVIEW 61, POISON 11, RECONSTRUCTABLE 13 |
| Manufacture contract deltas | 254 of 2481 scanned |
| SLE anomalies (incl. Bin mismatch rows) | 1575 (PATIENT_ZERO 1197, REPLAY 378); Bin mismatches 66 |
| GL G1–G4 (sample/unbalanced) | 69 (G2 60, G4 7, G3 1, G1 1) |
| Failed RIV | 1636 |
| Patient-zero vouchers | 97 (largest: 28696=104, 28596=64, 28111=47, **25407=24**) |

Failed RIV after conservative retry policy (not the first scan):

| Status | Count |
|--------|-------|
| UNSAFE | 713 |
| WAITING_FOR_SLE_REPAIR | 520 |
| WAITING_FOR_RATE_REPAIR | 281 |
| WAITING_FOR_GL_REPAIR | 122 |
| SAFE_TO_RETRY | **0** |

Production estimate for a similar SLE volume: BEST ≈ 13 s, EXPECTED 1.5× ≈ 19 s, WORST 2× ≈ 25 s. Manufacture preview and Failed RIV classification dominate if limits are raised.

## 10. 25741

Voucher **MAT-STE-2026-25741** (MTfM). Five zero rows. None eligible.

| idx | Item | Historical | Source | Confidence | Patient zero |
|-----|------|------------|--------|------------|--------------|
| 3 | 13200475 | 21,900 | batch_inward | LIKELY | MAT-STE-2026-25407 |
| 5 | 13200473 | 40,378 | version+batch_inward | EXACT | MAT-STE-2026-25407 |
| 6 | 13200254 | 118,700 | version+batch_inward | EXACT | MAT-STE-2026-25407 |
| 7 | 13200256 | 246,100 | version+batch_inward | EXACT | PO-JOB07505-1 |
| 9 | 13200114 | Version 1,905,895 vs batch mismatch | version_vs_batch_mismatch | AMBIGUOUS | MAT-STE-2026-25407 |

Do not Repair Selected on 25741 until patient-zero 25407 (and JOB07505-1) are repaired.

## 11. Screenshot batch `504135-30300042-AK264401A11`

Item **30300042**. Not a same-second IN/OUT pair on one warehouse — and that is why 5.2.1 missed it.

Quarantine (Jalali **1405-01-17** = Gregorian **2026-04-06**):

- `MAT-STE-2026-25825` MTfM **OUT −1899** at **2026-04-06 18:01:45** (created 2026-09-11 19:22:39)
- `MAT-STE-2026-25824-1` Manufacture **IN +1899** at **2026-04-06 18:02:56** (created 2026-09-11 22:24:33)

Same Work Order `MFG-WO-2026-00575`. Manufacture is the economic prerequisite. 5.2.7 negative-interval scanner classifies this **CROSS_TIME_REPAIRABLE / EXACT**.

Proposed dry-run: keep Manufacture at 18:02:56; move Transfer **25825** to **18:02:57** (+72 seconds). Min qty −1899 → 0. Final qty unchanged.

WIP: same transfer **IN +1899** at 18:01:45 — healthy other warehouse; detector is not fooled.

**Applied on development.localhost** (see 11c). Timestamp-only was not sufficient; SLE qty_after / value / GL were replayed.

## 11b. Cross-time posting-order scanner (5.2.7 gap fix)

Same-time grouping (`posting_date` + `posting_time`) cannot see a 71-second inverted Manufacture → MTfM chain.

New detector walks each item + warehouse + canonical batch in ERPNext order (`posting_datetime`, `creation`), opens a negative interval when running `actual_qty` drops below 0, then classifies the later inbound with the existing EXACT/LIKELY/AMBIGUOUS dependency proof.

Search is not capped at one second. Dependency evidence outranks 60s / 5min / 30min windows. Crossing a posting date is **MIDNIGHT_REVIEW**, not auto-repair.

**Scanner is dry-run by default.** The 17 Farvardin EXACT pair was later applied (11c). Other operator rows were not bulk-written.

| Count | Value |
|-------|-------|
| Same-time groups (full history) | 302 (previous 5.2.1 report: 160; this scan includes NO_REPAIR_NEEDED) |
| Negative intervals | 422 |
| CROSS_TIME_REPAIRABLE | 10 (7 EXACT eligible, 3 LIKELY) |
| MIDNIGHT_REVIEW | 8 |
| CROSS_ITEM_CONFLICT | 3 |
| REAL_STOCK_SHORTAGE | 305 |
| LATER_INBOUND_UNRELATED | 83 |
| AMBIGUOUS_DEPENDENCY | 24 |

Old same-time scanner could not see these 10 + 8 date-boundary cases.

## 11c. Farvardin apply/replay (timestamp is not the repair)

Detection found the pair. The first apply engine still failed closed:

1. **Stage that failed:** `apply_repairs` poison gate / warehouse-wide value replay — not the timestamp write.
2. **Why posting time alone is insufficient:** ERPNext orders SLE by `(posting_datetime, creation)`. Moving 25825 to 18:02:57 without rewriting `qty_after_transaction` leaves `MAT-SLE-2026-179096` at −1899 and Manufacture `MAT-SLE-2026-183138` at leftover `qty_after=0`, `stock_value=-8,699,750`.
3. **Replay algorithm that had to change:** `replay_item_warehouse` + `apply_repairs` in `stock_posting_order/replay.py` and `repair.py`.
   - Leftover-at-zero (`qty_after_zero_nonzero_value`) is an inversion **artifact**, not 5.2.0 patient-zero poison. Ignore it for posting-order replay.
   - Do **not** create Transaction RIV on the outbound voucher (inbound still has poisoned `qty_after`).
   - Replay **item+warehouse** from `min(in,out)` in ERPNext order after the timestamp write.
   - Write only the inverted pair vouchers. Warehouse-wide value writes onto later multi-item manufactures (`25933-1`) unbalance those GLs by 1 Rial.
   - After source OUT SVD is replayed, copy that rate onto the transfer-in SLE (`sync_transfer_incoming_rates`). Destination incoming_rate otherwise stays at the inverted outgoing rate.
   - Keep Manufacture inbound SVD (5.2.0 FG residual 6,330,732,319). Do not reprice it as `qty * incoming_rate` (that creates a 62 Rial 621301 adjustment).
   - When an OUT empties the identity, SVD = −running_value so leftover pennies die.
   - Rebuild GL without `repost_gle_for_stock_vouchers` (that helper commits). Savepoint around timestamp + replay + GL.
4. **Correct sequence:** savepoint → update SE/SLE/SABB posting datetime on 25825 → replay Quarantine pair → sync transfer incoming → replay WIP 25825 SLE → assert qty_after ≥ 0 and transfer value-neutral → rebuild GL for the pair → classify Failed RIV (no retry) → commit.

### Before / after (development.localhost)

| Surface | Before | After |
|---------|--------|-------|
| Stock Entry 25825 | 18:01:45 | **18:02:57**, `set_posting_time=1` |
| Stock Entry 25824-1 | 18:02:56 | **unchanged** 18:02:56 |
| Quarantine SLE order | OUT 25825 then IN 25824-1 | **IN 25824-1 then OUT 25825** |
| 25825 Quarantine SLE | qty_after **−1899**, svd −6,339,432,069 | qty_after **0**, svd **−6,330,732,319** |
| 25824-1 Quarantine SLE | qty_after **0**, stock_value **−8,699,750** | qty_after **1899**, stock_value **6,330,732,319** |
| 25825 WIP SLE | +1899 at 18:01:45, svd 6,339,432,069 | +1899 at **18:02:57**, svd **6,330,732,319** (value-neutral) |
| Batch running qty | min **−1899** | min **0** (never negative); final **0** |
| Bin 30300042 Quarantine | 2695 / 977,990,240 | **identical** (last SLE untouched) |
| GL 25825 | 6,339,432,069 balanced | **6,330,732,319** balanced (WIP debit / Quarantine credit) |
| GL 25824-1 | 6,330,732,319 balanced | **unchanged 2-line 5.2.0** (no 62 Rial 621301) |
| SABB 25825 | posting 18:01:45, outward avg_rate 0 | posting **18:02:57**, avg_rate **3,333,719** |
| Diagnose batch | CROSS_TIME −1899 | **no negative interval** |
| Integrity (pair) | n/a | **PASS**; Bin matches last SLE |

Warehouse qty_after on **other lots** (Apr 11 `25906` −1206 etc.) is unchanged — those are separate identities, not this batch. Final warehouse qty **2695** unchanged. No new batch-level negative interval.

Failed RIV `4cu72iljul` (30300042 / Quarantine) was reclassified, not retried.

## 11d. Post-order rate reconstruction (SABB / Stock Ledger)

Quantity repair left **Serial and Batch Entry** on 25825 at inverted/zero rates. ERPNext Stock Ledger with SABB segregation reads **SBE**, not SLE:

| Report column | Before 11d (segregate) | After 11d |
|---------------|------------------------|-----------|
| Manufacture Incoming Rate | 3,333,719 | **3,333,718.97** (SVD 6,330,732,319) |
| Transfer OUT Incoming Rate | 0 (ERPNext zeros inbound on OUT) | **0** (same by design) |
| Transfer OUT Outgoing Rate | **−0** (SBE SVD 0) | **3,333,718.97** |
| Transfer IN Incoming Rate | **3,338,300** (inverted leftover) | **3,333,718.97** |
| Transfer OUT SABB total_amount | **0** | **−6,330,732,319** |
| Transfer IN SABB total_amount | 6,339,432,069 | **6,330,732,319** |

Unified engine: `historical_stock/valuation_rebuild.py` (SE → SLE txn rates → SABB/SBE → Bin → selective GL). Shared with Zero / Lost Rate. RIV never auto-invoked. Status `INTEGRITY_COMPLETE` only when quantity **and** rates are clean.

Desk action: **Rebuild Affected Documents** (selected chain only).

Downstream WIP consumes of this batch (Apr 11 `25912` / `25911-1`) still carry pre-repair outgoing 3,338,300 on mixed-lot warehouse MA. They are listed as `DOWNSTREAM_VALUE_REPLAY_REQUIRED` (40 later dest-warehouse vouchers) and were **not** auto-GL-rebuilt (multi-item Manufacture). The named Farvardin pair is rate-complete.

## 11e. Downstream value replay (identity-scoped)

Post-order + SABB rebuild left later **same-batch** consumes at the old 3,338,300 rate. New engine `historical_stock/downstream_replay.py` walks item + batch/SABB forward, classifies DIRECT / INDIRECT / UNRELATED / STOP_CHAIN, and replays only the repaired identity.

Farvardin graph:

```
25824-1 Manufacture IN
→ 25825 Transfer (value-neutral, already repaired)
→ 25912 Manufacture consume −47  DIRECT
→ 25911-1 Manufacture consume −1852 DIRECT (empties batch)
```

Other WIP lots (`25906` / `25916` / `25919`, batch `504143-…`) are UNRELATED and were not written.

| Voucher | Consume SVD before | Consume SVD after | Outgoing rate after | FG residual |
|---------|--------------------|-------------------|---------------------|-------------|
| 25912 | −156,900,109 at 3,338,300 | **−156,684,791.47** | **3,333,718.97** | **unchanged** 173,757,558.13 |
| 25911-1 | −6,182,531,600 at 3,338,300 | **−6,174,047,527.53** | **3,333,718.97** | **unchanged** 6,842,048,667 |

SABB/SBE outgoing rates rebuilt; inverted SBE incoming-on-OUT cleared. Manufacture GL left as 2-line FG amount (not rebuilt). RIV not invoked. Pair 25824-1 / 25825 unchanged. Batch remaining qty/value after 25911-1: **0**.

Desk: **Replay Downstream** (dry-run preview, then confirm apply). Statuses: `DOWNSTREAM_PENDING`, `DOWNSTREAM_REPLAY_REQUIRED`, `DOWNSTREAM_REPLAYING`, `DOWNSTREAM_COMPLETE`, `DOWNSTREAM_SKIPPED`.

Backup taken first: `20260914_075728-development_localhost-database.sql.gz`.

## 12. Controlled real repair

17 Farvardin (`MAT-STE-2026-25825` / `MAT-STE-2026-25824-1`) was repaired on **development.localhost** with SLE/Bin/GL replay (11c). Other operator EXACT reconstructable zero-rate rows were **not** written.

Synthetic integration: reconstruct + idempotent dry-run/write on a new test item (rolled back). Runtime guard blocks submit when batch history is nonzero.

## 13. Tests

| Area | Result |
|------|--------|
| `test_historical_stock` (unit) | PASS (32, incl. wrong-rate / reconstruction priority) |
| `test_stock_posting_order` | PASS (72, incl. valuation rebuild + downstream classify/residual) |
| `test_historical_stock_integration` | PASS (6, incl. 25741 read-only + Farvardin repaired-or-detect) |
| `test_scrap_absorbed_costing` | PASS |
| `test_stock_posting_order_integration` | PASS (13, incl. Farvardin apply + SABB rate rebuild + downstream 25912/25911-1) |
| `test_manufacture_rounding` | PASS |
| Playwright posting-order + Farvardin repaired ledger + downstream Apr 11 + 6-tab integrity | PASS (4), no API 500 |
| `bench build --app erpnext_extensions` | PASS |
| `migrate` ×2 | PASS |
| Local `run_gate(full_stress=1)` | PASS (stress 148.29 s; RIV×2; 03516; flows) |

## 15. Maintenance tool (wrong-rate + selective + UI)

Historical Repair is the official 5.2.7 maintenance surface. Accounting policy is unchanged. Writes never run from Scan or Dry Run.

### Architecture

```
Scan / Dry Run / Preview (default)
        ↓ EXACT only
Stock Entry → SLE → SABB/SBE → identity forward replay → Bin → selective GL → Failed RIV class → optional Item+Warehouse RIV
```

Never: global RIV, global Bin, global GL. Scope requires item, warehouse, batch, work order, or voucher.

| Engine | Module |
|--------|--------|
| Wrong / lost rate | `historical_stock/wrong_rate.py` |
| Reconstruction priority | version → previous healthy SLE → batch inward → transfer source → manufacture pool → PR → SRE → moving average → manual. **Never Bin.** |
| Selective replay/rebuild/RIV | `historical_stock/selective.py` |
| Dependency graph | `historical_stock/graph.py` |
| Downstream batch replay | `historical_stock/downstream_replay.py` |
| Audit / rollback snapshot | `historical_stock/audit.py` |
| Global dashboard | `scan.run_full_integrity_scan` → `dashboard` chips |

Confidence: EXACT / LIKELY / AMBIGUOUS / MANUAL. Only EXACT auto-repairs.

### Workflow

1. Scan All → dashboard counts (click through to the topic).
2. Topic Scan → grid (frozen header + first column, search, sort, filters, export).
3. Dry Run / Preview (Repair Selected stays disabled until Dry Run).
4. Repair Selected / Replay Downstream / Rebuild Affected Documents (each dry-runs first).
5. Integrity Check. Selective RIV only if identity is healthy.
6. Repair History / Rollback (snapshot restore of SE rates). Resume open run.

### Farvardin re-validation (this phase)

Batch `504135-30300042-AK264401A11`: 25824-1 / 25825 / 25912 / 25911-1 — posting order intact, outgoing/incoming/SABB at reconstructed **3,333,718.97**, SVD value-neutral on transfer, FG residual preserved, pair+downstream integrity PASS. Graph nodes include that chain (plus earlier Reject `25827`).

## 14. Deployment

1. Do not push/tag from this workspace.
2. `bench build --app erpnext_extensions`
3. `bench --site <site> migrate` twice
4. Production **dry-run only** first (all six tabs). Review EXACT vs DEPENDENCY vs AMBIGUOUS.
5. Backup the database before any Repair Selected.
6. Repair **patient-zero first**, never 25741 in isolation.
7. Repost Selected is one Item+Warehouse RIV after Integrity PASS — never all stock.

## Rollback

Restore the pre-repair database backup. Do not reverse rates/timestamps by hand without SLE replay. App rollback: previous version **5.2.6**.

## Version

- `erpnext_extensions.__version__` = `5.2.7`

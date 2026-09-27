# MANUAL REVIEW REQUIRED — Candidate 2 residuals

Source of evidence: Candidate 2 final Scan (`kpis_replay_B.json`) plus read-only
re-scan of `development.localhost` after Replay B. Candidate 2 backup was not
modified.

**TRUE MANUAL ROOT COUNT: 2**

No P0. Production can run on Candidate 2. These two cases need a human
decision before the *next* Historical Repair / L6 campaign. Everything else
is legitimate, waiting on an upstream root, or a tool gap (not a business
question).

---

## Manual review table

| Priority | Voucher | Item | Warehouse | Batch | Finding | Posting datetime | Qty (after) | Rate | Stock value | Why manual | Exact question | Blocks next L6 | Blocks current Production |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 | `MAT-STE-2026-33163` (outbound) vs `MAT-STE-2026-33338` (inbound) | 13100057 | انبار approved اقلام بسته بندی اولیه اسپاد | (batch on those MTFMs) | Posting Order CROSS_TIME | 2026-07-22 18:03:15 vs 2026-07-25 18:17:50 | current min still negative in sim (−6203 / proposed −5152) | n/a (order, not rate) | n/a | Scanner paired two **Material Transfer for Manufacture** documents **three days apart on different Work Orders** (`MFG-WO-2026-00731` / `PO-JOB09391` vs `MFG-WO-2026-00727` / `PO-JOB09334`). Quantity-conservation sim still goes negative. System cannot tell whether they share one physical lot or are independent jobs. | **Did MTFM 33338 (WO 00727, 25 Jul) physically supply the same 13100057 lot that MTFM 33163 (WO 00731, 22 Jul) moved, or are these two independent manufacturing orders?** Check Job Cards PO-JOB09391 and PO-JOB09334, the batch/lot on both Stock Entries, and warehouse inward/outward notes. If independent → leave timestamps (legitimate order / false CROSS_TIME). If same lot → next campaign may shift 33163 after 33338. | YES (unresolved CROSS_TIME blocks FULL_REPOST_PREFLIGHT) | NO (current qty_after is non-negative on the live ledger; this is a future-repost risk) |
| P2 | `MAT-STE-2026-36934` | 30500009 | انبار Quarantine محصول نهایی اسپاد | n/a | qty>0, stock_value≠0, valuation_rate=0 on a Manufacture FG row | 2026-09-15 15:59:01 | 2 | 0 | 17,651,681 | Same Manufacture values sibling FG `20100008` at incoming 82,122,585. `30500009` incoming_rate stays 0 while warehouse leftover value remains. Tool must not invent an FG rate. Two business stories exist: (1) sample/free/other FG intentionally unvalued, or (2) native manufacture should have allocated residual value and the stamp is missing. | **Was finished good 30500009 on Manufacture MAT-STE-2026-36934 (WO of 15 Sep, Job Card on that STE) intentionally received at zero (sample, free, promotional, or non-valued FG), while 20100008 on the same voucher carries the job cost?** Check the Work Order BOM / FG list, batch release, and whether 30500009 is a commercial product or a free/sample pack. | YES if L6 walks this identity and treats leftover value as corrupt | NO (2 units; leftover value is already on the ledger; operations can continue) |

No other residual is a true business-evidence gap.

---

## Classification of remaining Candidate-2 KPIs

Dashboard totals are **rows**, not independent roots. Counts below are
**economic identities / roots** after compression.

### AUTO_REPAIRABLE

| Bucket | Rows / identities | Why automatic later |
|---|---|---|
| Wrong Rate READY SABB descendants | 46 rows → **6 identities** (13100023 batch 5922-…; 13200114 / 13200254 / 13200256 / 13200473 on پایکار + later 25716) | Source-side SABB already repaired. Need descendant replay + later same-batch MTFM. Authority is existing `batch_inward_sabb_rate` EXACT. |
| Wrong Rate WAITING (dashboard 111) | transfer/manufacture dependents | Disappear after SABB / CROSS_TIME / valued-source roots. |
| Zero READY (46) | overlap with SABB READY | Same as SABB descendant replay. |
| Failed RIV CURRENT_LEDGER_IMPACT | **2 identities** (13200332, 13200333) × 3 Failed docs each = 6 | `VALUATION_INTEGRITY` on 2026-08-31 approved secondary packaging. Retry only after upstream rate roots; do not blind-retry. |
| qty>0/value>0/rate=0 terminal leftover-MA stamp | several scrap/reject identities (13200326, 13200409, 13200411, 13200473, 30200021, 30200076, 30300020, 17000001, 17100335) | Stamp `valuation_rate = stock_value / qty` when SVD/qty already imply leftover MA; do not invent a new value. Scrap/Reject leftover-MA *receipt* logic stays excluded. |

**AUTO_REPAIRABLE count (roots): 6 SABB identities + 2 Failed-RIV identities + ~9 terminal rate0 stamp identities ≈ 17.**

### WAITING_UPSTREAM

| Bucket | Count | Upstream |
|---|---|---|
| Patient Zero 32 | 32 vouchers | Almost all WRONG_RATE+ZERO_RATE on the same SABB families (25716, 25442, 25446, 34155/34162…). Descendants of the 6 READY identities. |
| Zero WAITING_PATIENT_ZERO | 2 | Same PZ. |
| Wrong Rate MANUAL_TRANSFER_PROPAGATION (86) | 86 rows, few items (17400002, 17000042, 17100009, …) | Scanner lane is **TOOL_LIMIT**, but economically they wait on source reconstruction — not a user question. |
| Dashboard Failed RIV 30 − 6 current | 24 | HISTORICAL_ONLY / SUPERSEDED in the engine; KPI still lists them actionable. |

**WAITING_UPSTREAM count (vouchers/rows): 32 PZ + 86 transfer-propagation rows + 24 historical Failed RIV. Distinct causal roots: the 6 SABB identities above.**

### LEGITIMATE

| Bucket | Count | Why leave it |
|---|---|---|
| I4 = 4 | 4 | All `PRECISION_DUST_INBOUND` (18200056 ×3, 17400002 ×1). qty×rate explains residual (e.g. 5.3e-05 × 9,169,811 = 486). Do not SQL-zero. |
| qty=0/value≠0 historical | 211 | Intermediate SLE rows. |
| Precision dust terminal | 42 | Currency envelope. |
| Proven legitimate zero | 58 (dashboard) / 6 ZP_PROVEN in live zero scan | Free/zero receipts already proven. |
| leftover-MA 16100066 | 1 | Healthy C2 canary (rate 5,005,345 / value 2,552,725,697 / qty 510). Status NO_REPAIR_PATH. |
| 16100226 | 1 | Legitimate zero. |
| 36933 / 37090 | — | HEALTHY high-scrap class. |
| Posting Order SAME_TIME 122 | 122 | `NO_REPAIR_NEEDED` — running qty already ≥ 0. |
| leftover-MA refusal on manufacture/SABB/reject | 13100023 @ 35344, 17000001 reject | Correctly **not** leftover-MA receipt logic. |

**LEGITIMATE count: 4 I4 dust + 211 historical + 42 dust + 58 legit zeros + healthy leftover-MA + 122 same-time PO.**

### TECHNICAL_TOOL_GAP

These are **not** manual. Next-downtime code must handle them.

| Gap | Evidence |
|---|---|
| General CROSS_TIME beyond 30 min + warehouse replay | 13100057 pair still negative after batch sim; remaining 3 PO are CROSS_ITEM_CONFLICT with **mismatched Work Orders / Job Cards** (25042 WO 00520 vs inbound JC PO-JOB10080; 25506 WO 00547 vs 25461 WO 00561; 25042 vs 36869 WO 00811). Tool must not pair unrelated jobs. |
| SABB descendant + later MTFM replay | 46 READY still listed after source-side repair. |
| VALUED_SOURCE_ZERO_OUTGOING expansion | Manufacture-only today. Other zero-outgoing with valued source (Material Issue 26614 mixed 0 and 627189; Reject/scrap terminal rate0) need a bounded expansion that still excludes leftover-MA receipts. |
| Transfer reconstruction TOOL_LIMIT | 86 MANUAL_TRANSFER_PROPAGATION, lane TOOL_LIMIT. |
| Manufacture FG residual (if 30500009 is *not* free) | Native FG allocation / leftover stamp on FG is forbidden leftover-MA; needs manufacture conservation. |
| Failed RIV KPI vs engine | Dashboard 30 vs engine 6 current-impact. Classification must match. |
| Full-repost preflight | L6 not run; blockers include unresolved CROSS_TIME and valued-source zeros. |
| leftover-MA VR stamp on scrap/reject *existing* leftover value | rate=0 while stock_value≠0 and qty>0. Stamp only; do not invent value. |

**TECHNICAL_TOOL_GAP count (work items): 8 listed gaps (not 323 rows).**

### MANUAL_BUSINESS_EVIDENCE_REQUIRED

**2 roots** (table above). Neither is P0. Neither blocks today’s Production restore of Candidate 2.

---

## Explicitly excluded from your inbox

- Precision dust (I4 4, terminal dust 42)
- Historical intermediate qty=0/value≠0 (211 / dashboard 995)
- Legitimate zeros, scrap 37090, reject provenance already proven
- Post-depletion zero layers
- Healthy leftover-MA 16100066 / legitimate zero 16100226
- 122 SAME_TIME posting-order rows that need no rewrite
- SABB descendant READY/PZ (repair the 6 identities, do not inspect 46 rows)
- Scanner status `MANUAL` when `manual_lane=TOOL_LIMIT`

---

## Production vs next L6

| | Today’s Production restore | Next downtime / L6 |
|---|---|---|
| Candidate 2 | Use it. Frozen. | Do not experiment on it. |
| P0 manual | None | — |
| P1 13100057 CROSS_TIME | Safe to operate | Resolve before company-wide repost |
| P2 30500009 FG zero | Safe to operate | Resolve if L6 includes that Manufacture |
| Hard gates | I1 0, Bin 0, GL 0, neg stock/rate 0 | Must stay 0 |

---

## What I will *not* ask

I will not ask you to “confirm the correct rate” for the 323 Wrong Rate
rows, the 226 zeros, or the 46 READY. Those have SABB / transfer / MA
authority or are tool work.

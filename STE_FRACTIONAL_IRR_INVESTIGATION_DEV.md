# Read-only investigation — 512 fractional IRR STE SLEs

**Site:** `development.localhost` only  
**App version:** `5.7.18`  
**Mode:** Investigation / diagnostics only — **no mutations, no RIV, no backup, no Production**  
**Evidence JSON:** `STE_FRACTIONAL_IRR_INVESTIGATION_DEV.json`  
**JE `ACC-JV-1405-169046`:** not touched  
**Asset PRs:** not touched  

---

## 1. Inventory

| Stock Entry type | Frac SLE rows | Vouchers | Item+WH identities | Frac field |
|------------------|---------------|----------|--------------------|------------|
| Manufacture | 343 | 251 | 42 | `incoming_rate` only |
| Material Transfer for Manufacture | 96 | 96 | 18 | `incoming_rate` only |
| Material Transfer | 70 | 37 | 17 | `incoming_rate` only |
| Repack | 3 | 3 | 2 | `incoming_rate` only |
| **Total** | **512** | **387** | — | — |

- **Outgoing rates** on these vouchers’ consumption/source legs are **integer** in all analyzed cases.
- **384 / 387** vouchers also still store fractional `Stock Entry Detail.valuation_rate`.
- **3 / 387** have integer SED rates but fractional SLE `incoming_rate` (`PO-JOB07515`, `PO-JOB12371`, `PO-JOB12572`).
- Overlap with the 11 already-corrected PR identities: **0**.
- Company currency: **IRR** only for these rows (integer-IRR rule scope).

---

## 2. Root cause by transaction type

### 2.1 Manufacture (343) — two subtypes

**A. Finished goods absorb (118 SLE)**  
- Flag: `is_finished_item=1` (FG items `201*`, also SFG `301*`/`302*`/`303*` when finished).  
- Mechanism: consumption pool (integer outgoing) is absorbed into FG as  
  `incoming_rate ≈ pool_amount / fg_qty` → **IEEE float**, persisted on SED + SLE.  
- Example `MAT-STE-2026-29578` / `20100096`:

| Leg | Qty | Rate | SVD |
|-----|-----|------|-----|
| Consumption `30300052` | −716 | **3,118,453** (int) | −2,434,592,678.85 |
| FG incoming (row 1) | +300 | **6,800,538.209066666** | 2,040,161,462.72 |
| FG incoming (row 2) | +58 | **6,800,538.209137931** | 394,431,216.13 |

- Expected integer IR (HALF_UP): `6,800,538`  
- Class A residual example (row1): `SVD − qty×int = 62.72` (amount-authoritative; not Class B).

**B. Packaging / reject return incoming (225 SLE)**  
- Items `132*` (secondary packaging), mostly into  
  `انبار ضایعات اقلام اسپاد` (209) / reject WH (16).  
- `is_finished_item=0`, `is_scrap_item=0`, `t_warehouse` only.  
- Same economics: allocated return value / qty → fractional SED/SLE rate.  
- Example `MAT-STE-2026-24872-2` / `13200144`: IR `88,567.984…`, qty 117, SVD `10,362,454.14`  
  → expected int `88,568`, Class A residual ≈ `−2`.

**Classification:** **Class A reconstructable** for all 343 (consumption integer; incoming float = amount÷qty).

### 2.2 Material Transfer for Manufacture (96)

- Same item moves source → shop-floor; **value conserved**.  
- SED stores fractional `basic_rate = valuation_rate = amount/qty`.  
- Source SLE often shows **integer** `outgoing_rate` while **SVD keeps the fractional conserved amount** (rate field ≠ SVD/qty).  
- Destination SLE `incoming_rate` matches SED float.

Example `MAT-STE-2026-25752` / `30100012` qty 2978:

| Field | Value |
|-------|-------|
| SED VR / basic | 1,572,909.524667562 |
| SED / SLE amount (SVD mag) | 4,684,124,564.46 |
| Source `outgoing_rate` | **1,572,911** (int) |
| Source SVD | −4,684,124,564.46 |
| Dest `incoming_rate` | **1,572,909.524667562** |
| Expected int IR | **1,572,910** |
| Class A residual vs conserved SVD | `4,684,124,564.46 − 2978×1,572,910 = −1,415.54` |

**Origin:** transfer/repack **valuation propagation + amount conservation**, not BOM formula per se.  
**Classification:** Class A.

**Subtype (3 vouchers):** integer SED, fractional SLE IR with **integer SVD**  
(`PO-JOB07515`, `PO-JOB12371`, `PO-JOB12572`):  
`incoming_rate = SVD / qty` **without** ROUND_HALF_UP  
(e.g. `4,400,547,964 / 3000 = 1,466,849.32133…`).  
Still Class A vs integer IR `1,466,849`.

### 2.3 Material Transfer (70)

- Same pattern as MTfM (conservation + SED float → dest IR float).  
- Example `MAT-STE-2026-27631` / `30300052` qty 315:  
  IR `1,457,323.646888889`, SVD `459,056,948.77`, source OR int `1,306,979`.  
  Expected int IR `1,457,324`; Class A residual ≈ `−111.23`.

### 2.4 Repack (3)

- FG (or dual) row absorbs outgoing value / qty as float rate.  
- Example `MAT-STE-2026-29903` / `30200048` qty 659:  
  IR `1,650,366.45657…`, SVD `1,087,591,494.88`, source OR int `1,399,938`.  
  Expected int `1,650,366`; Class A residual ≈ `300.88`.  
- Note: this sample currently has **no GL rows** (`gl_debit=0`) while SLE nets to 0 — ledger completeness must be checked before any rebuild; **GL balance alone is not acceptance**.

---

## 3. Code paths (v5.7.18)

### Paths that *should* enforce integer IRR rates (live submit)

| Step | Location |
|------|----------|
| SED rate-first align | `domain/qty_rate_amount.align_stock_entry_item_amounts` via `stock_entry.validate_stock_entry` / `apply_irr_manufacture_economic_finalize` |
| FG residual polish | `manufacture_rounding.align_manufacture_finished_good_residual` → `integer_valuation_rate_from_amount` |
| Output contract | `manufacture_output_contract` / `scrap_costing` (integer VR writers) |
| SLE sync from SED | `domain/stock_entry_sync._sle_rate_from_row` / `_apply_native_rates_to_sle` / SABE align → `round_monetary_rate` |
| Transfer inward conservation | `conserve_transfer_inward_from_outward_sle` → `round_monetary_rate(magnitude/qty)` |
| RIV re-align | `domain/riv_valuation_guard` calls `align_stock_entry_item_amounts` when `through_repost_item_valuation` |

### Why historical SLE still fractional

1. **Submitted SED still holds float VR** (384 vouchers) — ledger was posted from those floats and never rebuilt.  
2. In-memory **align-only simulation** on representatives → **all SED rates become integer** (no save performed). So the primary defect is **persisted historical document + SLE state**, not “math that cannot integerize.”  
3. Transfer legs can leave **integer outgoing_rate with non-integer SVD** (representational rate vs conserved amount) — reconciliation must use **SVD/amount**, not rate×qty alone.

### Paths that can still persist / reintroduce fractional IRR

| Risk | Detail |
|------|--------|
| Skip-align guards | `protect_closed_row_amounts`, `flags.iran_sle_authoritative_amount` skip rate-first rewrite |
| Historical repair helpers | `historical_stock/manufacture_native.py` computes `write_sle_rate = write_sle_amount / qty` **without** `integer_valuation_rate_from_amount` / `round_monetary_rate` |
| Rate = SVD/qty float stamp | The 3 `PO-JOB*` rows: integer SVD, float IR — sync wrote division result without HALF_UP |
| Post-align Core recalculate | If Core rewrites inward SED to float and Iran conservation reapplies amount but an older path stamps `incoming_rate = amount/qty` raw |
| Assuming GL balanced ⇒ fixed | All sample vouchers can be GL-balanced with fractional SLE; **not resolved** |

### FX / non-IRR

- `align_stock_entry_item_amounts` returns immediately unless `is_irr_company`.  
- No fractional STE SLE found outside IRR company `اسپاد فارمد دارو`.  
- Integer-IRR rule does not apply to foreign-currency commercial rates / non-IRR companies.

---

## 4. Proposed minimal 5.7.18-compatible remediations (not implemented)

### R1 — Historical STE chain rebuild (controlled, per identity)

For each affected Item+Warehouse (start with one per category):

1. Ensure SED `basic_rate` / `valuation_rate` / amounts pass `align_stock_entry_item_amounts` (+ manufacture finalize if Manufacture/Repack).  
2. Controlled **Item+Warehouse RIV** from earliest SLE date (not company-wide).  
3. Verify: integer `incoming_rate`/`outgoing_rate`, SVD vs `qty×int` Class A residual only, stock GL coherent with SVD, no new Class B.

### R2 — Harden SLE rate stamping (code, still 5.7.18)

- Any `rate = abs(svd)/qty` or `amount/qty` write into SLE/SED must go through `round_monetary_rate` / `integer_valuation_rate_from_amount` under IRR.  
- Fix/guard the `PO-JOB*` pattern (integer SVD + float IR).  
- Audit `historical_stock/manufacture_native.py` write path before any repair tool use.

### R3 — Do not use JE / Round Off as substitute

- Residuals after integerization are **Class A Stock Adjustment** (existing manufacture/PR policy), not manual JE, and not silent Round Off hiding.

### R4 — Transfer conservation contract

- Keep amount conservation; keep representational rates integer; document that `outgoing_rate` may differ from `\|SVD\|/qty` by Class A residual — acceptance checks must include SVD + GL stock accounts.

---

## 5. Regression tests required (proposed)

| Type | Test focus |
|------|------------|
| Manufacture FG | After align+finalize, FG `valuation_rate`/`basic_rate` integer; SLE IR integer after sync; Class A residual = amount − qty×IR |
| Manufacture packaging return (`132*`) | Non-FG incoming scrap/reject rows integerized; consumption OR stays integer |
| MTfM | Dest IR integer; conserved \|SVD\| matches source; no float IR when SVD integer |
| Material Transfer | Same as MTfM |
| Repack | FG absorb integer VR; net SLE 0; GL present and balanced when expected |
| FX / non-IRR company | Align no-op; rates unchanged |
| Guard | `PO-JOB*`-shape: integer SVD must not yield float `incoming_rate` |

---

## 6. Controlled test plan (one identity per category) — **do not run yet**

| # | Category | Candidate voucher | Identity (item @ WH) | Steps when authorized |
|---|----------|-------------------|----------------------|------------------------|
| 1 | Manufacture FG | `MAT-STE-2026-29578` | `20100096` @ FG WH from SLE | Before snapshot → align+RIV identity → after IR/SVD/GL |
| 2 | Manufacture packaging | `MAT-STE-2026-24872-2` | `13200144` @ `انبار ضایعات اقلام اسپاد` | Same |
| 3 | MTfM | `MAT-STE-2026-25752` | `30100012` @ shop-floor WH | Same; also check source WH |
| 4 | Material Transfer | `MAT-STE-2026-27631` | `30300052` @ dest WH | Same |
| 5 | Repack | `MAT-STE-2026-29903` | `30200048` @ dest WH | Same; **investigate missing GL** before claiming recon |
| 6 | Integer-SVD float-IR | `PO-JOB07515` | `30100060` @ dest WH | Prove R2 fix / RIV clears float IR |

**Stop criteria per test:** integer IRR rates on SED+SLE; Class A only; SLE stock value and stock GL reconciled; no touch to Asset PRs or `ACC-JV-1405-169046`.

---

## 7. Explicit non-claims

- These 512 rows are **not resolved**.  
- In-memory align simulation ≠ ledger correction.  
- GL balanced ≠ SLE integer-rate compliance.  
- Production not investigated.  
- No fix implemented; no RIV executed in this investigation.

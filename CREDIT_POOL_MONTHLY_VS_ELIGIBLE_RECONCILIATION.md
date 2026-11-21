# Credit Pool: Monthly Credit Amount vs Eligible Credit — Reconciliation

**Environment:** `development.localhost` (`_19b2f83259cbf92a`)  
**Version:** `erpnext_extensions 5.8.5`  
**Mode:** READ-ONLY investigation (no code/data changes)  
**Currency:** IRR  
**Report:** Script Report `MFG Cost Control Credit Pool`  
**Date:** 2026-11-21

---

## Executive answers (section 8)

| # | Question | Answer |
|---|---|---|
| 1 | What does **Monthly Credit Amount** represent? | Raw period total of **non-cancelled** `GL Entry.credit` on the Labor/Overhead control account for the Persian month. Persisted as `labor_period_credit` / `overhead_period_credit`. |
| 2 | What does **Eligible Credit** represent? | Sum of **line-level remaining** amounts on Landed Cost Taxes and Charges lines classified as `eligible` (after subtracting prior Credit-Allocation consumption). Persisted as `labor_eligible_credit` / `overhead_eligible_credit`. |
| 3 | Why are they different? | Monthly Credit counts every GL credit. Eligible keeps only LCV lines that pass `_classify_landed_line`. Differences equal **unresolved** (and, if any, PA-consumed / excluded) line amounts. In these six months the entire nonzero gap is **unresolved**. |
| 4 | Which column is used for allocation? | **Eligible Credit** (`identification.labor_eligible` / `overhead_eligible`), **not** Monthly Credit Amount. |
| 5 | Is the current allocation result correct? | **Yes, relative to current 5.8.5 rules:** Allocated Amount = Eligible Credit for every month; Remaining = 0; no double allocation; Labor/Overhead independent. |
| 6 | Is any correction required? | **No code/data correction in this investigation.** Optionally review whether “Corrective Operation Cost” LCV lines without `has_operating_cost` / operation markers should become eligible (business-rule decision — not implemented here). |

---

## 1. Definition of Monthly Credit Amount

### Report mapping

| Report column | Fieldname | Persisted parent field |
|---|---|---|
| Monthly Credit Amount | `monthly_credit_amount` | `labor_period_credit` or `overhead_period_credit` |

**Code:**

- Report columns: `erpnext_extensions/costing/report/mfg_cost_control_credit_pool/mfg_cost_control_credit_pool.py` (`get_columns`, lines ~35–40)
- Row builder: `erpnext_extensions/iran_accounting/fg_cost_allocation/control_credit/reporting.py` → `fetch_control_credit_pools` maps `period_credit` → `monthly_credit_amount` (lines 101–102, 139)
- Populated by: `control_credit/engine.py` → `calculate_control_credit_allocation` from `identification.labor_period_credit` / `overhead_period_credit` (lines 71–72)
- Computed in: `control_credit/identify.py` → `identify_control_credits` (lines 174–199)

### Exact calculation logic

```sql
-- Conceptual equivalent (Overhead example)
SELECT SUM(credit)
FROM `tabGL Entry`
WHERE company = <company>
  AND account = <overhead_control_account>   -- 211237 - کنترل سربار - E
  AND posting_date BETWEEN <period_start> AND <period_end>
  AND IFNULL(is_cancelled, 0) = 0
  AND credit > 0
```

Source (`identify.py`):

```174:199:erpnext_extensions/iran_accounting/fg_cost_allocation/control_credit/identify.py
	gl_rows = frappe.db.sql(
		"""
		SELECT name, posting_date, account, voucher_type, voucher_no, cost_center,
		       debit, credit, is_cancelled, company
		FROM `tabGL Entry`
		WHERE company = %(company)s
		  AND account IN %(accounts)s
		  AND posting_date BETWEEN %(start)s AND %(end)s
		  AND IFNULL(is_cancelled, 0) = 0
		  AND credit > 0
		...
		"""
	)
	# Period credit totals from GL (informational / reconciliation) — credits only
	labor_period = Decimal("0")
	oh_period = Decimal("0")
	for g in gl_rows:
		amt = _money(g.credit, ccy)
		kind = control_kind_for_account(g.account)
		if kind == "labor":
			labor_period += amt
		elif kind == "overhead":
			oh_period += amt
```

### Clarifications (A)

| Question | Finding |
|---|---|
| Raw monthly `SUM(GL Entry.credit)`? | **Yes** (control accounts only, credits only). |
| Includes PA consumption postings? | **Yes, if those postings appear as GL credits** on the control account. They are **not** filtered out of Monthly Credit. |
| Cancelled / reversed vouchers? | **Cancelled GL rows excluded** (`is_cancelled = 0`). Debit-side reversals are not summed into this credit total. |
| Before line-level attribution? | **Yes** — period total is computed from GL only, before LCV classification. |
| Filters | Company, Labor+Overhead control accounts, Persian-month posting date range, `credit > 0`, `is_cancelled = 0`. No docstatus filter on Stock Entry itself. |

### Independent verification (this site)

| Period | GL `SUM(credit)` Overhead | Parent `overhead_period_credit` | Match |
|---|---:|---:|---|
| 1405-01 | 0 | 0 | PASS |
| 1405-02 | 0 | 0 | PASS |
| 1405-03 | 351,300 | 351,300 | PASS |
| 1405-04 | 190 | 190 | PASS |
| 1405-05 | 6,971,596 | 6,971,596 | PASS |
| 1405-06 | 75,218,486,393 | 75,218,486,393 | PASS |

1405-06 also has **21 cancelled** Overhead GL credits totaling **3,668,349,086 IRR** — correctly **excluded** from Monthly Credit.

---

## 2. Definition of Eligible Credit

### Report mapping

| Report column | Fieldname | Persisted parent field |
|---|---|---|
| Eligible Credit | `eligible_credit` | `labor_eligible_credit` / `overhead_eligible_credit` |

**Code path:** `reporting.py` ← `engine.py` ← `identify_control_credits` aggregation (lines 378–394).

### Exact calculation logic

1. For each GL credit, load matching `Landed Cost Taxes and Charges` rows on the same Stock Entry voucher with the same `expense_account`.
2. Classify **each LCV line** via `_classify_landed_line` (`identify.py` 119–149):
   - `consumed_by_product_allocation` — description contains `PA <hex>` and that Product Allocation exists  
   - `excluded` — linked MCA (`custom_manufacturing_cost_adjustment`)  
   - `eligible` — `has_operating_cost` / `operation_id` / `operating_component` set  
   - `unresolved` — otherwise (including “Corrective Operation Cost” text **without** flags)
3. For `eligible` lines:  
   `remaining = line_amount - prior_credit_consumption(...)`  
   Prior consumption = amounts already taken by **other** active Credit Allocation docs (`_prior_credit_consumption`, lines 84–116).
4. Pool total:  
   `labor_eligible` / `overhead_eligible` = **sum of `remaining_amount`** for sources with `eligibility_status == 'eligible'` (lines 378–394).

### Clarifications (B)

| Question | Finding |
|---|---|
| Which lines excluded from Eligible? | `consumed_by_product_allocation`, `excluded`, `unresolved`; also whole-GL unresolved when no LCV match / GL≠line sum residual. |
| PA-related LCV identification | Regex `\bPA\s+([0-9a-f]{8,})\b` on LCV **description**; verified against DocType `MFG Cost Product Allocation`. |
| Exclusion level | **Individual LCV line**, not whole GL voucher. Sibling lines on the same voucher can still be eligible. |
| Partially attributable / unresolved | Tracked separately; amounts go to `*_unresolved_amount`, **not** into Eligible. |
| Reversals | Cancelled GL credits never enter identification. |
| Previously allocated deducted here? | **Two different concepts:** (1) Prior **Credit Allocation** consumption reduces eligible via `remaining_amount`. (2) Report column “Previously Allocated (Product Allocation)” is **only** PA-linked consumed line totals (`*_consumed_by_product_allocation`), **not** prior credit docs. |

---

## 3. Difference by month

`Difference = Monthly Credit Amount − Eligible Credit`

### Overhead

| Month | Monthly Credit | Eligible Credit | Difference | Explanation |
|---|---:|---:|---:|---|
| 1405-01 | 0 | 0 | 0 | No Overhead GL credits in period. |
| 1405-02 | 0 | 0 | 0 | No Overhead GL credits in period. |
| 1405-03 | 351,300 | 351,288 | **12** | 4 unresolved LCV lines (“Non stock items”), sum 12. |
| 1405-04 | 190 | 188 | **2** | 1 unresolved LCV line (“Non stock items”), amount 2. |
| 1405-05 | 6,971,596 | 6,971,596 | **0** | All 8 source lines eligible. |
| 1405-06 | 75,218,486,393 | 75,157,941,821 | **60,544,572** | 25 unresolved LCV lines (see §4). |

**Identity (all months PASS):**

`Monthly Credit = Eligible + Unresolved + PA-consumed`

| Month | LHS Monthly | RHS Eligible+Unresolved+PA | Match |
|---|---:|---:|---|
| 1405-01…06 | (as above) | equal in every month | PASS |

Labor: Monthly = Eligible = Allocated = 0 for all six months (no Labor control GL credits).

### Nonzero difference source lines

#### 1405-03 (diff 12) — Allocation `MFGCCCA-2026-00003`

| GL Entry | Voucher | LCV row | Amount | Status / reason |
|---|---|---|---:|---|
| ACC-GLE-2026-1280089 | MAT-STE-2026-40955 | dn54cpom2h | 6 | unresolved — Landed Cost line could not be reliably classified |
| ACC-GLE-2026-1192227 | MAT-STE-2026-37782 | p8k1reu8jv | 2 | unresolved — … |
| ACC-GLE-2026-1195869 | MAT-STE-2026-41198 | barjqh0muj | 2 | unresolved — … |
| ACC-GLE-2026-1195890 | MAT-STE-2026-41177 | ceb44ocq3g | 2 | unresolved — … |

All descriptions: **Non stock items**. Flags: no PA, no operating markers, no MCA.

#### 1405-04 (diff 2) — `MFGCCCA-2026-00004`

| GL Entry | Voucher | LCV row | Amount | Reason |
|---|---|---|---:|---|
| ACC-GLE-2026-1188484 | MAT-STE-2026-41159 | 209e0v0891 | 2 | unresolved — Non stock items |

#### 1405-06 (diff 60,544,572) — see §4

---

## 4. Detailed 1405-06 source-level reconciliation

**Document:** `MFGCCCA-2026-00006`  
**Period:** 2026-08-23 … 2026-09-22  
**Control account:** `211237 - کنترل سربار - E`

| Component | Amount (IRR) | Count |
|---|---:|---:|
| Monthly Credit (GL sum) | 75,218,486,393 | 286 GL entries |
| Eligible Credit | 75,157,941,821 | 317 eligible LCV lines |
| Unresolved (excluded from Eligible) | 60,544,572 | 25 lines |
| PA-consumed | 0 | 0 |
| Excluded (MCA) | 0 | 0 |
| **Eligible + Unresolved** | **75,218,486,393** | — |

### Unresolved buckets

| Bucket | Lines | Amount |
|---|---:|---:|
| Description `Corrective Operation Cost` (large) | 3 | 60,544,471 |
| Description `Non stock items` (small) | 22 | 101 |
| **Total** | **25** | **60,544,572** |

### Three material unresolved lines

| Amount | GL Entry | Stock Entry | LCV row | Description | Classifier inputs |
|---|---:|---|---|---|---|
| 46,106,240 | ACC-GLE-2026-1198952 | MAT-STE-2026-43858 | pae33q61ga | Corrective Operation Cost | `has_operating_cost=0`, empty operation_id/component, no MCA, no PA token |
| 7,456,194 | ACC-GLE-2026-1191232 | MAT-STE-2026-25132 | 8ukcscc83i | Corrective Operation Cost | same |
| 6,982,037 | ACC-GLE-2026-1191236 | MAT-STE-2026-25179 | eadnrblkj2 | Corrective Operation Cost | same |

**Line-level (not voucher-level) exclusion evidence** — same vouchers still contribute eligible sibling lines:

| Voucher | Eligible lines / amount | Unresolved lines / amount | GL credit |
|---|---|---|---:|
| MAT-STE-2026-43858 | 1 / 959,073 | 1 / 46,106,240 | 47,065,313 |
| MAT-STE-2026-25132 | 1 / 72,772,456 | 1 / 7,456,194 | 80,228,650 |
| MAT-STE-2026-25179 | 1 / 644,027 | 1 / 6,982,037 | 7,626,064 |

### Why “Corrective Operation Cost” is unresolved

`_classify_landed_line` does **not** key off description text. It requires operating-cost **fields** or a PA reference. These three lines have description text suggesting operating cost but **flags are unset**, so status = `unresolved` with reason `Landed Cost line could not be reliably classified`.

That behavior matches the coded rules. Whether those lines *should* be eligible is a **business-rule / data-quality** question, not a calculation bug in the pool math.

### Small unresolved (22 × “Non stock items”)

Amounts 1–6 IRR each; total **101 IRR**. Same classification failure (no operating markers / PA / MCA). Full list is on `tabMFG Cost Control Credit Source` for parent `MFGCCCA-2026-00006` with `eligibility_status='unresolved'`.

### Expected under current rules?

**Yes for arithmetic and eligibility engine:**  
Difference = sum(unresolved landed amounts). Those amounts are intentionally withheld from Eligible and from allocation.  
**Open for product decision:** whether Corrective Operation Cost lines without flags should be reclassified.

---

## 5. Allocation engine input (which column is used)

### Call chain

```
MFG Cost Control Credit Allocation.calculate (whitelist)
  → control_credit/engine.calculate_control_credit_allocation
       → identify_control_credits(...)          # builds period + eligible pools
       → build_credit_allocation_plan(identification, ...)
            labor_pool = identification.labor_eligible
            oh_pool    = identification.overhead_eligible
            → _distribute_pool(pool, batches)   # applied_batch_share + residual
            → product_share / document qty split
       → persist parent fields + Source / Batch / Product children
       → apply_credit_shares_to_product_allocations (PA breakdown)

Report MFG Cost Control Credit Pool
  → fetch_control_credit_pools  # read-only from persisted parent fields
```

### Exact pool variables

```164:168:erpnext_extensions/iran_accounting/fg_cost_allocation/control_credit/allocate_credit.py
	labor_pool = _money(identification.labor_eligible, precision)
	oh_pool = _money(identification.overhead_eligible, precision)

	labor_dist = {b.batch_no: (amt, res) for b, amt, res in _distribute_pool(labor_pool, batches, currency=ccy)}
	oh_dist = {b.batch_no: (amt, res) for b, amt, res in _distribute_pool(oh_pool, batches, currency=ccy)}
```

**Allocation uses Eligible Credit** (`labor_eligible` / `overhead_eligible`), which is **eligible line remaining after prior credit-allocation consumption** — **not** Monthly Credit Amount.

`applied_batch_share` from `MFG Cost Batch Allocation` (current monthly revision) is the batch basis; residual-to-largest policy mirrors expense pool allocation (`_distribute_pool`). Confirmed for 1405-06: 82 batch rows, **1** residual overhead flag applied, batch OH sum = 75,157,941,821.

---

## 6. Six-month allocation reconciliation

| Month | Doc | Monthly Credit | Eligible | Allocated | Remaining | Allocated = Eligible? | Allocated ≤ Monthly? |
|---|---|---:|---:|---:|---:|---|---|
| 1405-01 | MFGCCCA-2026-00001 | 0 | 0 | 0 | 0 | PASS | PASS |
| 1405-02 | MFGCCCA-2026-00002 | 0 | 0 | 0 | 0 | PASS | PASS |
| 1405-03 | MFGCCCA-2026-00003 | 351,300 | 351,288 | 351,288 | 0 | PASS | PASS |
| 1405-04 | MFGCCCA-2026-00004 | 190 | 188 | 188 | 0 | PASS | PASS |
| 1405-05 | MFGCCCA-2026-00005 | 6,971,596 | 6,971,596 | 6,971,596 | 0 | PASS | PASS |
| 1405-06 | MFGCCCA-2026-00006 | 75,218,486,393 | 75,157,941,821 | 75,157,941,821 | 0 | PASS | PASS |

| Check | Result |
|---|---|
| Product-row `SUM(overhead_allocated)` = parent allocated | PASS all months |
| Allocation exceeded Eligible | **No** |
| Same source credited twice across Credit docs | **No** — one active Credit Allocation per period; source `previously_allocated_amount` = 0 |
| Labor vs Overhead independence | PASS — Labor pools all zero; Overhead allocated only from Overhead eligible |

**Monthly Credit vs Allocated:** Allocated is **less than or equal** to Monthly whenever unresolved lines exist (03, 04, 06). That is expected because Monthly is informational; Eligible is the allocation base.

---

## 7. Explanation of Previously Allocated

Report column **Previously Allocated (Product Allocation)** ← `labor_consumed_by_product_allocation` / `overhead_consumed_by_product_allocation`.

That field is the sum of LCV line amounts with status **`consumed_by_product_allocation`** (PA hex in description), **not**:

- prior Credit Allocation documents, and  
- not “everything not eligible”.

In all six months:

| Check | Value |
|---|---|
| Parent `overhead_consumed_by_product_allocation` | **0** |
| Source rows with status `consumed_by_product_allocation` | **0** |
| Source `previously_allocated_amount` (prior credit docs) | **0** |

So the screenshot showing **Previously Allocated = 0 is correct** for these months: no PA-linked control-credit LCV lines were found. The Monthly−Eligible gap is carried in **Unresolved**, which appears in the document’s unresolved fields (and is visible on Source children), not in Previously Allocated.

---

## 8. Incorrect, ambiguous, or unresolved behavior

| Item | Severity | Notes |
|---|---|---|
| Arithmetic Monthly vs Eligible | **Correct** | Diff = unresolved (+ PA/excluded if present). |
| Allocation base | **Correct** | Uses Eligible, fully allocated. |
| “Corrective Operation Cost” without operating flags → unresolved | **Ambiguous / data-vs-rule** | Large 1405-06 gap (60.5M of 60.5M). May warrant a rule or data fix later — **not** a silent math error. |
| “Non stock items” micro-amounts unresolved | **By design** | Same classifier; small IRR. |
| Report label “Previously Allocated (Product Allocation)” | **Easy to misread** | Means PA-consumed only; unresolved is a separate concept. |
| Unresolved still shown in Monthly Credit | **By design** | Monthly is gross GL; Eligible is allocable subset. |

---

## 9. Evidence and SQL

### Parent snapshot

```sql
SELECT name, persian_period,
       overhead_period_credit, overhead_eligible_credit, overhead_allocated, overhead_remaining,
       overhead_consumed_by_product_allocation, overhead_unresolved_amount
FROM `tabMFG Cost Control Credit Allocation`
WHERE persian_year = 1405 AND persian_month BETWEEN 1 AND 6 AND docstatus < 2
ORDER BY persian_month;
```

### Independent GL vs Monthly Credit

```sql
SELECT COALESCE(SUM(credit),0), COUNT(*)
FROM `tabGL Entry`
WHERE company = %s AND account = '211237 - کنترل سربار - E'
  AND posting_date BETWEEN %s AND %s
  AND IFNULL(is_cancelled,0)=0 AND credit>0;
```

### Status breakdown (e.g. 1405-06)

```sql
SELECT eligibility_status, COUNT(*) n, SUM(landed_cost_amount) s
FROM `tabMFG Cost Control Credit Source`
WHERE parent = 'MFGCCCA-2026-00006' AND account_kind = 'overhead'
GROUP BY eligibility_status;
-- eligible: n=317, s=75157941821
-- unresolved: n=25, s=60544572
```

### Key source files

| File | Role |
|---|---|
| `…/costing/report/mfg_cost_control_credit_pool/mfg_cost_control_credit_pool.py` | Report columns |
| `…/control_credit/reporting.py` | Pool row mapping |
| `…/control_credit/identify.py` | Period credit, eligibility, unresolved |
| `…/control_credit/allocate_credit.py` | Eligible → batch/product allocation |
| `…/control_credit/engine.py` | Orchestration + persistence |

---

## 10. Final conclusion

1. **Monthly Credit Amount** = gross monthly control-account **GL credit** total (non-cancelled).  
2. **Eligible Credit** = sum of **allocatable LCV line remainders** after classification.  
3. They differ when LCV lines are unresolved (or PA-consumed / MCA-excluded). In 1405-03/04/06 the entire gap is **unresolved**; 1405-05 has no gap.  
4. **Allocation uses Eligible Credit**, distributed with `applied_batch_share` and residual policy.  
5. **Allocation results are correct** under current rules: Allocated = Eligible; Remaining = 0; no double-count; Labor independent.  
6. **No correction implemented** in this investigation. If business intends “Corrective Operation Cost” text (or unset operating flags) to be eligible, that requires an **approved rule/data change** before any recalculation.

**Verdict:** Differences are explained and evidence-backed. Pool report columns are consistent with persisted Credit Allocation documents. The large 1405-06 gap (60,544,572 IRR) is entirely unresolved LCV lines, 99.9998% of which are three “Corrective Operation Cost” lines lacking operating-cost markers.

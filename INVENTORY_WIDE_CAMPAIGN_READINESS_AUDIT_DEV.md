# Inventory-Wide Campaign Readiness Audit — Development

**Site:** `development.localhost` only (Production **not accessed**)  
**erpnext_extensions:** **5.7.18** (unchanged)  
**ERPNext:** 16.53.0 · **Frappe:** 16.54.0  
**Mode:** Read-only investigation — **no** RIV, HRC, cancel/submit, SLE/GL mutation, commit, or push  
**Denylist:** `ACC-JV-1405-169046` — not touched  

Machine evidence: `INVENTORY_WIDE_CAMPAIGN_READINESS_AUDIT_DEV.json`

---

## A. Executive Summary

| Metric | Value |
|---|---:|
| Active SLE rows examined | 122,211 |
| Active GL rows examined | 102,348 |
| Submitted Stock Entries | 16,006 |
| Submitted Purchase Receipts | 1,512 |
| Submitted Purchase Invoices | 1,425 |
| Fractional IRR SLE unit rates (in or out) | **513** |
| Of which Stock Entry incoming fractional | **512** (387 vouchers) |
| Fractional SED `valuation_rate` | 1,122 |
| PR rows with nonzero `amount_difference_with_purchase_invoice` | 143 (92 vouchers) |
| Asset PRs still on Land expense / Land GL | **0** (Cohort A Dev complete) |
| Unbalanced inventory voucher GL (debit≠credit) | **0** |
| Submitted STE with **zero** GL rows | **1,387** (triage required) |
| Negative qty/value SLE samples | **4** |
| Open/Failed/Queued RIV docs | Failed 3 · Queued 3 · In Progress 1 |
| Asset vs PRI VR cost mismatches (heuristic) | 37 |
| Cohort A Asset PRs reconciled on Dev | **8 / 8** |
| Cohort B STE fractional | Dry-run only; **not authorized / not executed** |

**Campaign readiness status:** existing frameworks cover **narrow, authorized cohorts** well. They do **not** yet form a safe **inventory-wide** single campaign. Discovery proves the problem space is larger than “512 STE frac + 8 Asset PRs.”

### Final recommendation

**NOT READY — FIXES REQUIRED** for inventory-wide correction.

- **READY** for continued **scoped dry-runs** and architecture design (no mutation).  
- **Cohort A (Asset PR)** on Development: financially reconciled (account/GL); Asset/depr Case E on `01311` still open as a **separate** Finance track.  
- **Cohort B (STE fractional):** dry-run exists; **blocked** from execution until zero-GL Repack `MAT-STE-2026-29903` and related STE↔GL gaps are classified; requires explicit authorization.  
- **Production:** not assessed; any Prod path needs deploy of 5.7.18 patches, backup, and separate dry-run on Prod data.

---

## B. Document Coverage Matrix

| Document Type | Records Examined (submitted / active) | Issues Found | Root Cause class | Campaign Available | Readiness |
|---|---:|---|---|---|---|
| Purchase Receipt | 1,512 | 0 Land GL left in Cohort A; 92 vouchers with PI amt_diff; 0 PRI fractional VR | B, C, H | Cohort A (done Dev); FA GL patch; no stock-PR amt_diff campaign | A: Dev done · stock-PR: **Not ready** |
| Purchase Invoice `update_stock=0` linked to PR | (via 1,425 PI) |  rate mismatches vs PRI present; drives amt_diff | B, H | Core PI→PR path + #59476 compat; FA debit patch | Patch ready · **no bulk campaign** |
| Purchase Invoice `update_stock=1` | **0** | None on Dev | — | N/A | N/A on Dev |
| Delivery Note | 260 | 1 fractional outgoing rate; 52 fractional SLE VR | A / residual | No dedicated DN frac campaign | **Not ready** |
| Sales Invoice `update_stock=1` | 24 | 30 fractional SLE VR | A / residual | No dedicated SI campaign | **Not ready** |
| Stock Entry — Manufacture | (in 16,006) | 343 frac incoming SLE / 251 vouchers | A, D | Cohort B dry-run (partial); STE integer hardening code | Dry-run only · **blocked** |
| Stock Entry — Material Transfer for Manufacture | | 96 frac incoming | A, E | Cohort B | Dry-run only |
| Stock Entry — Material Transfer | | 70 frac incoming; **932** zero-GL STE | A, E, J | Partial | Zero-GL triage required |
| Stock Entry — Repack | | 3 frac incoming; **345** zero-GL; blocker `29903` | A, F, J | Cohort B notes Repack | **Not ready** |
| Stock Entry — Material Issue / Receipt | | Issue 28 / Receipt 19 zero-GL | J | None | Triage |
| Subcontracting Receipt | 77 | Not deep-scanned in this pass | G/J | None dedicated | **Unknown / not ready** |
| Landed Cost Voucher | 2 | Low volume; UVR/IRR align on submit hook | G | Core LCV + IRR UVR | Monitor |
| Stock Reconciliation | 32 | IRR SR amount-diff alignment exists in extension | A/J | Domain helpers; no inventory-wide campaign | Scoped only |
| Asset-related PR | Cohort A 8 + other FA PRs | Cohort A cleared Land; 37 Asset↔VR mismatches | C | Asset PR correction + FA GL patch | GL ready · Asset cost **separate** |
| Extension hooks (STE/PR/DN/PI/SI/LCV/SR/RIV/SLE/GL) | — | Multiple monkey patches (IRR, FA GL, #59476, ZVT, …) | — | Must stay in every worker bootstrap | Verify on each execute |

**Installed inventory hooks (extension):** Stock Entry (full lifecycle + consignment), Purchase Receipt validate/on_submit, Delivery Note validate, PI/SI invoice hooks, LCV on_submit, Stock Reconciliation validate/submit, RIV on_change, SLE/GL validate/insert. No `override_doctype_class` for stock doctypes.

---

## C. Root Cause Matrix

| Class | Root Cause | Affected (Dev evidence) | Financial Impact | Proposed Fix | RIV? | GL repost? | Asset/depr? | Tests |
|---|---|---|---|---|---|---|---|---|
| **A** | Amount-authoritative residual; float `amount/qty` → fractional IRR unit rates | 512 STE frac incoming SLE; 1,122 SED frac VR; DN/SI VR noise | Valuation drift; Class A residual on integerize | Integer VR from amount; STE sync harden (exists); controlled Item+WH RIV per identity | Yes (scoped) | Via RIV | No | `test_ste_integer_rate_hardening_v5718`; need DN/SI |
| **B** | PI rate ≠ PR rate with invoice-based valuation | 92 PR vouchers / 143 rows amt_diff; stock vs FA split | FA: acquisition at PI cost; stock: SLE/divisional loss | Preserve core PI→PR; FA debit patch (done); stock-PR campaign TBD | FA: accounting RIV; stock: often SLE RIV | Yes when VR/expense changes | FA cost may diverge | `test_fa_pr_gl_amount_difference_v5718`; stock-PR cases missing |
| **C** | FA expense mis-accounted to Land; FA GL omitted amt_diff | Cohort A 8 (cleared on Dev) | Wrong FA account / unbalanced FA GL | Source `expense_account` + accounting RIV + FA GL patch | Accounting-only RIV | Yes | Inspect only; Case E no auto rebuild | Asset PR + FA GL tests |
| **D** | Manufacture / scrap / reject absorption float rates | 343 Manufacture frac SLE | FG/scrap valuation | `manufacture_native` / `manufacture_scrap_fg` integer paths + Cohort B | Yes | Yes | No | Hardening tests; need scrap E2E |
| **E** | Material Transfer valuation conservation / float IR | 70 frac SLE; many zero-GL transfers | May be GL-less by design if same warehouse account | Classify same-account transfers vs true gaps | Selective | Selective | No | Transfer conservation tests |
| **F** | Repack valuation | 3 frac; 345 zero-GL Repack; `MAT-STE-2026-29903` | Missing GL / frac IR | Investigate zero-GL Repack before any RIV | After GL gap fixed | Required if GL missing | No | Missing Repack zero-GL tests |
| **G** | LCV valuation propagation | 2 LCV; IRR UVR on buying | Landed cost into VR | Core LCV + regional UVR | Item+WH if history | Yes | Possible on FA+LCV | LCV IRR tests exist historically |
| **H** | GRNI / PR–PI reconciliation | PI↔PR rate mismatches; residual GRNI after price change | Clearing account residues | Do not force PR amount=PI amount; use amt_diff + FA/stock paths | As needed | As needed | Indirect | Need GRNI residual report tests |
| **I** | Historical RIV failures / stuck queue | Failed: `cbhpqc3uau` (stale 00747), `olt2ejc428`, `8tt4rqd11g` (Class B residual); Queued×3; In Progress×1 | Blocks later RIV | Drain/classify failed RIV; do not company-wide rebuild | Ops hygiene | — | — | HRC/RIV status tests exist |
| **J** | Zero-GL STE, negative stock, Asset cost≠VR | 1,387 zero-GL STE; 4 neg SLE; 37 Asset mismatches | Unknown until classified | Separate discovery campaigns; never force SR/JE | Case-by-case | Case-by-case | Case E Finance | Largely **missing** |

---

## D. Campaign Architecture Review

### What exists today

| Framework | Scope | Dry-run | Mutation gate | Denylist JE |
|---|---|---|---|---|
| `financial_correction_campaign` | Cohort A (8 Asset PR) · Cohort B (STE frac identities) | Yes | Confirm tokens | Yes |
| `asset_purchase_receipt_correction` | Per-PR Land→approved FA + accounting RIV | Yes | `CONFIRM_APPLY` + allowlist | Yes |
| `historical_rebuild` / HRC | Broad historical rebuild | Manifest | Campaign doctype | Separate |
| `riv_campaign` | Item+WH RIV generation | Bench | Schedulers stopped | N/A |
| `job_card_stock_rebuild` | Job Card / semi-FG | Page/API | Dedicated | — |
| `wrong_rate_campaign` / `riv_sabe_integer` / `manufacture_gl_reclass` | Specialized | Varies | Varies | — |
| Asset depr repair campaign | Depreciation | Separate | Must not auto-run for Case E | — |

### Can one inventory-wide campaign absorb everything?

**No.** Valuation, accounting, Asset, and zero-GL gaps need **separate cohorts** by root-cause class (A–J above), with:

- Explicit allowlists / discovery manifests  
- Fingerprints + idempotency  
- Overlap detection (same voucher in Cohort B and HRC)  
- Stop-on-fail per voucher  
- No company-wide RIV/HRC as default  

Recommended structure: **Inventory Correction Program** with **typed cohorts**, not one generic “fix all inventory” job.

### Proposed execution order (when authorized)

1. Ops: clear stuck/failed RIV queue (classify; no blind replay of Class B rejects).  
2. Cohort A Prod (only after deploy + backup) — Dev already done.  
3. Zero-GL STE triage (F/J) — especially Repack `29903` — **before** Cohort B.  
4. Cohort B STE fractional identities (73) — dry-run refresh → controlled Dev execute.  
5. Class B stock-PR amt_diff discovery dry-run (non-FA).  
6. DN/SI fractional VR discovery.  
7. Negative stock chain investigation.  
8. Asset cost Case E (`01311` and 37 heuristic mismatches) — Finance-approved only.  
9. Never auto-include denylisted JE; never Cohort B without auth.

---

## E. Asset and Depreciation Protection

| Case | Dev finding | Handling |
|---|---|---|
| No linked Asset | e.g. `00747` | No Asset/depr action |
| Draft Asset | none flagged in Cohort A | Update purchase fields before submit only |
| Submitted, cost unchanged | most Cohort A assets after allocation fix | No rebuild |
| Submitted, cost changed, **no** posted depr | rare | Optional controlled rebuild after approval |
| Submitted, cost changed, **posted** depr | `01311` (10 assets); broader heuristic 37 | **No auto rebuild**; preserve historical Depreciation Entry + GL; future-only Finance design |

Successful PR GL RIV **does not** update Asset `purchase_amount` or schedules.

---

## F. Dry-Run Design (inventory-wide discovery — not yet implemented as one API)

A future `dry_run_inventory_program` should:

1. Enumerate SLE/GL/document anomalies by class A–J.  
2. Deduplicate voucher membership across cohorts.  
3. Fingerprint source rows (PRI expense, SED rates, SLE rates).  
4. Emit expected before/after **without** writing ledgers.  
5. Flag denylist JE, open RIV conflicts, zero-GL blockers.  
6. Produce category manifests + blocked lists.  
7. Remain idempotent and read-only.

Current Cohort A/B dry-runs are **prototypes**, not full inventory coverage.

---

## G. Regression / Safety Test Gaps

**Present:** STE integer hardening, FA GL amt_diff (±/0), Asset PR account resolution, many HRC/RIV/UVR tests (~310 iran_accounting test files).

**Missing / thin for inventory-wide:**

- DN / SI fractional VR campaigns  
- Stock-item PR + positive/negative amt_diff GL balance  
- Repack / Manufacture **zero-GL** detection gates  
- Negative stock chain safety  
- Overlap detection between Cohort B and HRC  
- Idempotent re-run of accounting RIV after FA patch  
- GRNI residual report after PI price change  
- Subcontracting Receipt valuation  

---

## H. Blockers and Risks

1. **No unified inventory-wide campaign API** — only scoped cohorts.  
2. **Cohort B unauthorized** + Repack **`MAT-STE-2026-29903` has 0 GL**.  
3. **1,387 zero-GL STE** — unknown mix of benign vs broken.  
4. **Stuck/failed RIV** documents (including stale FA failure on 00747).  
5. **Class B stock PRs** (amt_diff) without a dedicated correction cohort.  
6. **Asset Case E** with posted depreciation — cannot auto-fix via RIV.  
7. **Negative stock** SLE rows — may poison RIV.  
8. **Production unassessed** — Dev counts are not Prod truth.  
9. Risk of **company-wide HRC/RIV** if operators reuse broad tools.  
10. **JE denylist** must remain enforced at every entry point.

---

## I. Final Recommendation (evidence)

| Option | Choice |
|---|---|
| READY FOR DRY-RUN | **Yes — scoped discovery dry-runs only** |
| READY FOR DEVELOPMENT EXECUTION | **Only Cohort A already executed**; others **not** |
| READY FOR PRODUCTION AFTER DEPLOYMENT AND BACKUP | **No** |
| **NOT READY — FIXES REQUIRED** | **Yes — for inventory-wide program** |

Evidence: Dev DB shows 513 fractional SLE rates, 1,122 fractional SED VR, 92 PR amt_diff vouchers, 1,387 zero-GL STE, 4 negative SLE, open RIV failures, and 37 Asset cost mismatches — far beyond the completed 8 Asset PRs and the pre-scoped 512 STE frac set. Architecture must stay **cohort-separated**; zero-GL and Asset/depr tracks need design before mutation.

---

## Explicit non-actions (this audit)

- No campaign execution · No RIV/HRC · No ledger writes  
- No Cohort B · No Production access  
- No touch of `ACC-JV-1405-169046`  
- No version bump · No git commit/push · Existing evidence preserved

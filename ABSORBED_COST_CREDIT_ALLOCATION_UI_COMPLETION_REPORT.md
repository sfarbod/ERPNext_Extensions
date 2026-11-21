# Credit Control Allocation UI Completion Report

**Target version:** `5.8.5` (unchanged)  
**Base implementation commit:** `51f879cbe341c07de49ebe60d423588502dfdbbf`  
**Follow-up commit:** (see Git section after commit)  
**Environment:** `development.localhost`  
**Push / tag / deploy:** none  

---

## 1. Gap analysis (pre-change)

| Item | Codebase file | Live Desk DB | Action |
|---|---|---|---|
| Workspace link to Credit Allocation | Present in JSON | **Absent** | Sync Workspace from JSON + patch |
| Sidebar entries | Absent | Absent | Add + sync |
| Labor/Overhead pool list | Parent list only (1 row/month) | No Labor vs Overhead unfold | Script Report `MFG Cost Control Credit Pool` |
| Batch×Product credit details | istable children only | Not list-browsable | Script Report `MFG Cost Control Credit Batch Product Allocation` |
| PA → credit pool navigation | Four fields only | No buttons | Form custom buttons |
| Allocation engine | Intact | 6 Calculated docs | **Unchanged** |

Full notes: `ABSORBED_COST_CREDIT_UI_GAP_ANALYSIS.md`

---

## 2. Components reused

- Persisted parent totals (`labor_*` / `overhead_*`) and child tables (`sources`, `batch_rows`, `product_rows`)
- Existing Costing Workspace / Sidebar patterns
- Batch Expense list/report navigation conventions (Script Report + Workspace Report links)
- No new allocation engine; no GL/SLE mutations

---

## 3. Workspace / Sidebar

**Workspace `Costing`**

- Monthly Calculations → **MFG Cost Control Credit Allocation** (DocType)
- Reports & Details → **Control Credit Pools** (Report)
- Reports & Details → **Control Credit Batch × Product** (Report)
- Shortcut: **Control Credit Allocations**

**Sidebar `Costing`:** matching DocType + Report links.

**Patch:** `sync_costing_credit_workspace_v585` (forces DB sync; prior migrate left Workspace stale).

---

## 4. Credit Control Pool Report

**Name:** `MFG Cost Control Credit Pool`  
**Source:** `fetch_control_credit_pools()` over persisted allocation documents (2 rows per month: Labor + Overhead).

Columns include company, Persian period, control type/account, monthly credit (`period_credit` = persisted `SUM(credit)` totals), previously allocated (Product Allocation consumption), eligible / allocated / remaining, pool status, source count, allocation reference.

Filters: company, Persian period/year/month, control type, account, document/pool status, allocation reference.

Does **not** recalculate GL credits independently of the allocation document.

---

## 5. Batch / Product Allocation Details Report

**Name:** `MFG Cost Control Credit Batch Product Allocation`  
**Source:** persisted `product_rows` joined to `batch_rows` shares.

Unpivots Labor/Overhead allocated amounts into separate rows (zeros omitted by default).

Exposes batch, product, product allocation link, applied batch share, product share, allocation ratio, allocated amount, source manufacturing document.  
GL Entry line-level detail remains on the parent **Sources** child table (not duplicated synthetically per batch).

---

## 6. Navigation improvements

| From | To |
|---|---|
| Credit Allocation form → Traceability | Pool report, Batch×Product report, Monthly FG calc, Product Allocation list |
| Product Allocation → Credit Control | Pool report (period), product credit shares report, open linked Credit Allocation |

Four Document Breakdown fields unchanged in meaning.

---

## 7. Evidence / screenshots

`/tmp/control_credit_ui_v585/`

- `01_workspace.png`
- `02_credit_list.png`
- `03_credit_form.png`
- `04_pool_report.png`
- `05_batch_product_report.png`
- `06_product_allocation.png`
- `07_batch_expense_regression.png`
- `results.json`

---

## 8. Automated tests

| Suite | Result |
|---|---|
| `test_control_credit_reporting_v585.py` | **8 PASS** |
| `test_control_credit_allocation_v585.py` (regression) | **11 PASS** |
| `test_document_account_control_breakdown_v5755.py` (regression) | **9 PASS** |
| **Total** | **28 PASS / 0 FAIL / 0 SKIP** |

---

## 9. Playwright E2E

Script: `e2e/playwright_control_credit_ui_v585.py`  
**8 PASS / 0 FAIL** (browser executed)

| Check | Result |
|---|---|
| workspace_credit_entry | PASS |
| credit_list_view | PASS |
| credit_form_labor_overhead | PASS |
| pool_report | PASS |
| batch_product_report | PASS |
| navigate_credit_doc | PASS |
| pa_four_fields_and_nav | PASS |
| batch_expense_regression | PASS |

Restricted-user permission browser test: **NOT RUN** (no dedicated restricted Costing user fixture in this environment).

Excel export click for new reports: **NOT RUN** as separate Playwright case (Query Report native export used; data correctness covered by reporting unit tests).

---

## 10. Known limitations

1. Batch/Product report does not invent per-row Source GL Entry columns; open Credit Allocation → Sources for GL/voucher traceability.
2. Workspace required explicit sync patch because Desk DB had drifted from app JSON.
3. Restricted-role Playwright path not executed.

---

## 11. Git

- Version remains **`5.8.5`**
- Base commit preserved (not amended): `51f879c…`
- New follow-up local commit created (hash below after commit)
- Working tree clean after commit
- **No push, tag, or deployment**

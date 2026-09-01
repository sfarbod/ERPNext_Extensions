# Material Loan — Solution Design (Revised)

**Application:** `erpnext_extensions`  
**Target release:** `3.8.1`  
**Status:** Design revised — **Party account model requires explicit approval before implementation**  
**Date:** 2026-08-01  

---

## 1. Goals

1. Track company-owned materials held by third parties (qty + value).  
2. Post Party monetary balances via Recognition / Settlement JEs and Temporary Clearing.  
3. Keep Stock Entry GL standard (forced Temporary Clearing as `expense_account`).  
4. Avoid PLE locks on Stock Entry (no `reference_type=Stock Entry` on JE party lines).  
5. Coexist with inbound Consignment Stock 3.8.0 without shared ambiguous fieldnames.  
6. Support Customer, Supplier, and other Party Types via **mapped** dedicated accounts.

---

## 2. Recommended Party account model (pending approval)

### Decision recommendation: **Option E — Party Type → Account child table**

| Party Type | Mapped account_type | Example account name | Recognition party line |
| --- | --- | --- | --- |
| Customer | Receivable | Material Loan Receivable - Customer | Dr account + Customer |
| Supplier | Payable | Material Loan with Suppliers | Dr account + Supplier |
| Other PT | Match PT.account_type | Material Loan - {Type} | Dr account + party |

**Reject Option A** (Supplier on Receivable): ERPNext 16 JE validation blocks it.  
**Reject Option B** (Common Party Accounting) as primary: wrong instrument; dual masters; trade AR pollution.  
**Defer Option C** (custom Receivable Party Type + DocType): high cost; revisit only if E is rejected.  
**Reject Option D** (operational only): business requirement.

### Resolution rule (not `get_party_account`)

```text
account = Settings.material_loan_party_accounts row where party_type = Issue.party_type
assert account.account_type == Party Type.account_type
assert account != Company.default_receivable_account
assert account != Company.default_payable_account
# optional stronger: reject if account equals any party's default Party Account for trade
```

Trade defaults are **rejected** even if user maps them — force dedicated Material Loan accounts.

### Reporting / reconciliation

- Script Reports: Outstanding Material Loans (qty/value), Party Material Loan Balance (from Recognition − Settlements and/or GL on mapped accounts filtered by JE role).  
- Standard AR/AP aging may show dedicated accounts — document chart-of-accounts placement under separate parents (e.g. “Material Loans”) for clarity.  
- JE role `custom_material_loan_je_role` filters Material Loan vs commercial vouchers.

---

## 3. Settings

Extend **Consignment Stock Settings** — section **Material Loan**:

### Parent fields

| Fieldname | Type | Default | Notes |
| --- | --- | --- | --- |
| `material_loan_temporary_clearing_account` | Link/Account | — | Required when Material Loan used; not Stock; not warehouse-linked; Asset or Liability OK (prefer Asset or Equity/Liability clearing — recommend **Asset** or dedicated clearing under Current Assets; validate not Stock, not group) |
| `material_loan_valuation_difference_account` | Link/Account | — | **Required** (recommended); P&amp;L or suitable Diff account like inbound |
| `default_material_loan_source_warehouse` | Link/Warehouse | — | Optional |
| `default_material_loan_return_warehouse` | Link/Warehouse | — | Optional |
| `require_expected_return_date` | Check | 0 | |
| `allow_return_to_different_warehouse` | Check | **1** | |

### Child table `Material Loan Party Account` (`material_loan_party_account`)

DocType: child of Consignment Stock Settings.

| Fieldname | Type | Validation |
| --- | --- | --- |
| `party_type` | Link/Party Type | Unique within parent |
| `account` | Link/Account | Company match; enabled; not group; currency OK; **account_type = Party Type.account_type**; not company default receivable/payable |

No Inventory / Cost Center / Finance Book settings.

**Note:** Prior “Materials Held by Third Parties” single asset account is **replaced** by Temporary Clearing + Party mapping (Held presentation moves to Party ledger + reports).

---

## 4. Stock Entry Type flags

Unchanged intent:

- `custom_is_material_loan_issue` — Material Issue only  
- `custom_is_material_loan_return` — Material Receipt only  
- Mutual exclusion with each other and with inbound consignment flags  

---

## 5. Custom fields

### Journal Entry

| Fieldname | Type | Options |
| --- | --- | --- |
| `custom_material_loan_je_role` | Select | `\nRecognition\nSettlement` |

(Separate from inbound `custom_consignment_je_role`.)

### Stock Entry — Issue / shared

| Fieldname | Notes |
| --- | --- |
| `custom_is_material_loan_issue` / `_return` | fetch from type |
| `custom_material_loan_party_type` / `_party` | mandatory when loan |
| `custom_material_loan_status` | physical lifecycle (see §6) |
| `custom_material_loan_recognition_status` | Pending / Draft / Recognized / Cancelled |
| `custom_material_loan_settlement_status` | On Return: Pending / Draft / Settled / Cancelled |
| `custom_material_loan_expected_return_date` | |
| `custom_material_loan_external_reference` | |
| `custom_material_loan_issue_reference` | Return header default |
| `custom_material_loan_recognition_je` | Link JE on Issue |
| `custom_material_loan_settlement_je` | Link JE on Return |

### Stock Entry Detail

| Fieldname | Notes |
| --- | --- |
| `custom_material_loan_issue` / `_issue_detail` | Return refs (mandatory) |
| `custom_material_loan_issue_rate` / `_issue_value` / `_issue_qty` | Frozen after Issue submit |
| `custom_material_loan_previously_returned_qty` / `_remaining_returnable_qty` | |
| `custom_material_loan_return_value` | qty × frozen rate (= R component) |
| `custom_material_loan_settlement_amount` | R for row (for Settlement service) |

Do not reuse `custom_consignment_*`.

---

## 6. Status model (cleanest)

**Do not overload one field.** Use:

### A. Physical status — `custom_material_loan_status` (Issue)

| Status | Rule |
| --- | --- |
| Draft | docstatus 0 |
| Issued | submitted; remaining = original |
| Partially Returned | 0 &lt; remaining &lt; original |
| Fully Returned | remaining ≤ 0 |
| Overdue | outstanding qty &gt; 0 and expected_return_date &lt; today (supersedes Issued/Partial for display) |
| Cancelled | docstatus 2 |

### B. Recognition status — `custom_material_loan_recognition_status` (Issue)

| Status | Rule |
| --- | --- |
| Pending | submitted Issue; no active JE |
| Draft | linked Recognition JE docstatus 0 |
| Recognized | linked JE docstatus 1 |
| Cancelled | JE cancelled / cleared |

### C. Settlement status — `custom_material_loan_settlement_status` (Return)

| Status | Rule |
| --- | --- |
| Pending | Return submitted; no Settlement JE |
| Draft | linked Settlement draft |
| Settled | Settlement submitted |
| Cancelled | Settlement cancelled |

### D. Optional computed “Accounting completeness” (report only)

Issue-level: Settled when Fully Returned **and** every submitted Return has Settled settlement status.

Stored Issue status must **not** say “Settled” alone without B/C fields.

All statuses recalculated server-side from documents.

---

## 7. Document lifecycle & buttons

### Issue (submitted)

| Button | Enabled when |
| --- | --- |
| Create Material Loan Recognition Entry | No active Recognition JE |
| View Recognition JE | Link present |
| Create Material Loan Return | Recognition status = Recognized; remaining &gt; 0 |
| View Returns | Returns exist |
| View Outstanding | Always when submitted |

### Return (submitted)

| Button | Enabled when |
| --- | --- |
| Create Material Loan Return Settlement | No active Settlement JE |
| View Settlement JE | Link present |
| View Original Issue | Ref present |

Gates:

- Return validate: Issue Recognition JE must be submitted.  
- Settlement create: Return submitted; R and A &gt; 0.

---

## 8. Cancellation order (enforced)

For Issue with one or more returns:

1. Cancel each Return’s **Settlement JE** (if any).  
2. Cancel each **Material Loan Return** Stock Entry.  
3. Cancel **Recognition JE**.  
4. Cancel **Material Loan Issue** Stock Entry.

Per partial return: Settlement JE of that return before that Return SE.

Blocks:

| Attempt | Block |
| --- | --- |
| Cancel Issue while Recognition submitted and returns exist | Yes |
| Cancel Issue while any Return exists | Yes |
| Cancel Recognition while any Return exists | Yes |
| Cancel Return while Settlement JE submitted | Yes |
| Cancel Return while Settlement draft exists | Prefer block or auto-unlink only if draft — **block** until Settlement cancelled |

After complete reverse: Party 0, Temp 0, no PLE against SE, no orphan links.

---

## 9. Package structure

```text
consignment_stock/material_loan/
    __init__.py
    constants.py
    custom_fields.py
    accounting.py              # temp/diff getters; force expense; warehouse reuse
    party_account.py           # mapping resolve + reject trade defaults
    recognition_service.py     # draft Recognition JE
    settlement_service.py      # draft Settlement JE + D=A-R
    stock_entry_hooks.py
    stock_entry_type.py
    stock_entry_rates.py
    frozen_valuation.py
    returnable_qty.py
    status.py
    repost_guards.py
    api.py
    queries.py
```

Reuse from inbound (thin): `resolve_warehouse_account`, `force_expense_account_on_items`, additional-costs block pattern, PLE-safe JE line construction (no SE references).

Keep inbound recognition/settlement services untouched.

---

## 10. Extension points

Prefer: doc_events, doctype_js, whitelist API, draft JE services, expense_account force.  
Avoid: SE class override, new GL monkey patch, PLE writes, SE refs on party JE lines.

Hook order: after iran_accounting + inbound consignment handlers; early-return when loan flags off.

---

## 11. Decisions requiring approval

1. **Party model Option E** (child table; Customer→Receivable dedicated; Supplier→Payable dedicated) — approve or choose C.  
2. Reject mapping to company default Debtors/Creditors — confirm.  
3. Valuation Difference Account **required** vs optional+block when D≠0.  
4. Dual status fields (physical + recognition + settlement) — confirm.  
5. Recognition mandatory before return — confirm (aligned with inbound).  
6. Temporary Clearing account root_type policy (Asset vs Liability) — confirm chart placement.  
7. Daily Overdue refresh job — include in 3.8.1 or Phase 4.

**Do not implement until Party account model (item 1) is explicitly approved.**

---

## 12. Future Enhancement (post-5.0.3)

**Accounting Dimension propagation for Material Loan Settlement Difference Journal Entry lines.**

Settlement JE builders currently auto-set Cost Center only. On sites where dimensions such as Department are `mandatory_for_pl`, Valuation Difference lines (`D ≠ 0`, P&L account) may fail GL validation until those dimensions are propagated onto Diff JE lines. Recognition and normal Settlement BS lines are unaffected. Tracked as a known limitation of release 5.0.3; do not implement as part of that release.

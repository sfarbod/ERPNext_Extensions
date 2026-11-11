# Release 5.7.25 — Bounced Cheque Reassignment & Debt Purchase Return

**Version:** **5.7.25**  
**Branch:** `develop`  
**Runtime scope:** Post Dated Cheque receivable workflow + journal-centric accounting  
**Deployment status:** Local Development validated on `development.localhost` for both capabilities on `PDC-2026-00304`. **No Production deploy. No push from this step.**

## Objective

Complete two complementary PDC capabilities in one release:

1. **Reassign after bounce** — move a bounced receivable cheque back to bank collection or Debt Purchase.
2. **Return from Debt Purchase assignment** — unassign a cheque from Debt Purchase collection back to **Registered** (cheques in hand).

Both use legitimate Desk workflow actions, server-side state-machine validation, and system-managed Journal References (v5.7.6 policy preserved).

## Capability 1 — Reassignment from Bounced

### Root cause

`Bounced` exits were limited to **Returned / Replaced / Under Legal Action**. There was no forward reassignment accounting edge (only workflow rollback could restore **Sent to Bank** by cancelling the bounce JE).

### Transitions

| From | Action | To |
|---|---|---|
| Bounced | Send to Bank | Sent to Bank |
| Bounced | Assign for Debt Purchase | Assigned to Bank for Debt Purchase |

### Accounting

Credits the **bounce holding** account (debit side of the prior bounce JE; protested preferred, else CIH) and debits:

- clearing — `Bounced → Sent to Bank` (purpose **Under Collection**)
- Debt Purchase In Collection — `Bounced → Assigned DP` (purpose **Debt Purchase Assignment**)

Bounce ↔ re-send / Bounce ↔ re-assign edges are cycleable for open-occurrence idempotency.

## Capability 2 — Return from Debt Purchase Assignment

### Root cause / destination choice

Assigned DP Desk exits were historically **Bounce only**; Assigned → Registered was blocked with guidance to use Rollback. Party **Returned** remains forbidden (different GL / business meaning).

The accounting reverse of Debt Purchase Assignment (Dr DPIC / Cr CIH or bounce holding) restores the **cheques-in-hand / prior holding** pool. That matches operational **Registered** (in hand), analogous to **Return from Bank** (Sent → Registered) and to DP1 rollback’s restore target — without cancelling the historical assignment JE.

### Transition

| From | Action | To |
|---|---|---|
| Assigned to Bank for Debt Purchase | Return from Debt Purchase | Registered |

Still forbidden from Assigned DP: **Cleared**, party **Returned**. Bounce and Facility **Debt Purchase Settled** remain supported.

### Accounting

- New JE (does **not** cancel the assignment JE): **Dr** assignment credit account (from open Debt Purchase Assignment JE; usually CIH), **Cr** Debt Purchase In Collection.
- Purpose: **Return from Debt Purchase** (new Journal Reference Select value).
- Transition key: `…|Assigned to Bank for Debt Purchase|Registered`.
- Registered ↔ Assigned edges are cycleable so assign → return → assign does not duplicate-block.

Posting date for the return JE prefers the open assignment JE’s posting date (never `received_date`, which would back-date before assignment).

## Journal Reference policy

Unchanged from v5.7.6: create/remove only via authorized internal services; no manual row mutation; auth flags cleared on success and failure.

## Backward compatibility

- Existing Bounce / Facility settlement paths unchanged.
- Existing Bounced → Sent / Assigned paths preserved.
- Party return from Assigned DP remains rejected.
- Historical assignment and bounce JEs stay submitted; return posts a **new** reversing JE.

## Verified target cheque — `PDC-2026-00304`

### Forward reassignment (prior step in this release)

Moved **Bounced → Assigned to Bank for Debt Purchase** with system JR + assignment JE (when exercised on the bounce path).

### Return (this completion)

| Field | Before return | After return |
|---|---|---|
| Workflow State | Assigned to Bank for Debt Purchase | **Registered** |
| Cheque Status | Debt Purchase In Collection | **In Hand** |
| Assignment JE | `ACC-JV-1405-172499` Submitted | Unchanged Submitted |
| Bounce / prior JEs | Submitted | Unchanged Submitted |
| Return JE | — | `ACC-JV-1405-172500` Submitted |
| Return JR | — | Purpose **Return from Debt Purchase**, key `…\|Assigned to Bank for Debt Purchase\|Registered` |
| Return JE lines | — | Dr CIH `111204…` / Cr DPIC `111201…`, Party Customer `CUS-00003` |

Duplicate return unavailable after state change. Guest workflow attempt → `PermissionError`.

## Tests

- `test_pdc_bounced_reassignment_v5725.py` — bounce reassign + Assigned → Registered return builders/purposes/cycles
- `test_pdc_debt_purchase_foundation.py` — Assigned → Registered allowed; Returned still rejected
- Related: workflow transition validation, JR guard v5.7.6, workflow rollback, payload builders

**Combined unit result:** **142/142 OK** (0 fail / 0 error).

## Deploy note

Run migrate / post-model-sync so:

- `ensure_bounced_reassign_pdc_workflow`
- `ensure_debt_purchase_return_pdc_workflow`

add Desk transitions, and DocType metadata picks up the **Return from Debt Purchase** purpose option.

## Version

`erpnext_extensions.__version__` → **5.7.25**

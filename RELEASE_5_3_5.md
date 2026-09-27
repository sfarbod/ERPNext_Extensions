# Release 5.3.5 — Simplified Historical Repair

Package version: **5.3.5**.

This is a Historical Repair redesign on Development only. Production is untouched.

The tool now answers five questions:

1. What is wrong?
2. Where did it first become wrong?
3. What should the correct state be?
4. Can we prove the repair safely?
5. Did ERPNext remain correct after official repost/RIV?

Lifecycle: **SCAN → ROOT CAUSE → DRY RUN → REPAIR → REPOST → VERIFY**.

---

## What was simplified

Existing engines were not deleted. They were hidden behind one model:

| Layer | Count |
|---|---|
| Primary user states | 6 — READY, WAITING, MANUAL, LEGITIMATE, REPAIRED, FAILED |
| Root families | 5 — POSTING_ORDER, VALUATION, MANUFACTURE_FLOW, DERIVED_STATE, ACCOUNTING |
| User actions | SCAN ALL, SCAN SELECTED, DRY RUN, REPAIR SAFE, VERIFY |

Leftover-MA, posting order, wrong rate, I1, I4, Bin, and GL remain reasons under those families. They are no longer separate user workflows.

---

## Leftover Moving Average — receipt only

Leftover-MA is a **VALUATION** reason.

It applies only to:

- Material Receipt
- Material Transfer (not for manufacture)
- Purchase Receipt

It never applies to:

- Manufacture consumption or output
- Material Transfer for Manufacture
- scrap / Reject / Paykar
- finished-good or scrap rows

Incoming Rate may stay 0. Valuation Rate must not become 0 while stock still holds value. 16100066 remains the canary (`≈ 5,005,344.52`).

---

## Job Card material flow

One equation. No quantity writes.

Completed: `Transferred = Returned + Consumed + Scrap`

Open: `Transferred = Returned + Consumed + Scrap + remaining in Paykar`

Statuses: BALANCED / OPEN_WITH_VALID_REMAINDER / BROKEN / MANUAL.

---

## Safety

If an experimental wave damages Development data, restore

`20260924_103455-erp_espadpharmed_com-database.sql.gz`

and keep the code. Do not add compensating repair modes.

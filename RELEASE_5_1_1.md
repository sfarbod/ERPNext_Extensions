# Release 5.1.1 — Account Explorer Asymmetric Stock ↔ Account Contract

## Final asymmetric contract (FROZEN)

### Case A — source / filter is Item or Item Group

Stock population is authoritative.

```
Item Group = Σ Item = Σ Account Level breakdown
```

Same scoped SLE → Warehouse → ERPNext inventory account map.

Mandatory: Opening / Period Inward / Period Outward / Displayed In/Out / Signed Balance / Debit·Credit Balance — all Δ=0.

Account rows are a **breakdown** of the selected stock population only (no peer Items/Groups, no JE, no company-wide GL).

Engine: **`sle_scoped_stock`**

Same-account WH transfers keep SLE **gross** Inward+Outward (not posted-GL net-to-zero).

### Case B — source is Account

Account is posted General Ledger (E1/E2/E3).

Account → Item / Item Group is **discovery only**. Reverse equality is **NOT** mandatory.

Σ Item / Item Group may be `<`, `=`, or `0` vs Account.

## Axes

- **Item Groups** / **Items**: SLE stock measures
- **Account Levels**: Case A = SLE-scoped stock breakdown; Case B = posted GL
- **Party / Dimension / Currency / Voucher**: posted GL; with inventory filters, vouchers with scoped SLE (EXISTS) — no stock-value equality vs Item Group
- **No Inventory Account nav tab**
- **No construction-replay Account summary**
- **No voucher-scoped GL as Case A Account measures**

## Fingerprint

`v511.6` (includes `account_fact_engine`)

## Account classification (v5.1.1 presentation)

### No user-facing synthetic classification rows

Account Level grids **must not** show `__UNCLASSIFIED__`, Unspecified, Unassigned, Unmapped, or similar synthetic buckets as ordinary analysis rows.

### Unmappable accounts excluded from Account root totals

Accounts with **missing or non-numeric `account_number`** cannot be placed in the numeric Account Level hierarchy. In v5.1.1 they are **excluded before** hierarchy grouping, row materialization, and Account root/footer totals.

This is intentional release behavior: Account root totals reflect only classifiable (numeric `account_number`) accounts. Unmappable account activity is **not** folded into an Unclassified row and **not** added into displayed totals.

### `classification_residual` (diagnostic only)

API responses may include `classification_residual`: a **diagnostic** aggregate of activity that was excluded because accounts were unmappable (missing/non-numeric `account_number`).

- Not a grid row
- Not part of Account root / footer totals
- Not a user-facing Unclassified / Unmapped presentation
- Under Case A (Item / Item Group stock scope), residual must be **0** for the scoped population (scoped accounts are expected to classify)

Use residual only for ops / support diagnostics when chart-of-accounts data quality leaves unmappable accounts with GL/SLE activity.

# erpnext_extensions 5.7.45

## FX Sales Invoice Pattern A — RATE-FIRST Header Reaggregation

### Summary

Sales Invoice `1405-00269` (EUR → IRR, multi-line, high exchange rate) failed to submit
with `Debit and Credit not equal … Difference is 114.0`.

Iran Accounting already aligned FX Sales Invoice **item** company-currency amounts as
RATE-FIRST integers, but left **header** `base_*` totals as PRODUCT-FIRST
(`grand_total × conversion_rate`). Receivable GL used the header; income GL used the
items → 114 IRR imbalance. Pattern A Purchase Invoice already reaggregated headers and
skipped ERPNext’s product-first precision-loss Round Off; Sales Invoice did not.

### Fix

1. Shared Pattern A detection: `is_pattern_a_fx_invoice` / `_sales_` / `_purchase_`.
2. `reaggregate_fx_invoice_base_totals` for both FX PI and FX SI after item align.
3. Fail-closed precision-loss skip extended to eligible FX Sales Invoices.
4. EUR transaction amounts unchanged; domestic IRR and Pattern B unchanged.

### Anchor verification

| Check | Result |
|---|---|
| SI `1405-00269` submit | Success |
| RATE-FIRST base grand | `82,365,433,140` |
| GL debit = credit | Exact |
| Precision-loss Round Off | None |
| EUR totals | Unchanged (`48,087`) |

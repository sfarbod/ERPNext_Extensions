# Release 5.2.24 — FX Purchase Invoice Pattern A (RATE-FIRST headers)

## Summary

- **Accounting policy:** RATE-FIRST remains authoritative for Iran Accounting company-currency IRR.
- **Pattern A:** Eligible FX Purchase Invoices now derive `base_total` / `base_net_total` / `base_grand_total` from aligned item bases, not from `grand_total × conversion_rate`.
- **Pattern B:** Additional/distributed-discount invoices (5.0.1 policy) are skipped. No header re-aggregation.
- **Sales Invoice / Purchase Order / Purchase Receipt:** No header re-aggregation in this release. Shared row helper remains RATE-FIRST; SI header gap is a documented follow-up.

## Item and header contract (eligible FX PI)

```
base_rate      = ROUND_HALF_UP(rate × conversion_rate, 0)
base_amount    = ROUND_HALF_UP(qty × base_rate, 0)
base_net_rate  = ROUND_HALF_UP(net_rate × conversion_rate, 0)   # no distributed discount
base_net_amount= ROUND_HALF_UP(qty × base_net_rate, 0)
base_total     = Σ item.base_amount
base_net_total = Σ item.base_net_amount
base_grand_total = base_net_total + existing Total-category base tax
```

Transaction currency (`qty`, `rate`, `amount`, `grand_total`, `conversion_rate`, tax amounts) is not rewritten.

ERPNext `make_precision_loss_gl_entry` is skipped for Pattern A **only after**
`validate_pattern_a_rate_first_invariants` proves item/header RATE-FIRST consistency.
Invalid / stale Pattern A headers throw — they never fall through to vanilla
(product-first Round Off would glue the forbidden hybrid). The upstream method is
fingerprint-gated (ERPNext 16.35.x allow-list; 16.36 blocked). GL debit/credit
allowance remains 0.5. No generic Round Off absorber.

## Reference (Development draft)

`ACC-PINV-2026-01345` — 54,000 × 0.09 EUR × 2,830,792:

| Field | Before (hybrid) | After |
|-------|-----------------|-------|
| `rate` / `amount` / `grand_total` | 0.09 / 4,860 / 4,860 EUR | unchanged |
| `base_rate` | 254,771 | 254,771 |
| `base_amount` / `base_net_amount` | 13,757,634,000 | 13,757,634,000 |
| `base_grand_total` / payable | 13,757,649,120 | 13,757,634,000 |
| GL difference | 15,120 | 0 |

Payable account `211002` is IRR, so `outstanding_amount` = 13,757,634,000 IRR. A later EUR payment at another FX rate may post exchange gain/loss relative to this RATE-FIRST IRR liability — that is expected.

## Out of scope (explicit)

- Pattern B documents (`ACC-PINV-2026-00629`, `ACC-PINV-2026-00284`)
- Sales Invoice header re-aggregation
- Purchase Order / Purchase Receipt header re-aggregation
- Historical data patch / submitted GL rewrite
- Production

## Compatibility stack

- ERPNext **16.35.0**
- Frappe **16.34.0**
- erpnext_extensions **5.2.24**

## Files

- `iran_accounting/domain/qty_rate_amount.py`
- `iran_accounting/qty_rate_amount.py`
- `iran_accounting/accounts_invoice.py`
- `iran_accounting/integration/monkey_patches.py`
- `iran_accounting/tests/test_fx_purchase_invoice_pattern_a.py`
- `iran_accounting/tests/test_fx_pi_pattern_a_precision_loss_guard.py`
- `RELEASE_5_2_24.md`
- `__init__.py` version bump

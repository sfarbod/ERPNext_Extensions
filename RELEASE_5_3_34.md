# Release 5.3.34 — Manufacture TYPE C IRR residual → Stock Adjustment

## Summary

Routes **mathematically proven** whole-IRR multi-output Manufacture allocation
residuals (FG + Product Reject) to native ERPNext **Stock Adjustment**
(Difference Account), instead of injecting them into
`Stock Entry Detail.additional_cost`.

Target canary: `MAT-STE-2026-37762`.

## Fixed

- Product Reject Manufacture pure IRR residual was incorrectly represented as
  `additional_cost` (TYPE A semantic pollution).
- Repeated validate / `before_submit` could inflate the leftover
  (`1 → 2 → 3`) because `additional_cost += leftover` was not idempotent when
  ERPNext did not reset row additional cost between Iran hooks.
- Ledger contract then observed row↔SLE divergence after the phantom plug
  drifted.

## New accounting policy (A / B / C)

| Class | Meaning | Treatment |
|-------|---------|-----------|
| **TYPE A** | Real Additional Cost / LCV capitalization | Capitalize normally — never classified as rounding |
| **TYPE B** | Single-FG economic residual (v5.3.30) | FG amount-authoritative; Stock Adjustment = 0 |
| **TYPE C** | Proven whole-IRR multi-output allocation residual | Native Stock Adjustment (Espad: `621301 - تعدیلات موجودی کالا - E`) |

Classifier: `iran_accounting.domain.manufacture_irr_residual.classify_manufacture_irr_residual`.

TYPE C requires **all** gates (Manufacture, whole-IRR, known economic pool,
integer qty×rate composition, residual within `|L| ≤ floor(gcd(qi)/2)`, no
integrity failures). Unknown differences **fail closed** — no
`abs(diff) ≤ 1` tolerance hacks.

## Safety

- TYPE C mathematically proven; fail-closed otherwise
- No tolerance-based ledger relaxation for TYPE C
- No custom GL path — Core Difference Account / Stock Adjustment only
- Contract stamp `custom_manufacturing_costing_contract_version`:
  - Drafts / new submits → `5.3.34` (TYPE C → SA)
  - Submitted documents stamped `5.3.3` keep legacy leftover→`additional_cost`
    so RIV cannot silently rewrite historical economics
- Policy keys off **persisted** DB `docstatus` (submit sets in-memory
  `docstatus=1` early)

## Canary expected economics (`MAT-STE-2026-37762`)

```
FG amount                  2,045,097,670   (qty × 358,915)
FG additional_cost                     0
Product Reject amount            717,752   (qty × 358,876)
Σ signed SLE                          −1
Stock Adjustment GL (621301)          +1
GL Debit == GL Credit
```

## Regression

- v5.3.30 single-FG TYPE B (`MAT-STE-2026-37736` / unit suite) preserved
- Component Scrap issued-rate costing preserved
- Real Additional Cost remains capitalization even when TYPE C coexists

## Version

`erpnext_extensions.__version__` = **5.3.34**

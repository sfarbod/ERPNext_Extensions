# Release 5.7.12 — Normalized Manufacture GL (residual-only SA)

**Version:** 5.7.12  
**Runtime scope:** IRR companies — Manufacture and Repack Stock Entries  
**Deployment status:** Local release commit only. **No Production deploy. No push.**

## Approved accounting behavior

**Keep** Stock Adjustment account `621301 - تعدیلات موجودی کالا - E`.

Do **not** introduce or use `511009` (or any other account) as a Manufacture
clearing / intermediary bridge.

Per Manufacture (and Repack) Stock Entry, Stock Adjustment posts only the
genuine net residual:

| Residual | GL on 621301 |
|---|---|
| Zero | No row |
| Debit net | Exactly one debit row |
| Credit net | Exactly one credit row |

Never post gross Dr/Cr bridge turnover, and never both debit and credit on
621301 for the same voucher.

```
Inventory GL  = SLE stock_value_difference per warehouse account
OH GL         = Additional Cost expense accounts once (no clearing debit)
SA_net        = −(Σ SLE.svd − total_additional_costs)   # debit − credit
```

Unchanged: multi-output costing, STAGE_CO, scrap/by/co-product/SFG allocation,
SABE/A2, integer IRR, ROUND_HALF_UP, and Additional Cost capitalization on SED/SLE.

## Purpose scope

| Purpose | Normalized residual-only GL |
|---|---|
| Manufacture | Yes (IRR) |
| Repack | Yes (IRR) |
| Material Transfer / Issue / Receipt | No |
| Material Transfer for Manufacture | No |
| Send to Subcontractor | No |
| Other Stock Entry purposes | No |

## Department

Department handling remains a **manual business-process** responsibility.
This release does **not** change, redesign, or add Department allocation logic.

## Company field `custom_iran_manufacturing_expense_account`

Diagnostic / unused for GL posting. Retained for compatibility only. It does
**not** route Manufacture GL expense legs to 511009 or any clearing account.

## HRC-00012 historical correction (TEST only)

Package **HRC-00012** was successfully applied on **`development.localhost` (TEST)**.

Exactly these six submitted Manufacture vouchers were GL-rebuilt in TEST
(SED/SLE/Bin unchanged):

- `MAT-STE-2026-40332`
- `MAT-STE-2026-40149`
- `MAT-STE-2026-36826`
- `MAT-STE-2026-36928`
- `MAT-STE-2026-40902`
- `MAT-STE-2026-43589`

**Production historical correction has not been performed.**  
**No Production deployment, push, merge, or tag has occurred for this release.**

Production apply (separately authorized) remains:

```text
preflight_manufacture_gl_reclass(company)
apply_manufacture_gl_reclass(company, dry_run=False, confirm_apply=True)
```

## Excluded from this release

- Obsolete HRC-00011 Draft Journal Entry consolidation path
  (`manufacture_sa_residual_consol` and its tests) — not shipped; not activated.
- Temporary HRC-00012 execution / diagnostic scripts under `iran_accounting/scripts/`.

## Release files

* `erpnext_extensions/__init__.py` — version 5.7.12
* `domain/manufacture_expense_account.py` — normalized builder + SA collapse
* `domain/manufacture_gl_reclass_campaign.py` — HRC-00012 allowlisted campaign API
* `zero_value_transfer.py` — IRR Manufacture/Repack dispatch
* `integration/monkey_patches.py` — gated post-processor SA collapse
* `custom_fields.py` — diagnostic Company field description
* `tests/test_manufacture_expense_account_v5712.py`
* `RELEASE_5_7_12.md`

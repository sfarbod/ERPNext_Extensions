# Release 5.7.23 — Sales Return Opening Stock Valuation Fallback

**Version:** **5.7.23**  
**Branch:** `develop`  
**Runtime scope:** Iran Accounting sales-return incoming_rate hierarchy  
**Deployment status:** Local Development validated. Document `RTN-1405-00035` **Submitted** on `development.localhost`. **No Production deploy. No push. No RIV/HRC.**

## Objective

Prevent zero or selling-price inventory valuation on Sales Returns when:

- `return_against` is missing
- referenced Batch has no SLE/SABE valuation history (or Batch is missing)
- FOC rows have selling rate `0`

New fallback: earliest valid **Opening Stock** valuation for the same Item + Company.

## Valuation hierarchy

1. **Original transaction** — `return_against` outgoing valuation (Core behaviour preserved)
2. **Valid Batch / stock ledger valuation** — `_get_incoming_rate` > 0 (Batch Master alone is insufficient)
3. **Earliest Opening Stock valuation** — Stock Reconciliation purpose `Opening Stock`, or Stock Entry `is_opening=Yes`
4. **`allow_zero_valuation_rate`** or a clear submit-time validation error

**Selling rate is never used as inventory cost** (FOC or non-FOC).

### Opening rate selection

- Same Company, exact Item Code
- Prefer the return warehouse when an opening rate exists there
- Else earliest valid opening rate for the Item anywhere in the Company
- Exclude zero, cancelled, and non-opening transactions
- Integer IRR rounding via Iran Accounting helpers

## Verified opening source

| | |
|--|--|
| Document | `MAT-RECO-2026-02786-1` |
| Purpose | Opening Stock |
| Item | `20100043` |
| Warehouse | `انبار آماده فروش -میلان پارس` |
| Posting date | 2026-03-21 |
| Opening valuation rate | **2,369,208 IRR** |

Current warehouse moving-average (`775,796`) is **not** the opening rate and is not used by this fallback.

## Submitted document verification (`RTN-1405-00035`)

| Check | Result |
|--|--|
| Site | `development.localhost` |
| Version at submit | `5.7.23` |
| Docstatus / status | `1` / Return Submitted |
| Workflow | Submitted via `Sales Invoice Approval03` |
| SLE count | 12 |
| GL balanced | Debit = Credit = 69,746,330,537 IRR |
| Unexpected zero incoming rates on target rows | None |

### Target SLE incoming rates (submitted)

| Item | Qty (in) | Incoming Rate | SVD | Result |
|---|---:|---:|---:|--|
| 20100042 FOC | 20 | **1,126,016** | 22,520,320 | Batch preserved |
| 20100043 FOC | 1,220 | **2,369,208** | 2,890,433,760 | Opening fallback |
| 20100043 FOC | 380 | **2,369,208** | 900,299,040 | Opening fallback |
| 20100043 Paid | 2,950 | **2,369,208** | 6,989,163,600 | Opening (selling rate removed) |
| 20100043 Paid | 2,334 | **2,369,208** | 5,529,731,472 | Opening (selling rate removed) |
| 20100043 Paid | 2,716 | **2,369,208** | 6,434,768,928 | Opening (selling rate removed) |

### GL summary (stock impact)

| Account | Debit | Credit |
|--|--:|--:|
| 111635 — کالای امانی ما نزد دیگران(موجودی محصول) | 22,961,567,937 | 0 |
| 511001 — قیمت تمام شده کالای فروش رفته محصولات | 0 | 22,961,567,937 |

Stock Ledger SVD totals for inventory receipts match the inventory GL debit/credit pair.

## Changed files

- `erpnext_extensions/iran_accounting/domain/standalone_return_valuation.py` — hierarchy + opening lookup
- `erpnext_extensions/iran_accounting/integration/monkey_patches.py` — `set_incoming_rate` / `get_rate_for_return` wrappers
- `erpnext_extensions/iran_accounting/tests/test_standalone_return_foc_valuation_v5722.py` — regression suite
- `erpnext_extensions/__init__.py` — version `5.7.23`
- `RELEASE_5_7_23.md` — this document

## Regression tests

```text
python -m unittest erpnext_extensions.iran_accounting.tests.test_standalone_return_foc_valuation_v5722 -v
# 15 tests OK
```

Coverage includes: linked original rate priority, valid batch over opening, orphan/missing/nonexistent batch → opening, FOC and non-FOC, warehouse preference, company-wide opening fallback, allow_zero, submit-time missing-rate error, multi-row same item, integer IRR.

## Operational notes

- Linked returns with a populated original rate remain untouched.
- Do not run RIV/HRC for this draft-originated valuation path; SLEs were created correctly on first submit.
- User pushes the commit manually; no remote push from this release step.

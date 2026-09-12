# Release 5.1.9 — Stock Repost Precision Runtime / Schema-Drift Reliability

## Summary

v5.1.8 correctly defined and (on first migrate) applied `DECIMAL(30,9)` for the full stock/repost monetary graph, including `tabStock Entry Detail.basic_amount`.

A live Repost Item Valuation nevertheless still failed with:

```text
MySQLdb.DataError: (1264, "Out of range value for column 'basic_amount' at row 1")
```

for values such as `basic_amount = -1647101590164.27`.

**v5.1.9 is not another duplicate allowlist.** It is a runtime/schema-drift reliability fix that makes the v5.1.8 guarantee survive later `updatedb` / migrate cycles.

## Root cause (confirmed)

Frappe `updatedb()` builds Currency SQL from DocField `length` (default **21** → `DECIMAL(21,9)`).

Controlled forensic result on this bench:

| Step | `basic_amount` SQL |
|------|--------------------|
| Start with Property Setter `length=30` | `decimal(30,9)` |
| Force narrow to `DECIMAL(21,9)`, keep PS | `updatedb` widens back to `decimal(30,9)` |
| Delete Property Setter `length=30`, column at `30,9` | `updatedb` **narrows to `decimal(21,9)`** |

v5.1.8 architecture used **one-shot** pre/post patches (recorded in `tabPatch Log`). After the first successful migrate:

1. Patches do not re-run.
2. If Property Setters are later missing / not loaded / site restored without them / another sync clears effective length, `updatedb` reverts columns to `DECIMAL(21,9)`.
3. No recurring repair/assert existed, so migrate could succeed while `basic_amount` was again `DECIMAL(21,9)`.

That is why a v5.1.8-covered field could still overflow in production.

## Forensic evidence (dev site at diagnosis)

| Item | Value |
|------|-------|
| Loaded `erpnext_extensions.__version__` | `5.1.8` (before this release) |
| Git HEAD | `f28cbc4a8d72e226c41c770abcb2b6d4dd9d30cf` |
| v5.1.8 files present | Yes |
| Patch log pre | `set_stock_repost_amount_decimal_metadata_v518` executed |
| Patch log post | `expand_stock_repost_amount_precision_v518` executed |
| Property Setter `Stock Entry Detail.basic_amount length` | `30` (present on this site) |
| Effective meta length | `30` |
| Actual SQL at diagnosis (healthy site) | `decimal(30,9)` |
| Drift reproduction | Removing PS + `updatedb` → `decimal(21,9)` |

Production sites that lost Property Setters (or never retained them across deploy/restore) would run repost against `DECIMAL(21,9)` despite v5.1.8 code being present.

## Fix architecture

- **Authoritative registry:** unchanged — `stock_repost_decimal_precision_v518.REPOST_MONETARY_FIELDS_BY_DOCTYPE`
- **v5.1.9 layer:** `stock_repost_decimal_precision_v519.py`
  - `repair_stock_repost_decimal_schema()` — re-apply Property Setters + idempotent ALTER
  - `assert_stock_repost_decimal_schema()` — fail migrate if any target ≠ `DECIMAL(30,9)`
  - `get_stock_repost_precision_status()` — support diagnostic (not per-SLE)
  - Critical hard-check for `basic_amount`, `amount`, `basic_rate`, `valuation_rate`
- **Recurring hook:** `hooks.after_migrate` → `stock_repost_decimal_precision_v519.after_migrate`
- **One-shot upgrade patch:** `ensure_stock_repost_decimal_precision_v519` (post_model_sync)

Self-heal flow every migrate:

1. Ensure Property Setter `length=30` for all registry fields
2. ALTER any drifted compatible decimals to `DECIMAL(30,9)` (preserve NULL/default)
3. Assert entire registry + critical SED fields
4. Fail migrate loudly on mismatch

## Explicit non-goals

- No change to ERPNext negative-stock validation / NonNegative checks
- No global Frappe Currency type-map change
- No duplicate third allowlist of stock fields

## Regression values (production)

```text
basic_rate      = -4626689860
valuation_rate  = -4626276180
basic_amount    = -1647101590164.27
amount          = -1646954320184
```

Verified: SQL + `Stock Entry Detail.db_update()` + `recalculate_amounts_in_stock_entry` without DataError 1264.

## Files

- `stock_repost_decimal_precision_v519.py`
- `patches/post_model_sync/ensure_stock_repost_decimal_precision_v519.py`
- `hooks.py` — after_migrate entry
- `tests/test_stock_repost_decimal_precision_v519.py`
- `tests/test_stock_repost_decimal_precision_v519_sync_e2e.py`
- `erpnext_extensions/__init__.py` — version `5.1.9`

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.1.8 registry; does not replace it

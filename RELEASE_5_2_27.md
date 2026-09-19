# Release 5.2.27 — Scope-aware RIV integrity + IRR GL precision alignment

This release finalizes the Iran Accounting RIV/GL hardening delivered across
**v5.2.25 → v5.2.26 → v5.2.27**. Package version: **5.2.27**.

## Summary

| Area | Behavior |
|------|----------|
| RIV valuation integrity (I1/I4/…) | Strict **inside** the declared RIV target item scope; **non-blocking + audited** for unrelated items reached only via dependant expansion |
| IRR GL posting / repost | Fractional stock valuation maps are aligned to IRR currency precision (0) so a proven **1 IRR** rounding residual does not abort vanilla debit/credit validation |
| Fail-closed | Larger or unexplained GL imbalances still fail; in-scope integrity violations still block |

---

## 1. Scope-aware RIV valuation guard (v5.2.25 / v5.2.26)

ERPNext Item-and-Warehouse RIV may expand through multi-hop
`dependant_sle_voucher_detail_no` chains and encounter legacy/converted SLE
corruption on **other** items.

### Rule

- **Blocking:** offending SLE `item_code` is one of the RIV’s *declared* targets
  (`Repost Item Valuation.item_code`; same item, any warehouse).
- **Non-blocking:** different item codes reached only via dependant expansion —
  log and continue vanilla `process_sle`.
- **Fail-closed:** non-RIV submit / unresolved targets — unchanged.

Declared targets do **not** include ERPNext’s expanding `items_to_be_repost` list
or the current `args.item_code` under dependant walking (v5.2.26 hardening).

### Audit

Out-of-scope anomalies are written to:

- site log `logs/iran_riv_out_of_scope.log` (survives RIV rollback)
- Error Log (committed so desk audit survives rollback)

### Safety

- No global I1/I4 disable
- No bare `except Exception: pass`
- Same-target integrity failures still abort the RIV (atomic target chain)

---

## 2. IRR 1-unit GL precision alignment (v5.2.27)

### Problem class

Vanilla `process_debit_credit_difference` validates at Currency precision
(**0** for IRR) with a hardcoded Stock Entry allowance of **0.5**. When a GL map
still carries a fractional stock-adjustment residual (e.g. **0.48**) while
inventory legs round independently, that residual rounds to **0** and the
voucher fails with **Difference is 1.0** — never reaching `make_round_off_gle`.

### Known arithmetic pattern

Approximate Manufacture map before alignment:

| Account role | Amount |
|--------------|--------|
| WIP Cr | 50,671,372.03 |
| Inventory Dr | 50,671,372.51 |
| Stock Adjustment Cr | 0.48 |

At IRR precision 0 without alignment:

| Account role | Amount |
|--------------|--------|
| WIP Cr | 50,671,372 |
| Inventory Dr | 50,671,373 |
| Stock Adjustment | 0 |
| **Diff** | **1 IRR** |

### Fix

`align_irr_gl_map_to_currency_precision` (Iran `StockController.make_gl_entries`):

1. Quantize GL monetary fields to IRR precision.
2. If the remaining net is exactly **one IRR quantum**, absorb it into Company
   `stock_adjustment_account` (standard Manufacture valuation residual account).
3. Gaps larger than one quantum remain fail-closed for vanilla validation.

Does **not** widen global tolerance. Does **not** disable Iran guards.

---

## 3. Safety checklist

- No global I1 bypass
- Genuine **in-scope** valuation corruption still blocks
- Unrelated-item legacy I1 is logged / non-blocking only
- Unexplained GL imbalance **larger than one IRR quantum** still blocks
- No SQL SLE/GL rewrites; standard ERPNext RIV/GL paths only

---

## 4. Operational note (not repaired by this release)

A Failed RIV can leave **corrected SLE** with **stale GL** because ERPNext commits
SLE progress mid-flight and may roll back the GL chunk on later failure.

Existing SLE↔GL drift from previously failed RIVs is **not** automatically
repaired by installing this release. Recovery requires a **separate controlled
repost / accounting-ledger recovery** process after upgrade.

---

## Files (cumulative)

- `iran_accounting/domain/riv_valuation_scope.py`
- `iran_accounting/domain/riv_valuation_guard.py`
- `iran_accounting/domain/irr_gl_precision_align.py`
- `iran_accounting/integration/monkey_patches.py`
- `iran_accounting/tests/test_riv_valuation_scope.py`
- `iran_accounting/tests/test_irr_gl_precision_align.py`
- `RELEASE_5_2_25.md` / `RELEASE_5_2_26.md` / `RELEASE_5_2_27.md`

## Tests

```text
bench --site <site> run-tests --app erpnext_extensions \
  --module erpnext_extensions.iran_accounting.tests.test_riv_valuation_scope

bench --site <site> run-tests --app erpnext_extensions \
  --module erpnext_extensions.iran_accounting.tests.test_irr_gl_precision_align
```

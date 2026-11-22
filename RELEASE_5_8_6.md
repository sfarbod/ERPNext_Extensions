# erpnext_extensions 5.8.6

## Manufacturing fixes — Job Card Record Output ZeroDivision + Stock Entry ledger mirror

### Summary

Extension-only fixes for two production-critical manufacturing defects. ERPNext
core is untouched. Existing IRR valuation, SLE↔SED ledger contract, and RIV
behaviour are preserved; the ledger contract is not weakened.

---

## Issue 1 — ZeroDivisionError on Job Card Record Output (ERPNext #68700 / PR #68701)

### Root cause

`bom.add_operations_cost` appends **Corrective Operation Cost** as:

```text
remaining_corrective_cost / (max_operation_qty − produced_qty) × fg_completed_qty
```

On Work Orders with `track_semi_finished_goods`, `max_operation_qty` is the
**minimum** completed qty across *all* operations (including semi-finished Job
Cards). Once the finished-good operation has produced `produced_qty`, that
denominator is **0**. Semi-finished Job Card **Record Output** (e.g.
`PO-JOB10628` on `MFG-WO-2026-00846`) therefore raises `ZeroDivisionError`.

Upstream PR [#68701](https://github.com/frappe/erpnext/pull/68701) fixes this by:

1. Skipping WO-level corrective cost on Job Cards whose `finished_good` ≠ WO
   `production_item` (semi-FG).
2. Filtering max operation qty to FG Job Cards when tracking semi-finished goods.
3. (Extension hardening) Skipping the append when the remaining FG denominator
   is ≤ 0.

### Fix (extension)

- Module: `stock_extensions/bom_operations_cost/patch.py`
- Idempotent monkey-patch of `erpnext.manufacturing.doctype.bom.bom.add_operations_cost`
  registered from `iran_accounting.integration.monkey_patches`.
- Core corrective block is disabled for the wrapped call; corrective cost is
  re-appended with the PR #68701-safe rules.

### Acceptance

- Recording output for `PO-JOB10628` no longer raises ZeroDivisionError.
- Corrective cost is not booked on semi-FG manufacture Stock Entries.
- Normal FG corrective allocation with a positive denominator is unchanged.

---

## Issue 2 — Stock Entry ledger contract violation (`MAT-STE-2026-44477`, Δ = 981)

### Root cause

Document shape: Manufacture / STAGE_CO closed plan with consume + FG + scrap.

1. Consume SLE ABSORB remirrors SED to warehouse-clear amount
   (`1106952098`; qty×rate was `1106952552`).
2. FG SLE posts at `1095565104` (rate `1116784`); remirror aligns SED to SLE.
3. `on_submit` then calls `set_total_incoming_outgoing_value`, whose IRR patch
   re-ran **STAGE_CO restore** (`_restore_closed_manufacture_contract_after_core_rates`
   → `apply_iran_manufacture_output_contract`) and rewrote FG SED to
   `1095564123` (rate `1116783`) — exactly **qty × 1 IRR = 981** lower.
4. SLE was left at `1095565104` → contract failure:

```text
SLE movement 1095565104 != row mirror 1095564123
Σ SLE 454 != signed Σ row.amount -527
```

(The 981 gap is the same in both comparisons.)

This is **not** a floating-point drift and must not be “balanced away”. The
post-SLE STAGE_CO restore was the source of the SED/SLE divergence.

### Fix (extension)

- `patched_set_total_incoming_outgoing_value` detects when Stock Ledger Entries
  already exist for the voucher.
- **After SLE posted:** only refresh header totals from current row amounts;
  do **not** re-run STAGE_CO restore or FG residual align.
- **Before SLE posted** (validate / before_submit): previous restore + align
  behaviour is unchanged.

### Acceptance

- Submitting `MAT-STE-2026-44477` (with valid batches) passes the ledger
  contract; FG SLE movement equals SED amount authority.
- No suppression of the contract; no artificial balancing entries.
- Integer IRR rates and ROUND_HALF_UP rules unchanged.

---

## Modified files

- `erpnext_extensions/__init__.py` — version `5.8.6`
- `erpnext_extensions/stock_extensions/bom_operations_cost/` — new patch + tests
- `erpnext_extensions/iran_accounting/stock_entry.py` — post-SLE set_total guard
- `erpnext_extensions/iran_accounting/integration/monkey_patches.py` — register patch
- `erpnext_extensions/iran_accounting/tests/test_ste_post_sle_set_total_v586.py`
- `RELEASE_5_8_6.md`

## Tests

```bash
bench --site <site> run-tests --module \
  erpnext_extensions.stock_extensions.bom_operations_cost.tests.test_corrective_cost_zerodiv_v586

bench --site <site> run-tests --module \
  erpnext_extensions.iran_accounting.tests.test_ste_post_sle_set_total_v586
```

## Compatibility

- ERPNext: deployed tree still has corrective cost inline in `bom.py` (pre
  operations_cost service split). Patch targets that symbol; safe no-op skip
  when denominator ≤ 0.
- Frappe / Iran accounting / RIV: unchanged contracts.
- No ERPNext or Frappe core file modifications.

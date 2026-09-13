# v5.2.3 — Allow Incoming Movements to Heal Existing Negative Stock

## Title

Allow incoming movements to heal existing negative stock

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.2**
- `erpnext_extensions.__version__` = **5.2.3**

---

## 1. Summary

v5.2.3 allows a **negative-stock healing incoming movement**: a legitimate incoming stock ledger entry that **reduces** an already-negative warehouse running balance, even when the resulting balance remains negative.

Example:

| | Qty |
|---|---|
| Previous balance | −565.17 |
| Incoming qty | +300 |
| New balance | −265.17 |

This transaction is now allowed. It improves an existing deficit; it is **not** general negative-stock enablement.

Outbound movements that create or worsen negative stock remain blocked. Source warehouse and outward Serial and Batch Bundle (SABB) protections are unchanged.

---

## 2. Problem / Root Cause

ERPNext Core `update_entries_after.validate_negative_stock` rejects an SLE whenever the **resulting** warehouse qty is negative:

```
previous_qty < 0
incoming actual_qty > 0
resulting qty < 0
```

even when the incoming movement **improves** the balance.

Production-shaped example:

```
-565.17 + 300 = -265.17
```

ERPNext reports:

```
Insufficient Stock
265.17 units ... needed in Warehouse ...
```

The named warehouse can be the **TARGET** warehouse, because that warehouse’s running ledger was already negative **before** the incoming SLE. ERPNext treats “result still negative” as insufficient stock, not as a healing receipt.

This was **not**:

- reversed source/target mapping
- incorrect SABB direction
- an I4 regression from v5.2.2
- a global stock shortage in the **source** warehouse

The source warehouse could have enough qty. The block was on the incoming target SLE because the target running qty (`wh_data.qty_after_transaction`) was already negative. `Bin.actual_qty` must not be used as the decision figure; it can disagree with the SLE running balance.

---

## 3. New Business Rule

A movement is a **negative-stock healing incoming movement** when:

```
previous_qty < 0
AND actual_qty > 0
AND new_qty > previous_qty
```

where:

```
new_qty = previous_qty + actual_qty
```

`previous_qty` is the running qty ERPNext already holds inside `validate_negative_stock` (`wh_data.qty_after_transaction`), never `Bin.actual_qty`.

`actual_qty > 0` is SLE incoming semantics. The rule is **not** inferred from `s_warehouse` / `t_warehouse` alone.

If the predicate is true, that incoming SLE (IRR companies only) is not enqueued as warehouse `NegativeStockError`, even if `new_qty < 0`.

### Allowed

| Previous | Incoming | Result | Decision |
|---|---|---|---|
| −565.17 | +300 | −265.17 | ALLOW |
| −565.17 | +565.17 | 0 | ALLOW |
| −565.17 | +600 | +34.83 | ALLOW |
| 0 | +300 | +300 | ALLOW (normal incoming; not healing) |
| +100 | +300 | +400 | ALLOW (normal incoming; not healing) |

The principle: a positive stock movement that moves an existing negative balance **toward zero** must not be rejected merely because the final balance remains negative.

---

## 4. What Is Still Blocked

| Previous | Movement | Result | Decision |
|---|---|---|---|
| +100 | −300 | −200 | **BLOCKED** — outbound creates negative stock |
| 0 | −100 | −100 | **BLOCKED** — outbound from zero |
| −565.17 | −100 | −665.17 | **BLOCKED** — outbound worsens existing negative |
| source 34.83, transfer 300 | −300 on source | −265.17 needed | **BLOCKED** — Insufficient Stock **265.17 on SOURCE** |
| source batch 100, transfer 300 | outward SABB | shortage | **BLOCKED** — `BatchNegativeStockError` |

Still enforced:

- outbound movements cannot create negative stock
- outbound movements cannot worsen an existing negative balance
- source warehouse availability validation remains fully active
- outward SABB / batch shortage validation remains fully active
- a voucher with one healing incoming SLE **and** one invalid outgoing SLE still fails on the invalid outgoing SLE

---

## 5. Batch / SABB Support

v5.2.3 applies the same healing predicate to valid **Inward** SABB movements when the target batch running qty is already negative and the inward qty reduces that deficit.

| Layer | Healing applied? |
|---|---|
| Warehouse `validate_negative_stock` | Yes, incoming SLE only |
| Inward SABB (`type_of_transaction == "Inward"`) | Yes, when every currently-negative target batch in that bundle is reduced by the inward qty |
| Outward SABB | **No** — unchanged |

Clarifications:

- Source Outward SABB remains unchanged
- Target Inward SABB may heal an existing negative batch/warehouse balance
- Source batch shortages still raise `BatchNegativeStockError`
- No source/target SABB mapping changes were introduced

Tested Material Transfer:

| Leg | SABB |
|---|---|
| Source | Outward, total_qty **−300** |
| Target | Inward, total_qty **+300** |

Vanilla `validate_batch_inventory` would still throw after `validate_negative_batch` via `throw_error_message`, so Inward `validate_batch_inventory` is skipped **only** when the inward bundle heals those already-negative batches. Outward SABB is never skipped.

---

## 6. Valuation Integrity

No valuation calculations were rewritten. There were no manual changes to:

- `valuation_rate`
- `incoming_rate`
- `outgoing_rate`
- `stock_value`
- `stock_value_difference`

ERPNext continues to calculate Moving Average normally.

Tested target incoming SLE after `-565.17 + 300`:

| Field | Value |
|---|---|
| `qty_after_transaction` | −265.17 |
| `valuation_rate` | 1,000,000 |
| `stock_value` | −265,170,000 |
| `stock_value_difference` | +300,000,000 |

`stock_value = qty_after × valuation_rate`. That is vanilla Moving Average on a still-negative qty, not a custom clamp. I4 does not apply here (`qty_after ≠ 0`).

---

## 7. I4 / v5.2.2 Compatibility

- v5.2.2 I4 lifecycle handling is **unchanged**
- transient incoming SLE false positives remain fixed
- genuine zero-qty leftover-value corruption still raises I4
- I1 / I2 / I3 / I5 were not weakened

Genuine corruption still blocked:

```
previous qty = 300
actual_qty = -300
qty_after = 0
stock_value = 100000
```

Result: `Stock valuation integrity (I4)`.

Incoming transient shape from v5.2.2 (`actual_qty = +300`, `qty_after = 0`, `stock_value = SVD`) still does **not** raise a false I4.

---

## 8. Repost / Replay

A historical incoming movement that reduces an existing negative balance is **not** rejected during Repost Item Valuation / forward replay solely because the resulting balance remains negative.

During replay:

- healing incoming movements remain allowed
- outbound negative-stock protections remain active
- I4 protections remain active

---

## 9. Safety Boundaries

v5.2.3 does **not**:

- enable global negative stock
- set `Stock Settings.allow_negative_stock`
- set `Item.allow_negative_stock`
- enable negative batch stock as a setting
- disable warehouse `validate_negative_stock`
- disable batch negative-stock validation
- globally bypass `NegativeStockError`
- swallow unrelated negative-stock exceptions after ERPNext has accumulated other failures
- repair historical negative stock automatically
- modify production data (including Item 18000007, Bin, SLE, SABB, or production vouchers)
- rewrite stock valuation
- bypass source shortages

The exception is decided **per SLE / warehouse / batch movement**, and only for a qualifying incoming healing row.

---

## 10. Historical Ledger Note

v5.2.3 allows **future** legitimate incoming movements to heal existing negative running balances.

It does **not** repair historical SLE / Bin / SABB inconsistencies (for example Bin `+169.77` vs reconstructed SLE `−565.17`). Those remain a separate ledger repair / repost problem.

---

## 11. Tests

| Scenario | Result |
|---|---|
| Production-shaped healing: −565.17 + 300 = −265.17 | **PASS** (ALLOW) |
| Healing to zero: −565.17 + 565.17 = 0 | **PASS** (ALLOW) |
| Healing to positive: −565.17 + 600 = +34.83 | **PASS** (ALLOW) |
| Normal incoming into zero / positive | **PASS** (ALLOW) |
| Outgoing creates negative: +100 − 300 = −200 | **PASS** (BLOCKED) |
| Outgoing from zero: 0 − 100 = −100 | **PASS** (BLOCKED) |
| Existing negative worsened: −565.17 − 100 | **PASS** (BLOCKED) |
| Source shortage: +34.83 − 300 | **PASS** (BLOCKED, 265.17 on **SOURCE**) |
| Batch/SABB healing Material Transfer | **PASS** (ALLOW; Outward −300 / Inward +300) |
| Batch source shortage | **PASS** (`BatchNegativeStockError`) |
| Genuine I4 leftover-value corruption | **PASS** (BLOCKED) |
| v5.2.2 transient I4 regression | **PASS** (no false I4) |
| Repost Item Valuation of healing transfer | **PASS** |

Totals:

| Suite | Passed | Failed | Skipped |
|---|---|---|---|
| v5.2.3 (`test_negative_stock_healing`) | 20 | 0 | 0 |
| Related I4 / RIV / process_sle regressions | 66 | 0 | 0 |
| Combined | 86 | 0 | 0 |

---

## 12. Files / Technical Scope

Narrow IRR-only wrappers; ERPNext functions are not forked.

| File | Role |
|---|---|
| `erpnext_extensions/iran_accounting/domain/negative_stock_healing.py` | Healing predicate and wrappers |
| `erpnext_extensions/iran_accounting/integration/monkey_patches.py` | Install wrappers on `validate_negative_stock`, Inward `validate_negative_batch`, and Inward `validate_batch_inventory` |
| `erpnext_extensions/iran_accounting/tests/test_negative_stock_healing.py` | Mandatory A–M matrix and site-level batch transfer |

Intercepted ERPNext points:

- `update_entries_after.validate_negative_stock`
- `SerialandBatchBundle.validate_negative_batch` (Inward healing only)
- `SerialandBatchBundle.validate_batch_inventory` (Inward healing only)

---

## 13. Upgrade Notes

| Item | Required? |
|---|---|
| Database migration | **NO** |
| Production data migration | **NO** |
| Global negative-stock setting change | **NO** |

Recommended deployment:

1. Update the app to v5.2.3
2. `bench migrate` (no schema change expected; keep the site procedure)
3. Restart web / workers
4. Smoke-test an incoming healing Material Transfer onto a warehouse whose **SLE running qty** is already negative

Do not enable `allow_negative_stock` as part of this upgrade. Do not run a production repost solely because of this release.

---

## Version

- `erpnext_extensions.__version__` = `5.2.3`

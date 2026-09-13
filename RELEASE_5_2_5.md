# v5.2.5 — Preserve Zero Stock Value on Full Consumption

## Title

Preserve vanilla zero-qty stock value after Iran Stock Entry sync

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.4**
- `erpnext_extensions.__version__` = **5.2.5**

---

## 1. Summary

v5.2.5 restores ERPNext’s empty-warehouse invariant after Iran Stock Entry SLE sync:

When Moving Average quantity is fully consumed:

```
qty_after_transaction = 0
stock_value = 0
```

A legitimate **Material Transfer for Manufacture** (MAT-STE-2026-25734) was blocked by I4 because Iran sync resurrected a **−534 IRR** leftover on the source SLE after vanilla `process_sle` had already zeroed `stock_value`.

This is **not** an I4 tolerance increase. I4 still blocks unexplained leftover value. The fix stops Iran sync from undoing vanilla’s zero-qty terminal `stock_value`. Transfer SVD (row amount) is unchanged, so source and target values stay conserved.

---

## 2. Production incident

Stock Entry **MAT-STE-2026-25734** (`Material Transfer for Manufacture`) failed on Submit:

```
Stock valuation integrity (I4).
qty_after_transaction is 0 but stock_value leftover exceeds ±1 IRR quantum

item_code=30300022
warehouse=انبار Quarantine محصول نیمه ساخته اسپاد   ← SOURCE
actual_qty=-3815.0
qty_after_transaction=0.0
outgoing_rate=2149321.0
stock_value_difference=-8199659615
stock_value=-534
```

This is a **final processed outgoing/source SLE**, not the v5.2.2 incoming/transient false positive.

---

## 3. Root cause

Vanilla ERPNext `update_entries_after.process_sle` (`erpnext/stock/stock_ledger.py`) does:

```
if not self.wh_data.qty_after_transaction:
    self.wh_data.stock_value = 0.0
stock_value_difference = self.wh_data.stock_value - self.wh_data.prev_stock_value
```

Iran `sync_irr_sle_from_stock_entry_row` then rebuilds:

```
value_before = stock_value - stock_value_difference   # previous warehouse value
stock_value_difference = signed(row.amount)           # 3815 × 2,149,321
stock_value = value_before + movement
```

Exact arithmetic for 30300022 / Quarantine:

| Quantity | Value |
|---|---|
| Previous qty | 3,815 |
| Previous valuation_rate | 2,149,321 |
| Previous stock_value | **8,199,659,081** |
| qty × rate / row.amount | **8,199,659,615** |
| Iran SVD | −8,199,659,615 |
| Iran stock_value | 8,199,659,081 − 8,199,659,615 = **−534** |
| Vanilla stock_value at qty 0 | **0** |

−534 is the warehouse identity gap that already existed on the previous SLE (MAT-STE-2026-25732), not a new rounding of this voucher.

Iran sync applied document amount independently of previous warehouse value and **undid vanilla’s zero clamp**. I4 then correctly saw a final consume-to-zero with leftover > ±1 IRR.

---

## 4. Historical residual (why previous stock_value was off)

The previous SLE already had `stock_value ≠ qty × rate` by −534. That gap is historical:

| Voucher | qty_after | stock_value at qty 0 |
|---|---|---|
| MAT-STE-2026-31073 | 0 | **−850** (first persisted zero-qty leftover) |
| MAT-STE-2026-31871 | 4,111 | carried forward |
| MAT-STE-2026-31965 | 0 | **+436** |
| MAT-STE-2026-25732 | 3,815 | stock_value 8,199,659,081 (gap **−534**) |
| MAT-STE-2026-25734 | would be 0 | **−534** before this fix |

v5.2.5 does **not** repair that historical chain. It only prevents a valid full consume from failing I4 when vanilla already emptied the warehouse.

---

## 5. Fix

After vanilla `process_sle` and Iran sync, if and only if:

- the SLE is a **final processed consume-to-zero** (v5.2.2 lifecycle)
- vanilla already computed `qty_after = 0` and `|stock_value| ≤ 1 IRR`
- Iran sync then left a material leftover

restore:

```
stock_value = 0
```

`stock_value_difference` stays the signed row amount so transfer legs remain conserved.

Do **not** restore when vanilla itself left material leftover (genuine I4). Do **not** restore incoming/transient `qty_after = 0`.

---

## 6. What is still blocked

```
previous qty = 300
actual_qty = -300
qty_after = 0
stock_value = 100000   # vanilla did not empty the warehouse
```

still raises `Stock valuation integrity (I4)`.

v5.2.2 incoming transient (`actual_qty > 0`, `qty_after = 0`, `stock_value = SVD`) still does **not** raise a false I4.

---

## 7. Transfer conservation

Isolated incident economics after the fix:

| Leg | actual_qty | qty_after | SVD | stock_value |
|---|---|---|---|---|
| Source | −3,815 | 0 | −8,199,659,615 | **0** |
| Target | +3,815 | 3,815 | +8,199,659,615 | 8,199,659,615 |

Net SVD = **0**. The −534 is not moved onto the target.

---

## 8. Version involvement

| Release | Involvement |
|---|---|
| v5.2.2 I4 lifecycle | **CONTRIBUTING** — correctly classifies this SLE as final consume-to-zero, so I4 now sees the post-sync leftover. It did not create −534. |
| v5.2.3 negative-stock healing | **NO** — this is an outgoing full consume, not an incoming healing receipt. |
| v5.2.4 purchasing DECIMAL(30,9) | **NO** — unrelated schema hardening. |

---

## 9. Site-level result for MAT-STE-2026-25734

On restored `development.localhost` (submit then rollback):

- Previous I4 on 30300022 / Quarantine: **no longer raised**
- Independent later blocker (not in this release): `BatchNegativeStockError` on item **13200040**, batch `708-13200040-0411005-hp-168951255-0`, warehouse `انبار approved اقلام بسته بندی ثانویه اسپاد`, available qty **−3850**

That batch shortage is a separate source-availability problem and is **not** weakened by v5.2.5.

---

## 10. Tests

| Scenario | Result |
|---|---|
| Incident leftover −534 is I4 before restore | PASS (BLOCKED) |
| Restore zeros stock_value, keeps row SVD | PASS (ALLOW) |
| Vanilla leftover 100,000 is not restored | PASS (I4 BLOCKS) |
| v5.2.2 incoming transient | PASS (no false I4) |
| Full consume with historical residual −534 | PASS (qty_after 0, stock_value 0, net SVD 0, RIV OK) |
| Batch/SABB full consume with residual | PASS |
| v5.2.2 I4 lifecycle suite | 10 PASS |
| v5.2.3 healing (−565.17+300, source shortage, outward batch) | 20 PASS |
| Genuine I4 Case G | PASS (BLOCKED) |

| Suite | Passed | Failed | Skipped |
|---|---|---|---|
| v5.2.5 (`test_i4_zero_qty_terminal_residual`) | 6 | 0 | 0 |
| Related I4 / healing / RIV / process_sle | 86 | 0 | 0 |
| Combined | 92 | 0 | 0 |

---

## 11. Files / Technical Scope

| File | Role |
|---|---|
| `erpnext_extensions/iran_accounting/domain/riv_valuation_guard.py` | `restore_vanilla_zero_qty_terminal_stock_value` |
| `erpnext_extensions/iran_accounting/integration/monkey_patches.py` | Snapshot vanilla qty/value after `process_sle`; restore after Iran sync, before I4 |
| `erpnext_extensions/iran_accounting/tests/test_i4_zero_qty_terminal_residual.py` | Incident + conservation + Case G |

I4 tolerance remains **±1 IRR**. No global negative stock. No production SLE/Bin repair.

---

## 12. Upgrade Notes

| Item | Required? |
|---|---|
| Database migration | **NO** |
| Production data migration | **NO** |
| Global negative-stock setting change | **NO** |
| Historical SLE repair | **NO** (separate ledger work) |

Recommended deployment: update app, `bench migrate`, restart web/workers, smoke-test MAT-STE-2026-25734 (I4 path). Remaining batch shortages on other rows are independent.

---

## Version

- `erpnext_extensions.__version__` = `5.2.5`

# v5.2.4 — Purchasing + Asset Acquisition DECIMAL(30,9)

## Title

Harden all Purchasing and Asset Acquisition monetary DB fields to `DECIMAL(30,9)`

## Baseline

- Frappe **16.33.0** (production failure environment)
- ERPNext **16.34.1**
- Previous erpnext_extensions **5.2.3** (app version before this release)
- `erpnext_extensions.__version__` = **5.2.4**

---

## 1. Production failure

Actual error:

```text
MySQLdb.DataError:
(1264, "Out of range value for column 'net_purchase_amount' at row 1")
```

| Item | Value |
|---|---|
| DocType | `Asset` |
| Field | `net_purchase_amount` |
| Value | `4250664632505` (~4.25 trillion IRR) |
| Default Frappe Currency storage | `DECIMAL(21,9)` → integer capacity `10^12 − 1` |
| Failure mode | Inserting a new Asset during fixed-asset acquisition |

`DECIMAL(21,9)` allows at most **12 integer digits**. `4250664632505` is **13 integer digits**, so MariaDB rejects the write.

**Do not fix only `Asset.net_purchase_amount`.** The same overflow class exists across the purchasing lifecycle (PO → PR → PI → Landed Cost / Subcontracting → Asset).

---

## 2. Root cause

1. Frappe maps `Currency`/`Float` to `DECIMAL(21,9)` by default.
2. IRR purchase/asset amounts regularly exceed `10^12 − 1`.
3. Earlier layers hardened selling (v5.0.7), stock-repost (v5.1.8), and schema-drift healing (v5.1.9), but **Asset acquisition and purchase-parent totals** were still `DECIMAL(21,9)`.
4. One-shot SQL `ALTER` without Property Setter `length=30` is insufficient: `updatedb()` narrows columns back to `21,9` (v5.1.9 finding).

---

## 3. Architecture

| Layer | Module |
|---|---|
| Authoritative registry | `purchasing_decimal_precision_v524.py` |
| Pre-model-sync metadata | `patches/pre_model_sync/set_purchasing_amount_decimal_metadata_v524.py` |
| Post-model-sync ALTER + assert | `patches/post_model_sync/expand_purchasing_amount_precision_v524.py` |
| Recurring drift heal | `decimal_precision_after_migrate.after_migrate` (coordinator) |
| Stock heal (existing) | `stock_repost_decimal_precision_v519.repair_*` (called by coordinator) |
| Audit CLI | `scripts/audit_purchasing_v524.print_post_migration_stats` |
| Guards | `assert_purchasing_decimal_schema()`, `assert_purchasing_field_classification_completeness()` |
| Status | `get_purchasing_precision_status()` |

Coordinator preference: **one** `after_migrate` hook runs stock (v5.1.9) + purchasing (v5.2.4). The previous stock-only hook was replaced.

### Document graph audited

```text
Material Request / Supplier Quotation / RFQ
  → Purchase Order (+ Item / Supplied Item)
  → Purchase Receipt (+ Item / Supplied Item)
  → Purchase Invoice (+ Item / Advance / Taxes / Payment Schedule)
  → Landed Cost Voucher (+ Item / Taxes / Purchase Receipt)
  → Subcontracting Order / Receipt (+ supplied items)
  → Asset (+ Finance Book / Depreciation Schedule)
  → Asset Capitalization / Value Adjustment / Repair
```

Accounting rows already owned elsewhere are **re-asserted / not duplicated**:

- `GL Entry`, `Journal Entry*` → accounting_core / stock_repost
- Landed Cost / PR-PI item amounts / Payment Schedule → stock_repost v5.1.8
- Payment Entry → existing Payment Entry precision module

---

## 4. Metadata protection

For every owned Currency/Float monetary target:

```text
Property Setter:
  property = length
  value = 30
```

Then idempotent:

```sql
ALTER TABLE ... MODIFY COLUMN ... DECIMAL(30,9)
```

preserving NULL/NOT NULL and defaults. No core ERPNext edits; no global `type_map` change.

---

## 5. Classification policy

| Class | Action |
|---|---|
| Monetary Amount | `DECIMAL(30,9)` |
| Monetary Rate (unit price) | `DECIMAL(30,9)` when IRR can overflow 21,9 |
| Qty / % / exchange rate | intentionally excluded |
| Virtual | excluded |
| Already in stock_repost v5.1.8 | Already covered / re-asserted there |

**Monetary rates hardened** include PO/PR/PI/SQ item `rate`, `base_rate`, `net_rate`, `price_list_rate`, stock UOM rates, subcontracting cost-per-qty, asset capitalization valuation rates.

**Excluded examples:** `conversion_rate`, `plc_conversion_rate`, `discount_percentage`, tax template `Purchase Taxes and Charges.rate` (tax %), qtys, weights, allocation percentages.

**Custom PI monetary fields included (site):** `guarantee_deposit`, `insurance_deposit`, `retention_money`, `total_deductions`, `withholding_tax`.

---

## 6. Critical proof

Expected schema:

```text
tabAsset.net_purchase_amount
  DATA_TYPE = decimal
  NUMERIC_PRECISION = 30
  NUMERIC_SCALE = 9
  COLUMN_TYPE = decimal(30,9)
```

Mandatory regression value:

```text
4250664632505
```

also:

```text
4250664632505.123456789
```

---

## 7. after_migrate / updatedb

- Every `bench migrate` runs the coordinator healer.
- Representative DocTypes (`Purchase Order*`, `Purchase Receipt*`, `Purchase Invoice*`, `Purchase Taxes and Charges`, `Asset`, `Asset Finance Book`, `Asset Value Adjustment`) must survive `updatedb()` without narrowing.
- Schema assert fails migrate if any registered target is not decimal(30,9).

---

## 8. Tests

- Unit: registry ownership, critical Asset field, completeness, repair error path
- Sync E2E: schema guard, updatedb non-revert, Property Setter drift heal, coordinator heal
- Asset insert of `4250664632505`
- PO / PR / PI large IRR round-trips
- Landed Cost schema + Asset Value Adjustment
- Fixed-asset acquisition path (PI → Asset.net_purchase_amount)
- Migrate ×2 idempotency + `bench build --app erpnext_extensions`

---

## 9. Audit CLI

```bash
bench --site SITE execute \
  erpnext_extensions.scripts.audit_purchasing_v524.print_post_migration_stats
```

---

## 11. Coverage counts (post-migrate audit)

| Metric | Count |
|---|---|
| DocTypes audited (graph) | 40 |
| Currency/Float/Percent fields inspected | 383 |
| Registered owned targets | 194 |
| Monetary Amount (owned) | 149 |
| Monetary Rate (owned) | 44 |
| Already hardened by stock_repost | 55 |
| Intentionally excluded (qty/%/exchange) | 135 |
| Schema correct after migrate | 194 / 194 |
| Custom PI monetary fields included | 5 |

Critical Asset schema after migrate:

```text
tabAsset.net_purchase_amount → decimal(30,9)
tabAsset.purchase_amount → decimal(30,9)
tabAsset.gross_purchase_amount → decimal(30,9)
tabAsset.total_asset_cost → decimal(30,9)
```

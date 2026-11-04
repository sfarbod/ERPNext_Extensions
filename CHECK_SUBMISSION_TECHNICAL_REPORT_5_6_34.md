# Technical Report — Delivery Note Check Submission (v5.6.34)

## Requirement

Check Submission must **never** modify the database. Savepoint mutation-and-rollback is
**not** an acceptable architecture.

## Separation of ERPNext Delivery Note submit lifecycle

### Category A — reused safely (read-only)

These methods only read DB state and/or mutate the **in-memory** working document copy
(never persisted):

| Area | Methods |
|---|---|
| Document fields | `_validate_links`, `_validate_mandatory`, `_validate_selects`, `_validate_data_fields`, `_validate_non_negative`, `_validate_code_fields`, `_validate_length` (+ children) |
| DN | `validate_posting_time`, `set_internal_return_target_warehouse`, `validate_references`, `so_required`, `validate_proj_cust`, `check_sales_order_on_hold_or_close`, `validate_warehouse`, `validate_uom_is_integer`, `validate_with_previous_doc`, `validate_against_stock_reservation_entries`, `validate_packed_qty`, `check_credit_limit` |
| Selling | `validate_items`, `validate_max_discount`, `validate_selling_price`, `validate_price_list_country`, `validate_for_duplicate_items`, `validate_target_warehouse`, `validate_sample_retention_warehouse`, `validate_standalone_serial_nos_customer` |
| Stock | `validate_duplicate_serial_and_batch_bundle`, `validate_inspection`, `validate_warehouse_of_sabb`, `validate_serialized_batch`, `clean_serial_nos` (memory), `validate_customer_provided_item`, `validate_internal_transfer`, `validate_putaway_capacity`, `validate_inventory_dimension_mandatory` |
| Accounts | `validate_date_with_fiscal_year`, `validate_party*`, `validate_currency`, `validate_enabled_taxes_and_charges`, `validate_tax_account_company`, `validate_qty_is_not_zero` |
| Auth | `Authorization Control.validate_approving_authority` |
| App hook (allowlisted) | `erpnext_extensions.iran_accounting.buying_selling.validate_delivery_note` (in-memory IRR align only) |

**Explicitly not called** from Category A (would write or weaken guarantees):

- Full `run_method("validate" / "before_submit" / "on_submit")` (Server Scripts + hooks)
- `set_status()` (can `add_comment`)
- `set_serial_and_batch_bundle` / `set_serial_and_batch_bundle_from_pick_list` (`db_set` / duplicate package)
- `make_packing_list` (only needed for packed stock when table already saved; packed rows validated if present)
- `Document._validate` wholesale (`_save_passwords`, image extract)
- `update_prevdoc_status`, `update_billing_status`, `update_stock_reservation_entries`
- `update_stock_ledger`, `make_gl_entries`, `repost_future_sle_and_gle`
- `make_bundle_*`, `update_pick_list_status`, `make_return_invoice`

### Category B — dedicated read-only implementations

| Submit-time mutation | Read-only substitute |
|---|---|
| SLE + Bin.actual_qty + negative stock | `validate_stock_availability` — Bin actual/reserved, SRE reserved-for-this-SO-detail, batch qty; respects `allow_negative_stock` |
| SO `delivered_qty` / over-delivery (`update_qty` + `validate_qty`) | `validate_over_delivery` — projects draft qty + allowance without writing |
| Serial/Batch Bundle create + Serial No warehouse update | `validate_serial_and_batch_requirements` — existence, warehouse, status, batch usability; flags auto-pick as blocking limitation |
| `make_gl_entries` | `validate_accounting_readiness` — expense account, cost center, stock account, dimension mandatory; does **not** compute GLE amounts |
| Stock reservation `delivered_qty` updates | Covered indirectly via availability (usable reserved qty) + `validate_against_stock_reservation_entries` warehouse match |

### Cannot be reproduced safely (reported limitations)

| Check | Severity | Why |
|---|---|---|
| Active DN Server Scripts | blocking | Arbitrary Python; may write / call HTTP / enqueue |
| Non-allowlisted `doc_events` | blocking | Unknown side effects |
| Auto-create outward SABB / serial pick | blocking | Requires creating bundles / picking algorithm |
| Pick List → SABB duplicate | blocking | Writes new SABB |
| Return + Issue Credit Note | blocking | Creates Sales Invoice |
| Full FIFO / MA valuation & future SLE | advisory | Requires SLE insert + repost engine |
| Repost Item Valuation | advisory | Creates / enqueues documents |
| Pick List status | advisory | Persists PL state |
| Exact GLE amount matrix | advisory | Needs SLE rates |

**False success is forbidden:** `ok` / `ready_for_submission` requires zero errors **and** zero **blocking** limitations.

## Strict read-only enforcement

1. **Write guard** (`write_guard.enforce_read_only`): rejects mutating SQL, `set_value`/`delete`/`bulk_insert`, commit/rollback/savepoint, enqueue, realtime publish.
2. **No savepoints** in the validation path.
3. **Fingerprint**: DN, items, linked SO headers/items, Bin, SRE, SLE/GLE/SABB counts — must be unchanged.
4. **Hard fail** on guard trip or fingerprint drift (`ReadOnlyViolation` / ValidationError).
5. Tests assert immutability and inject a deliberate write to prove the guard trips.

MariaDB `START TRANSACTION READ ONLY` is **not** used mid-request: Frappe already holds a site write transaction for the HTTP request; switching would require committing/ending that transaction. Application-level interception is the enforceable control inside that transaction.

## Testing

Suite: `test_delivery_note_check_submission_v5634`

- Write-without-Submit allowed; no-perm rejected; non-draft rejected
- Valid DN → `ok` with advisory limitations disclosed
- Insufficient stock → `NegativeStockError`
- Missing batch / invalid serial / missing warehouse / missing expense account
- Fingerprint immutability (DN/SLE/GLE/Bin/SABB)
- Write-guard unit tests + injected mutation hard-fail

## Production posture

Safe to use as a **read-only readiness aid** that:

- Detects common ERPNext submission blockers without side effects
- Refuses to claim “ready” when Server Scripts / unsafe hooks / auto-pick gaps apply
- Does **not** claim bit-identical parity with every `on_submit` edge (valuation/repost)

Existing ERPNext submit behaviour is unchanged.

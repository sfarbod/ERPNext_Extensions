# erpnext_extensions 5.7.0

## Historical Stock Rebuild Campaign — Reject-only manufacture cost allocation + RIV

### Accounting policy (approved)

When a Manufacture operation produces **only Reject** output (no MAIN_FG), the
eligible consumed material cost and header Additional Costs must be allocated
to Reject. Legitimate integer composition leftovers remain TYPE C Stock
Adjustment. Do **not** invent Work Order planned/actual operating cost that is
not already on the Stock Entry `additional_costs` table.

### Root cause

1. Core `is_costed_out_of_finished_item` values Scrap without `bom_secondary_item`
   from **target-warehouse MA** → often 0 on Reject WH.
2. Iran `allocate_scrap_absorbed_cost` previously required **exactly one MAIN_FG**
   and returned early for reject-only → CASE F never ran.
3. `permit_scrap_zero_valuation` allowed submit with zero Reject rate.
4. Full consume posted to Stock Adjustment (`621301`).
5. After SED repair, Core `get_produced_row_rate` retained historical SLE
   `incoming_rate=0` (no FG absorber), and `apply_integer_sabe_policy_to_sle`
   then clobbered Iran SED-synced SLE from still-zero SABE rows.

Job Card `finished_good` is the **stage SFG item** (not a Reject item). Reject
disposition is STE Scrap + Reject warehouse. No new Job Card Secondary Item
field is required for this fix.

### Fix — shared absorbed-cost allocator + RIV integration

`allocate_scrap_absorbed_cost` / `_apply_absorbed_product_rates` now support:

| Shape | Behavior |
|---|---|
| Main + Reject | Existing CASE F integer rate pair + header operating spread |
| Reject-only | Shared `ROUND_HALF_UP(pool / reject_qty)` + header operating spread |

Wired for live submit and historical RIV:

- `apply_iran_manufacture_output_contract` Priority **2b** (`_has_reject_only`)
- Unstamped historical: `repair_historical_zero_reject_only`
- Campaign `source_rebuild` / coverage / processor zero-reject detection
- Iran wrap of Core `get_produced_row_rate` → SED amount÷qty for reject-only
- SABE integer policy seeds from document-valued SLE when SABE is still zero
- Campaign forces fresh Item+WH RIV when SED valued / Reject SLE still ~0
- Campaign verify refuses SED/SLE lag, full-pool SA, and Reject GL debit mismatch
- `create_exact_vouchers(...)` builds a six-document Manifest without identity expansion

Preserves Bulk Scrap intentional zero, Component Scrap issued-rate, STAGE_CO,
By-product Core valuation, and v5.6.43 A2 net Stock Adjustment invariants.

### Known reject-only documents (Dev verified / Prod target)

Material pool = live consume SLE / SED outgoing `basic_amount` sum. Header
`total_additional_costs` and STE Additional Costs = **0** on all six (WO
planned/actual op cost is **not** on these vouchers and must not be
double-counted).

| Voucher | Material pool | Addl | Qty | Rate | Reject value | Residual SA |
|---|---:|---:|---:|---:|---:|---:|
| MAT-STE-2026-27570 | 721,096,838 | 0 | 1694 | 425,677 | 721,096,838 | 0 |
| MAT-STE-2026-26818 | 1,776,316,890 | 0 | 45 | 39,473,709 | 1,776,316,905 | −15 |
| MAT-STE-2026-30790 | 113,357,932 | 0 | 317 | 357,596 | 113,357,932 | 0 |
| MAT-STE-2026-31867 | 71,519,200 | 0 | 200 | 357,596 | 71,519,200 | 0 |
| MAT-STE-2026-33323 | **302,526,216** | 0 | 49 | **6,174,004** | **302,526,196** | **+20** |
| MAT-STE-2026-34221 | 2,235,304,925 | 0 | 133 | 16,806,804 | 2,235,304,932 | −7 |

`MAT-STE-2026-33323` uses the **live** consume pool `846 × 357,596 = 302,526,216`
(`ROUND_HALF_UP(302526216 / 49) = 6,174,004`). An earlier draft used pool
`302,524,208` / rate `6,173,963` / residual `+21` — that stale pool must not be
used for acceptance.

Residual = pool − Σ(rate × qty) after IRR `ROUND_HALF_UP`.

### Campaign

- Dev campaign `HRC-00009` is scoped to exactly these six vouchers
  (`SCOPED_HRC_TEST:v5.7.0-reject-only-six`).
- After SED+RIV correction, resume marks items **Skipped Covered** when
  evidence-based coverage passes (source not required; no SED/SLE lag; Bin and
  Reject GL debit reconcile). Completed covering RIV alone is never enough.
- Production: resume `HRC-00009` when present; otherwise
  `create_exact_vouchers` then `start` / `resume`. Do **not** use
  `create_scoped` for these six — it expands identity-related vouchers.

### Optional campaign start date

- `create(..., start_date=None)` / `create_scoped(..., start_date=None)`
- Alias of `posting_boundary_from`

### Tests

- `test_reject_only_zero_mfg_v570.py` — reject-only allocate, additional cost,
  multi-reject, zero pool, Main+Reject parity, bulk/component, live contract
  Priority 2b, idempotency, six-document expected rates, start_date
- `test_reject_only_riv_sle_v571.py` — SED→SLE rate selection, SABE non-clobber,
  SED/SLE lag force-RIV signals, idempotent re-selection
- `test_case_f_27800_product_reject_v569.py` — CASE F regression

### Backward compatibility

- Callers without `start_date` unchanged
- Main+Reject CASE F path preserved
- Intentional `allow_zero_valuation_rate` reject rows are not repaired
- TYPE C residual SED.amount vs SLE on already-valued reject-only is not treated
  as a source-rebuild gap on campaign resume

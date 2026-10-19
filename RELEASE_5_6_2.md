# RELEASE 5.6.2 — Component Scrap Issued Valuation Bridge

**App:** `erpnext_extensions`  
**Version:** `5.6.2`  
**Branch:** `feature/5.6.2-component-scrap-valuation`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 (unchanged from 5.6.0)

---

## Summary

Preserve Iran Accounting **issued-component** valuation for `COMPONENT_SCRAP`
across Target Warehouse changes and repeated Draft Save/Submit. Core must no
longer abort (or temporarily adopt destination-warehouse economics) before the
Iran manufacture output contract can reprice scrap.

This release does **not** use Manual valuation stamps.

---

## Root Cause

On Manufacture Stock Entries, ERPNext Core values bomless secondary Scrap via
`valuation_type = "Valuation Rate"` → `get_row_valuation_rate(t_warehouse)` on
**every** `set_basic_rate()` call.

Iran Accounting already prices `COMPONENT_SCRAP` from the same-voucher issued
consumption rate (`apply_component_scrap_issued_rates`), but that runs in the
**post-Core** `validate` hook.

| Path | Behavior before 5.6.2 |
|------|------------------------|
| First create (rate empty) | `permit_scrap_zero_valuation` set `allow_zero` → Core passed → Iran applied issued rate |
| Re-save / change Target WH (rate already set) | `permit_scrap_zero_valuation` **skipped** → Core called `get_valuation_rate(t_warehouse)` → throw if no SLE / Item rate → Iran never ran |

Affected example: Draft `MAT-STE-2026-41833` rows 12–13 (`13100060`, `13100061`).

---

## Business Impact

- Users could not change Target Warehouse on Component Scrap rows without a
  Valuation Rate Missing error.
- Even re-saving an already-priced Draft could fail when the receiving warehouse
  had no SLE history for the scrap item.
- Incorrect destination-warehouse rates must never become Component Scrap cost.

---

## Technical Changes

### Temporary Zero-Valuation Bridge (non-Manual)

Same architecture as `permit_stage_equivalent_zero_valuation`:

| Function | Role |
|----------|------|
| `permit_component_scrap_zero_valuation` | Before Core: set in-memory `COMPONENT_SCRAP_BRIDGE_FLAG` + `allow_zero_valuation_rate=1` for classified `COMPONENT_SCRAP` rows **even when rates are already non-zero**. Does **not** change `valuation_type` or `set_basic_rate_manually`. |
| `apply_component_scrap_issued_rates` | After Core: recalculate from `_issued_rate_for_component` (source consumption). |
| `assert_bridged_component_scrap_priced` | Fail-closed: bridged rows must match issued rate; temporary allow_zero cleared; no silent destination valuation. |

### Lifecycle wiring

1. `before_validate_stock_entry` — calls `permit_component_scrap_zero_valuation`
2. `_patch_stock_entry_pre_core_bridge` — same permit after Server Scripts, before Core `get_valuation_rate`
3. `apply_iran_manufacture_output_contract` — asserts bridged component scrap on all exit paths

### Files

- `erpnext_extensions/iran_accounting/scrap_costing.py`
- `erpnext_extensions/iran_accounting/stock_entry.py`
- `erpnext_extensions/iran_accounting/integration/monkey_patches.py`
- `erpnext_extensions/iran_accounting/tests/test_component_scrap_issued_rate.py`
- `erpnext_extensions/__init__.py` → `5.6.2`

---

## Valuation Contract (Non-Manual)

| Rule | Enforcement |
|------|-------------|
| Source of Component Scrap cost | Same-voucher issued / consumption rate |
| Target Warehouse | May change inventory **account**; must **not** change economic rate |
| `valuation_type` | Preserved (typically `Valuation Rate`) |
| `set_basic_rate_manually` | Preserved (`0`) |
| Temporary `allow_zero_valuation_rate` | Pre-Core only; cleared after issued pricing / assert |
| Global Allow Zero | **Not** enabled |
| ERPNext / Frappe core | **Unchanged** |

---

## Accounting Safeguards

- Fail-closed assert if bridge armed but issued pricing missing or mismatched.
- Destination SLE rates (CASE B) are overwritten by issued rate, not accepted.
- FG / RM / Product Reject / Bulk Scrap / stage Co-Product paths unchanged by
  this bridge (classification-gated).
- No historical SLE/GL mutation; no automatic RIV or rebuild campaign.

---

## Regression Tests

Module: `erpnext_extensions.iran_accounting.tests.test_component_scrap_issued_rate`

- Existing issued-rate / source-side suite retained and green.
- New class `TestComponentScrapPreCoreBridgeV562` covers:
  - bridge with non-zero rates
  - Target WH without SLE
  - poison destination SLE ignored
  - source rate recalculation
  - Manual fields unchanged
  - allow_zero cleanup
  - fail-closed assert
  - Product Reject / Bulk Scrap exclusion
  - late Server Script row (pre-Core wrap path)
  - idempotent repeated validation

**Result (development.localhost):** 39 tests OK.

---

## Live Document Verification (non-persistent)

`MAT-STE-2026-41833` (Draft; not saved/submitted during verification):

| Scenario | Result |
|----------|--------|
| Current Quarantine WH + full before_validate → Core → Iran | Issued 1,266,402 / 148,500 restored; allow_zero cleared |
| Target → `انبار اقلام فنی کارخانه اسپاد` (no SLE) | Same issued rates; no Core throw |
| Target → packaging Quarantine WH (SLE ≠ issued) | Final rate remains issued, not SLE |
| Repeated validation ×3 | Idempotent |
| DB after probes | Unchanged |

---

## RIV Compatibility

- Mid-RIV path in `apply_component_scrap_issued_rates` unchanged.
- Bridge flags are **in-memory only** (`_iran_component_scrap_bridge`).
- Assert allows mid-RIV persistence fallback when issued lookup is temporarily empty.
- No rebuild/repost jobs started by this release.

---

## Deployment Considerations

1. Deploy `erpnext_extensions` 5.6.2 (no migrate required for this change).
2. Restart workers / web so monkey patches reload.
3. Draft Manufacture entries with Component Scrap can change Target Warehouse and Save.
4. Do **not** mass-repost; no historical rebuild is part of this release.

---

## Known Limitations

- Bridge applies only to rows classified as `COMPONENT_SCRAP` (Iran classifier).
- Ambiguous scrap that cannot be classified still fails closed (existing policy).
- Intentional zero Bulk Scrap remains a separate contract (`BULK_SCRAP`).
- Changing Target Warehouse may still change the **inventory account** mapping;
  that is intentional ERPNext warehouse accounting, not scrap costing.

---

## Upgrade Notes

From 5.6.1 → 5.6.2: code-only; no DocType schema changes for this fix.

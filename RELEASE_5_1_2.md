# Release 5.1.2 — ERPNext 16.34.1 / Frappe 16.33.0 compatibility

## Summary

- **Compatibility:** Explicit allow-lists extended for **ERPNext 16.34.x** and **Frappe 16.33.x**.
- **RIV fingerprint:** `recalculate_amounts_in_stock_entry` body changed in ERPNext 16.34 (persist redistributed `additional_costs` on all incoming rows during repost). New normalized source hash is primary; 16.29–16.33 hash kept as alternate. Signature and required tokens unchanged.
- **UVR fingerprints:** Unchanged (Class A). Version allow-list only.
- **Accounting policy:** No change. Distributed-discount preserve (5.0.1), scrap/`secondary_item_type`, canonical rounding ownership (5.0.0), and residual classifiers unchanged.
- **Historical repair:** Out of scope.

## Upstream deltas revalidated (16.33.0 → 16.34.1)

| Area | Impact |
|------|--------|
| `stock_ledger.recalculate_amounts_in_stock_entry` | Class B — allow-list + alternate hashes; IRR persist-after-recalculate still authoritative |
| BOM / Stock Entry / SCR secondary `valuation_type` | Scrap detection still via `secondary_item_type` + legacy fallback; gate Manufacture + Additional Cost / RIV×2 PASS |
| UVR / regional hook | Unchanged fingerprints |
| PI distributed discount / taxes | No invalidating change; 5.0.1 contract still holds |
| LCV `make_gl_entries_on_cancel(from_repost=True)` | Compatible with UVR/LCV integerization path |

## Compatibility stack

- ERPNext **16.34.1**
- Frappe **16.33.0**
- erpnext_extensions **5.1.2**

## Files

- `iran_accounting/domain/riv_rate_guard.py`
- `iran_accounting/domain/uvr_regional_guard.py`
- `iran_accounting/tests/test_riv_rate_first_preserve.py`
- `iran_accounting/tests/test_uvr_regional_guard.py`
- `iran_accounting/tests/test_purchase_invoice_distributed_discount_gl.py`
- `iran_accounting/e2e_round_off_ui.py` (E2E fixture hygiene: enabled warehouse / site department)
- `RELEASE_5_1_2.md`
- `__init__.py` version bump

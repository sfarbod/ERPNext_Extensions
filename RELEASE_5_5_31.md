# erpnext_extensions 5.5.31

## Manufacture dynamic valuation unlock, JC rebuild sync, and RIV campaign filter

This release adds an explicit, bench-only Manufacture manual-rate unlock path,
extends Job Card Rebuild secondary-type correction with approval-gated
Scrap → By-Product / Co-Product synchronization to Manufacture Stock Entry
metadata, and adds an optional Manufacture purpose filter to the existing
native RIV campaign helper.

It does **not** automatically unlock or revalue Production history.
Historical unlock, contract adoption, Job Card type approval, and RIV
campaign execution remain explicit operator actions.

### Added

- Manufacture manual-rate unlock utility
  (`unlock_manufacture_manual_rates`) for submitted Manufacture output rows.
- Optional historical Manufacture contract adoption helper
  (`adopt_manufacture_contract_version`) for eligible documents.
- Bulk Scrap output-class stamp helper that preserves Manual valuation.
- Optional `stock_entry_purpose` filter on the existing RIV campaign helper
  (`generate_full_riv_campaign`). When omitted, behavior is unchanged.

### Improved

- Job Card Rebuild secondary item classification.
- Approval-gated Scrap → By-Product / Co-Product transitions.
- Synchronization of Job Card and Manufacture Stock Entry secondary output
  metadata (`secondary_item_type`, `custom_output_class`, equivalence factor
  when unset).
- Historical Manufacture contract adoption for eligible Co-Product /
  By-Product unlock paths (existing Iran stage-costing contract only).

### Preserved

- Existing Iran Accounting valuation formulas (no new pricing engine).
- BULK_SCRAP manual valuation policy.
- Native ERPNext RIV processing through the existing Iran wrapper.
- Historical stock quantities, warehouses, batches, and movements
  (metadata unlock / classification sync only; no cancel-recreate).

### Validation

Isolated clone validation results (`jc-manufacture-unlock.localhost`):

- 2,799 Manufacture documents **VERIFIED**.
- 581/581 relevant output Item/Warehouse pairs covered by Completed RIV.
- 0 pending RIV.
- 0 failed RIV.
- 0 blocked Manufacture documents.
- 0 non-BULK Manual Manufacture outputs.
- 2 legitimate BULK_SCRAP Manual outputs.
- Job Card / Stock Entry synchronization tested
  (PO-JOB07031 / MAT-STE-2026-24883-1 / item 30500006).
- No unexplained accounting drift in validated target documents.

These are isolated clone validation results, **not** Production execution
results. Production historical data is not corrected by installing this
version alone.

### Deployment Notes

- **Upgrade:** install / update `erpnext_extensions` to **5.5.31**.
- **`bench migrate`:** not required for this release (no new DocTypes /
  schema patches introduced for these features).
- **Patches:** none added for Manufacture unlock / JC sync / RIV filter.
- **Historical unlock:** **not automatic**. Operators must explicitly run
  the bench-only unlock / adopt helpers on an intended site.
- **Job Card type correction:** **not automatic**. Requires explicit
  per-row approval through Job Card Rebuild.
- **RIV campaign:** **not automatic**. Generation and execution remain
  manual bench operations; use the existing Iran-wrapped native RIV path.
- Do not claim Production history has already been unlocked or revalued
  after upgrade.

### Testing

- `test_unlock_manufacture_manual_rates` — unlock planner, Bulk Scrap
  preserve, Co-Product contract gate, idempotency.
- `test_secondary_type_se_sync_v5531` — Scrap→By suggestion, SE sync,
  approval gating.
- `test_riv_campaign_v5529` — purpose filter backward compatibility and
  campaign guards.

### Production

Not pushed. Not tagged. Not deployed by this release preparation step.
User publishes and upgrades Production separately.

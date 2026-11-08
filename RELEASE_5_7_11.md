# erpnext_extensions 5.7.11

## Historical Rebuild REST preflight checksum false positive

### Incident

Production `HRC-00011` (exact six Manufacture seeds for SA `621301`) was created
successfully via v5.7.10 REST (`create_exact_campaign`) with checksum
`dbb7d5907b89e8e787a40f6b324d3b08f5cdf96c1b6c78a13499f3d5be61a4a8`.

`preflight_campaign` then returned **BLOCKED** / `manifest_checksum_mismatch`
even though the stored checksum matched `discover_dependencies` and the
campaign item voucher set was correct.

### Root cause

1. Create/discover compute `manifest_checksum` from live voucher
   `creation` values (microsecond precision, e.g. `…15:56:44.821508`).
2. Campaign Item persists `creation_ts` with truncated precision
   (`…15:56:44`).
3. Preflight v5.7.10 recomputed the checksum from Campaign Item fields,
   producing a different digest (`fdc2c4c9…`) — a false integrity failure.

Scope, allowlist (64 identities), and voucher list were correct. No campaign
start was performed.

### Fix

`preflight_campaign` recomputes the manifest checksum from **live voucher
metadata** (same inputs as create/discover), not from denormalized
`creation_ts`. Still verifies voucher existence and other safety gates.

### Operator note for HRC-00011

After deploying 5.7.11, re-run `preflight_campaign` on existing `HRC-00011`.
Do **not** create a duplicate campaign if preflight now PASSes — start the
existing Ready campaign with `start_sequential_campaign`.

### Tests

Extend `test_historical_rebuild_rest_api_v5710` / add v5711 coverage for
preflight checksum using microsecond creation vs truncated item timestamp.

### Version

`erpnext_extensions` → **5.7.11**

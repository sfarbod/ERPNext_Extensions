# erpnext_extensions 5.8.3

## Iran Subcontracting Receipt clearing GL + service-cost correction

### Summary

Permanent Development fix for Iran-company Subcontracting Receipt (SCR)
accounting when finished-good clearing (`111421`) and supplied RM expense
diverge (e.g. Item Group default `611233`). Restores zero-net clearing,
normalized Transaction RIV GL maps, unit-rate Desk display precision, and a
supported remediation API for existing submitted SCRs.

Reference scenario (fixture values; not hardcoded into business logic):
`MAT-SCR-2026-00015` — FG qty 2,015; service 145,122,080; SLE SVD 3,615,407,705;
SA residual 235; final GL without `111421` / `611233` rows.

### 1. SCR clearing-account enforcement

- `enforce_scr_clearing_expense_accounts` aligns supplied RM `expense_account`
  to the FG clearing account for Iran non-return SCRs.
- Hooked on `Subcontracting Receipt` `validate` and `before_submit`
  (`hooks.py`).
- Complements the Desk Server Script (Before Submit only), which does not run
  on RIV and can be bypassed by Item Group defaults.

### 2. Normalized GL during Transaction RIV

- `SubcontractingReceipt.get_gl_entries` is wrapped so RIV expected-map
  comparison uses the Iran-normalized map (`monkey_patches.py`).
- Prevents skip/repost of the raw Core map that reintroduces orphan clearing.

### 3. Balanced clearing-row removal

- After aligning debit/credit to document material amounts and rewriting SA
  for integer IRR Δ, `_drop_balanced_clearing_accounts` removes no-op
  `111421` legs (Manufacture/Repack convention).

### 4. Orphan clearing protection

- Mismatched RM expense GL debits are remapped onto FG clearing when amounts
  match, then dropped.
- If clearing still nets ≠ 0 after normalization, posting throws
  `SCR Clearing Account Imbalance`.
- Already net-zero clearing after Core `process_gl_map` (including negative
  Landed Cost Voucher legs folded into clearing) is left intact and dropped;
  it is not rewritten to pre-LCV `amount − service` (see
  `test_G_lcv_folded_clearing_already_balanced_is_dropped`).

### 5. SCR unit-rate display precision

- Idempotent patch `patches/v5_8/set_scr_unit_rate_display_precision`:
  Currency precision **9** on `service_cost_per_qty`, `rate`,
  `additional_cost_per_qty`, `secondary_items_cost_per_qty` so IRR `#,###`
  formatting does not hide allocation decimals on Desk.

### 6. Service-cost correction API (v5.8.0 package, shipped in 5.8.3)

- Whitelisted dry-run / apply API for PO-based `service_cost_per_qty`
  (never `last_purchase_rate`).
- Fingerprint, confirm token, allocation caps, idempotency.

### 7. Existing SCR remediation API

- `subcontracting_receipt_clearing.api`:
  - `inspect_scr_clearing_expense_alignment`
  - `discover_scr_clearing_expense_mismatches`
  - `align_scr_supplied_clearing_expense` (confirm token)
- Document mapping only; GL/SLE via supported Transaction / accounting RIV.

### 8. ETO SCR valuation propagation (expanded 5.8.3 scope)

Reusable orchestration (`eto_scr_valuation_propagation`):

- Inspect chronological SCR → Transfer → Manufacture valuation chains.
- Optional PO-based service-cost correction (existing API).
- Supported Item/Warehouse or Transaction stock RIV (no SLE/GL/Bin edits).
- Concurrent RIV conflict guard; idempotent dry-run/apply; confirm token.
- Deleted/cancelled LCV is never treated as an active dependency and is never
  recreated.

Empty-warehouse batch align (extends 5.8.1):

- During ETO-scoped repost, consume-to-zero outward legs for ETO finished goods
  stamp batch SABE onto SLE/SED instead of absorbing understated warehouse MA
  residual (`should_align_empty_warehouse_sle_from_batch_sabe`).
- Fixes the MAT-SCR-2026-00011 pattern (SABE correct, Transfer SLE/SED stale).

### 9. Regression coverage

Focused modules run for this release commit:

| Module | Result |
|---|---|
| `test_scr_clearing_balance_v5757.py` | 20 OK |
| `test_scr_service_cost_correction_v580.py` | 11 OK, 1 skipped |
| `test_eto_scr_valuation_propagation_v583.py` | 11 OK |

**Total: 42 OK, 1 skipped, 0 failed.** The live dry-run skip is
`test_dry_run_mat_scr_2026_00015` (environment guard; not treated as pass).

Coverage intent:

- Clearing: matching/mismatch, target fixture, idempotency, non-Iran skip,
  LCV-folded clearing.
- Service-cost: allocation math / API guards (never `last_purchase_rate`).
- ETO propagation: patterns A/B/C orchestration, confirm token, RIV conflict
  and active-LCV guards.

### Safeguards and limitations

- No direct SLE / GL / Bin / Batch valuation edits; supported RIV only.
- Active LCV blocks auto ETO propagation; deleted LCV is not recreated.
- Empty-warehouse batch align is ETO-scoped only.
- Concurrent open RIVs block apply until cleared.
- Production deploy is out of scope for this release preparation.

### Expected GL (reference fixture)

| Account | Debit | Credit |
|---|---:|---:|
| 111605 | 3,615,407,705 | 0 |
| 111635 | 0 | 3,470,285,390 |
| 611218 | 0 | 145,122,080 |
| 621301 | 0 | 235 |
| 111421 / 611233 | no rows | no rows |

Totals: 3,615,407,705 = 3,615,407,705.

### Migration

- Patches in `patches.txt` are idempotent Property Setter / reporting only.
- No Production deploy in this task.

### Rollback

1. Revert to previous app tag / commit.
2. Restart workers / clear cache.
3. Re-run accounting-only Transaction RIV on remediated vouchers if GL must
   be restored from pre-5.8.3 Core maps (Dev only).

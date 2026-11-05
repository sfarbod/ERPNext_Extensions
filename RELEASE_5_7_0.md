# erpnext_extensions 5.7.0

## Historical Stock Rebuild Campaign — Manufacture zero-rate + start date

### Manufacture reject-only zero valuation (critical)

Some historical Manufacture Stock Entries have **no MAIN_FG** row because the
linked Job Card sets `finished_good` to the scrap/reject item itself (stage
scrap completion). Classification yields `MAIN_PRODUCT_REJECT` at rate 0 while
consume rows remain valued. Core then posts the **entire consume pool** to
Stock Adjustment (`621301`).

Known examples (Production backup / Dev):

| Voucher | Consume → SA (before repair) | Reject item |
|---|---:|---|
| `MAT-STE-2026-27570` | 721,096,838 | 30100015 |
| `MAT-STE-2026-26818` | 1,776,316,890 | 30100309 |
| `MAT-STE-2026-30790` | 113,357,932 | 30100023 |
| `MAT-STE-2026-31867` | 71,519,200 | 30100023 |
| `MAT-STE-2026-33323` | (reject-only zero) | 30100277 |
| `MAT-STE-2026-34221` | (reject-only zero) | 30200022 |

**Fix:** `historical_zero_reject_only_needs_repair` /
`allocate_reject_only_absorbed_cost` / `repair_historical_zero_reject_only`
allocate the material pool onto MAIN_PRODUCT_REJECT with IRR
`ROUND_HALF_UP` rates (`amount = qty × rate`). Pool leftover remains native
TYPE C Stock Adjustment — **no** artificial Round Off / WIP A2 counter-entry.
Wired into:

- `apply_iran_manufacture_output_contract` (unstamped historical path)
- Historical Rebuild `source_rebuild`
- Coverage / processor reject-rate issue detection

Preserves v5.6.39–5.6.44 SA aggregation / A2 net / one-sided GL behaviors.

### Optional campaign start date

- `create(..., start_date=None)` and `create_scoped(..., start_date=None)`
- Alias of existing `posting_boundary_from` (backward compatible when omitted)
- `status()` reports `start_date` / `posting_boundary_from`
- Start date scopes **which vouchers are rebuilt**; engines still read earlier
  SLE history for opening valuation

### Discovery / reassessment APIs

```text
campaign.discover_manufacture_zero_rate(company=, start_date=, limit=)
campaign.reassess_pending(campaign=, limit=, include_skipped_covered_zero_reject=True)
```

### HRC-00008 — 83 pending + previously open cases

Reassessment on restored Production DB (`development.localhost`):

| Bucket | Count | Finding |
|---|---:|---|
| Manifest Pending | 83 | **NOT APPLICABLE** — all `docstatus=2` (cancelled); no live SLE/GL |
| Skipped Covered + illegal zero reject-only | 6 | **REQUIRES REBUILD** (includes the four named Manufacture examples) |
| Open-unknown after ledger diagnosis | 0 | — |

The previously cited “8 open / unknown” set is explained as: campaign Pending
rows that never recorded an error (coverage could not verify cancelled
vouchers), plus Skipped Covered Manufacture reject-only zeros that coverage
treated as verified (`sle=N gl=2`) without inspecting reject SED rates. This
release detects **6** confirmed reject-only rebuild targets; no additional
unknown open Manufacture cases remain in the reassessment.

### Tests

- `test_reject_only_zero_mfg_v570.py` — reject-only detection, allocation,
  intentional-zero skip, contract routing, ownership claim, start_date alias
- Existing CASE F `test_case_f_27800_product_reject_v569.py` — still green

### Backward compatibility

- Callers without `start_date` unchanged
- FG+reject CASE F path unchanged
- Intentional `allow_zero_valuation_rate` reject rows are not repaired

### Known limitations

- Repair updates Stock Entry Detail economics; full SLE/GL rebuild still
  requires Item+Warehouse RIV / Sequential campaign execution after install
- Cancelled Pending manifest rows are classified NOT APPLICABLE; operators may
  mark them Skipped Covered / exclude them in a follow-up campaign pass
- Does not auto-execute Production campaigns

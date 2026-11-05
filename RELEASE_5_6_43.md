# erpnext_extensions 5.6.43

## Critical Fix — Net Stock Adjustment Only (No Artificial A2 Counter-Entry)

### Accounting rationale

A2 (`localization_difference`) is an **intermediate** Iran integerization
reconciliation component. When Core Manufacture/Repack already posts
`Company.stock_adjustment_account` for the authoritative plug, that Core SA
**is** the final Stock Adjustment amount.

Do **not**:

- Retain gross Core + A2 on 621301 (e.g. Cr 2,431)
- Create Debit 1,970 in Round Off (`622515`) or WIP / FG / inventory
- Invent adjustment turnover merely to preserve gross A2 components

### Formula (credit−debit on Stock Adjustment)

```
gross_sa_before_recon = core_sa_net + proposed_a2
final_sa_net =
    core_sa_net                         if Core SA present (or Manufacture VD exact match)
    proposed_a2                         otherwise
a2_recon_effect = -proposed_a2          when Core SA / VD covers
outstanding_to_post = final_sa_net - core_sa_net
```

For `MAT-STE-2026-43536`:

| Component | Amount |
|---|---:|
| Core Stock Adjustment | Cr 461 |
| Iran A2 | +1,970 |
| `final_sa_net` | Cr 461 |
| `a2_recon_effect` | −1,970 |
| Posted A2 / Round Off | **none** |

### Expected GL (`MAT-STE-2026-43536`)

| Account | Debit | Credit |
|---|---:|---:|
| 111701 WIP | 0 | 5,451,221,439 |
| 621301 Stock Adjustment | 0 | 461 |
| 111625 FG Inventory | 5,404,134,596 | 0 |
| 111608 Scrap Inventory | 15,781,209 | 0 |
| 111620 Packaging Inventory | 31,306,095 | 0 |

### Code

- `reconcile_final_stock_adjustment_net` / `core_stock_adjustment_net_in_map`
- `apply_sabe_localization_sa_gl` posts at most one SA-only leg when the open
  map debit−credit gap already equals `outstanding_to_post`; never a partner
- Tests: `test_sabe_a2_net_stock_adjustment_v576.py`

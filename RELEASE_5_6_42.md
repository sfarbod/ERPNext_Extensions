# erpnext_extensions 5.6.42

## Critical Fix — A2 Counter Uses Round Off, Not WIP

### Accounting rationale

After integerized SLE, Core Manufacture GL is already balanced (including Core
Stock Adjustment). The SABE A2 residual is a **P&L localization adjustment**,
not a revaluation of WIP/FG/inventory.

Posting `Dr WIP / Cr Stock Adjustment` incorrectly mutated Core Manufacture WIP.

Approved balanced treatment (Company-configured adjustment accounts only):

| Leg | Account | Role |
|---|---|---|
| A2 SA | `Company.stock_adjustment_account` | localization residual |
| A2 counter | `Company.round_off_account` | balancing adjustment account |

Ordinary Core stock accounts (WIP, FG, scrap, inventory) are never selected as
the A2 partner.

### Example (`MAT-STE-2026-43536`)

- WIP 111701: Core Credit 5,451,221,439 only (no A2 debit)
- 621301: Core 461 + A2 1,970 → aggregated Credit 2,431
- Round Off: A2 Debit 1,970 (configured counter)
- Exact Debit = Credit; SLE unchanged

### Code

- `sabe_integer_valuation._resolve_sabe_a2_partner_account` → Round Off only

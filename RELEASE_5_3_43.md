# Release 5.3.43

## BULK_SCRAP intentional-zero contract

Business-authorized **Bulk Scrap** (`ضایعات بالک`) is a distinct Manufacture
output class from Component Scrap:

| Class | Valuation |
| --- | --- |
| `COMPONENT_SCRAP` | Same-voucher issued/consumption rate (unchanged) |
| `BULK_SCRAP` | Intentional rate **0** — legitimate zero |

Generic authority (no voucher/item hard-code):

- `custom_output_class = BULK_SCRAP`, or
- `valuation_type = Manual` + zero rate + `allow_zero_valuation_rate` /
  `set_basic_rate_manually`

Effects:

- Does not enter FG/stage cost-sharing pool
- Does not receive Product Reject absorbed rate or Component Scrap issued rate
- Historical Repair / WR / Zero Rate treat it as `LEGITIMATE_ZERO` (not
  WRONG_RATE / ZERO_RATE corruption / MANUAL_BUSINESS)

Proven example (not the generic rule): `MAT-STE-2026-37603` / `30100101`.

## Six terminal I4 / known RIV poison identities

Root-cause: mid-chain (or tip) `qty_after=0` with non-zero `stock_value`, with
`READY_I4` previous-healthy / checkpoint replay that clears the tip in
simulation.

Generic bridge: `poisoned_opening` classifies such identities as
`I4_REPAIRABLE` (not permanent `TECHNICAL_TOOL_GAP`) when I4 proves READY.
Full-repost preflight excludes `I4_REPAIRABLE` from the permanent poison-root
blocker count; I4 apply remains required before L6.

Frozen plan `SEPT30_DETERMINISTIC_REPAIR_PLAN_V1` includes these six I4 ops
plus `MAT-STE-2026-37603` Iran-native adoption.

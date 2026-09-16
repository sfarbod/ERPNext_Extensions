# ERPNext Extensions v5.2.13

## Historical Repair — production I4 leftover repair + real scan filters

### Objective

Make Historical Repair a practical production maintenance tool for historical
data correction **without** changing accounting policy, Moving Average,
FIFO, I4 live prevention, selective GL philosophy, or global-replay restrictions.

Reuse the existing `replay_series` / Historical Repair architecture. No new
valuation engine.

### What changed

#### 1. Real scan filters (highest priority)

- UI sends full scope on every Scan / Dry Run: Company, Voucher, Item,
  Warehouse, Batch, Serial & Batch Bundle, Work Order, Posting Date From/To,
  Repair Class, Planner Status, Patient Zero.
- Server SQL applies these filters (`scan_filters.py`); scans no longer ignore
  filters except Company.
- Voucher quick search + Clear Filters + saved filter presets.

#### 2. I4 leftover repair path

- Repair class: `I4_LEFTOVER_REPAIR`
- Planner: `READY_I4`, `WAITING_I4`, `I4_REPLAY_REQUIRED`, `I4_REPAIRED`
- Dedicated **Repair I4 Patient Zero** (not “Repair Wrong Rate”)
- Identity-scoped only: Dry Run → Impact → Savepoint → repair PZ →
  `replay_series` → Bin → selective GL → Integrity → re-scan
- Never global / company replay; stops before hard non-I4 poisons

#### 3. Patient Zero navigation & Root Cause Explorer

- Downstream voucher selection explains that repair must start at Patient Zero
- Go To Root Cause stays on SLE for I4 (no longer jumps to Zero/Lost Rate)
- Root Cause Explorer: Healthy → First Patient Zero → Replay chain → Current

#### 4. Operator UX

- Repair wizard steps (Backup → Dry Run → Impact → Repair → Replay → Integrity → Done)
- Identity Health panel + Overall Health %
- Progress / ETA labels for long steps
- Master Repair Plan roadmap (month / warehouse / GL / Failed RIV estimates)
- Dashboard KPI: **I4 Leftover**
- Dependency messaging prefers “Repair I4 Patient Zero …” for leftover roots

### Preserved (unchanged)

- Accounting policy / Moving Average / valuation / replay algorithm
- Posting-order rules / I4 live guard (`assert_zero_qty_stock_value`)
- No global RIV / no global replay / selective GL (G1–G4) / Failed RIV policy

### Migration notes

- No DocType schema migration required for core I4 path.
- Soft feature maturity entries added for `repair_i4_patient_zero`,
  `root_cause_explorer`, `identity_health`, `master_repair_plan`,
  `find_patient_zero`.
- Operators should take a **database backup** before any non-dry-run I4 apply.
- After I4 repair of a Patient Zero, re-scan the identity before repairing
  downstream Wrong Rate / GL / Failed RIV rows.

### Tests

- Unit: `test_i4_leftover_repair_v5213.py`
- Playwright: `historical-repair-i4.spec.ts`

### Operator start (example)

Historical Repair → **SLE / Bin Integrity** → set Voucher `MAT-STE-2026-25791`
(or Repair Class `I4_LEFTOVER_REPAIR`) → Scan → Dry Run → Impact →
**Repair I4 Patient Zero** (backup confirmed).

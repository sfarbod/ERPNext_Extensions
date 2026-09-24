# Release 5.3.6 — Unified RepairPlan pipeline + Wrong Rate residual contract

Package version: **5.3.6**.

Development only. Production untouched.

## What changed

### One mutation pipeline

`repair_pipeline.py` is the public automatic repair path:

```
scan → find_root → plan → repair → repost → verify
```

`repair_safe_api` routes through `repair_safe_roots()` only.

Family/reason are strategies, not separate user workflows:

- VALUATION / LEFTOVER_MA
- VALUATION / WRONG_RATE
- POSTING_ORDER
- DERIVED_STATE / BIN
- ACCOUNTING / GL

### Wrong Rate residual contract (critical fix)

The old cleared check treated any nonzero after-rate as success when the
current rate was zero. That accepted warehouse MA drift (example: expected
223,282 became 1,593,971).

New contract:

`abs(after_rate - expected) <= 1`

SE-surface repairs sync SLE from the Stock Entry detail and defer descendant
rebuild to official ERPNext RIV. Custom warehouse replay is no longer treated
as success unless the residual still matches the planned expected rate.

### Root compression + manufacturing boundary

`compress_roots()` groups findings by causal root.

Manufacture / MTFM / Paykar / scrap / Reject Wrong Rates are forced to
MANUAL / MANUFACTURE_FLOW — not the generic receipt Wrong Rate path.

### Job Card reason groups

Read-only grouping for BROKEN/MANUAL Job Cards (no quantity writes).

### Central safety fingerprint

Shared before/after gates: I1, negative stock, Bin value mismatch sample,
manufacturing quantity fingerprint.

## Known limit (this cut)

The first non-manufacturing Wrong Rate READY root
(`MAT-STE-2026-33496` / `13100023`) can repair the voucher residual and GL
balance for that document, but a subsequent full Scan All showed Patient Zero
and Wrong Rate READY inflation on Development.

Per policy the clean backup was restored again. Waves are blocked until that
root's official RIV + rescans prove no KPI regression.

LEFTOVER_MA `16100066` regression remains the frozen canary and still holds.

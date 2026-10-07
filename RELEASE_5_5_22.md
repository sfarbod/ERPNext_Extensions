# erpnext_extensions 5.5.22

## Job Card Stock Rebuild — Canonical Manufacture Consumption Correction

### Summary

1. **Capability** — Operators can REMOVE / REDUCE over-consumed Manufacture
   source Batches and ADD / INCREASE missing consumption on the repaired
   canonical Manufacture via explicit, fingerprinted approvals. Net Item qty
   need not be zero-paired; Golden Rule / stock / valuation validators still
   apply. Distinct from no-repair Batch Offset.

2. **Canary** — PO-JOB08631 / Item 13200475: Batch 502 consume 2961 → 0;
   Batch 503 consume 0 → 2961; total remains 2961 (not 5922). Dry Run PASS;
   Apply PASS (`MAT-STE-2026-40852`).

3. **R1** — Historical Manufacture `MAT-STE-2026-29972-1` carries a proven
   898 IRR Stock Adjustment residual (R3). Value-neutral batch correction
   preserves that residual through a narrow, fingerprinted historical-repair
   bridge. R1 is not globally weakened; unexplained gaps still block.

4. **Contract** — `MANUFACTURE_COSTING_CONTRACT_VERSION` remains **5.3.43**.

### Version

Patch bump from 5.5.21 → 5.5.22.

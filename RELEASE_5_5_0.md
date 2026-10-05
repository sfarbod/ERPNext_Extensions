# ERPNext Extensions v5.5.0 — Release Notes

**Version:** 5.5.0  
**Manufacture Costing Contract:** 5.3.43 (unchanged)  
**Focus:** Job Card Manufacture Reconciliation (Golden Rule) + controlled historical repair

---

## Golden Rule

Authoritative equation for each Job Card × Component Item × Batch:

`ISSUED = RETURNED + PHYSICAL MANUFACTURE CONSUME + OTHER PROVEN WIP OUTFLOW + LEGITIMATE REMAINING WIP`

WIP remainder drives disposition suggestions (CONSUMED / SCRAP / RETURN / STILL IN WIP / MANUAL REVIEW).

## Job Card Stock Rebuild

Desk page for evidence scan, disposition, Dry Run (rollback), and System Manager Apply.  
Fingerprints lock plan staleness. Historical Repair Mode preserves original Manufacture stamps (e.g. 5.3.34).

## Canonical Manufacture

Exactly one active submitted Manufacture after successful repair, containing:

- existing valid consumption  
- approved unresolved WIP consume  
- paired Component Scrap  
- secondary outputs  
- MAIN FG rows  

## Manufacture Merge

Multiple Manufacture documents can be selected for merge into one canonical SE.  
Unsupported downstream (e.g. Delivery Note) blocks Apply.

## Material Issue Merge

Proven manufacturing Material Issues may merge into canonical Manufacture (`MI_MERGE_SAFE`).  
Shared / ambiguous / finance-review MIs remain blocked for MVP. Feature unchanged by temporary bridge work.

## Component Scrap Pairing

Consume ↔ Component Scrap paired once. Scrap quantity is visible but does not double-drain Remaining WIP.

## Atomic Repair

Dry Run and Apply share one engine. Failures roll back everything. Sync valuation only; no async RIV.  
Audit log written after Dry Run rollback / Apply commit.

## Temporary Receipt Bridge

Disposable Material Receipt covers exact cancel shortages so shared logistics can be cancelled/recreated without foreign-document chains. Lifecycle: create → submit → use → cancel → delete. Must not survive Apply. Excluded from Golden Rule evidence.

## Shared Logistics

Classification: DEDICATED / SHARED_RECREATE_SAFE / SHARED_BLOCKED.  
Whole-document recreate (no row-split). Bridge unlocks stock-shortfall SHARED_BLOCKED when exact shortages are known.

## Golden Rule Audit Report

**Report:** Job Card Golden Rule Audit  
**Date basis:** `Job Card.posting_date`  
**Grain:** Job Card × Component Item × Batch  
**Read-only.** Default shows exceptions only (`Show Balanced` optional).  
Flags: missing consumption, unexplained WIP, over consume/return, scrap mismatch, multiple Manufacture, merge/MI review, blocked/ambiguous.  
Links to Job Card and Job Card Stock Rebuild.

## Safety / Rollback

- Dry Run always rolls back business mutations  
- Apply commits once after verification  
- Pre-Apply Development DB backup recommended  
- No Core patch, no SQL repair, no Fix All from the audit report  

## Historical Repair Mode

Repaired Manufactures keep the selected historical contract stamp (e.g. 5.3.34) while the live contract remains 5.3.43.

## Controlled Canary

PO-JOB08760 — disposition `13200544 × 1148 = CONSUMED` applied atomically with temporary receipt bridge and shared logistics recreate.

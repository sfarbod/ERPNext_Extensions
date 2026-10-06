# erpnext_extensions 5.5.14

## Job Card Stock Rebuild — User-approved zero-net Batch Offset

### Summary

Explicit operator exception for historical Batch attribution mismatches when:

- Same Job Card × same Item
- Batch remainders net to **exactly zero**
- Safety gates pass (`QUANTITY_AND_VALUE_SAFE`)
- Operator checks **Accept Batch Difference — No Repair** (default unchecked)

Checked semantics: **NO STOCK DOCUMENT CHANGE** for that offset group.
Status: `APPROVED_BATCH_OFFSET` (not a silent BALANCED).
Fingerprinted to evidence; stale approvals → `STALE_PLAN`.

### Primary canary

**PO-JOB08773 / 13200091**

| Batch | Remaining | Rate |
|-------|-----------|------|
| 926-… | +303 | 65765 |
| 929-… | −303 | 65765 |

Item net = 0 · value delta = 0 · classification = `QUANTITY_AND_VALUE_SAFE`

Root cause: return + scrap/reject attributed to batch 929 though only batch 926 was issued to this Job Card.

### Non-goals / unchanged

- Golden Rule repair remains **Job Card × Item × Batch**
- Audit remains **Job Card × Item** (5.5.10/5.5.12)
- No weighted rates, Repack, batch rewrite, SA, Additional Cost
- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- 5.5.13 department + issued-rate preservation

### Tests

- `test_batch_offset_v5514` BO01–BO22 (+ dry-run exception-only)

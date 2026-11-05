# Technical Report — Delivery Note Check Submission (v5.6.44)

## Requirement

Check Submission must not return READY when ERPNext Submit would reject the
Delivery Note for an applicable stock / business validation — without mutating
the database.

## Reference discrepancy (MAT-DN-2026-00281)

| Path | Result |
|---|---|
| Check Submission (≤5.6.37) | READY — advisory limitations only |
| ERPNext Submit | Negative Stock Error — Batch `3123-20100138-ss241607p22`, need **46.0** |

### Ledger facts

| Event | Batch qty (warehouse) | Cumulative |
|---|---:|---:|
| Opening reco | +428 | 428 |
| Prior DN | −91 | 337 |
| **MAT-DN-2026-00281 posting (proposed −55)** | | **282** |
| Later STE MAT-STE-2026-43565 | −328 | **−46** |

Historical `get_batch_qty(..., posting_date)` returned **337** → false READY.
Submit-time SABB cumulative walk after inserting −55 hits −46 → need 46.

## Coverage matrix (critical blockers)

| Blocker | Classification | Outcome in 5.6.44 |
|---|---|---|
| Mandatory / select / link / length | Reuse Category A | PASS / FAIL |
| Warehouse present | Per-row + Category A | PASS / FAIL |
| Historical item qty as of posting | Category B (`_get_stock_balance`) | PASS / FAIL |
| Historical batch qty as of posting | Category B (`get_batch_qty` + `for_stock_levels`) | PASS / FAIL |
| **Future warehouse negative after outbound** | Dedicated RO (`_future_warehouse_negative_after_outbound`) | **PASS / FAIL** |
| **Future batch negative after outbound (SLE + SABB)** | Dedicated RO (`_future_batch_negative_after_outbound`) | **PASS / FAIL** |
| Batch expiry vs posting_date | Category B | PASS / FAIL |
| Serial presence / warehouse / status | Category B | PASS / FAIL / INCOMPLETE (auto-pick) |
| Stock reservation warehouse match | Category A | PASS / FAIL |
| Over-delivery | Category B | PASS / FAIL |
| SO hold / close | Category A | PASS / FAIL |
| Credit limit | Category A | PASS / FAIL |
| Approving authority | Category A | PASS / FAIL |
| Taxes / party / fiscal year | Category A | PASS / FAIL |
| Expense / stock account / dimensions | Category B accounting | PASS / FAIL |
| Exact GLE amounts / FIFO valuation | Cannot RO-simulate | Advisory limitation |
| Repost Item Valuation engine | Cannot RO-simulate | Advisory (negative qty covered) |
| Server Scripts / unsafe hooks | Cannot safely run | Blocking → INCOMPLETE |
| Return + Issue Credit Note | Mutating | Blocking → INCOMPLETE |
| Auto-create outward SABB | Mutating | Blocking → INCOMPLETE |
| Iran `ValuationIntegrityError` on SLE insert | Requires SLE mutation path | Advisory / covered only if raised by allowlisted RO hooks |

## Implementation

- `stock_availability.py`: future warehouse + batch collectors; SABB-aware batch
  ledger; respect `allow_negative_stock` / `allow_negative_stock_for_batch`.
- `validator.py`: status `ready` / `blocked` / `incomplete`; READY message
  never used when errors or blocking limitations exist.
- Desk JS: READY / BLOCKED / INCOMPLETE titles.
- Limitations text narrowed so future **negative quantity** is not listed as
  unchecked.

## Read-only guarantees

Unchanged from v5.6.34: write guard, fingerprint, no submit / SLE / GLE / Bin /
SO / SRE / SABB mutations, no mutation+rollback.

## Tests

- `test_delivery_note_check_submission_v5644`
  - Synthetic backdated DN + later batch issue → BLOCKED, needed=30
  - Live `MAT-DN-2026-00281` → not READY, batch needed=46
  - Write guard around collectors
  - Status enum
- Prior v5634 / v5637 suites remain green.

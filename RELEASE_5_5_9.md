# erpnext_extensions 5.5.9

## Job Card Stock Rebuild — tracking reconstruction + canonical JCI linkage

### Summary

1. **Root cause** — Canonical Manufacture repair built source rows without `job_card_item`, while historical Job Card Item `transferred_qty` / `consumed_qty` stayed at 0 because migrated `Stock Entry Detail.job_card_item` was NULL. Site Server Script **Custom 6 - Manufacturing Integrity Validator** correctly blocked submit.

2. **Tracking reconstruction** — New module `tracking_reconstruction.py` deterministically:
   - Resolves UNIQUE Job Card Item maps (`resolve_unique_jc_item` semantics; AMBIGUOUS/MISSING block)
   - Classifies historical SED rows (`SAFE_BACKFILL` / `AMBIGUOUS` / `NOT_REQUIRED`)
   - Metadata-only backfills `SED.job_card_item` for SAFE submitted rows (no qty/rate/SLE/GL change)
   - Writes Core/Custom-2a-compatible counters: `transferred_qty = issued − returned`, pre-submit `consumed_qty = 0` after merged Manufacture cancel
   - Stamps every canonical Manufacture CONSUME row with the resolved JCI before insert/submit

3. **Repair sequence** — Inside the single Dry Run / Apply transaction:
   JCI map → SED backfill → transferred update → (bridge / cancels) → consumed pre-submit → canonical with JCI → logistics → valuation → verify → commit/rollback.

4. **Custom 6** — Remains **ENABLED**. No bypass, disable, or monkey-patch. Validator passes on corrected in-transaction data.

5. **UI** — Manufacture Preview shows **Job Card Tracking Repair** (current→proposed transferred/consumed, SED link counts, mapping confidence) and JCI on Final Manufacture rows.

6. **Valuation** — Synchronous repair repost uses `allow_negative_stock=True` only while `HISTORICAL_REPAIR_FLAG` is set, matching historical_stock repair practice for pre-existing intermediate warehouse negatives (does not change rates).

### Non-goals / unchanged

- Golden Rule / Component Scrap / Product Reject semantics
- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- Custom 6 script body / enabled state
- Blind item-only mapping when Job Card Items are ambiguous

### Canary PO-JOB08760 / 13200544 (backup `20261006_001111`)

| Check | Result |
|-------|--------|
| Dry Run | PASS, mutated=false, committed=false, metadata rolled back |
| Apply | PASS — COMMITTED |
| Canonical | 17/17 source JCI; 13200544 qty 1148 → `39o0p9p4ep` |
| Tracking | transferred 0→1148, consumed →1148 |
| Golden Rule | remaining 0 / OK |
| 13200190 scrap | consume 580 / scrap 5 / no double count |
| Custom 6 | enabled throughout |

### Tests

- `test_tracking_reconstruction_v559` (map, SED classify, plan stamp, Dry Run PASS, fail-injection rollback)
- Real restored-fixture Dry Run + Apply gate

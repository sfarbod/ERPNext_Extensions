# erpnext_extensions 5.5.7

## Historical Repair October-6 Campaign Pause Checkpoint

### Summary

1. **Campaign paused before final repair/repost proof** — The October-6
   Production-backup Historical Repair campaign is paused. Development database
   is **PARTIALLY_MUTATED** after repair waves and an incomplete L6 full native
   repost (550/4368). **Not** Production-ready. **Not** repost-proven.

2. **Product Reject + TYPE-C residual align fix** — Under TYPE-C manufacture
   policy, `align_manufacture_finished_good_residual` must not pool-close into
   FG when Product Reject is present after equal-rate allocation with shared
   header operating cost. That rewrite undid Iran-native historical apply
   (observed on Manufacture Product Reject with operating cost).

3. **Checkpoint documentation** —
   `docs/historical_repair/OCT06_CAMPAIGN_PAUSE_CHECKPOINT.md` is the resume
   authority. Immutable source remains
   `20261006_001111-erp_espadpharmed_com-database.sql.gz`.

### Scope

| Area | Change |
|------|--------|
| `manufacture_rounding.py` | Skip FG pool-close for Product Reject under TYPE-C policy |
| Unit test | `test_product_reject_residual_align_v557.py` |
| Docs | October-6 pause checkpoint |

### Non-goals / explicit non-claims

- Do **not** treat this release as Production Ready, Repair Completed, or
  Repost Proven.
- Do **not** restore the current Development database to Production.
- Resume requires source restore + fresh proof path documented in the checkpoint.

### Tests

- Focused: `test_product_reject_residual_align_v557` (unit)
- Not run: full L6, Clean A/B, full integrity campaign, broad exploratory suites

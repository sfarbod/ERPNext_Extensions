# erpnext_extensions 5.6.27

## Purchase Receipt Integer-Rate Rounding — Domestic + FX Standalone Stock Adjustment

### Summary
Extends the 5.6.26 FX-only standalone Stock Adjustment path to **domestic IRR
Purchase Receipts** whose integer valuation rates leave a Class A gap vs
commercial net (typically header-discount allocation).

GRNI remains protected. Legitimate gaps post to Stock Adjustment with the
correct sign. Historical partner-path pollution (inflated GRNI + SA debit) is
repairable via HRC `repair_one`.

**Local merge into `develop`. No push / publish / production deploy.**

---

### Root cause (5.6.26 gap)
`_purchase_receipt_is_fx()` gated standalone SA. Domestic IRR PRs with integer
VR Class A still required a non-stock partner; GRNI is forbidden →
`partner_error` (production RIV on `MAT-PRE-2026-02069`, net residual 31).

### Fix
1. **Classify** all PR Class A `amount_authoritative_*` residuals as
   `pr_integer_rate_standalone_sa` (FX and domestic), with inverted SA sign.
2. **Apply** standalone SA; restore commercial GRNI from full stock-row auth;
   SA magnitude clears the actual post-restore GL gap (SLE vs GRNI).
3. **Align** domestic VR: LCV/PI-diff stays amount-preserving; other domestic
   commercial nets use integer ROUND_HALF_UP (FX unchanged).
4. **Repair** order: adjust → RIV (SLE sync) → GL regenerate; fingerprint /
   SA verification covers domestic integer gaps.

### Evidence

**MAT-PRE-2026-02069** (domestic IRR, discount 150M, 39 items)

| | Before (polluted / amount-preserving) | After |
|--|--:|--:|
| SLE / Inventory | 2,674,800,000 | **2,674,800,031** |
| GRNI | 2,674,800,000 or 2,674,800,062 | **2,674,800,000** |
| SA | 0 or D 31 | **C 31** |

**MAT-PRE-2026-01642** (FX regression): Inv 57,748,167,000 / GRNI 57,748,156,800 /
SA C 10,200 — unchanged; RIV idempotent.

### Regression (development.localhost)
| Module | Result |
|--|--|
| test_fx_pr_rate_first_residual_v5625 | 10 pass |
| test_irr_residual_classification | 25 pass |
| test_irr_rounding_residual | 19 pass |
| test_pr_lcv_amount_preserving_vr_v5625 | 7 pass |
| test_pr_legacy_uvr_float_v5528 | 11 pass |
| test_purchase_valuation_detection_v5625 | 5 pass |
| test_transfer_inward_conservation_v5625 | 3 pass |
| test_pr_pi_compat_control_flow_v5625 | 8 pass |
| E2E 02069 + 01642 | **pass** |

### Production / Campaign
After deploy, resume HRC jobs that failed with residual partner_error on
domestic discounted PRs. Dry-run scan first; repair via `repair_one` /
campaign path with Iran monkey patches applied. Do not use JE to balance GRNI.

### Version
`erpnext_extensions.__version__` → **5.6.27**

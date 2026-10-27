# erpnext_extensions 5.6.26

## FX Purchase Receipt Integer IRR Valuation + Stock Adjustment

### Summary
Corrects the 5.6.25 FX Purchase Receipt treatment that used a **fractional** IRR
valuation rate (`auth / qty`) to eliminate rounding. Iran Accounting policy
requires:

1. **Integer IRR unit valuation rates** (ROUND_HALF_UP) on FX Purchase Receipts.
2. Inventory valued at `qty × integer VR`.
3. Purchase GRNI remains at the **authoritative commercial** amount (`EUR × FX`).
4. The integer-rate gap posts to **Stock Adjustment** (`621301`), never GRNI /
   Financial Round Off / protected Additional Cost accounts.

**Local merge into `develop` after quality gate. No push / publish / production deploy.**

---

### Policy (MAT-PRE-2026-01642 evidence)

| Quantity | 51,000 |
| FX rate (EUR) | 0.4 |
| Exchange rate | 2,830,792 IRR |
| Commercial auth | 51,000 × 0.4 × 2,830,792 = **57,748,156,800** IRR |
| Unit product | 0.4 × 2,830,792 = 1,132,316.8 → **1,132,317** IRR |
| Inventory | 51,000 × 1,132,317 = **57,748,167,000** IRR |
| Rounding gap | **10,200** IRR → Stock Adjustment credit |

**Must not** store valuation rate `1,132,316.8`.

Expected GL:

| Account | Debit | Credit |
|---------|------:|-------:|
| Inventory 111620 | 57,748,167,000 | 0 |
| Purchase GRNI 211004 | 0 | 57,748,156,800 |
| Stock Adjustment 621301 | 0 | 10,200 |

---

### Root cause (original RIV failure)

With integer document VR, SLE posts inventory at `qty × VR`. ERPNext PR GL posts
GRNI at `base_net` (commercial auth). When `Company.default_expense_account` is
empty, Core `make_divisional_loss_gl_entry` falls back to GRNI and merges the
gap into purchase clearing — contaminating GRNI to the inventory amount.

Iran Class A residual then required a non-stock partner. Purchase GRNI is
**forbidden** as a residual partner (5.6.25), so RIV aborted with
`partner_error` (production RIV `7q5c056oc3` / campaign `HRC-00006`).

5.6.25 temporarily “fixed” this by forcing fractional `auth/qty` VR so SLE=auth
and Class A was skipped. That violated the integer IRR unit-rate policy.

---

### Implementation (smallest safe correction)

| Area | Change |
|------|--------|
| FX VR align | `qty_rate_amount._align_purchase_valuation_rate_amount_preserving` — FX → `round_monetary_rate(auth/qty)`; domestic LCV keeps fractional amount-preserving |
| SLE acquisition | `stock_ledger_deterministic.authoritative_acquisition_incoming_rate` — document VR only (no fractional rewrite) |
| Residual classify | FX PR Class A inverts SA sign (`preset_debit=residual`) and stamps `fx_integer_rate_standalone_sa` |
| Residual evaluate | `standalone_sa` → READY without partner |
| Residual apply | Restore commercial GRNI when Core divisional merged into GRNI; append Stock Adjustment; no generic partner |
| HRC detection | Flags FX fractional VR / SLE lag for `FX_INTEGER_VR_STOCK_ADJUSTMENT`; correct integer+SA not flagged |
| HRC repair | Fingerprint inventory vs `qty×integer VR`; verify SA credit = rounding gap |

Domestic LCV amount-preserving, GRNI partner forbid-list, Financial Round Off
prohibition, and protected Additional Cost rules are unchanged.

---

### Real document test (development.localhost)

Purchase Receipt `MAT-PRE-2026-01642` / Item `13100023`:

1. Forced fractional baseline (5.6.25 shape): VR `1,132,316.8`, SLE=auth, no SA.
2. Detection: `unbilled_fx_fractional_vr_needs_integer_sa`.
3. `repair_one(execute=True)` → status `repaired`.
4. After: VR `1,132,317`; SLE incoming `1,132,317`; SVD `57,748,167,000`.
5. GL: Inv D `57,748,167,000` / GRNI C `57,748,156,800` / SA C `10,200`.
6. Second RIV: Completed; SA remains `10,200` (no duplicate); GRNI/SLE stable.

---

### Regression results (development.localhost)

| Module | Result |
|--------|--------|
| `test_fx_pr_rate_first_residual_v5625` | 10 passed |
| `test_irr_residual_classification` | 25 passed |
| `test_irr_rounding_residual` | 19 passed |
| `test_pr_lcv_amount_preserving_vr_v5625` | 7 passed |
| `test_pr_legacy_uvr_float_v5528` | 11 passed |
| `test_purchase_valuation_detection_v5625` | 5 passed |
| `test_transfer_inward_conservation_v5625` | 3 passed |
| `test_pr_pi_compat_control_flow_v5625` | 8 passed |
| `test_fx_purchase_invoice_pattern_a` | 7 errors / 2 skipped — site Buying Settings “Purchase Receipt Required” (environmental; unrelated to this change) |
| E2E MAT-PRE-2026-01642 | **passed** (16/16 assertions) |

**Focused gate: PASS** (excluding known environmental FX PI Pattern A site constraint).

---

### Production deployment considerations

1. Deploy `5.6.26` with Iran monkey patches applied (`bootstrap.apply` / worker boot).
2. Do **not** resume production campaign `HRC-00006` until this build is live —
   prior fractional-rate repair would reintroduce the wrong VR.
3. After deploy, dry-run purchase valuation scan for FX PRs with
   `unbilled_fx_fractional_vr_needs_integer_sa` / SLE lag notes.
4. Repair via HRC `repair_one` / campaign resume (RIV job path with patches).
5. Verify per voucher: integer VR, SLE=`qty×VR`, GRNI=commercial auth, SA=gap,
   second RIV idempotent.

### Campaign recovery

- Campaign remains resumable after deploy.
- Detection now classifies correct integer+SA as valid (no false “amount-preserving” repair).
- Failed RIV `7q5c056oc3` shape is fixed by standalone SA (no GRNI partner).

---

### Version

`erpnext_extensions.__version__` → **5.6.26**

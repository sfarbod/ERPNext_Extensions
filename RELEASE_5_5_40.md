# Release 5.5.40 — Native outgoing valuation + PR/PI discount landed-cost compatibility

**Version:** 5.5.40  
**Date:** 2026-10-13  
**Previous:** 5.5.39  
**Branch:** `develop`  
**Development site:** `development.localhost`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (stock Docker image; no Core fork)

**Validation scope:** Development site only.  
**Production:** Not deployed by this release preparation. Historical repair and RIV campaign are **separate operational phases** after deploy.

---

## A. Version and compatibility

| Component | Version |
|---|---|
| erpnext_extensions | **5.5.40** (`__version__`) |
| Frappe | 16.36.1 |
| ERPNext | 16.37.0 (unmodified Core) |

Docker-safe: all changes live in `erpnext_extensions`. Standard ERPNext images remain usable.

---

## B. Fix A — Native outgoing stock valuation preservation

For Stock Entry purposes:

- Material Issue
- Material Transfer
- Material Transfer for Manufacture
- Send to Subcontractor

Iran Accounting **preserves ERPNext native SLE valuation** (BatchNoValuation / SABB / moving average). When SED drifted from the native outgoing movement, SED is realigned to native SLE — not the reverse.

**Preserved manufacturing contracts** (document amount authority unchanged):

- Manufacture, Repack, Material Consumption for Manufacture
- Secondary items, co-products, by-products, scrap, Product Reject
- Stage-equivalent / equal-rate / explicit allocation factors
- SAME_ITEM_MULTI_FG, residual classification, BULK_SCRAP manual-rate

Manual rate and allow-zero valuation keep document authority.

---

## C. Fix B — Purchase Receipt / Purchase Invoice discount valuation

Module: `iran_accounting/integration/pr_pi_landed_cost_compat.py`

When Buying Settings **Set Landed Cost Based on Purchase Invoice Rate** is enabled, stock ERPNext v16 compared PI billed net to PR **gross** `item.rate`, double-applying Additional Discount.

This release wraps `purchase_receipt.update_billing_percentage` narrowly and applies the official upstream net comparison:

```python
adjusted_amt = (
    flt(billed_amt / qty) * item.qty
    - flt(item.base_net_amount)
)
```

Then invokes native `adjust_incoming_rate_for_pr`.

---

## D. Upstream reference

| | |
|---|---|
| ERPNext PR | [#59476](https://github.com/frappe/erpnext/pull/59476) |
| Upstream commit | `1fe6c2e92b0dcf3cdca6e945ea89c120fb42fa19` |
| Detection | Source inspection for the upstream formula marker |
| Behavior if Core already fixed | Compat patch **skips** install (no double semantics) |

---

## E. Hook lifecycle and removal strategy

| Path | Registration |
|---|---|
| Web / Desk / API requests | `hooks.before_request` → `bootstrap.apply` |
| Background workers / RQ jobs | `hooks.before_job` → `bootstrap.apply` |
| After migrate | `hooks.after_migrate` includes `bootstrap.apply` |
| Install | Idempotent monkey patch; attribute `_iran_pr_pi_landed_cost_59476` |

**Removal after ERPNext image includes #59476:**

1. Upgrade Core image; confirm `core_has_upstream_59476()` is true (patch auto-skips), **or**
2. Call `uninstall_pr_pi_landed_cost_compat_patch()` / drop the module and its `monkey_patches` call in a later release.

---

## F. Real Development RIV verification (Material Issue)

| | |
|---|---|
| Stock Entry | `MAT-STE-2026-36831` |
| RIV | `6qerri51rj` (Completed) |
| Item 13100057 | Before 32,980,402,089,536 → After **67,589** IRR |
| Item 13100058 | Before 30,547,766 → After **290,007** IRR |
| GL | Balanced; equals Σ\|SLE stock_value_difference\| = 3,086,373 |

Evidence preserved on Development. Not re-executed for this release commit.

---

## G. Regression tests (executed 2026-10-13)

| Module | Result |
|---|---|
| `test_native_outgoing_sle_sync_v5540` | **13/13 OK** |
| `test_pr_discount_pi_valuation_backport_59476` | **12/12 OK** |
| `test_riv_campaign_v5529` | **29/29 OK** |
| `test_product_reject_bridge_v5337` | **40/40 OK** |
| `test_product_reject_residual_align_v557` | **1/1 OK** |
| `test_manufacture_irr_residual_v5334` | **43/43 OK** |
| `test_stage_equivalent_byproduct_bridge_v5335` | **40/40 OK** |
| `test_co_product_residual_align_v5518` | **10/10 OK** |
| `test_manufacture_native_historical_v5339` | **5/5 OK** |
| **Total executed** | **193 OK** |

### Known unrelated blockers (not introduced by 5.5.40)

- `test_stock_entry_capitalization_amount`: fixture `_Doc` missing `purpose` (4 ERRORS)
- `test_manufacture_native_v537`: missing `classify_wrong_clusters` import in legacy campaign wizard
- ERPNext Core PI integration suite: site UOM bootstrap / supplier similarity Server Script

---

## H. Known limitations

1. **RIV alone does not fix historical discounted PRs** — stored `amount_difference_with_purchase_invoice` / `valuation_rate` must be refreshed via patched billing first.
2. Not every nonzero `amount_difference_with_purchase_invoice` is incorrect (genuine PI price changes, partial billing, multi-PI).
3. Historical SED≠SABB on Material Issue / Transfer / MTFM are candidates; manufacturing SED≠SABB must not be treated as outgoing corruption.
4. Development DB already contains approved RIV `6qerri51rj` mutations — not identical to a fresh Production restore.

---

## I. Production deployment prerequisites

1. Deploy `erpnext_extensions` **5.5.40** on the standard ERPNext Docker image (no Core rebuild).
2. Restart web + workers so `before_request` / `before_job` bootstrap loads both patches.
3. Smoke-check: new Material Issue uses native SABB rates; new discounted PR→PI yields amt_diff=0 when PI matches PR net.
4. **Do not** clear RIV or run campaigns until the separately approved procedures below.

---

## J. Historical Purchase Receipt API correction procedure (POST-DEPLOY — NOT IN THIS RELEASE)

**Status:** Documented only. Do not run during 5.5.40 deploy.

### Goal

Recompute PR billing adjustments through the **patched** native workflow for an explicit allowlist, preserving genuine PI deltas.

### Regression references (do not mutate during release prep)

| Purchase Receipt | Purchase Invoice |
|---|---|
| `MAT-PRE-2026-01417` | `ACC-PINV-2026-01149` |
| `MAT-PRE-2026-01416` | `ACC-PINV-2026-01148` |

Expected (when PI billed net equals PR `base_net_amount`): `amount_difference_with_purchase_invoice = 0`, `valuation_rate` = net landed rate (e.g. 819,000,000 for 15300381).

### Authentication

Use an authenticated Administrator (or Stock/Accounts Manager) session:

- Desk / `frappe.call` over HTTPS, or
- `bench --site <site> execute` / console (same process; call `bootstrap.apply()` first), or
- Token / password API against `/api/method/...`

Patch initialization:

- Desk/API: automatic via `before_request`
- RQ jobs spawned by refresh: automatic via `before_job`
- Bench execute: call `erpnext_extensions.iran_accounting.integration.bootstrap.apply()` once before billing refresh

### Recommended API surface (implement/execute later under approval)

Whitelisted method (to be added in a follow-up if not already present), conceptually:

```text
erpnext_extensions.iran_accounting.api.refresh_pr_pi_landed_cost_billing
  args: purchase_receipts=[], dry_run=1, stop_on_unexpected=1
```

Until a dedicated whitelist ships, an approved operator may run an allowlisted script that, **per PR**:

1. Loads submitted PR + linked PIs (`pr_detail`).
2. Records before: `rate`, `net_rate`, `base_net_amount`, `billed_amt`, `amount_difference_with_purchase_invoice`, `valuation_rate`, taxes, discounts, dimensions.
3. Classifies row (double-discount signature vs legitimate PI delta vs ambiguous).
4. **Dry-run:** compute expected `#59476` amt_diff without writing; emit report; stop on unexpected class.
5. **Apply:** call patched  
   `update_billing_percentage(pr_doc, update_modified=True, adjust_incoming_rate=True)`  
   (same entry Core uses from `PurchaseInvoice.update_billing_status_in_pr` when Buying Setting is on).
6. Record after values; fail closed if result ≠ expected class.
7. Do **not** force amt_diff to zero in SQL.

### RIV side effects (cannot be bypassed safely)

Native chain when `adjust_incoming_rate=True`:

1. Compat wrap applies `#59476` amt_diff  
2. `adjust_incoming_rate_for_pr(pr)` → `update_valuation_rate` + item `db_update`  
3. `repost_future_sle_and_gle(force=True)`  
4. With `item_based_reposting=1`: **`create_item_wise_repost_entries`** → one Queued **Item and Warehouse** RIV per distinct SLE item×warehouse on that PR (posting_date/time from SLE)

**Limitation:** Billing correction cannot be fully separated from RIV *creation* without changing Core. Controlled procedure:

- Stop parallel/sequential RIV schedulers **before** Phase A apply, **or**
- Accept Queued RIVs and execute them only in the later campaign phase
- Do not delete Redis blindly; creation is DB `Repost Item Valuation` submit, not necessarily an immediate RQ payload

### Dry-run / allowlist / resume

- Default `dry_run=1`
- Explicit PR name allowlist only (start with 01416 / 01417)
- Idempotent: re-running patched refresh with same PI data yields same amt_diff
- Stop on unexpected differences; produce CSV reconciliation before/after

### Verification

Per PR item: amt_diff, valuation_rate, SLE incoming_rate / stock_value_difference, GL balance, SABB unchanged for non-stock paths. No automatic bulk RIV execution in this phase.

### Rollback

Restore site DB backup taken immediately before Phase A apply.

---

## K. RIV campaign dependency (SEPARATE PHASE)

After code deploy + optional Phase A PR refresh:

1. **User-authorized** RIV queue reset (only if using fresh `riv_campaign.generate_full_riv_campaign`, which requires Total RIV = 0).
2. Stop RIV schedulers (`assert_schedulers_stopped` guards).
3. Run Bench helper:  
   `erpnext_extensions.iran_accounting.riv_campaign.generate_full_riv_campaign`  
   (generation only; does not execute; does not refresh PR billing).
4. Execute Queued RIVs under controlled scheduler window.
5. Verify SLE / GL / SABB / SED / Bin.

**Helper does not recalculate PR billing.** Phase A must precede Phase B for discounted PRs.

Prefer targeted dependency-aware repost over full 15k+ voucher regenerate when scope allows.

---

## L. Rollback considerations

| Layer | Action |
|---|---|
| Code | Redeploy previous extensions image/tag (5.5.39); uninstall PR compat if needed |
| Data (after historical phases) | Restore pre-phase DB backup |
| Core | Unchanged — no Core rollback required |

---

## Operational phase separation (mandatory)

| Phase | What | In 5.5.40 release commit? |
|---|---|---|
| 1. Code deployment | Ship 5.5.40 | **Yes (this release)** |
| 2. Historical PR API correction | Allowlisted billing refresh | **No — later** |
| 3. RIV queue reset | Approved empty/clear procedure | **No — later** |
| 4. RIV campaign generate + execute | `riv_campaign` + scheduler | **No — later** |

---

## Files in this release

- `erpnext_extensions/__init__.py` — version 5.5.40
- `iran_accounting/domain/stock_entry_sync.py` — Fix A
- `iran_accounting/domain/repost_determinism.py` — post-RIV SED reload after native align
- `iran_accounting/stock_entry.py` — Fix A integration
- `iran_accounting/integration/monkey_patches.py` — register Fix B
- `iran_accounting/integration/pr_pi_landed_cost_compat.py` — Fix B (new)
- `iran_accounting/tests/test_native_outgoing_sle_sync_v5540.py` — new
- `iran_accounting/tests/test_pr_discount_pi_valuation_backport_59476.py` — new
- `RELEASE_5_5_40.md` — this document

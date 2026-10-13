# Release 5.5.41 — Secure Purchase Receipt Billing Refresh API

**Version:** 5.5.41  
**Date:** 2026-10-13  
**Previous:** 5.5.40  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1** (Development) / **16.36.0** (Production-compatible whitelist API), ERPNext **16.37.0** (unmodified Core)

**Validation scope:** Development restore + repair + API tests.  
**Production:** Not deployed by this release preparation. No automatic historical repair on install.

---

## A. Summary

Adds a **whitelisted, allowlisted** Purchase Receipt billing-refresh API so Production can correct historical ERPNext **#59476** Additional Discount double-counting without SSH/Bench access to native `update_billing_percentage`.

Deployment alone does **not** change historical PR/PI valuations. Correction requires an explicit authenticated API call after dry-run.

---

## B. Historical Purchase Receipt Billing Refresh API

Module: `iran_accounting/pr_billing_refresh/`

| Method | HTTP | Purpose |
|--------|------|---------|
| `get_purchase_receipt_billing_fingerprint` | GET/POST | Before-state SHA-256 fingerprint |
| `dry_run_purchase_receipt_billing_refresh` | GET/POST | Server-side expected deltas; no mutation |
| `refresh_purchase_receipt_billing` | **POST only** | Allowlisted refresh (`dry_run` default **1**) |

Mutations call patched native:

```python
update_billing_percentage(pr_doc, update_modified=True, adjust_incoming_rate=True)
```

No direct SLE/GL writes. No direct PR item SQL. No arbitrary method execution.

---

## C. ERPNext #59476 compatibility

Depends on v5.5.40 compat patch (`pr_pi_landed_cost_compat.py`):

- Marker: `_iran_pr_pi_landed_cost_59476`
- Formula: `flt(billed_amt / qty) * item.qty - flt(item.base_net_amount)`
- Bootstrap via `before_request` / `before_job`

If Core already contains upstream #59476, the wrap is skipped and native semantics are used.

**ERPNext Core is not modified by this release.**

---

## D. Security controls

- Administrator or System Manager only
- Explicit Purchase Receipt allowlist (required)
- Maximum **50** PRs per request
- POST required for mutations
- Client financial values are **never** authoritative (server recalculates)
- Per-PR commit isolation with explicit `summary.repaired_prs` / `failed_prs` / `partial`
- Audit via logger + Comment on each PR

---

## E. Dry-run and fingerprint validation

1. Call fingerprint endpoint for each PR.
2. Dry-run the allowlist; review `server_expected_delta`.
3. Mutate with `dry_run=0` and optional `fingerprints={pr: sha256}`.
4. Fingerprint mismatch aborts that PR without writing.

---

## F. Development restore test results

Restored Production backup: `20261012_110744-erp_espadpharmed_com-database.sql.gz` → `development.localhost` only.

| Metric | Value |
|--------|------:|
| Submitted PRs scanned | 1,298 |
| Affected / repaired PRs | 8 |
| Affected items | 50 |
| Expected inventory correction | **1,108,919,500 IRR** |
| Ambiguous excluded | MAT-PRE-2026-00547 |

Allowlist (Development-validated):

- MAT-PRE-2026-01416, 01417, 00780, 01895
- MAT-PRE-2026-00927, 01089, 01505, 01524

Original four still total **464,260,000 IRR**.

---

## G. Accounting validation

| Check | Result |
|-------|--------|
| PR billing corrected | PASS (8/8) |
| SLE = PRI stock value | PASS (stock PRs) |
| GL balanced to corrected value | PASS |
| Idempotence (expected delta ≈ 0) | PASS |
| 01505 fixed asset (no SLE) | PASS (GL only) |

---

## H. RIV behavior

Native `adjust_incoming_rate_for_pr` → item-wise **Repost Item Valuation** (Queued).

- This API **creates** RIVs through Core only.
- It does **not** execute the RIV campaign or purge RIVs.
- Operator must process Queued RIVs (and any full campaign) separately.
- Development processed 49 RIVs to Completed for validation; do not auto-run that on Production.

---

## I. Production deployment instructions

1. Confirm Production backup exists and is restorable (SSH/Bench).
2. Deploy `erpnext_extensions` **5.5.41** (no schema migration).
3. Restart **web + workers** so bootstrap loads the API and #59476 patch.
4. Verify patch marker / `install_pr_pi_landed_cost_compat_patch` status.
5. Confirm Buying Setting: **Set Landed Cost Based on Purchase Invoice Rate** = enabled.
6. Do **not** expect historical PRs to change from deploy alone.

Compatible with Production Frappe **16.36.0** (`@frappe.whitelist(methods=...)` supported).

---

## J. Production correction procedure

1. **Dry-run** allowlist of the 8 PRs (exclude MAT-PRE-2026-00547).
2. Capture fingerprints; confirm `server_expected_delta` ≈ 1,108,919,500 IRR total.
3. **POST** `refresh_purchase_receipt_billing` with `dry_run=0` in small batches.
4. Verify each PR item `amount_difference_with_purchase_invoice` / valuation rates.
5. Hand Queued RIVs to operator; after RIV completion verify SLE/GL.
6. If `summary.partial` is true, inspect `repaired_prs` vs `failed_prs` before retry.

---

## K. Known limitations

- MAT-PRE-2026-00547 remains **excluded** (ambiguous).
- No unrestricted bulk scan/repair endpoint.
- Does not run or clear the Production RIV campaign.
- Does not stop Production schedulers.
- Fixed-asset PRs (e.g. 01505) correct billing/GL without SLE rows.
- Worker restart required after deploy for runtime patch activation.

---

## L. Rollback procedure

1. Redeploy `erpnext_extensions` **5.5.40**.
2. Restart web + workers.
3. If historical mutation already ran: restore DB from the pre-correction Production backup.
4. Do not delete Completed RIVs as a rollback substitute.

---

## Files

- `erpnext_extensions/__init__.py` — version 5.5.41
- `erpnext_extensions/iran_accounting/pr_billing_refresh/__init__.py`
- `erpnext_extensions/iran_accounting/pr_billing_refresh/api.py`
- `erpnext_extensions/iran_accounting/pr_billing_refresh/service.py`
- `erpnext_extensions/iran_accounting/tests/test_pr_billing_refresh_api.py`
- `RELEASE_5_5_41.md`

# erpnext_extensions 5.6.30

## ERPNext 16.53.0 / Frappe 16.54.0 compatibility allow-lists

### Summary
Re-enable Iran Accounting RIV/UVR monkey patches and the Job Card component-return
bypass on **ERPNext v16.53.0** with **Frappe v16.54.0** after fail-closed upgrade
guards rejected the new minors.

Fingerprints were revalidated on the live development bench. ERPNext 16.52.x
bodies match 16.51.1; ERPNext 16.53.0 introduces Core
``recalculated_stock_entries`` once-per-repost caching for transfer + additional
costs. The rate-first skip path now mirrors Core's discard so a prior cache hit
cannot suppress a document-rate recalculate.

**Development only in this change set. No push / publish / production deploy.**

### Root cause
After upgrading ERPNext past 16.51, ``assert_erpnext_riv_rate_patch_supported``
(and the UVR / JC sibling gates) refused to install wrappers because the explicit
allow-lists stopped at ERPNext 16.51 / Frappe 16.52. Site requests returned HTTP
500 from the fail-closed bootstrap.

### Fix
1. `riv_rate_guard` — allow ERPNext 16.52–16.53 and Frappe 16.53–16.54; primary
   fingerprints for 16.53 ``update_rate_on_stock_entry`` /
   ``recalculate_amounts_in_stock_entry``; keep 16.51–16.52 as alternates;
   discard ``recalculated_stock_entries`` on the rate-first skip path.
2. `uvr_regional_guard` — same version allow-list (UVR fingerprints unchanged).
3. `job_card_component_return/patch` — allow ERPNext 16.52–16.53 (method hash
   unchanged).
4. Matching unit-test allow-list expectations.

### Tests
- `test_riv_rate_first_preserve.py` — 20 OK
- `test_uvr_regional_guard.py` — allow-list/fingerprint unit paths OK;
  `test_lcv_integerizes_valuation_rate` remains a documented non-blocking
  assertion mismatch vs amount-preserving domestic LCV policy (v5.6.25); not
  fixed in this release.
- JC component-return fingerprint gate (N35) OK; canary/data tests remain
  site-fixture failures, unrelated to this compatibility patch.

### Out of scope
- HRMS upgrade (bench-level; not this app commit)
- LCV integerization test rewrite
- Job Card canary fixture refresh
- Unrelated scripts / `.gitignore` salary-sync ignore

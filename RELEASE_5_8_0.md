# erpnext_extensions 5.8.0

## ERPNext / Frappe v16.60 Compatibility Migration

### 1. Release overview

Version **5.8.0** opens explicit ERPNext/Frappe **v16.60** support for
Iran Accounting UVR/RIV guards, aligns Purchase Receipt Class A valuation
helpers with upstream UVR API changes introduced between 16.54 and 16.60,
and extends the Job Card component-return fingerprint allow-list after
revalidation on the live development stack.

This is a compatibility / guard release. Existing IRR business rules
(rate-first UVR, RIV rate preserve, residual classification, SCR clearing)
are preserved.

### 2. ERPNext / Frappe v16.60 compatibility

| Component | Installed (dev) | Status |
|---|---|---|
| Frappe | **16.60.2** (`2df0425`) | Validated |
| ERPNext | **16.60.0** (`886ab6a`) | Validated |
| HRMS | **16.53.0** (`b12d935`) | Unchanged; no HRMS guard edits in this release |
| erpnext_extensions | **5.8.0** | This release |

Allow-lists open **major.minor** `16.60` for ERPNext and Frappe. Intermediate
minors that were not revalidated remain closed:

- ERPNext: `16.56`–`16.59` still **not** allow-listed
- Frappe: `16.55`–`16.59` still **not** allow-listed
- ERPNext `16.54` / `16.55` added where UVR body fingerprints were measured

### 3. Breaking upstream changes addressed

Validated against live ERPNext **16.60.0** `BuyingController.update_valuation_rate`:

1. **Qty divisor** prefers `BuyingController.get_valued_stock_qty(row)`
   (accepted + billed rejected stock qty) before rejected-only /
   conversion-factor fallbacks.
2. **Internal transfer numerator** uses module helper `get_transit_amount`
   with `get_internal_transfer_qty` (stock-qty × transit rate /
   conversion_factor) instead of plain `qty × sales_incoming_rate`.
3. **Rejected materials** (16.54+): Core uses `rejected_qty × base_net_rate`
   (not `net_rate`). Class A auth mirrors that when `base_net_rate` is present.

RIV guarded bodies (`update_rate_on_stock_entry`,
`recalculate_amounts_in_stock_entry`) are **unchanged** vs 16.53.0 on live
16.60.0 (same fingerprints). Pattern A
`make_precision_loss_gl_entry` fingerprint unchanged. Job Card
`StockEntry.validate_work_order_status_for_return` body unchanged.

### 4. Compatibility guards opened or modified

| Guard | Change |
|---|---|
| `iran_accounting/domain/uvr_regional_guard.py` | Allow ERPNext `16.54`/`16.55`/`16.60`, Frappe `16.60`; primary UVR fingerprint → 16.60 digest; prior digests kept as alternates |
| `iran_accounting/domain/riv_rate_guard.py` | Allow ERPNext `16.54`/`16.55`/`16.60`, Frappe `16.60`; RIV primary fingerprints unchanged (16.53≡16.60) |
| `stock_extensions/job_card_component_return/patch.py` | Allow ERPNext `16.54`/`16.55`/`16.60`; method SHA unchanged |
| Pattern A precision-loss wrapper | Inherits RIV allow-list (no separate fingerprint change) |

### 5. Modules and files changed

| File | Role |
|---|---|
| `erpnext_extensions/__init__.py` | `__version__ = "5.8.0"` |
| `iran_accounting/domain/uvr_regional_guard.py` | UVR allow-list + fingerprints |
| `iran_accounting/domain/riv_rate_guard.py` | RIV allow-list |
| `iran_accounting/domain/irr_residual_classification.py` | Class A helpers track 16.54+/16.60 UVR |
| `iran_accounting/domain/qty_rate_amount.py` | Pass `doc` into valuation stock-qty helper |
| `stock_extensions/job_card_component_return/patch.py` | JC allow-list |
| `iran_accounting/tests/test_uvr_regional_guard.py` | Allow-list assertions |
| `iran_accounting/tests/test_riv_rate_first_preserve.py` | Allow-list assertions |
| `iran_accounting/tests/test_irr_residual_classification.py` | New 16.54+/16.60 helper tests |
| `RELEASE_5_8_0.md` | This document |

No new DocTypes, fixtures, or `patches.txt` entries in this release.

### 6. Database migrations and patches

On **development.localhost** only:

- `bench --site development.localhost migrate` — **succeeded** after installing
  missing env dependency `sqlite-vec` (required by upstream Frappe Flow
  `v17_0.make_flow_memories_of_non_managers_personal`; not part of this app).
- DocType sync for `erpnext_extensions` completed; fixtures/dashboards/
  customizations synced; `after_migrate` hooks ran.
- Non-blocking env noise: Flow server socket bind warning
  (`flow.sock` Invalid argument). Does not affect this app’s schema.
- **No** new erpnext_extensions data patches; existing business data not
  rewritten by this release.

**Not claimed:** production migrate.

### 7. Build results

- `bench build --app erpnext_extensions` — **succeeded**
  (esbuild ~141 ms; assets linked; translations compiled).
- Upstream Frappe island chart size budget warning is unrelated to this app.

### 8. Tests and regression coverage

#### Implemented / verified on live 16.60.0 / 16.60.2

| Suite | Result |
|---|---|
| Live RIV / UVR / JC / Pattern A fingerprint asserts | Pass |
| Bootstrap `apply()` + guard asserts on development site | Pass |
| UVR unit (`TestUvrRegionalGuardUnit`, annotation, patch-state) | Pass |
| RIV unit (`TestRivRateGuardUnit`) | Pass |
| IRR residual / PR Class A helpers (incl. new 16.60 tests) | Pass |
| SCR clearing v5.7.57 regression | Pass |

**Focused compatibility + SCR run:** **36 passed**, **0 failed**, **0 skipped**.

**Broader unit run** (UVR + RIV + residual units + JC component-return module):
**85 ran** → **82 passed**, **3 failed**, **0 errors**, **0 skipped**.

The 3 failures are **Job Card canary / live-data** tests
(`TestCanaryPOJOB10492`: returnable qty already consumed on the canary WO).
They are environment-specific and unrelated to the guard / UVR helper changes.
JC fingerprint allow-list and patch install (`bypass=True`) were verified
separately.

#### Not run / not claimed

| Item | Reason |
|---|---|
| `TestUvrRegionalGuardIntegration` / `TestRivRateFirstIntegration` | Full PR/PI fixture integration; not executed in this cut (prior `bench run-tests` hung on site bootstrap) |
| Full `bench run-tests --app erpnext_extensions` | Not run end-to-end |
| Asset Usage / Depreciation / Facility / HRMS payroll suites | No code changes in those modules for 5.8.0 |
| Production or multi-site validation | Dev only |

### 9. Known limitations and unresolved issues

1. ERPNext minors **16.56–16.59** and Frappe minors **16.55–16.59** remain
   fail-closed until explicitly fingerprinted.
2. Job Card canary returnable assertions depend on mutable site stock state.
3. Dev migrate initially failed until `sqlite-vec` was installed; deploy
   environments on Frappe 16.60.x that run Flow `v17_0` patches need that
   dependency present (upstream, not this app).
4. Flow server socket warning on this bench is unresolved and unrelated.

### 10. Upgrade instructions and deployment considerations

1. Deploy on Frappe **≥ 16.60.x** and ERPNext **16.60.x** (or another
   already-allow-listed pair such as 16.53/16.54).
2. Ensure `sqlite-vec` is installed if the target Frappe build includes Flow
   `v17_0` patches that import it.
3. `bench --site <site> migrate`
4. `bench build --app erpnext_extensions` (or full `bench build`)
5. `bench --site <site> clear-cache` and restart workers/web.
6. Confirm Iran Accounting bootstrap: UVR/RIV asserts pass; JC component
   return bypass enabled when fingerprint matches.
7. Do **not** treat this release as a historical stock rebuild or HRC
   campaign trigger.

### Verification honesty

Implemented and validated on the development bench against **ERPNext 16.60.0**
and **Frappe 16.60.2**. Items listed under “Not run / not claimed” are **not**
asserted complete.

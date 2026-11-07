# erpnext_extensions 5.7.8

## Transfer inward conservation after SABE (PO-JOB12112 / MAT-STE-2026-43659)

### Incident (Production backup `20261107_202825`)

Two draft Stock Entries fail ledger-contract validation on submit:

| Document | Purpose | Individual SLE Δ | Transfer Σ SLE | Abs Σ vs row |
|----------|---------|-----------------:|---------------:|-------------:|
| `PO-JOB12112` | Material Transfer for Manufacture | +1 IRR | +1 | +1 |
| `MAT-STE-2026-43659` | Material Transfer | −281 IRR | −281 | −281 |

Documents are independent (no shared source voucher). Both are **draft**
(`docstatus=0`); failure occurs during submit SLE write + contract check.

Evidence (submit-rollback on Development after restore):

* PO outward SLE movement `−13_533_459`; inward poisoned to `+13_533_460`.
* MAT outward SLE movement `−5_867_923_151`; inward poisoned to `+5_867_922_870`.

### Root cause

1. `sync_irr_sle_from_stock_entry_row` correctly applies
   `conserve_transfer_inward_from_outward_sle` so destination SLE magnitude
   equals the outward SLE (plus destination `additional_cost`).
2. Later **SABE integer align** (`qty × ROUND_HALF_UP(rate)`) re-stamps the
   destination SLE and breaks transfer net-zero — the same post-sync overwrite
   class as v5.7.3 (document amount) and v5.7.4 (Case D), but for **transfer
   inward conservation**.
3. Validator correctly rejects: SLE ≠ row mirror, Σ SLE ≠ 0, |Σ SLE| ≠ Σ amount.

Not a regression of Manufacture amount authority (v5.7.3) or Case D (v5.7.4).
Those paths remain authoritative for their purposes; this is an uncovered
transfer-specific hole after SABE.

### Fix

1. `reassert_transfer_inward_conservation_sle` — idempotent re-apply of outward
   conservation after SABE / Case D on IRR companies.
2. Inside `apply_integer_sabe_policy_to_sle`, for transfer inward legs (both
   warehouses, native outgoing purposes, not document-amount authority):
   skip A2 invent for the qty×rate residual and conserve instead of stamping
   `qty × integer rate` onto the SLE.
3. Monkey-patch `process_sle` (main + hot-upgrade) calls the reassert after
   SABE + document-amount reassert + Case D.

**Authority:** for Material Transfer / Material Transfer for Manufacture, the
**outward SLE movement** is inventory authority for the matching inward leg.
Destination SED is realigned to that magnitude (plus `additional_cost` once).
Signed Σ of transfer SLE movements remains exactly zero.

Does **not** bypass the ledger contract validator. Does **not** create
artificial SA / JE / Round Off. Preserves integer IRR and `ROUND_HALF_UP`.
Preserves Manufacture/Repack amount authority (v5.7.3) and Case D (v5.7.4).
Does not double-count `additional_cost` / LCV.

### Explicitly unchanged / limitations

* Validator strictness unchanged (no tolerance).
* Material Issue / Receipt / Manufacture / Repack paths unchanged except shared
  post-SABE reassert no-ops when inapplicable.
* If the outward SLE is not yet available when the inward leg is processed,
  conservation falls through to SABE qty×rate (same as pre-fix behaviour for
  that edge); normal Core ordering writes outward first.

### Development validation

Restored `20261107_202825` → `development.localhost` only
(DB `_19b2f83259cbf92a`). Production untouched.

Permanent submit of both Development copies through normal ERPNext flow:

| Document | Σ SLE | \|Σ SLE+\| = Σ amount | Mirrors | GL | Contract |
|----------|------:|----------------------:|:-------:|:--:|:--------:|
| `PO-JOB12112` | 0 | 346_777_553 | OK | 346_777_553 = 346_777_553 | pass |
| `MAT-STE-2026-43659` | 0 | 5_867_923_151 | OK | 0 = 0 (same-company transfer) | pass |

No artificial SA / JE. Validator unchanged.

### Tests

New: `test_transfer_inward_conservation_after_sabe_v578.py` — **14/14**

Also re-run:

| Suite | Result |
|-------|--------|
| `test_transfer_inward_conservation_v5625` | 3/3 |
| `test_se_ledger_amount_authority_v573` | 8/8 |
| `test_sabe_case_d_after_align_v574` | 2/2 |

### Production deployment guidance

1. Push `develop` (or the release tag) containing commit
   `d59c96b` / subsequent finalize commits for v5.7.8.
2. On Production: `bench --site <prod> update --pull` (or install the
   released app revision) so source reports `__version__ == "5.7.8"`.
3. Restart workers / bench so monkey-patches reload. No schema migration is
   required for this fix.
4. Do **not** force-submit, amend, or SQL-patch existing Stock Entries.
5. Re-try normal submit for blocked draft transfers (e.g. Production copies of
   `PO-JOB12112` / `MAT-STE-2026-43659` if still draft). Validator remains
   strict — success means conservation holds, not a bypass.
6. Do **not** auto-resume HRC-00009 / HRC-00010. Prefer a **new** campaign if
   sequential rebuild work continues after this release.

### Version

`erpnext_extensions` → **5.7.8**

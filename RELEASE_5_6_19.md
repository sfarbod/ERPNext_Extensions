# erpnext_extensions 5.6.19

## Facility Management — Permanent Integrity Guards

### Summary
Prevents duplicate Facility registrations (same Company + Bank + normalized
loan number), enforces exclusive receipt Journal Entry ownership, adds a
narrow **SAFE_CLEAR** repair for incorrectly shared submitted receipt JEs,
improves reconciliation scanner classifications, and ships a READ-ONLY
Finance duplicate-audit report.

### Business rules
- **Facility number uniqueness:** `(company, bank, normalize(facility_name))`
  across all statuses. Persian/Arabic/English digits and equivalent separators
  normalize to the same key. Leading zeros are preserved.
- **New duplicates blocked** on insert, save, API, import, and Duplicate/Copy.
- **Historical duplicates** remain readable; unrelated field updates are not
  blocked. Changing an identifier onto an existing conflict is rejected.
- **Receipt JE exclusivity:** a receipt Journal Entry may belong to only one
  Facility. Assignment validates existence, submit status, company, `cheque_no`
  (= Facility `name`), and exclusive ownership (file-locked).
- **Duplicate/Copy** clears `receipt_journal_entry` and `received_amount` before
  insert.
- **SAFE_CLEAR:** clears an incorrect receipt pointer only when ownership is
  proven via multiple independent signals, no submitted repayments, fingerprint
  matches, and confirm=True. Never deletes JEs or touches the legitimate owner.
- **Scanner:** shared/wrong ownership always reported; amount-only SAFE_E is
  not recommended while ownership is invalid; opening partial schedules are not
  false-positive H; known profit cases stay MANUAL_REVIEW_REQUIRED.

### Concurrency (cross-worker)
Local filesystem locks are **not** used. Uniqueness and receipt-JE assignment
serialize via MariaDB `GET_LOCK` named locks (same pattern as Petty Management
allocation), held until transaction commit/rollback. This works across Docker
workers that share the database. Identity uniqueness is re-checked in
`before_insert` immediately before naming so concurrent inserts cannot both
pass `validate` then race `tabSeries`.

### Database uniqueness (staged)
Existing Production duplicates prevent a safe UNIQUE index today. Application
validation + MariaDB named locks protect new writes. After Finance resolves
historical duplicates, a follow-up patch may add a stored `facility_number_key`
with a UNIQUE index. Until then, do not enable an unsafe constraint.

### Key modules
- `facility_management/facility_identity.py` — normalize, uniqueness, JE assign
- `facility_management/doctype/facility/facility.py` — validate / before_insert
- `facility_management/facility_je_reconciliation.py` — SAFE_CLEAR + scanner
- `facility_management/facility_duplicate_audit.py` — Finance MD/CSV audit
- Tests: `facility_management/tests/test_facility_integrity_guards.py`

### Finance duplicate audit
READ-ONLY reports classify groups as:

- `EXACT_DUPLICATE`
- `HIGH_CONFIDENCE_POTENTIAL`
- `NEEDS_FINANCE_REVIEW`

Deliverables:

- `docs/facility_management/facility_duplicate_audit_20261026.md`
- `docs/facility_management/facility_duplicate_audit_20261026.csv`

### Non-goals
- No automatic Facility / JE deletion or merge
- No Production data modification in this release
- No Frappe / ERPNext Core changes
- No repayment schedule rewrites
- No UNIQUE DB index while historical duplicates remain

### Version
**5.6.19**

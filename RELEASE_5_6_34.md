# erpnext_extensions 5.6.34

## Delivery Note — Check Submission (strict read-only)

### Summary

Check Submission no longer uses savepoint mutation-and-rollback.

Validation is **strictly read-only**: no INSERT/UPDATE/DELETE, no SLE/GLE/Bin/SO/SRE/Serial/Batch
writes, no enqueue, no notifications. Accidental writes are blocked by a write guard and
fail the request hard.

### Architecture

| Category | Approach |
|---|---|
| **A – read-only ERPNext validators** | Called directly (mandatory/select/links, warehouse, SO hold, credit limit, approving authority, serial/batch field rules, taxes company, etc.) |
| **B – mutating submit paths** | **Not executed.** Replaced with dedicated read-only checks (stock availability with reservations, over-delivery, serial/batch eligibility, accounting account readiness) |
| **Unsafe custom code** | Server Scripts and non-allowlisted `doc_events` are **not** run; reported as **blocking limitations** (never a false “ready”) |

### Enforcement

- `enforce_read_only()` intercepts `db.sql` (mutating statements), `set_value`, `delete`,
  `bulk_insert`, `commit`/`rollback`/`savepoint`, `frappe.enqueue`, `publish_realtime`.
- Before/after fingerprint across DN, SO, Bin, SLE, GLE, SRE, SABB must be identical.
- Mutation attempt → `ReadOnlyViolation` (hard fail).

### Intentionally not certified as “full submit simulation”

| Limitation | Severity |
|---|---|
| SLE/GLE creation, Bin/SO/SRE updates, SABB creation | advisory (covered by Category B equivalents where possible) |
| Full FIFO valuation / future SLE integrity / Repost Item Valuation | advisory |
| Active DN Server Scripts | **blocking** |
| Non-allowlisted DN doc_event hooks | **blocking** |
| Auto-pick serial/batch without explicit serials | **blocking** |
| Return + Issue Credit Note invoice creation | **blocking** |
| Pick List bundle duplication | **blocking** |

`ok` / `ready_for_submission` is **true only** when supported checks pass **and** there are
no blocking limitations.

### Files

| Path | Role |
|---|---|
| `.../write_guard.py` | Read-only enforcement |
| `.../stock_availability.py` | Stock + over-delivery (read-only) |
| `.../serial_batch.py` | Serial/batch eligibility (read-only) |
| `.../accounting.py` | GL readiness without GLE |
| `.../limitations.py` | Server Script / hook / structural gaps |
| `.../validator.py` | Orchestrator |
| `.../api.py` | Whitelist API |
| `.../public/js/delivery_note_check_submission.js` | Desk UI + limitations |
| `.../tests/test_delivery_note_check_submission_v5634.py` | Integration tests |
| `CHECK_SUBMISSION_TECHNICAL_REPORT_5_6_34.md` | Full Category A/B report |

### Version

- `erpnext_extensions`: **5.6.34**
- Supersedes savepoint dry-run in **5.6.33**

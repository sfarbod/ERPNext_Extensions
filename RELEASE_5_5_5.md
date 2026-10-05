# erpnext_extensions 5.5.5

## PM Clearance — Active Funding Reservation + Return Deadlock + Concurrency

### Fixed

1. **Zero-available Return deadlock** — `pm_request_passes_clearance_filters` hard
   `available <= 0` gate no longer blocks **PM Return for Correction** (or remark-only
   pending saves). Scoped via existing `pm_return_for_correction` /
   `_skip_funding_availability_over_allocation_gate` (same class as v5.2.11).
   Finance Approve and ordinary Draft saves remain strict.

2. **Active Clearance reservation** — PM Request (and Opening Advance twin via shared
   SQL) funding is reserved for the full active lifecycle:
   Draft (including Returned Draft), Pending*, Approved, Pending JE Submission, Settled.
   Released only for Rejected, Cancelled, or Deleted.

3. **Self-exclusion for Draft** — `clearance_exclude_name_for_validation` now excludes
   the current Clearance for docstatus 0 as well as 1, so saving/reducing a Draft does
   not double-count its own reservation against holder/policy totals.

4. **Transactional concurrency** — Before reading reservation aggregates, allocators
   acquire MariaDB `GET_LOCK('pm_req_alloc:<name>')` (sorted) plus `SELECT … FOR UPDATE`
   on the PM Request row; locks release on commit/rollback. Competing 700+700 against
   1000 cannot both commit.

### Reservation predicate (authoritative)

```
IFNULL(docstatus, 0) < 2
AND IFNULL(status, '') NOT IN ('Cancelled', 'Rejected')
```

### Live availability (authoritative)

```
available(request, exclude_clearance?) =
  paid(submitted PE) − SUM(active reservations excl. self)
```

Child `available_amount` / `previously_allocated_amount` are display snapshots only.

### Backward compatibility

Existing documents where active reservations already exceed funded amount are **not**
auto-mutated. They may Return / reduce / remove / cancel. They may **not** increase
over-allocation or Finance Approve while invalid. Use
`legacy_reservation_conflicts` diagnostic on the E2E prep module.

### Tests

- Unit/integration/concurrency: `test_pm_clearance_active_reservation_v555`
- Playwright: `playwright_pm_clearance_active_reservation_v555.mjs`
  (Desk Actions → PM Return for Correction — no API fallback for Return)

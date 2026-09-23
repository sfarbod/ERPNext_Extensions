# Release 5.3.2 — Payment Request Related Post Dated Cheques

Package version: **5.3.2**.

Builds on **v5.3.1** (Draft PDC allocation sync). This release is UI/traceability only.

---

## Payment Request — Related Post Dated Cheques

Added direct visibility of all **Post Dated Cheques** related to a Payment Request on the
standard ERPNext related-documents dashboard.

### UX

Under the existing **Payment** group:

```text
Payment
 ├─ Payment Entry
 ├─ Post Dated Cheque (N)
 └─ Payment Order
```

Clicking **Post Dated Cheque** opens the standard Post Dated Cheque List filtered to
exactly those related PDCs (`name in (...)`).

### Relationship (authoritative)

A PDC is related when any of the following still hold (same fields as allocation /
settlement linkage):

| Location | Fields |
|----------|--------|
| PDC Allocation child | `reference_doctype` / `reference_name` = Payment Request |
| PDC Allocation child | `source_doctype` / `source_name` = Payment Request |
| PDC header | `reference_doctype` / `reference_name` = Payment Request |

### Traceability

The list is **not** limited to current settlement capacity. PDCs remain discoverable after
Register, settlement JE posting, and when the Payment Request is Partially Paid / Paid,
as long as the relationship rows remain. **Cancelled** PDCs stay in the total count for
audit; the open badge excludes cancelled.

### Not changed

- PDC allocation calculations
- Payment Request outstanding / remaining capacity
- Settlement summary
- Register / Journal Entry logic
- v5.3.1 allocation sync behavior

### Indexes

Uses existing `tabPDC Allocation.parent` and header filters. No new index or migrate.

---

## Deployment

| Step | Required |
|------|----------|
| `bench migrate` | No |
| Patch | No |
| Schema change | No |
| Asset build | No (Python/hooks only; restart workers so hooks reload) |
| Cache clear | Recommended (`bench clear-cache`) |
| Restart | Recommended |

`erpnext_extensions.__version__` = **5.3.2**

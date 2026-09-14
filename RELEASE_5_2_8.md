# ERPNext Extensions v5.2.8

## Job Card Work Order Reassignment

### New

- System Manager repair tool for manually reassigning Job Cards between Work Orders.
- Desk page: **Manufacturing → Repair Tools → Reassign Job Cards** (`/app/reassign-job-cards`)
- Job Card action: **Actions → Reassign Work Order**
- Preview before execution.
- MOVE / KEEP / BLOCK Stock Entry classification.
- Exclusive related Stock Entries move with selected Job Cards.
- Missing Target Work Order Operation rows can be created automatically.
- Exact compatible Target operations can be reused.
- Atomic locking and stale-preview protection.
- Dedicated **Job Card Work Order Reassignment Log**.
- Audit comments on affected documents.

### Safety

- System Manager only, enforced server-side with `frappe.only_for("System Manager")`.
- Shared Stock Entries block execution.
- Cancelled / unsupported Job Cards remain blocked.
- No Stock Entry cancel/re-submit.
- No Stock Ledger repost.
- No GL repost.
- No changes to stock qty, valuation, rate, warehouse, Serial/Batch Bundle, posting date or posting time.
- Source Work Order is never automatically deleted.

### Historical Repair Behavior

- Supports reassignment of submitted Job Cards and their exclusive submitted Stock Entries.
- Historical execution may exceed planned Work Order quantity.
- Planned `Work Order.qty` / Qty To Manufacture is preserved exactly on both Source and Target.
- Only derived execution counters are recalculated after reassignment.
- Repair-specific recalculation bypasses the native historical-capacity throw in
  `WorkOrder.update_operation_status()` without changing normal ERPNext behavior.

### Fixed During Validation

- Fixed missing `CLASS_MOVE` import that broke Preview.
- Removed incorrect Target Work Order qty reconciliation that previously raised planned qty
  to satisfy native capacity checks.
- Prevented reassignment from changing planned Work Order quantity.
- Added regression coverage for historical completed quantities greater than planned quantity.

### Validation

Tested successfully on **development.localhost** with real manufacturing Work Orders and Job Cards.
Unit coverage: `erpnext_extensions.tests.test_job_card_reassign` (21 tests).

### Out of scope

- Automatic Work Order merge / conversion rollback / date-based repair
- Moving Class B Work Order–level Stock Entries not tied to selected Job Cards
- Automatic deletion or cleanup of empty Source Work Orders
- Corrective / subcontracted / paused Job Cards

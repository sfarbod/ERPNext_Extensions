# Release 5.3.1 — PDC Allocation Sync Fix

Package version: **5.3.1**.

## Summary

Fixed a Draft Post Dated Cheque bug where reducing **Cheque Amount** after save left a
stale single **PDC Allocation** row at the original amount, causing:

> Allocated Amount cannot exceed Cheque Amount

when saving or registering — even though partial PDC against a Payment Request is supported.

---

## PDC Allocation Sync Fix

### Problem

Creating a PDC from a Payment Request (or similar source) prefills:

```text
Cheque Amount = Allocation = remaining capacity
```

On a **new unsaved** form, the client already clamped the single allocation row when
cheque amount decreased. After the first Draft save, that sync stopped (`__islocal`
gate). Server validation correctly rejected `allocation > cheque_amount`, so the UI
appeared broken for a valid partial-cheque workflow.

### Fix

- **Client** (`post_dated_cheque.js`): Draft (and new) PDCs with **exactly one**
  allocation row clamp that row when cheque amount is reduced below it. Submitted/
  cancelled documents are not rewritten. Manual under-allocation is preserved;
  cheque increases do not inflate allocation. Multi-row allocations are never
  auto-redistributed.
- **Server** (`sync_single_pdc_allocation_on_reduced_cheque_amount` in
  `pdc_allocation.py`, called from `_validate_allocations`): same safe clamp for
  API/import/non-UI saves before the existing summary validation.
- **Unchanged**: `Allocated Amount cannot exceed Cheque Amount` remains authoritative
  for multi-row and other genuine over-allocation cases.

### Not changed

- No schema / migrate / patch.
- No weakening of over-allocation validation.
- No automatic redistribution across multiple allocation rows.

---

## Deployment

| Step | Required |
|------|----------|
| `bench migrate` | No |
| Patch | No |
| Schema change | No |
| Asset build (`bench build --app erpnext_extensions`) | **Yes** (JS change) |
| Cache clear | Recommended after asset build |
| Restart | Recommended (workers / web) |

Do not deploy to Production from this note alone without your usual release process.

---

## Tests

- `test_pdc_allocation_cheque_amount_sync_v531` — unit A–H + Draft save integration
- Existing PDC allocation / create-from-source / settlement suites remain green

`erpnext_extensions.__version__` = **5.3.1**

# Release 5.3.1 — PDC Allocation Sync, Payment Request Related PDCs & Coverage

Package version: **5.3.1**.

This unreleased cut consolidates Desk/cheque improvements previously staged as a local
`5.3.2` experiment. The **effective application version is 5.3.1** only.

---

## PDC Allocation Sync

Fixed an issue where reducing the Cheque Amount on a saved **Draft** Post Dated Cheque
created from a Payment Request left the single allocation row at its original amount,
causing `Allocated Amount cannot exceed Cheque Amount`.

- Synchronizes safe **single-row** allocations when Draft cheque amount is reduced
  (client + server).
- Preserves manual **under-allocation**.
- Does **not** auto-redistribute multiple allocation rows.
- Preserves existing over-allocation validation.

---

## Payment Request — Related Post Dated Cheques

Added **Post Dated Cheque** to the Payment Request related-documents dashboard
(under **Payment**, beside Payment Entry / Payment Order).

- Click opens the standard PDC List filtered to related cheques.
- Relationship: `PDC Allocation` reference/source and/or header
  `reference_doctype` / `reference_name` = Payment Request.
- Historical traceability: Draft, Registered, settled (Register JE posted), and
  Cancelled remain discoverable while the link rows exist (Cancelled excluded from
  the open badge only).

---

## Payment Request — Direct PDC Coverage & Remaining Balance

Confirmed bug: after Register JE, invoice-style `sum_effective_pdc_allocations_to_reference`
dropped PR coverage to **0** even though the JE does **not** reference the Payment
Request or reduce its PE/outstanding — so the desk showed zero “Covered by Direct
Post Dated Cheque” and inflated Remaining Balance.

Also, Draft PDCs did not reserve PR capacity, allowing a second PDC to over-allocate.

**Fix (PR-specific):** `sum_payment_request_pdc_coverage_allocations` —

- Coverage = **allocation** amounts to the PR (not full cheque face when under-allocated).
- Includes **Draft + Submitted**; excludes Cancelled / Replaced.
- Does **not** clear coverage when Register JE exists (no JE/PDC double-gap on PR).
- Powers settlement summary `effective_pdc_amount` / Remaining and `get_pr_remaining_capacity`.

Invoice JE-aware effective sums are **unchanged**.

---

## Payment Request Settlement UI

Settlement help / headline strings no longer embed HTML tags inside `__()` alone.
Markup is composed in code with escaped translated labels so Desk cannot show raw
`<div>` / `<b>` text when translations or `is_html` detection mis-handle the string.

---

## Deployment

| Step | Required |
|------|----------|
| `bench migrate` | No |
| Patch / schema | No |
| Asset build | **Yes** (JS: settlement summary + existing PDC form sync) |
| Cache clear | Recommended |
| Restart | Recommended |

`erpnext_extensions.__version__` = **5.3.1**

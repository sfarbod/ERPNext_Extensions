# v5.2.6 — Safe Frappe Error Transport for Broken Diagnostic Pipes

## Title

Preserve original Frappe business exceptions when diagnostic stdout/stderr is a dead pipe

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.5**
- `erpnext_extensions.__version__` = **5.2.6**

---

## 1. Problem

A valid Frappe business exception could be replaced by a **secondary** `BrokenPipeError` from diagnostic output.

`frappe.handle_exception` → `report_error` → `frappe.errprint(traceback)` → `print(...)`.

On development.localhost, `bench serve` stdout/stderr pointed at a dead pipe (`/dev/pts/1` deleted). `print()` then raised:

```
BrokenPipeError: [Errno 32] Broken pipe
```

That secondary exception escaped the normal Frappe exception handler. Werkzeug’s debugger returned HTML 500 instead of Frappe JSON.

Developer-mode `log_error_snapshot` dumps with locals also increased the risk of long-running error handling **while a stock write transaction was still open** (rollback runs only after `handle_exception`). A previous failed submit left an in-flight transaction with thousands of row locks; retries then hit `QueryTimeoutError`.

---

## 2. User symptom

Workflow submit of Stock Entry **MAT-STE-2026-25734** (`Material Transfer for Manufacture`, action **Submit MTM**) appeared **frozen/blank**.

```
POST /api/method/frappe.model.workflow.apply_workflow
HTTP 500
Content-Type: text/html
```

Body began:

```
<!doctype html>
<html lang=en>
  <head>
    <title>BrokenPipeError: [Errno 32] Broken pipe
 // Werkzeug Debugger</title>
```

Desk `request.js` then failed:

```
SyntaxError: JSON Parse error: Unrecognized token '<'
```

No Frappe error dialog was shown.

---

## 3. Root cause

The **original** exception was correct:

```
BatchNegativeStockError
http_status_code = 417
item 13200040
batch 708-13200040-0411005-hp-168951255-0
warehouse انبار approved اقلام بسته بندی ثانویه اسپاد
requested 3971
available at posting datetime 121
shortage 3850
```

That validation must remain. What broke was **error transport**:

```
BatchNegativeStockError
    → frappe.errprint / print to dead stdout/stderr pipe
    → BrokenPipeError
    → Werkzeug debugger HTML 500
    → JSON parse error
    → blank/frozen UI
```

---

## 4. Fix

Diagnostic `BrokenPipeError` / `OSError(EPIPE)` (and `ECONNRESET`, the closed-socket equivalent) no longer replace the original Frappe exception.

- Wrap `frappe.errprint` so a dead diagnostic pipe still records the traceback for JSON and does **not** escape `handle_exception`.
- Unrelated `OSError` values (for example `ENOSPC`) are **not** swallowed.
- Skip `log_error_snapshot` for `frappe.ValidationError` and subclasses (including `BatchNegativeStockError` and `NegativeStockError`). Those errors already reach the client as JSON 417 `_server_messages`. Expensive `with_context` local dumps must not hold an open stock transaction. Snapshots remain for non-validation failures (`RuntimeError`, `PermissionError`, …). Snapshot I/O failures never replace the original exception.
- Installed idempotently from existing `before_request` / `before_job` bootstrap (`apply_safe_error_transport`). No wrapper stacking.

This does **not** change stock validation, batch validation, I4, SABB, workflow business rules, or negative-stock policy.

---

## 5. Example

**Before**

```
BatchNegativeStockError (417)
    became
BrokenPipeError
HTTP 500
text/html
<!doctype html> …
```

**After**

```
HTTP 417
Content-Type: application/json
exc_type = BatchNegativeStockError
_server_messages = readable batch shortage
body starts with {
```

Desk shows the normal Frappe error dialog.

---

## 6. Safety

| Item | Unchanged |
|---|---|
| Batch validation (`BatchNegativeStockError`) | **YES** — still BLOCKS 13200040 / 3971 vs 121 |
| Stock validation (`NegativeStockError`) | **YES** |
| I4 / terminal stock-value (v5.2.5) | **YES** |
| Workflow business rules / Submit MTM | **YES** (PM `apply_workflow` remains a Stock Entry pass-through) |
| Global negative stock | **NO enablement** |
| Production data migration | **NO** |

---

## 7. Compatibility

- **v5.2.5** I4 fix remains intact: full consume with historical residual −534 restores `stock_value = 0`; genuine leftover 100,000 still I4-blocks.
- **v5.2.3** negative-stock healing remains intact: −565.17 + 300 = −265.17 ALLOW; source shortage BLOCK; worsening −565.17 − 100 BLOCK; outward batch 100 vs 300 `BatchNegativeStockError`.

---

## 8. Database migration

| Item | Required? |
|---|---|
| Database schema changes | **NO** |
| Data migration | **NO** |
| Production data migration | **NO** |
| Global negative-stock setting change | **NO** |
| Historical SLE repair | **NO** |

Recommended deployment: update app, restart web/workers. No `bench migrate` schema change.

---

## 9. Tests

| Suite | Passed | Failed | Skipped |
|---|---|---|---|
| v5.2.6 (`test_safe_error_transport`) | 17 | 0 | 0 |
| v5.2.5 (`test_i4_zero_qty_terminal_residual`) | 6 | 0 | 0 |
| v5.2.3 (`test_negative_stock_healing`) | 20 | 0 | 0 |
| Combined | 43 | 0 | 0 |

HTTP (development.localhost, MAT-STE-2026-25734, Submit MTM): **417** `application/json`, `exc_type=BatchNegativeStockError`, valid JSON, document remained draft, no leftover InnoDB trx / SLE / SABB writes.

---

## 10. Files / Technical Scope

| File | Role |
|---|---|
| `erpnext_extensions/safe_error_transport.py` | Safe `errprint` + ValidationError snapshot skip |
| `erpnext_extensions/iran_accounting/integration/bootstrap.py` | Install on request and job bootstrap |
| `erpnext_extensions/tests/test_safe_error_transport.py` | BrokenPipe / EPIPE / non-EPIPE / JSON matrix |

---

## Version

- `erpnext_extensions.__version__` = `5.2.6`

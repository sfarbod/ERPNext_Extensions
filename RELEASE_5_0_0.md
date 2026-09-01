# Release 5.0.0 — Canonical Rounding Ownership (Runtime Guard)

## Summary

- **Runtime guard** validates canonical owners only: `core.rounding` → `domain.currency` → `domain.ledger_rounding`.
- **`iran_accounting.rounding`** remains a **compatibility re-export facade** (no business logic); legacy imports keep working.
- Fail-closed behavior preserved; ImportError diagnostics now report canonical module, missing symbols, facade status, and loaded file/`__spec__` paths.
- Read-only **deployment integrity** reporter detects duplicate package roots, file/origin mismatch, and facade completeness without mutating runtime.

## Compatibility

- ERPNext 16.33.x
- Frappe 16.32.x
- Existing `from erpnext_extensions.iran_accounting.rounding import …` callers unchanged

## Not changed

Ledger/currency rounding algorithms, Class A/B, Round Off, Stock Adjustment, UVR, RIV, manufacture, LCV, Purchase Receipt, Asset Request, permission manager, database schema.

## Files

- `iran_accounting/worker/guard.py` — canonical ownership + diagnostics + deployment report
- `iran_accounting/rounding.py` — explicit compatibility facade docstring
- `iran_accounting/domain/repost_determinism.py` — internal import → `domain.ledger_rounding`
- `iran_accounting/diagnostics.py` — integrity snapshot includes canonical + deployment report
- import integrity / stability tests expanded

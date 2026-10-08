# erpnext_extensions 5.5.23

## Parallel Repost Item Valuation — RQ/pickle-safe Historical Repair guard

### Summary

Production parallel RIV (`run_parallel_reposting` → `enqueue_reposting_entry`) failed with:

`_pickle.PicklingError: Can't pickle local object _patch_repost_compatibility.<locals>.execute_reposting_entry`

The Historical Repair dual-execution / company-GL lock guard was installed as a **nested** function on ERPNext’s `execute_reposting_entry`. Sequential inline calls worked; RQ serialization did not.

### Fix

- New module-level guard: `erpnext_extensions.iran_accounting.integration.riv_execute_guard`
- `_patch_repost_compatibility` installs that importable wrapper (same lock contract as 5.5.22)
- Nested IRR `repost` post-pipeline wrapper unchanged
- No change to ERPNext N=1 parallel selection logic (known/deferred)
- No schema / `patches.txt` changes

### Contract preserved

- HR RIV lock held + different owner → defer (return)
- Company GL lock held + no HR owner → defer
- Lock probe errors → fail-open
- Otherwise delegate to original ERPNext `execute_reposting_entry`

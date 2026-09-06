# Release 5.1.3 — UVR guard / monkey-patch state hardening

## Summary

- **Bug:** UVR upgrade guard could fingerprint the Iran override
  (`buying_selling.update_regional_item_valuation_rate`, hash `753002f1…`)
  as if it were vanilla ERPNext, then fail on missing token `allow_regional`.
  Root cause: monkey-patch install state corruption — when the installed flag
  was lost or reinstall ran while live was already Iran, the “saved original”
  pointer could be overwritten with the Iran function.
- **Fix:** Explicit UVR patch state machine (CLEAN / HEALTHY / FLAG_LOST /
  POISONED / LIVE_IRAN_NO_SAVED / UNKNOWN). Resolve and fingerprint **only**
  proven `erpnext.*` vanilla callables. Never store `erpnext_extensions.*` as
  upstream. FLAG_LOST restores the flag without overwriting a valid saved
  vanilla original. Poisoned Iran-as-original fails closed with an explicit
  “Iran override where vanilla ERPNext original was expected” error (not an
  “unsupported ERPNext fingerprint 753002…” message).
- **Unchanged:** Vanilla ERPNext regional stub fingerprint `0148e05e…`,
  required token `allow_regional`, ERPNext 16.34.x / Frappe 16.33.x allow-lists,
  hooks `regional_overrides["Iran"]` + country-agnostic monkey patch coexistence,
  accounting / integerization policy.

## Compatibility

- ERPNext **16.34.1**
- Frappe **16.33.0**
- erpnext_extensions **5.1.3**

## Historical repair

Out of scope.

## Files

- `iran_accounting/domain/uvr_regional_guard.py`
- `iran_accounting/integration/monkey_patches.py`
- `iran_accounting/tests/test_uvr_regional_guard.py`
- `RELEASE_5_1_3.md`
- `__init__.py`

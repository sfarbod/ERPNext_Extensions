# Copyright (c) 2026, ERPNext Extensions contributors
"""RELEASE 4.8.0 — ERPNext 16.33.0 / Frappe 16.32.0 compatibility.

## Compatibility

- ERPNext **16.33.0** (major.minor ``16.33``) added to UVR / RIV upgrade-guard
  allow-lists after live fingerprint revalidation on this bench.
- Frappe **16.32.0** (major.minor ``16.32``) added to the same allow-lists.
- Guarded method bodies / signatures unchanged vs prior allow-list digests:
  ``update_valuation_rate``, ``update_regional_item_valuation_rate``,
  ``update_rate_on_stock_entry``, ``recalculate_amounts_in_stock_entry``,
  ``is_manufacture_entry_with_sabb``.
- ``update_valuation_rate`` still calls ``update_regional_item_valuation_rate``
  (AST contract). Unknown versions remain fail-closed.

## Scrap field rename (ERPNext 16.33)

- ERPNext renamed Stock Entry Detail / secondary-item field ``type`` →
  ``secondary_item_type``.
- ``is_scrap_row`` / ``secondary_item_type_of`` prefer ``secondary_item_type``,
  then fall back to legacy ``type``.
- Legacy heuristics unchanged: ``is_legacy_scrap_item``, Z-convention /
  ``custom_main_item_code``.
- No change to scrap absorbed-cost mathematics, Manufacture residual policy, or
  FG valuation rules.

## PM v4.7.2 migration hardening

- ``migrate_pm_draft_approval_v472`` is version-aware:
  - **Already applied** (Pending* ``doc_status=0`` + Return-for-Correction):
    complete without requiring an empty Pending* queue; no PM document mutation.
  - **In-flight Pending* on first cutover**: defer (no workflow rebuild, no
    document changes); ``after_migrate`` retries when the queue is clear.
- ``petty_management.desk_visibility.after_migrate`` skips full v402 document
  remaps when draft approval is already applied; skips rebuild while cutover is
  still deferred with in-flight Pending* docs.
- First-time cutover with an empty Pending* queue still applies hard rebuild as
  before. No mass workflow-state rewrite of business documents.

## Unchanged

- No Iran accounting policy change (Class A/B, Round Off, Stock Adjustment,
  residual math, UVR/RIV wrappers beyond allow-list).
- No historical repair / no submitted accounting rewrite.
- Unknown ERPNext / Frappe minors still blocked by upgrade guards.

## Version

``4.7.4`` → ``4.8.0``
"""

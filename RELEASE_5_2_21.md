# ERPNext Extensions v5.2.21

**Version:** `5.2.21`  
**Previous:** `5.2.20`  
**App:** `erpnext_extensions`  
**Focus:** Historical Stock Integrity & Repair — fixes two defects in the v5.2.20 I1 engine found by running it against a live tenant.

v5.2.20 shipped the I1 negative incoming rate engine. Deployed against production data it classified correctly — 43 vouchers, 19 `READY_I1` / 24 `WAITING_I1` — but **nothing could actually be repaired**, and the Failed RIV backlog did not learn its root. Both causes are fixed here.

This is a correctness release. No new repair class, no new API surface, no schema change.

---

## Fix 1 — `READY_I1` was demoted to zero writes

**Symptom:** every `READY_I1` row came back `eligible: false`, `sql_updates: 0`, and the dashboard reported `Repairable: 0`. The repair action could never arm.

**Cause:** `READY_STATUSES` in `planner.py` is not the only readiness registry. `dependency.stamp_dependency` re-checks `scope.READY_SCOPES` and force-zeroes any row whose `planner_status` falls outside it:

```python
if status not in READY_SCOPES:
    out["eligible"] = False
    out["blocked"] = True
    out["sql_updates"] = 0
```

`READY_I4` was registered there; `READY_I1` was not. The demotion happened *after* evaluation, which the row itself proved — the inner `planner` sub-dict still held `eligible: true, sql_updates: 3` while the outer row read `0`.

**Fix:** `READY_I1` registered in `scope.READY_SCOPES`.

## Fix 2 — Failed RIV never learned its I1 root

**Symptom:** only **1 of 72** `VALUATION_INTEGRITY` reposts resolved an `i1_root`. The rest stayed `MANUAL`, so v5.2.20's headline routing improvement did essentially nothing on real data.

**Cause:** `_lookup_i1_root` searched by the repost's `item_code` + `warehouse`. Those rows are `based_on="Item and Warehouse"` with `voucher_no` NULL, and the negative incoming rate sits on the **finished-good** identity — not the raw-material identity being reposted. The lookup found nothing and fell back to a null voucher.

The guard message already names the offending voucher:

```
Stock valuation integrity (I1).
voucher_no=MAT-STE-2026-25087
detail=incoming_rate is negative on an incoming movement
```

**Fix:** parse `voucher_no=` from `error_log` as the authoritative root; identity lookup is retained only as a fallback.

---

## Expected effect after upgrade

| KPI | v5.2.20 | v5.2.21 |
|-----|---------|---------|
| `Repairable` (I1) | 0 | 19 |
| `VALUATION_INTEGRITY` rows with `i1_root` | 1 | ~72 |
| Those rows' planner status | `MANUAL` | `WAITING_I1` naming a root |

`READY_I1` / `WAITING_I1` classification counts are unchanged (19 / 24) — v5.2.20 already classified correctly.

---

## Testing

Added to `test_i1_repair_v5220.py`:

- `READY_I1` membership asserted in **both** `READY_SCOPES` and `READY_STATUSES`, so this registry omission cannot recur silently (mirrors the existing `READY_I4` assertion in `test_i4_leftover_repair_v5213.py`)
- I1 root parsed from a guard error log, including the case where the repost identity is deliberately unrelated to the finished-good identity

```bash
bench --site <site> run-tests --app erpnext_extensions \
  --module erpnext_extensions.iran_accounting.tests.test_i1_repair_v5220
```

---

## Known Limitations

Unchanged from v5.2.20, and worth restating plainly:

- The apply path (`repair_i1_selected` / `apply_i1_voucher`) has **still never been executed against data**. Everything verified so far is read-only classification.
- `master_plan` reports `promotion_status: NOT_PROVEN` for this class.
- Both defects fixed here were found by probing a live tenant, **not** by the test suite. Run the tests and prove the apply on a restored copy before it touches production.

---

## Upgrade Notes

Pure Python — no JS, no schema, no patches. No `bench migrate` or `bench build` required.

```bash
cd ~/frappe-bench/apps/erpnext_extensions && git pull
cd ~/frappe-bench && bench restart
```

Then Scan All → open **I1 Negative Rate** → confirm `Repairable` is 19, not 0.

---

## Version metadata

| Location | Value |
|----------|-------|
| `erpnext_extensions/__init__.py` | `__version__ = "5.2.21"` |
| Master Repair Plan / Campaign Wizard / Assisted Master Plan payloads | `"version": "5.2.21"` |

---

## Publish checklist (operator)

- [ ] Working tree clean after this preparation commit  
- [ ] Apply path proven on a restored copy before production use  
- [ ] Backup completed before deploy  
- [ ] Push / tag / publish only after explicit approval  

**This document is release preparation only. Do not push, tag, or publish until the operator authorizes it.**

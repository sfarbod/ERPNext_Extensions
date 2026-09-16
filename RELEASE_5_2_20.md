# ERPNext Extensions v5.2.20

**Version:** `5.2.20`  
**Previous:** `5.2.19`  
**App:** `erpnext_extensions`  
**Focus:** Historical Stock Integrity & Repair — I1 negative incoming rate (Manufacture value pool) becomes a first-class repair class.

v5.2.19 classified guard invariant **I1** (`incoming_rate` negative on an incoming movement) but gave it no exit. `failed_riv.py` bucketed I1 together with I4 into `VALUATION_INTEGRITY`, and `planner._evaluate_riv` routed that status to `MANUAL — Failed RIV must not be auto-retried`. Only I4 had a repair engine (`i4_repair.py`); I1 had none.

The result on production tenants: every Failed Repost Item Valuation blocked behind a negative Manufacture valuation was parked permanently, with no operator path and no named root.

v5.2.20 adds the missing engine.

---

## Overview

- New repair class `I1_NEGATIVE_RATE_REPAIR` / topic `I1_NEGATIVE_RATE`
- New engine `historical_stock/i1_repair.py`
- Planner states `READY_I1` / `WAITING_I1` / `MANUAL_I1` / `I1_REPAIRED`
- Failed RIV `VALUATION_INTEGRITY` now names its I1 root instead of parking as `MANUAL`
- Dashboard KPIs, canonical KPI buckets, topic tab, and an operator repair action
- Master Repair Plan places I1 immediately after Warehouse (it gates Zero / Wrong / GL / RIV)

---

## The invariant, and its inverse

`riv_valuation_guard.assert_manufacture_value_pool` (I5) states the Manufacture contract:

```
fg_amount = outgoing_pool + capitalized_cost - other_incoming
```

A finished good is the **residual** of the value pool. When a secondary inbound row (material returned to store, by-product) is priced from a poisoned warehouse valuation, `other_incoming` explodes and the FG residual is forced negative. The guard then fires I1 on the resulting SLE and refuses the repost — correctly.

The wrong number is the **secondary inbound rate**, not the FG rate. Repairing the FG rate directly would hide the cause and leave the pool inconsistent.

### EXACT source of truth: the document itself

When the secondary inbound item is **also issued in the same Stock Entry**, the issue rate of that document is the rate the returned material must carry. Nothing outside the voucher is consulted — no warehouse valuation, no Bin, no moving average.

Observed shape on a production tenant (`MAT-STE-2026-24798`), same item, same voucher:

| Row | Direction | Item | Qty | Rate |
|-----|-----------|------|-----|------|
| 2 | issued | `13100057` | 4,149 | **67,589** |
| 8 | returned | `13100057` | 49 | **12,902,012,539** |
| 7 | finished good | `30100046` | 4,100 | **−153,836,201** |

Repricing row 8 at the document's own issue rate restores a positive FG residual. No external truth was required.

When the secondary item is **not** issued in the document, its rate could only come from warehouse valuation — which is exactly what is poisoned. Those rows are `WAITING_I1` and wait for Zero / Wrong Rate repair upstream. They are never guessed.

---

## What's New

### I1 engine (`historical_stock/i1_repair.py`)

- `classify_i1_voucher(voucher_no)` — pool contract classification for one Manufacture voucher
- `document_issue_rates(rows)` — weighted issue rate per item, outgoing rows only
- `scan_i1_negative_rate(...)` — scope-filtered scan over live SLEs violating I1
- `dry_run_i1_repair(rows)` — full preview chain, writes nothing
- `repair_i1_selected(rows, *, dry_run=True)` — savepoint, audit log, no partial commits
- `apply_i1_voucher(voucher_no)` — reprice secondary → restore FG residual → header totals → SLE + SABB → identity replay
- `i1_root_cause(voucher)` — per-row evidence panel (stored rate vs document issue rate, ratio, verdict)
- `i1_root_for_identity(item, warehouse)` — the root a Failed RIV is waiting on

### Planner

- `PLAN_READY_I1`, `PLAN_WAITING_I1`, `PLAN_I1_MANUAL`, `PLAN_I1_REPAIRED`
- `_evaluate_i1` — `READY_I1` requires every secondary row repriceable in-document **and** a positive proposed rate
- `READY_I1` added to `READY_STATUSES`
- `_evaluate_riv` — `VALUATION_INTEGRITY` with a known `i1_root` returns `WAITING_I1` naming that root; without one it remains `MANUAL`

### Failed RIV

- I1 distinguished from I4 inside `VALUATION_INTEGRITY`
- New `i1_root` field on every classified row

### Dashboard / UI

- New topic tab **I1 Negative Rate**
- KPIs: `I1 Negative Rate`, `READY_I1`, `WAITING_I1`, `MANUAL_I1`
- Canonical buckets `i1_ready` / `i1_waiting` / `i1_manual` / `i1_all` in `kpi_buckets.py`
- **Repair I1 Negative Rate** action, gated behind Dry Run + Impact Analysis with an explicit backup confirmation

---

## Safety

- Never `abs()` or clamp a negative rate
- Never price a secondary inbound from live Bin or warehouse valuation
- Never repair the FG rate without correcting its cause
- Refuses to write when the corrected pool is still not positive (`MANUAL_I1`)
- Refuses multi-finished-good vouchers — the single-FG pool contract does not apply
- Post-write gate: the voucher must carry no negative inbound SLE, or the apply raises and rolls back
- Identity-scoped replay only; RIV is never invoked by the repair

---

## Testing

| Suite | Coverage |
|-------|----------|
| **Unit** | `test_i1_repair_v5220.py` — weighted issue rates, READY reprice shape, WAITING when no in-document source, WAITING on zero issue rate, MANUAL on multi-FG, MANUAL when corrected pool stays negative, NOT_I1 guards, planner READY/WAITING routing, Failed RIV → `WAITING_I1` |

Run with:

```bash
bench --site <site> run-tests --app erpnext_extensions \
  --module erpnext_extensions.iran_accounting.tests.test_i1_repair_v5220
```

---

## Known Limitations

`WAITING_I1` is the expected majority on a tenant whose warehouse valuation is still poisoned — a by-product received but not issued in its own document has no in-document rate source. Those rows clear only after Zero / Wrong Rate repair upstream, then re-scan.

The apply path is **not yet production-proven**. `master_plan` reports `promotion_status: NOT_PROVEN` for this class. Prove it on a restored copy before applying on production.

---

## Upgrade Notes

1. **Backup** the site database
2. **Deploy** `erpnext_extensions` v5.2.20
3. `bench migrate`
4. **Restart** web and workers
5. **Scan All**, then open **I1 Negative Rate**
6. **Validate Dashboard** — proceed only when KPIs PASS
7. **Dry Run** one `READY_I1` voucher and read the preview chain
8. **Impact** analysis
9. **Repair** that single voucher, then Integrity Check → Rescan
10. Only then widen to the remaining `READY_I1` rows
11. Retry Failed RIV once the identity is healthy

---

## Version metadata

| Location | Value |
|----------|-------|
| `erpnext_extensions/__init__.py` | `__version__ = "5.2.20"` |
| Master Repair Plan / Campaign Wizard / Assisted Master Plan payloads | `"version": "5.2.20"` |

---

## Publish checklist (operator)

- [ ] Working tree clean after this preparation commit  
- [ ] Apply path proven on a restored copy before production use  
- [ ] Backup completed before deploy  
- [ ] Push / tag / publish only after explicit approval  

**This document is release preparation only. Do not push, tag, or publish until the operator authorizes it.**

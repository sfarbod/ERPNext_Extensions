# ERPNext Extensions v5.2.19

**Version:** `5.2.19`  
**Previous:** `5.2.18`  
**App:** `erpnext_extensions`  
**Focus:** Historical Stock Integrity & Repair — correctness, KPI consistency, async Scan All, and production safety.

This release packages the Historical Repair maturity work already implemented after the v5.2.18 baseline: dashboard/grid consistency, canonical KPI buckets, async Scan All, topic routing isolation, Warehouse / Wrong Rate / Zero Rate / Posting Order / GL / Failed RIV hardening, and release-gate tests.

It does **not** claim automatic clearance of every remaining anomaly. Unsafe, ambiguous, and shortage cases remain operator-gated by design.

---

## Overview

v5.2.19 focuses on:

- Historical Stock Integrity & Repair maturity  
- Dashboard correctness  
- KPI consistency (exact sub-bucket membership)  
- Async Scan All (long-queue jobs)  
- Warehouse Engine  
- Wrong Rate engine  
- Zero Rate engine  
- Assisted Recovery  
- Posting Order improvements  
- Production safety  
- Release readiness (unit, integration, Playwright, Validate Dashboard)

---

## What's New

### Historical Repair

- **Async Scan All** — enqueue on the long queue with UI polling (`start_scan_all_job` / `get_scan_all_job`); avoids HTTP timeouts on production-size tenants  
- **Dashboard consistency** — `topic_expectations` contract; Dashboard KPI ↔ topic Scan ↔ grid load-state  
- **Topic routing fixes** — dedicated Wrong Rate tab; Zero / Wrong / Posting repair APIs stay isolated  
- **KPI bucket architecture** — backend-owned `kpi_buckets.py` as single source of truth  
- **Active vs Complete Wrong Rate** — main Wrong Rate KPI excludes `RATE_REPAIR_COMPLETE`; new **Wrong Rate Complete** chip  
- **Exact KPI filtering** — card clicks apply `kpi_bucket` server filters (no substring search)  
- **Master Repair Plan** improvements — per-class roadmap from live scans (read-only)

### Warehouse Engine

- Multi-pair campaigns  
- Dependency graph  
- Campaign optimizer  
- Replay safety  
- Savepoints  
- Warehouse planner + Warehouse Plan UI (dry-run path)

### Wrong Rate Engine

- Canonical bucket taxonomy (`ready` / `waiting` / `manual` / `complete` / `replay`)  
- Assisted recovery paths  
- Patient-zero improvements  
- External PZ detection  
- Replay validation  
- Selective SVD replay  
- Already-valued / `RATE_REPAIR_COMPLETE` detection (excluded from active problem KPIs)

### Zero Rate

- SAFE_GROUP campaign clustering  
- Dependency repair ordering  
- Replay improvements  
- Cluster Explorer (Zero Rate–scoped)

### Posting Order

- PRE-aware replay anchors  
- Warehouse campaigns integration  
- Multi-move planner  
- Ambiguity detection  
- Shortage classification (`REAL_STOCK_SHORTAGE` and related optimizer statuses)  
- Replay Downstream / Rebuild Affected Documents gated to Posting Order topic

### GL

- Balanced-map validation  
- Currency precision validation  
- Safer rebuild rules (refuse unbalanced / precision-unsafe READY)

### Failed RIV

- Better classifications  
- Waiting buckets (`WAITING_*`)  
- Safer retry rules (`SAFE_TO_RETRY` only; UNKNOWN never blindly retried)

---

## UI Improvements

- **Wrong Rate** topic tab (separate from Zero / Lost Rate)  
- **Wrong Rate Complete** dashboard KPI  
- **Active KPI filter** banner (visible bucket + clear control)  
- Dashboard ↔ Grid consistency contract (“Rows are not loaded yet / Click Scan”)  
- **Warehouse Plan** button wired to plan / dry-run APIs  
- **Campaign Wizard**  
- **Cluster Explorer** (Zero Rate SAFE groups)  
- **Validate Dashboard** / **Rebuild Metrics**

---

## Safety Improvements

- Backend-owned KPI taxonomy (no duplicated status lists in JS)  
- Removal of substring client filtering for KPI cards (which previously leaked COMPLETE into MANUAL via `required_action` text containing “manual”)  
- Exact KPI bucket filtering on Scan APIs  
- Deterministic planner statuses  
- No automatic writes for MANUAL / AMBIGUOUS / OPERATOR_DECISION  
- Stronger replay and GL rebuild gates  
- Company-control bootstrap no longer wipes async Scan All results  
- Wrong Rate Dry Run refuses unselected full-scan dry-runs

---

## Performance

- Async Scan All on long workers  
- Long-queue support and job deduplication  
- Improved UI responsiveness (poll instead of blocking HTTP)  
- Selective replay / reduced unnecessary GL rebuilds  
- Topic grids load on demand after Scan All (dashboard snapshot first)

---

## Testing

| Suite | Coverage |
|-------|----------|
| **Unit** | Async Scan All jobs; KPI buckets; UI routing; dashboard↔topic contract; Wrong/Zero/GL dry-run isolation; warehouse campaign / solver |
| **Integration** | Scan All ↔ topic scan counts; Validate Dashboard `all_pass` |
| **Dashboard Consistency** | `dashboard_consistency_v5214` / Validate Dashboard matrix |
| **Playwright** | Dashboard↔grid consistency; release gate; KPI sub-bucket exactness (READY/WAITING/MANUAL never mix COMPLETE) |
| **Release Gate** | Routing, cache, filter, repair-target, and bucket reconciliation on restored production data |

---

## Known Limitations

Automatic repair intentionally stops when only the following remain:

- `REAL_STOCK_SHORTAGE`  
- `MANUAL` / `RATE_MANUAL`  
- `AMBIGUOUS` / `RATE_AMBIGUOUS`  
- `UNKNOWN`  
- `OPERATOR_DECISION`  

The engine **refuses unsafe repairs by design**. Remaining rows require operator evidence, inventory correction, or assisted/manual workflows.

Dashboard SLE chips (Broken Bin / I4 / SABB) remain documented splits of the combined SLE / Bin topic grid. Manufacture Valuation is not row-cached by default Scan All — use topic Scan.

---

## Upgrade Notes

Recommended production sequence:

1. **Backup** the site database (and files if required)  
2. **Deploy** `erpnext_extensions` v5.2.19  
3. `bench migrate`  
4. **Restart** web and workers (ensure a **long** queue worker is running for Scan All)  
5. **Async Scan All** (Historical Repair → Scan All)  
6. **Validate Dashboard** — proceed only when all KPIs PASS  
7. **Master Repair Plan** — review class priorities  
8. **Dry Run** on selected READY / SAFE_GROUP rows  
9. **Impact** analysis  
10. **Repair** only SAFE / READY groups; never bulk-apply MANUAL/AMBIGUOUS  

After each campaign: Integrity Check → Rescan → Validate Dashboard.

---

## Version metadata

| Location | Value |
|----------|-------|
| `erpnext_extensions/__init__.py` | `__version__ = "5.2.19"` |
| Master Repair Plan / Campaign Wizard / Assisted Master Plan payloads | `"version": "5.2.19"` |

---

## Publish checklist (operator)

- [ ] Working tree clean after this preparation commit  
- [ ] Long worker available in production  
- [ ] Backup completed before deploy  
- [ ] Push / tag / publish only after explicit approval  

**This document is release preparation only. Do not push, tag, or publish until the operator authorizes it.**

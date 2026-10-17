# Production Runbook — HRC-00001 In-place RIV JSON Recovery (5.5.49)

**Site:** `erp.espadpharmed.com`  
**Required app version:** `erpnext_extensions` **5.5.49**  
**Campaign:** `HRC-00001` (do **not** recreate; do **not** rebuild manifest)  
**Database restore:** **FORBIDDEN**

This runbook recovers the **current** Production database in place.

---

## 0. Preconditions (must all pass)

Run on Production **before** any mutate:

```bash
SITE=erp.espadpharmed.com

# Version
docker compose exec backend bench --site $SITE list-apps | grep erpnext_extensions
# MUST show 5.5.49 — stop if not

# Campaign exists and is the paused/near-complete campaign
docker compose exec backend bench --site $SITE mariadb -N -e "
SELECT name,status,total_documents,manifest_checksum,pause_requested
FROM \`tabHistorical Rebuild Campaign\` WHERE name='HRC-00001';
SELECT item_status, COUNT(*) FROM \`tabHistorical Rebuild Campaign Item\`
 WHERE campaign='HRC-00001' GROUP BY item_status;
SELECT status, COUNT(*) FROM \`tabRepost Item Valuation\` WHERE docstatus=1 GROUP BY status;
"

# Dry-run recovery (read-only mutate path — no RIV execute)
docker compose exec backend bench --site $SITE execute \
  erpnext_extensions.iran_accounting.historical_rebuild.riv_file_recovery.recover_campaign_json_failures \
  --kwargs '{"campaign":"HRC-00001","dry_run":True}'
```

**Expected dry-run shape (counts may differ if Production moved):**
- `ok: true`, `dry_run: true`
- `recoverable` ≥ 0 (typically 3 for the known JSON failures)
- Candidates are Failed Item+WH RIVs only

**STOP if:**
- App version ≠ 5.5.49
- Campaign `HRC-00001` missing
- Dry-run fails to import / throws
- You intended a different campaign name

---

## 1. Backup (mandatory)

Take a **fresh Production database backup** before recovery (your normal ops procedure).  
Do not proceed without a restore point independent of the 20261017 investigation dump.

---

## 2. Maintenance / scheduler / workers

**Current Production posture (as of this task):** Maintenance Mode is **OFF**, users are online.
Enable MM only for the mutating recovery window (not for Dry Run / audits).

```bash
# Only when starting actual recovery (after backup + dry-run OK):
docker compose exec backend bench --site $SITE set-maintenance-mode on
docker compose exec backend bench --site $SITE disable-scheduler

# Workers MUST remain running (long queue executes RIV if enqueue path used;
# this recovery uses sync execute_reposting_entry by default).
docker compose ps queue-long queue-short backend
# Equivalent container name on many hosts: erp-backend-1
# docker exec erp-backend-1 bench --site $SITE ...
```

---

## 3. Exact recovery command (mutating)

```bash
docker compose exec backend bench --site $SITE execute \
  erpnext_extensions.iran_accounting.historical_rebuild.riv_file_recovery.recover_campaign_json_failures \
  --kwargs '{"campaign":"HRC-00001","execute":True,"dry_run":False}'
```

**What it does:**
1. Lists Failed Item+WH RIVs with JSON/file failure class only
2. Detaches File rows attached to **those** RIVs
3. Clears `reposting_data_file`, sets status Queued, clears error_log
4. Core `execute_reposting_entry` (no status force to Completed)
5. Releases Blocked campaign items whose SLE identities have no Failed RIV
6. `bulk_skip_covered` for released items
7. Marks campaign **Completed** only if pending=blocked=failed_riv=0

**What it never does:**
- Recreate campaign / manifest
- Delete Completed RIVs
- Direct SLE/GL UPDATEs
- Touch unrelated Queued/In Progress RIVs

---

## 4. Expected results

```text
ok: true
terminal_ok: true
status: Completed
failed_riv_remaining: 0
blocked_remaining: 0
pending_remaining: 0
skipped_covered: 15968   # or current total_documents
```

Each recovered RIV in `recovered[]` should show `"status": "Completed"`.

---

## 5. Verification commands

```bash
# Status API
docker compose exec backend bench --site $SITE execute \
  erpnext_extensions.iran_accounting.historical_rebuild.campaign.status \
  --kwargs '{"campaign":"HRC-00001"}'

# Coverage audit (read-only if fix=False)
docker compose exec backend bench --site $SITE execute \
  erpnext_extensions.iran_accounting.historical_rebuild.coverage_audit.audit_skipped_covered \
  --kwargs '{"campaign":"HRC-00001","fix":False}'

# Full reconcile (read-only)
docker compose exec backend bench --site $SITE execute \
  erpnext_extensions.iran_accounting.historical_rebuild.reconcile.full_reconcile \
  --kwargs '{"campaign":"HRC-00001"}'
```

**Financial spot-check (known vouchers):**

```bash
docker compose exec backend bench --site $SITE mariadb -N -e "
SELECT voucher_no, ROUND(SUM(stock_value_difference),2)
FROM \`tabStock Ledger Entry\`
WHERE is_cancelled=0 AND voucher_no IN ('MAT-PRE-2026-02002','MAT-STE-2026-41577')
GROUP BY voucher_no;
SELECT voucher_no, ROUND(SUM(debit-credit),2)
FROM \`tabGL Entry\`
WHERE is_cancelled=0 AND voucher_no IN ('MAT-PRE-2026-02002','MAT-STE-2026-41577')
  AND account LIKE '111604%'
GROUP BY voucher_no;
"
```

SLE SVD and inventory GL for these vouchers must remain balanced (dev: ±35,701,754).

---

## 6. Idempotent re-run

Safe to re-run the same recovery command. If already healthy:

```text
recovered: []
terminal_ok: true
```

---

## 7. Stop conditions (abort / escalate)

Stop and escalate if:
- Any recovered RIV returns `"status": "Failed"` again with a **non-JSON** error (scrap / I3 / valuation integrity)
- `failed_riv_remaining > 0` after one repair cycle of non-JSON failures
- `full_reconcile` shows Bin↔SLE latest delta ≠ 0
- SLE/GL for PRE/STE diverge after recovery
- Campaign total_documents changes unexpectedly (manifest was rebuilt — should not happen)

For non-JSON Failed RIVs use existing tools only:

```bash
# Component scrap / sticky rate / etc. — identity repair
docker compose exec backend bench --site $SITE execute \
  erpnext_extensions.iran_accounting.historical_rebuild.failure_registry.repair_all_failed_identities \
  --kwargs '{"campaign":"HRC-00001"}'
```

---

## 8. Rollback strategy

| Layer | Action |
|-------|--------|
| App code | Redeploy previous image tag (5.5.48) if needed |
| Data | Restore the **pre-recovery Production backup** taken in §1 |
| Partial RIV | Do **not** force status; re-run recovery or `repair_failed_identity` |

Do **not** roll back by restoring the 20261017 investigation dump onto Production unless ops explicitly chooses that disaster path (out of scope of this runbook’s safe path).

---

## 9. Return to normal operation

```bash
docker compose exec backend bench --site $SITE enable-scheduler
docker compose exec backend bench --site $SITE set-maintenance-mode off
docker compose exec backend bench --site $SITE version | grep erpnext_extensions
```

---

## 10. Compatibility with live Production drift

Live Production may have:
- Different Failed count than the 20261017 snapshot
- Newer stock documents after pause
- Different temporary files on disk

Recovery validates each Failed RIV’s preconditions at runtime. It only clears/re-executes recoverable JSON/file failures. Newer Completed RIVs are untouched.

---

## 11. Business-hours classification (MM currently OFF)

| Operation | Classification | Reason |
|-----------|----------------|--------|
| Deploy 5.5.49 app image / bench migrate | **CONDITIONALLY SAFE** | Code swap alone does not mutate stock; migrate may briefly lock schema. Prefer low-traffic deploy; avoid mid-submit windows. |
| Dry Run `recover_campaign_json_failures(dry_run=True)` | **SAFE ONLINE** | Read-only list of Failed RIVs; no RIV execute. |
| Actual recovery `execute=True` | **REQUIRES MAINTENANCE WINDOW** (recommended) / **CONDITIONALLY SAFE** only if Failed count stays ≈3 and identities are narrow | Sync `execute_reposting_entry` holds stock/GL locks for each identity; concurrent submits on same item+WH can fail or wait. Dev wall-clock for 3 RIVs was **~1s**, but Production may have deeper SLE chains → budget **1–15 min**. |
| Coverage Audit `fix=False` | **SAFE ONLINE** | Read-only SQL. |
| Full Reconciliation | **SAFE ONLINE** | Read-only aggregates; may be CPU-heavy (~few seconds to ~1 min) but non-mutating. |

**Recommendation:** Deploy after hours if possible; Dry Run anytime; run actual recovery with Maintenance Mode ON and scheduler disabled; keep workers up.

**Docker note:** Compose service is `backend` (container typically `erp-backend-1`). Prefer `docker compose exec backend …`. If your host only exposes the container name, `docker exec erp-backend-1 …` is equivalent.


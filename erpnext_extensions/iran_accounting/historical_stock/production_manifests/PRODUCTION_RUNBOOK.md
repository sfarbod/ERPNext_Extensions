# Historical Repair — Production Execution Runbook

**Manifest:** `HR-PROD-20260926-SEGMENTED-v1`  
**Rehearsal backup (authority only):** `20260926_090225-erp_espadpharmed_com-database.sql.gz`  
**Rollback backup:** a **new** Production backup taken immediately before mutation — never the 20260926 rehearsal file if Production has moved on.  
**Push:** do not push from this phase.

Working directory: `/workspace/development/frappe-bench`  
Site: `development.localhost` (rehearsal) / Production site name (live)  
User: bench/Administrator with Historical Repair write permission.

---

## PRE-FLIGHT (no mutation)

```bash
cd /workspace/development/frappe-bench
test -f apps/erpnext_extensions/erpnext_extensions/__init__.py
python3 -c "import pathlib; t=pathlib.Path('apps/erpnext_extensions/erpnext_extensions/__init__.py').read_text(); assert '__version__ = \"5.3.22\"' in t"
cd apps/erpnext_extensions && git rev-parse HEAD && git status --porcelain
cd /workspace/development/frappe-bench
bench --site SITE list-apps
bench --site SITE doctor
# workers: short,default,long must be online
```

Take **new** Production backups (database + files if required), then:

```bash
gzip -t PATH/TO/NEW_PRODUCTION_BACKUP.sql.gz
ls -l PATH/TO/NEW_PRODUCTION_BACKUP.sql.gz
```

Record timestamp and storage location in the audit log.

```bash
bench --site SITE execute erpnext_extensions.iran_accounting.historical_stock.api.scan_all --kwargs '{"company": "اسپاد فارمد دارو"}'
bench --site SITE execute erpnext_extensions.iran_accounting.historical_stock.production_execution.preflight --kwargs '{"manifest_id": "HR-PROD-20260926-SEGMENTED-v1"}'
bench --site SITE execute erpnext_extensions.iran_accounting.historical_stock.production_execution.dry_run_manifest --kwargs '{"manifest_id": "HR-PROD-20260926-SEGMENTED-v1"}'
```

**STOP** if preflight has CHANGED / MISSING / BLOCKED.  
**STOP** if dry-run planned mutate count is not explainable from MATCHED roots.  
Do not Apply.

---

## CHECKPOINT 0 — BEFORE MUTATION

| KPI | EXPECTED | PASS | STOP |
|---|---|---|---|
| I1 | 0 | =0 | >0 |
| Negative rates | 0 | =0 | >0 |
| New negative stock vs baseline | 0 new | ≤ baseline historical | any new |
| Broken GL | 0 or baseline | does not increase | increases |
| Broken Bin | baseline | recorded | n/a (rebuilt later) |
| Manufacturing qty fingerprint | recorded | delta 0 after every stage | any qty change |
| Manifest MATCHED | all eligible | MATCHED or ALREADY_REPAIRED only | CHANGED/MISSING/BLOCKED |
| Stock Adjustment account | 621301 | 621301 | any residual on 622515 |

---

## EXECUTION

```bash
# confirm string MUST equal manifest id — server refuses otherwise
bench --site SITE execute erpnext_extensions.iran_accounting.historical_stock.production_execution.apply_manifest --kwargs '{"manifest_id": "HR-PROD-20260926-SEGMENTED-v1", "confirm": "HR-PROD-20260926-SEGMENTED-v1"}'
```

Internal order (hard-coded): Posting Order allowlist → Leftover MA + strategy-dated RIV → Wrong Rate allowlist → SLE-authoritative Bin rebuild.

**FORBIDDEN (not in this command, do not run separately):**

- company-wide Repost Item Valuation
- Item+Warehouse RIV from earliest SLE
- Failed RIV retry
- Scan → repair all READY / drain

---

## CHECKPOINTS 1–5

After apply returns, read `sites/SITE/private/files/hr_prod_ready_0926/production_apply_audit.json`.

| Stage | PASS | STOP / ROLLBACK |
|---|---|---|
| 1 PO | I1=0, no new neg stock, mfg delta=0 | any hard gate |
| 2 WR/LMA | I1=0, 30470 still 621301, canaries stable | any hard gate |
| 3 strategy RIV | LMA RIV Completed; no open RIV created | I1>0 or GL poison |
| 4 Bin | Broken Bin=0 for synced rows | Bin rewrite without SLE tip |
| 5 Final Scan All | I1=0, Bin=0, GL=0, mfg delta=0 | unexplained new PZ / WR on allowlisted ids |

Then:

```bash
bench --site SITE execute erpnext_extensions.iran_accounting.historical_stock.api.scan_all --kwargs '{"company": "اسپاد فارمد دارو"}'
```

---

## ROLLBACK LEVELS

**L0** Dry-run / preflight mismatch — stop. No restore.  
**L1** Exception before `frappe.db.commit` in a single root — transaction abort. Re-scan.  
**L2** Committed document repair, no async RIV — if gates fail, restore the **new** pre-execution Production backup. Do not invent reverse repairs.  
**L3** Leftover-MA RIV started/finished — if gates fail, restore that same pre-execution Production backup. SQL rollback cannot undo RIV.

Post-rollback:

```bash
bench --site SITE list-apps
bench --site SITE execute erpnext_extensions.iran_accounting.historical_stock.api.scan_all --kwargs '{"company": "اسپاد فارمد دارو"}'
```

Confirm site up, I1/Bin/GL back to checkpoint 0, manufacturing fingerprint restored, latest business docs match the rollback backup point.

---

## AUDIT ARTIFACTS

Written under `sites/SITE/private/files/hr_prod_ready_0926/`:

- `production_preflight.json`
- `production_dry_run.json`
- `production_apply_audit.json`

Preserve them with the backup identity, git HEAD, and manifest sha256.

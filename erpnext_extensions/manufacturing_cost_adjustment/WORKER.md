# MCA Worker Architecture (Development / Production-ready)

## Purpose

Process submitted Manufacturing Cost Adjustment (MCA) documents through:

allocate → IRR persist → SLE remirror → contract → Transaction RIV → finalize Completed

with Iran Accounting runtime active and **concurrency 1** on the MCA queue to avoid Stock Entry / Batch deadlocks.

## Queues

| Work | Queue | Worker | Concurrency |
|---|---|---|---|
| `process_mca` orchestration | `mca` | dedicated MCA worker | **1** |
| Repost Item Valuation execute | `long` | long worker (also covers default/short) | ≥1 |
| Short/default jobs | `short` / `default` | short worker | ≥1 |

Constants (`constants.py`): `MCA_RQ_QUEUE="mca"`, `MCA_RIV_RQ_QUEUE="long"`, `MCA_JOB_TIMEOUT=3600`.

**Why split:** Core RIV already uses `long`. Parking RIV on `mca` blocked orchestration and left standard queues unconsumed when baseline workers were zombies.

---

## Application files (in this app — versioned with the release)

| Path | Role |
|---|---|
| `constants.py` | Queue names, timeouts, quarantine / permanent markers |
| `repost_engine.py` | `process_mca`, RIV kick → `long`, scheduler enqueue → `mca` |
| `worker_health.py` | RQ health probe (`queue_health_probe` / `mca_queue_health`) |
| `scripts/ensure_mca_worker.sh` | App-local copy of MCA worker start/stop/validate |
| `WORKER.md` | This document |
| `QUARANTINE_MCA_00004.md` | Quarantined MCA recovery / exclusion |

Install/copy the MCA worker script to the bench when deploying:

```bash
# From app tree → bench scripts (Development example)
cp apps/erpnext_extensions/erpnext_extensions/manufacturing_cost_adjustment/scripts/ensure_mca_worker.sh \
   scripts/ensure_mca_worker.sh
chmod +x scripts/ensure_mca_worker.sh
```

Prefer running the **bench** copy so `ROOT` resolves to the bench root (the app-local script also walks up to bench).

---

## Host / bench deployment configuration (not in app git by default)

| Path | Role |
|---|---|
| `frappe-bench/scripts/ensure_rq_workers.sh` | Baseline `short`/`default`/`long` workers; zombie detection |
| `frappe-bench/scripts/ensure_mca_worker.sh` | Installed MCA worker helper |
| `frappe-bench/sites/common_site_config.json` | Custom queue registration + write limit |
| Docker Compose `worker-short` / `worker-long` | Preferred when present; scripts fall back to local bench workers |

### Required `sites/common_site_config.json` keys

```json
"max_writes_per_transaction": 5000000,
"workers": {
  "mca": { "timeout": 3600 }
}
```

- `workers.mca` — Frappe only accepts custom RQ queues listed under `workers` (plus built-in short/default/long).
- `max_writes_per_transaction` — MCA allocate/rebuild exceeds the default write cap; `process_mca` also raises `frappe.db.MAX_WRITES_PER_TRANSACTION` at runtime.

---

## Development commands

From the **bench root** (`/workspace/development/frappe-bench` in DevContainer):

```bash
# Baseline workers (short/default/long) — heal zombies / start missing
./scripts/ensure_rq_workers.sh status
./scripts/ensure_rq_workers.sh restart   # only when healing; avoid during accounting pause

# MCA worker (concurrency 1)
./scripts/ensure_mca_worker.sh status
./scripts/ensure_mca_worker.sh start
./scripts/ensure_mca_worker.sh validate  # enqueues health probe on queue mca only
./scripts/ensure_mca_worker.sh stop
```

Do **not** run `validate` / enqueue / mass `process_mca` while a processing pause is in effect.

---

## Production commands (document only — do not execute from this session)

```bash
# On the Production bench host (after change control):
./scripts/ensure_rq_workers.sh status
./scripts/ensure_mca_worker.sh status

# If supervisors/systemd manage workers, align unit files to:
#   worker-short  → --queue short,default
#   worker-long   → --queue long,default,short
#   worker-mca    → --queue mca   (replicas=1)

# Site config must include workers.mca + max_writes_per_transaction as above.
# Deploy app release, then restart workers via the site's process manager —
# never from an unauthorized agent session.
```

---

## Runtime bootstrap

- RQ jobs: `hooks.before_job` → Iran `bootstrap.apply`
- `process_mca` / `_kick_riv_execution` also call `_ensure_iran_runtime()` for console/script safety
- Health probe proves Iran GL patch identity on the target queue

## Scheduling

`hooks.scheduler_events` cron `*/5` → `repost_engine.process_pending_mca_reposts`

- Enqueues onto `mca` with `job_id=mca-sched:{name}` (deduplicated)
- Skips `QUARANTINED_MCA_NAMES`
- Skips permanent failures (`negative pool_value`)
- Skips Failed without due `next_retry_at`
- SE locks prevent concurrent work on the same Stock Entry

## Do not

- Run multiple `mca` workers against the same site
- Blind-enqueue all submitted MCAs without excluding quarantine + permanent failures
- Bypass Iran ledger contract or force negative pools to zero
- Resume `MCA-2026-00004` without the written recovery plan

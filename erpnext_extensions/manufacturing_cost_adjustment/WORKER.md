# MCA Worker Architecture (Development / Production-ready)

## Purpose

Process submitted Manufacturing Cost Adjustment (MCA) documents through:

allocate → IRR persist → SLE remirror → contract → Transaction RIV → finalize Completed

with Iran Accounting runtime active. Independent Stock Entries may run in parallel on
standard Frappe `long` workers; same-SE exclusivity is enforced by Redis locks
(`locks.acquire_se_lock`).

## Queues

| Work | Queue | Worker | Concurrency |
|---|---|---|---|
| `process_mca` orchestration | `long` | long workers | ≥2 recommended |
| Repost Item Valuation execute | `long` | long workers | shared with MCA |
| Short/default jobs | `short` / `default` | dedicated replicas | ≥2 each |

Constants (`constants.py`): `MCA_RQ_QUEUE="long"`, `MCA_RIV_RQ_QUEUE="long"`,
`VALID_FRAPPE_QUEUES={"short","default","long"}`, `MCA_JOB_TIMEOUT=3600`.

**Important:** Frappe `frappe.enqueue` only accepts `short`, `default`, or `long`.
The previous custom queue name `mca` raised:

`Queue should be one of short, default, long`

Same-SE MCA and RIV remain serial via per-SE locks even though both use `long`.

---

## Application files (in this app — versioned with the release)

| Path | Role |
|---|---|
| `constants.py` | Queue names, timeouts, quarantine / permanent markers |
| `repost_engine.py` | `process_mca`, RIV kick → `long`, scheduler enqueue → `long` |
| `worker_health.py` | RQ health probe (`queue_health_probe` / `mca_queue_health`) |
| `scripts/ensure_mca_worker.sh` | Stops legacy dedicated `mca` workers; validate on `long` |
| `WORKER.md` | This document |
| `QUARANTINE_MCA_00004.md` | Quarantined MCA recovery / exclusion |

---

## Host / bench deployment configuration (not in app git by default)

| Path | Role |
|---|---|
| `frappe-bench/scripts/ensure_rq_workers.sh` | Multi-replica `short`/`default`/`long` workers |
| `frappe-bench/scripts/ensure_mca_worker.sh` | Legacy MCA helper (stop/validate only) |
| `frappe-bench/sites/common_site_config.json` | Worker timeouts + concurrency |

### Recommended `sites/common_site_config.json` keys

```json
"max_writes_per_transaction": 5000000,
"rq_worker_ttl": 7200,
"workers": {
  "short": { "timeout": 300, "concurrency": 2 },
  "default": { "timeout": 300, "concurrency": 2 },
  "long": { "timeout": 3600, "concurrency": 2 }
}
```

- Do **not** register a custom `mca` queue — it is invalid for `frappe.enqueue`.
- `max_writes_per_transaction` — MCA allocate/rebuild exceeds the default write cap.

---

## Development commands

From the **bench root** (`/workspace/development/frappe-bench` in DevContainer):

```bash
# Baseline workers (2× short, 2× default, 2× long) — heal zombies / start missing
./scripts/ensure_rq_workers.sh status
./scripts/ensure_rq_workers.sh restart

# Legacy MCA helper (stops old mca-only workers; validate health on long)
./scripts/ensure_mca_worker.sh status
./scripts/ensure_mca_worker.sh validate
./scripts/ensure_mca_worker.sh stop
```

Do **not** run `validate` / enqueue / mass `process_mca` while a processing pause is in effect.

---

## Production commands (document only — do not execute from this session)

```bash
# On the Production bench host (after change control):
./scripts/ensure_rq_workers.sh status
./scripts/ensure_mca_worker.sh status

# Align unit files / compose to:
#   worker-short-N  → --queue short
#   worker-default-N → --queue default
#   worker-long-N   → --queue long
```

Do **not** deploy custom `mca` queue workers.

---

## Scheduler / retry

- `process_pending_mca_reposts` enqueues onto `MCA_RQ_QUEUE` (`long`) with
  `job_id=mca-sched:{name}` (deduplicated).
- Transient failures (deadlock, 1020) remain retryable; permanent markers and
  quarantine are never auto-retried.

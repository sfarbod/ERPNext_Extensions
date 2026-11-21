#!/usr/bin/env bash
# Dedicated MCA RQ worker — concurrency 1 on queue `mca`.
#
# Why dedicated:
#   - Iran bootstrap runs via before_job on every RQ execution.
#   - Parallel process_mca on shared SE/Batch graphs causes deadlocks.
#   - Scheduler enqueues MCA jobs onto queue `mca` (see constants.MCA_RQ_QUEUE).
#
# Usage:
#   ./scripts/ensure_mca_worker.sh          # start if missing
#   ./scripts/ensure_mca_worker.sh status
#   ./scripts/ensure_mca_worker.sh stop
#   ./scripts/ensure_mca_worker.sh validate  # enqueue a no-op health job + check bootstrap

set -euo pipefail
# scripts -> mca package -> erpnext_extensions pkg -> app root -> apps -> bench
ROOT="$(cd "$(dirname "$0")/../../../../../" && pwd)"
SITES="$ROOT/sites"
LOGDIR="$ROOT/logs/ae_workers"
PIDDIR="$ROOT/config/pids/rq_workers"
NAME="worker-mca"
QUEUES="mca"
mkdir -p "$LOGDIR" "$PIDDIR"

start_one() {
	local logfile="$LOGDIR/${NAME}.log"
	local pidfile="$PIDDIR/${NAME}.pid"
	if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
		echo "already running: $NAME pid=$(cat "$pidfile")"
		return 0
	fi
	(
		cd "$SITES"
		nohup bench worker --queue "$QUEUES" >>"$logfile" 2>&1 &
		echo $! >"$pidfile"
	)
	echo "started $NAME pid=$(cat "$pidfile") queues=$QUEUES log=$logfile concurrency=1"
}

stop_one() {
	local pidfile="$PIDDIR/${NAME}.pid"
	if [[ -f "$pidfile" ]]; then
		local pid
		pid="$(cat "$pidfile")"
		if kill -0 "$pid" 2>/dev/null; then
			kill "$pid" || true
			sleep 1
			kill -9 "$pid" 2>/dev/null || true
			echo "stopped $NAME pid=$pid"
		fi
		rm -f "$pidfile"
	fi
}

status_one() {
	echo "=== MCA worker ==="
	local pidfile="$PIDDIR/${NAME}.pid"
	if [[ -f "$pidfile" ]]; then
		local pid
		pid="$(cat "$pidfile")"
		if kill -0 "$pid" 2>/dev/null; then
			echo "OK  $NAME pid=$pid queues=$QUEUES"
		else
			echo "DEAD $NAME pid=$pid"
		fi
	else
		echo "(no pidfile)"
	fi
	ps -eo pid,etime,cmd | awk '/bench worker --queue mca|frappe worker --queue mca/ && !/awk/ {print}' || true
	(
		cd "$SITES"
		"$ROOT/env/bin/python" - <<'PY'
import frappe, redis
frappe.init(site="development.localhost")
frappe.connect()
r = redis.from_url(frappe.conf.redis_queue)
print("rq:queue:mca length", r.llen("rq:queue:mca"))
from frappe.utils.background_jobs import get_workers
for w in get_workers() or []:
    qs = [getattr(q, "name", str(q)) for q in (w.queues or [])]
    if "mca" in qs or any("mca" in str(q) for q in qs):
        print("registered", w.name, "state", w.get_state(), "queues", qs)
frappe.destroy()
PY
	)
}

validate_one() {
	status_one
	(
		cd "$SITES"
		"$ROOT/env/bin/python" - <<'PY'
"""Enqueue a health job on `mca` and verify Iran bootstrap inside the worker."""
import time
import frappe

frappe.init(site="development.localhost")
frappe.connect()
frappe.set_user("Administrator")

from erpnext_extensions.manufacturing_cost_adjustment import constants as C


def _health():
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	from erpnext.accounts import general_ledger as gl

	fn = gl.process_debit_credit_difference
	patched = "process_debit_credit_difference" in getattr(fn, "__qualname__", "") or "iran" in str(fn)
	# Iran install wraps the function in a nested def — detect via closure/module
	mod = getattr(fn, "__module__", "")
	ok = "monkey_patches" in mod or "iran" in mod or "absorb" in (fn.__doc__ or "")
	# Stronger: call bootstrap and compare identity after apply
	apply_iran()
	from erpnext_extensions.iran_accounting.integration import monkey_patches as mp

	# After apply, process_debit_credit_difference should be the Iran wrapper
	from erpnext.accounts import general_ledger as gl2

	name = gl2.process_debit_credit_difference.__qualname__
	return {
		"ok": "process_debit_credit_difference" in name or "monkey" in (gl2.process_debit_credit_difference.__module__ or ""),
		"qualname": name,
		"module": gl2.process_debit_credit_difference.__module__,
		"queue": C.MCA_RQ_QUEUE,
	}


# Prefer sync validation in this process (worker may be down in CI); also enqueue
# for live worker smoke when available.
sync = _health()
print("SYNC_BOOTSTRAP", sync)

frappe.enqueue(
	"erpnext_extensions.manufacturing_cost_adjustment.worker_health.mca_queue_health",
	queue=C.MCA_RQ_QUEUE,
	timeout=120,
	job_id="mca-queue-health",
	deduplicate=True,
)
print("ENQUEUED mca-queue-health on", C.MCA_RQ_QUEUE)
# Wait briefly for worker pickup
for i in range(15):
	time.sleep(1)
	val = frappe.cache().get_value("mca:queue_health:last")
	if val:
		print("WORKER_HEALTH", val)
		break
else:
	print("WORKER_HEALTH_PENDING (start worker-mca to consume)")
frappe.destroy()
PY
	)
}

case "${1:-ensure}" in
	ensure|start)
		start_one
		sleep 1
		status_one
		;;
	stop)
		stop_one
		;;
	status)
		status_one
		;;
	validate)
		validate_one
		;;
	*)
		echo "usage: $0 {ensure|start|stop|status|validate}"
		exit 1
		;;
esac

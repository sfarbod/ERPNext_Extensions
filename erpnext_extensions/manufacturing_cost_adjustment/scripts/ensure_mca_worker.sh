#!/usr/bin/env bash
# Dedicated MCA RQ workers on queue `mca` — configurable concurrency.
#
# Independent Stock Entries may process in parallel. Same-SE exclusivity is
# enforced in-app via redis locks (locks.acquire_se_lock). RIV stays on ``long``.
#
# Concurrency (first match wins):
#   1. MCA_WORKER_CONCURRENCY env
#   2. common_site_config workers.mca.concurrency
#   3. default 3
#
# Usage:
#   ./scripts/ensure_mca_worker.sh          # start missing workers up to concurrency
#   ./scripts/ensure_mca_worker.sh status
#   ./scripts/ensure_mca_worker.sh stop
#   ./scripts/ensure_mca_worker.sh restart
#   ./scripts/ensure_mca_worker.sh validate

set -euo pipefail
# scripts -> mca package -> erpnext_extensions pkg -> app root -> apps -> bench
ROOT="$(cd "$(dirname "$0")/../../../../../" && pwd)"
# Prefer bench-root script when present (same behavior, clearer path)
BENCH_SCRIPT="$ROOT/scripts/ensure_mca_worker.sh"
if [[ -x "$BENCH_SCRIPT" ]] && [[ "$(cd "$(dirname "$0")" && pwd)" != "$(cd "$(dirname "$BENCH_SCRIPT")" && pwd)" ]]; then
	exec "$BENCH_SCRIPT" "$@"
fi

SITES="$ROOT/sites"
LOGDIR="$ROOT/logs/ae_workers"
PIDDIR="$ROOT/config/pids/rq_workers"
NAME_PREFIX="worker-mca"
QUEUES="mca"
mkdir -p "$LOGDIR" "$PIDDIR"

resolve_concurrency() {
	if [[ -n "${MCA_WORKER_CONCURRENCY:-}" ]]; then
		echo "$MCA_WORKER_CONCURRENCY"
		return
	fi
	local from_conf
	from_conf="$(
		cd "$SITES" && "$ROOT/env/bin/python" - <<'PY' 2>/dev/null || true
import json
p = "common_site_config.json"
try:
    c = json.load(open(p))
    w = (c.get("workers") or {}).get("mca") or {}
    print(int(w.get("concurrency") or 0) or "")
except Exception:
    print("")
PY
	)"
	if [[ -n "$from_conf" ]]; then
		echo "$from_conf"
		return
	fi
	echo 3
}

is_live_pid() {
	local pid="$1"
	[[ -n "$pid" ]] || return 1
	kill -0 "$pid" 2>/dev/null || return 1
	local stat
	stat="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d ' ' || true)"
	[[ "$stat" == Z* ]] && return 1
	local cmd
	cmd="$(ps -p "$pid" -o cmd= 2>/dev/null || true)"
	[[ "$cmd" == *"<defunct>"* ]] && return 1
	[[ "$cmd" == *"worker"* || "$cmd" == *"start_rq_worker"* ]] || return 1
	return 0
}

start_one() {
	local idx="$1"
	local name="${NAME_PREFIX}-${idx}"
	local logfile="$LOGDIR/${name}.log"
	local pidfile="$PIDDIR/${name}.pid"
	local starter="$ROOT/scripts/start_rq_worker.py"
	local ttl="${RQ_WORKER_TTL:-7200}"
	if [[ -f "$pidfile" ]]; then
		local pid
		pid="$(cat "$pidfile")"
		if is_live_pid "$pid"; then
			echo "already running: $name pid=$pid"
			return 0
		fi
		kill -9 "$pid" 2>/dev/null || true
		rm -f "$pidfile"
	fi
	(
		cd "$SITES"
		nohup "$ROOT/env/bin/python" "$starter" --queue "$QUEUES" --worker-ttl "$ttl" \
			>>"$logfile" 2>&1 &
		echo $! >"$pidfile"
	)
	sleep 1
	if is_live_pid "$(cat "$pidfile")"; then
		echo "started $name pid=$(cat "$pidfile") queues=$QUEUES ttl=$ttl"
	else
		echo "FAILED to start $name — see $logfile" >&2
		return 1
	fi
}

stop_all() {
	shopt -s nullglob
	for f in "$PIDDIR"/${NAME_PREFIX}.pid "$PIDDIR"/${NAME_PREFIX}-*.pid; do
		[[ -f "$f" ]] || continue
		local pid
		pid="$(cat "$f")"
		if kill -0 "$pid" 2>/dev/null; then
			kill "$pid" || true
			sleep 1
			kill -9 "$pid" 2>/dev/null || true
			echo "stopped $(basename "$f" .pid) pid=$pid"
		fi
		rm -f "$f"
	done
	pkill -f "start_rq_worker.py --queue mca" 2>/dev/null || true
}

status_all() {
	local n
	n="$(resolve_concurrency)"
	echo "=== MCA workers (target concurrency=$n) ==="
	shopt -s nullglob
	local any=0
	for f in "$PIDDIR"/${NAME_PREFIX}.pid "$PIDDIR"/${NAME_PREFIX}-*.pid; do
		[[ -f "$f" ]] || continue
		any=1
		local name pid
		name="$(basename "$f" .pid)"
		pid="$(cat "$f")"
		if is_live_pid "$pid"; then
			echo "OK  $name pid=$pid"
		else
			echo "DEAD $name pid=$pid"
		fi
	done
	(( any )) || echo "(no pidfiles)"
	ps -eo pid,etime,cmd | awk '/start_rq_worker.py --queue mca|frappe worker --queue mca/ && !/awk/ {print}' || true
	(
		cd "$SITES"
		"$ROOT/env/bin/python" - <<'PY'
import frappe
frappe.init(site="development.localhost")
frappe.connect()
from frappe.utils.background_jobs import get_workers
n = 0
for w in get_workers() or []:
    qs = [getattr(q, "name", str(q)) for q in (w.queues or [])]
    if any("mca" in str(q) for q in qs):
        n += 1
        print("registered", w.name, "pid", w.pid, "state", w.get_state(), "queues", qs)
print("mca_registered_workers", n)
frappe.destroy()
PY
	)
}

ensure_all() {
	local n i
	n="$(resolve_concurrency)"
	echo "ensuring MCA concurrency=$n"
	for ((i = 1; i <= n; i++)); do
		start_one "$i"
	done
	if [[ -f "$PIDDIR/${NAME_PREFIX}.pid" ]]; then
		local pid
		pid="$(cat "$PIDDIR/${NAME_PREFIX}.pid")"
		if is_live_pid "$pid"; then
			echo "note: legacy ${NAME_PREFIX}.pid still live pid=$pid"
		else
			rm -f "$PIDDIR/${NAME_PREFIX}.pid"
		fi
	fi
	status_all
}

case "${1:-ensure}" in
	ensure|start) ensure_all ;;
	restart) stop_all; sleep 1; ensure_all ;;
	stop) stop_all ;;
	status) status_all ;;
	validate)
		status_all
		(
			cd "$SITES"
			"$ROOT/env/bin/python" - <<'PY'
import frappe, time
frappe.init(site="development.localhost"); frappe.connect(); frappe.set_user("Administrator")
from erpnext_extensions.manufacturing_cost_adjustment import constants as C
frappe.enqueue(
    "erpnext_extensions.manufacturing_cost_adjustment.worker_health.mca_queue_health",
    queue=C.MCA_RQ_QUEUE, timeout=120, job_id="mca-queue-health", deduplicate=True,
)
frappe.db.commit()
for _ in range(20):
    time.sleep(0.5)
    val = frappe.cache().get_value("mca:queue_health:last")
    if val:
        print("WORKER_HEALTH", val); break
else:
    print("WORKER_HEALTH_PENDING")
frappe.destroy()
PY
		)
		;;
	*)
		echo "usage: $0 {ensure|start|restart|stop|status|validate}"
		exit 1
		;;
esac

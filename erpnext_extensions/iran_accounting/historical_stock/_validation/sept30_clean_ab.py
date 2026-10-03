# Copyright (c) 2026, ERPNext Extensions contributors
"""Clean Replay A/B after L6 — fresh restore + frozen plan only (no exploration)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from time import perf_counter

import frappe

from erpnext_extensions.iran_accounting.historical_stock._validation.sept30_deterministic_plan_v1 import (
	execute_plan,
	fingerprint,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.sept30_l6_full_repost import (
	_economic_fingerprint,
	run_l6,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_reconcile_0930 import (
	run_preflight,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
BACKUP = (
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/backups/20260930_093401-erp_espadpharmed_com-database.sql.gz"
)
EXPECTED_SHA256 = "55ee640d7ec149aa2eecf3f3138c21877ffa5a4dab46f3cf4d147bff1b9efd8d"


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def _sha256(path: str) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1024 * 1024), b""):
			h.update(chunk)
	return h.hexdigest()


def _run(cmd: list[str], timeout: int = 3600) -> dict:
	p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
	stdout = p.stdout if isinstance(p.stdout, str) else (p.stdout or b"").decode("utf-8", "replace")
	stderr = p.stderr if isinstance(p.stderr, str) else (p.stderr or b"").decode("utf-8", "replace")
	return {
		"cmd": cmd,
		"returncode": p.returncode,
		"stdout": stdout[-2000:] if stdout else "",
		"stderr": stderr[-2000:] if stderr else "",
	}


def _pause_riv_scheduler_jobs() -> dict:
	"""Stop dump-baked RIV scheduler jobs during Clean rehearsal (sync L6 owns exec)."""
	names = (
		"repost_item_valuation.repost_entries",
		"cbd5p88qfh",
		"e1eodrue7p",
	)
	stopped = []
	for name in names:
		try:
			if frappe.db.exists("Scheduled Job Type", name):
				frappe.db.set_value("Scheduled Job Type", name, "stopped", 1)
				stopped.append(name)
		except Exception:
			pass
	try:
		frappe.db.set_single_value("System Settings", "pause_scheduler", 1)
	except Exception:
		try:
			frappe.conf.pause_scheduler = 1
		except Exception:
			pass
	frappe.db.commit()
	return {"stopped_jobs": stopped, "pause_scheduler": 1}


def restore_authoritative() -> dict:
	"""DB-only restore of Sep-30 dump onto development.localhost + migrate."""
	sha = _sha256(BACKUP)
	gz = _run(["gzip", "-t", BACKUP], timeout=120)
	if gz["returncode"] != 0:
		return {"ok": False, "reason": "gzip_failed", "gzip": gz}
	restore = _run(
		[
			"bench",
			"--site",
			"development.localhost",
			"restore",
			BACKUP,
			"--force",
			"--db-root-password",
			"123",
		],
		timeout=1200,
	)
	if restore["returncode"] != 0:
		return {"ok": False, "reason": "restore_failed", "restore": restore, "sha256": sha}
	migrate = _run(
		["bench", "--site", "development.localhost", "migrate"],
		timeout=600,
	)
	_run(["bench", "--site", "development.localhost", "clear-cache"], timeout=120)
	return {
		"ok": migrate["returncode"] == 0,
		"backup": BACKUP,
		"sha256": sha,
		"expected_sha256_note": EXPECTED_SHA256,
		"restore": {"returncode": restore["returncode"]},
		"migrate": {"returncode": migrate["returncode"]},
	}


def run_clean(label: str = "A", *, include_l6: bool = True, include_l6_2: bool = True) -> dict:
	"""Fresh restore → infra report → frozen plan → settlement → L6#1/#2 → fingerprint.

	L6 uses foreground sync RIV (Workers=0 allowed for identity execution).
	Plan phases that still enqueue async work keep fail-closed worker preflight.
	Direct RIV SQL status mutation = 0.
	"""
	from erpnext_extensions.iran_accounting.historical_stock.worker_preflight import (
		WORKER_PREFLIGHT_BLOCKED,
		report_queue_status,
		require_worker_preflight,
	)
	from erpnext_extensions.iran_accounting.historical_stock.riv_settlement import (
		assert_campaign_queue_quiescent,
	)

	t0 = perf_counter()
	rest = restore_authoritative()
	if not rest.get("ok"):
		out = {"label": label, "status": "RESTORE_FAILED", "restore": rest}
		_dump(f"clean_{label.lower()}_result.json", out)
		return out

	frappe.connect()
	# Rehearsal: defer scheduler RIV dual-exec. L6 sync owns RIV under company GL lock;
	# background repost_entries is not required for sync identity execution.
	rehearsal_sched = _pause_riv_scheduler_jobs()
	# Rehearsal infrastructure snapshot (all queues). Plan still fail-closes on long.
	infra = report_queue_status(
		queues=("short", "default", "long"),
		phase=f"CLEAN_{label}:infra",
	)
	infra["riv_scheduler_pause"] = rehearsal_sched
	wp = require_worker_preflight(queues=("long",), require_probe=True, phase=f"CLEAN_{label}")
	if not wp.get("ready"):
		out = {
			"label": label,
			"status": WORKER_PREFLIGHT_BLOCKED,
			"worker_preflight": wp,
			"infra_report": infra,
			"restore": rest,
			"direct_lifecycle_overrides": 0,
		}
		_dump(f"clean_{label.lower()}_result.json", out)
		return out

	plan = execute_plan(stop_on_blocked=False)
	if plan.get("status") != "APPLIED":
		out = {
			"label": label,
			"status": "PLAN_FAILED",
			"plan_status": plan.get("status"),
			"plan": plan,
			"worker_preflight": wp,
			"infra_report": infra,
			"direct_lifecycle_overrides": 0,
		}
		_dump(f"clean_{label.lower()}_result.json", out)
		return out

	pre = run_preflight()
	l6 = None
	l6_2 = None
	if include_l6 and pre.get("ready"):
		l6 = run_l6(
			pass_name=f"CLEAN_{label}_L6",
			require_preflight=True,
			commit_every=25,
			require_worker_probe=False,
			sync_execution=True,
			max_db_concurrency_attempts=3,
		)
		if include_l6_2 and (l6 or {}).get("status") == "PASS":
			l6_2 = run_l6(
				pass_name=f"CLEAN_{label}_L6_2",
				require_preflight=False,
				commit_every=25,
				require_worker_probe=False,
				sync_execution=True,
				max_db_concurrency_attempts=3,
			)

	quiet = assert_campaign_queue_quiescent([], also_forbid_global_open=True)
	if not quiet.get("ready"):
		out = {
			"label": label,
			"status": "CAMPAIGN_QUEUE_NOT_QUIESCENT",
			"quiescence": quiet,
			"l6": l6,
			"l6_2": l6_2,
			"direct_lifecycle_overrides": 0,
		}
		_dump(f"clean_{label.lower()}_result.json", out)
		return out

	econ = _economic_fingerprint()
	fp = fingerprint()
	l6_ok = (not include_l6) or (l6 or {}).get("status") == "PASS"
	l6_2_ok = (not include_l6) or (not include_l6_2) or (l6_2 or {}).get("status") == "PASS"
	idem = {
		"sle_tip_match": ((l6 or {}).get("after_econ") or {}).get("sle_tip_sha256")
		== ((l6_2 or {}).get("after_econ") or {}).get("sle_tip_sha256"),
		"bin_match": ((l6 or {}).get("after_econ") or {}).get("bin_sha256")
		== ((l6_2 or {}).get("after_econ") or {}).get("bin_sha256"),
		"mfg_qty_match": ((l6 or {}).get("after_econ") or {}).get("mfg_qty_sum")
		== ((l6_2 or {}).get("after_econ") or {}).get("mfg_qty_sum"),
	}
	idempotent = (not include_l6) or (not include_l6_2) or all(idem.values())
	out = {
		"label": label,
		"status": "PASS"
		if plan.get("status") == "APPLIED"
		and pre.get("ready")
		and l6_ok
		and l6_2_ok
		and idempotent
		and quiet.get("ready")
		else "FAIL",
		"restore": rest,
		"worker_preflight": {"status": wp.get("status"), "ready": wp.get("ready"), "queues": wp.get("queues")},
		"plan_status": plan.get("status"),
		"plan_iran": plan.get("iran_summary"),
		"plan_i4_n": len(plan.get("i4") or []),
		"preflight": {"status": pre.get("status"), "ready": pre.get("ready"), "blockers": pre.get("blockers")},
		"l6": {
			"status": (l6 or {}).get("status"),
			"failed_n": (l6 or {}).get("failed_n"),
			"elapsed": (l6 or {}).get("elapsed"),
			"riv_settlement": (l6 or {}).get("riv_settlement"),
			"deadlock_metrics": (l6 or {}).get("deadlock_metrics"),
			"worker_preflight": (l6 or {}).get("worker_preflight"),
			"after_econ": (l6 or {}).get("after_econ"),
		}
		if l6
		else None,
		"l6_2": {
			"status": (l6_2 or {}).get("status"),
			"failed_n": (l6_2 or {}).get("failed_n"),
			"elapsed": (l6_2 or {}).get("elapsed"),
			"deadlock_metrics": (l6_2 or {}).get("deadlock_metrics"),
			"after_econ": (l6_2 or {}).get("after_econ"),
			"vs_l6_1": idem,
			"idempotency": "PASS" if idempotent else "FAIL",
		}
		if l6_2
		else None,
		"quiescence": quiet,
		"direct_lifecycle_overrides": 0,
		"infra_report": infra,
		"execution_semantics": "v5.3.47_atomic_riv_company_gl_retry",
		"fingerprint": fp,
		"econ": econ,
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump(f"clean_{label.lower()}_result.json", out)
	return out


def compare_ab() -> dict:
	a = json.loads((OUT / "clean_a_result.json").read_text())
	b = json.loads((OUT / "clean_b_result.json").read_text())
	cmp = {
		"sle_tip_match": (a.get("econ") or {}).get("sle_tip_sha256")
		== (b.get("econ") or {}).get("sle_tip_sha256"),
		"bin_match": (a.get("econ") or {}).get("bin_sha256") == (b.get("econ") or {}).get("bin_sha256"),
		"mfg_qty_match": (a.get("econ") or {}).get("mfg_qty_sum")
		== (b.get("econ") or {}).get("mfg_qty_sum"),
		"a_status": a.get("status"),
		"b_status": b.get("status"),
		"a_econ": a.get("econ"),
		"b_econ": b.get("econ"),
	}
	cmp["convergence"] = (
		"PASS"
		if cmp["sle_tip_match"]
		and cmp["bin_match"]
		and cmp["mfg_qty_match"]
		and cmp["a_status"] == "PASS"
		and cmp["b_status"] == "PASS"
		else "FAIL"
	)
	_dump("clean_ab_convergence.json", cmp)
	return cmp


def run_clean_a() -> dict:
	"""Entry for bench execute (no kwargs)."""
	return run_clean("A")


def run_clean_b() -> dict:
	"""Entry for bench execute (no kwargs)."""
	return run_clean("B")

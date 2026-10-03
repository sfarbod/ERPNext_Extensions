# Copyright (c) 2026, ERPNext Extensions contributors
"""L6 full native company historical repost (Development rehearsal only).

Requires FULL_REPOST_READY. Creates and executes Item+Warehouse RIV for every
SLE identity from its earliest posting datetime via CURRENT Iran/ERPNext native
``create_and_run_narrow_riv``.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_dev_waves_0930 import (
	_gates,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.sept30_deterministic_plan_v1 import (
	_canaries,
	fingerprint,
)
from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
	create_and_run_narrow_riv,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.preflight import (
	FULL_REPOST_READY,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_reconcile_0930 import (
	run_preflight,
)
from erpnext_extensions.iran_accounting.historical_stock.worker_preflight import (
	WORKER_PREFLIGHT_BLOCKED,
	report_queue_status,
	require_worker_preflight,
)
from erpnext_extensions.iran_accounting.historical_stock.riv_settlement import (
	CampaignRivRegistry,
	assert_campaign_queue_quiescent,
	wait_registry,
)

# L6 executes RIV synchronously (atomic + company GL lock). Long workers are
# NOT required for identity execution; they are only needed if a RIV remains
# Queued/In Progress and must settle asynchronously.
L6_SYNC_EXECUTION = True
L6_MAX_DB_CONCURRENCY_ATTEMPTS = 3

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def _economic_fingerprint() -> dict:
	"""Compact SLE/Bin tip fingerprint for idempotency / A-B compare."""
	import hashlib

	tips = frappe.db.sql(
		"""
		SELECT t.item_code, t.warehouse,
		       ROUND(t.qty_after_transaction,6) q,
		       ROUND(t.stock_value,2) sv,
		       ROUND(t.valuation_rate,2) vr
		FROM `tabStock Ledger Entry` t
		INNER JOIN (
			SELECT item_code, warehouse, MAX(posting_datetime) AS mx,
			       MAX(creation) AS mc
			FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0
			GROUP BY item_code, warehouse
		) m ON m.item_code=t.item_code AND m.warehouse=t.warehouse
		   AND t.posting_datetime=m.mx AND t.creation=m.mc
		WHERE t.is_cancelled=0
		ORDER BY t.item_code, t.warehouse
		""",
		as_dict=True,
	)
	seen = {(r.item_code, r.warehouse): (flt(r.q), flt(r.sv), flt(r.vr)) for r in tips}
	blob = json.dumps(
		[{"i": k[0], "w": k[1], "q": v[0], "sv": v[1], "vr": v[2]} for k, v in sorted(seen.items())],
		sort_keys=True,
	).encode()
	bins = frappe.db.sql(
		"""
		SELECT item_code, warehouse, ROUND(actual_qty,6) q,
		       ROUND(stock_value,2) sv, ROUND(valuation_rate,2) vr
		FROM `tabBin` ORDER BY item_code, warehouse
		""",
		as_dict=True,
	)
	bin_blob = json.dumps(
		[{"i": r.item_code, "w": r.warehouse, "q": flt(r.q), "sv": flt(r.sv), "vr": flt(r.vr)} for r in bins],
		sort_keys=True,
	).encode()
	return {
		"sle_tip_n": len(seen),
		"sle_tip_sha256": hashlib.sha256(blob).hexdigest(),
		"bin_n": len(bins),
		"bin_sha256": hashlib.sha256(bin_blob).hexdigest(),
		"mfg_qty_sum": flt(
			frappe.db.sql(
				"""
				SELECT ROUND(SUM(qty),6) FROM `tabStock Entry Detail` sed
				INNER JOIN `tabStock Entry` se ON se.name=sed.parent
				WHERE se.docstatus=1 AND se.purpose='Manufacture'
				"""
			)[0][0]
		),
	}


def _identities() -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT item_code AS item, warehouse,
		       DATE(MIN(posting_datetime)) AS posting_date,
		       TIME(MIN(posting_datetime)) AS posting_time,
		       COUNT(*) AS sle_n
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0
		GROUP BY item_code, warehouse
		ORDER BY item_code, warehouse
		""",
		as_dict=True,
	)


def _hard_gate_ok(gates: dict) -> bool:
	return (
		int(gates.get("i1") or 0) == 0
		and int(gates.get("neg_stock") or 0) == 0
		and int(gates.get("broken_bin") or 0) == 0
		and int(gates.get("broken_gl") or 0) == 0
		and int(gates.get("open_riv") or 0) == 0
	)


def run_l6(
	*,
	pass_name: str = "L6_1",
	require_preflight: bool = True,
	max_identities: int | None = None,
	commit_every: int = 25,
	require_worker_probe: bool = False,
	sync_execution: bool = L6_SYNC_EXECUTION,
	max_db_concurrency_attempts: int = L6_MAX_DB_CONCURRENCY_ATTEMPTS,
) -> dict:
	"""Full company Item+Warehouse native RIV — sync atomic + settlement.

	Direct SQL mutation of RIV status is forbidden. Campaign RIVs are tracked and
	must reach Completed via native execute; open/Skipped stops the run.

	When ``sync_execution=True`` (default), L6 does not require long-queue
	listeners for identity execution. Queue status is still reported. Workers
	are required only if settlement sees open Queued/In Progress RIVs.
	"""
	t0 = perf_counter()
	if sync_execution:
		wp = report_queue_status(
			queues=("short", "default", "long"),
			phase=f"L6:{pass_name}",
		)
		if not wp.get("ready"):  # Redis down
			out = {
				"pass": pass_name,
				"status": WORKER_PREFLIGHT_BLOCKED,
				"worker_preflight": wp,
				"elapsed": round(perf_counter() - t0, 3),
			}
			_dump(f"{pass_name.lower()}_blocked.json", out)
			return out
	else:
		wp = require_worker_preflight(
			queues=("long",),
			require_probe=require_worker_probe,
			phase=f"L6:{pass_name}",
		)
		if not wp.get("ready"):
			out = {
				"pass": pass_name,
				"status": WORKER_PREFLIGHT_BLOCKED,
				"worker_preflight": wp,
				"elapsed": round(perf_counter() - t0, 3),
			}
			_dump(f"{pass_name.lower()}_blocked.json", out)
			return out

	if require_preflight:
		pre = run_preflight()
		if not pre.get("ready") or pre.get("status") != FULL_REPOST_READY:
			# Do NOT SQL-skip open RIV to force READY — surface infrastructure/state.
			out = {
				"pass": pass_name,
				"status": "BLOCKED_PREFLIGHT",
				"preflight": pre,
				"worker_preflight": wp,
			}
			_dump(f"{pass_name.lower()}_blocked.json", out)
			return out
	else:
		pre = {"status": "SKIPPED_BY_CALLER", "ready": True}

	registry = CampaignRivRegistry(operation_id=f"L6:{pass_name}")
	before_fp = fingerprint()
	before_econ = _economic_fingerprint()
	idents = _identities()
	if max_identities:
		idents = idents[: int(max_identities)]

	results = []
	failed = []
	batch_names: list[str] = []
	deadlock_metrics = {
		"deadlocks_encountered": 0,
		"db_1020_encountered": 0,
		"lock_wait_timeouts": 0,
		"retry_success": 0,
		"retry_exhaustion": 0,
		"events": [],
	}
	for i, row in enumerate(idents, 1):
		# Sync path: report-only mid-run. Async path: fail-closed on worker loss.
		if i == 1 or i % max(commit_every, 1) == 0:
			if sync_execution:
				wp_mid = report_queue_status(
					queues=("short", "default", "long"),
					phase=f"L6:{pass_name}:mid:{i}",
				)
				if not wp_mid.get("ready"):
					out = {
						"pass": pass_name,
						"status": WORKER_PREFLIGHT_BLOCKED,
						"at_identity": i,
						"worker_preflight": wp_mid,
						"failed_n": len(failed),
						"registry_n": len(registry.names()),
						"deadlock_metrics": deadlock_metrics,
						"elapsed": round(perf_counter() - t0, 3),
					}
					_dump(f"{pass_name.lower()}_blocked.json", out)
					return out
			else:
				wp_mid = require_worker_preflight(
					queues=("long",),
					require_probe=False,
					phase=f"L6:{pass_name}:mid:{i}",
				)
				if not wp_mid.get("ready"):
					out = {
						"pass": pass_name,
						"status": WORKER_PREFLIGHT_BLOCKED,
						"at_identity": i,
						"worker_preflight": wp_mid,
						"failed_n": len(failed),
						"registry_n": len(registry.names()),
						"elapsed": round(perf_counter() - t0, 3),
					}
					_dump(f"{pass_name.lower()}_blocked.json", out)
					return out
		try:
			res = create_and_run_narrow_riv(
				row.item,
				row.warehouse,
				posting_date=row.posting_date,
				posting_time=row.posting_time or "00:00:00",
				allow_zero_rate=True,
				allow_negative_stock=True,
				atomic=True,
				exclusive_company_gl=True,
				max_db_concurrency_attempts=int(max_db_concurrency_attempts),
			)
			# Capture concurrency retry telemetry (does not alter economic rules).
			for att in res.get("attempts") or []:
				rr = att.get("retry_reason")
				if rr == "DB_DEADLOCK_RETRY":
					deadlock_metrics["deadlocks_encountered"] += 1
				elif rr == "DB_1020_RETRY":
					deadlock_metrics["db_1020_encountered"] += 1
				elif rr == "DB_LOCK_TIMEOUT_RETRY":
					deadlock_metrics["lock_wait_timeouts"] += 1
				if rr and not att.get("ok"):
					deadlock_metrics["events"].append(
						{
							"identity": i,
							"item": row.item,
							"warehouse": row.warehouse,
							"attempt": att.get("attempt"),
							"retry_reason": rr,
							"error": (att.get("error") or "")[:300],
						}
					)
			if res.get("ok") and int(res.get("attempt_n") or 1) > 1:
				deadlock_metrics["retry_success"] += 1
			if (not res.get("ok")) and res.get("status") == "DB_CONCURRENCY_EXHAUSTED":
				deadlock_metrics["retry_exhaustion"] += 1

			riv_name = res.get("riv") or res.get("riv_name") or res.get("name")
			if riv_name:
				registry.register(
					riv_name,
					item=row.item,
					warehouse=row.warehouse,
					phase=pass_name,
				)
				batch_names.append(riv_name)
			ok = bool(res.get("ok"))
			riv_status = str(res.get("riv_status") or res.get("status") or "")
			results.append(
				{
					"item": row.item,
					"warehouse": row.warehouse,
					"status": riv_status or ("OK" if ok else "UNKNOWN"),
					"riv": riv_name,
					"attempt_n": res.get("attempt_n"),
				}
			)
			if not ok or riv_status.upper() in ("FAILED", "ERROR", "QUEUED", "IN PROGRESS"):
				failed.append({"item": row.item, "warehouse": row.warehouse, "result": res})
				out = {
					"pass": pass_name,
					"status": "STOPPED_RIV_NOT_COMPLETED",
					"at_identity": i,
					"failed_n": len(failed),
					"failed_sample": failed[:5],
					"worker_preflight": wp,
					"deadlock_metrics": deadlock_metrics,
					"elapsed": round(perf_counter() - t0, 3),
				}
				_dump(f"{pass_name.lower()}_stopped.json", out)
				return out
		except Exception as exc:  # noqa: BLE001
			failed.append({"item": row.item, "warehouse": row.warehouse, "error": str(exc)[:400]})
			results.append({"item": row.item, "warehouse": row.warehouse, "status": "EXCEPTION"})
			out = {
				"pass": pass_name,
				"status": "STOPPED_EXCEPTION",
				"at_identity": i,
				"failed_sample": failed[:5],
				"deadlock_metrics": deadlock_metrics,
				"elapsed": round(perf_counter() - t0, 3),
			}
			_dump(f"{pass_name.lower()}_stopped.json", out)
			return out
		if i % commit_every == 0:
			frappe.db.commit()
			# Campaign-scoped settlement only — never SQL-skip open RIV.
			# Sync Completed RIVs settle without workers; require workers only if open.
			tmp = CampaignRivRegistry(f"L6:{pass_name}:batch:{i}")
			for n in batch_names:
				tmp.register(n)
			barrier = wait_registry(
				tmp,
				timeout_s=120.0,
				poll_s=0.5,
				require_workers=not sync_execution,
			)
			batch_names = []
			if not barrier.get("ready"):
				out = {
					"pass": pass_name,
					"status": "RIV_SETTLEMENT_BLOCKED",
					"at_identity": i,
					"barrier": barrier,
					"deadlock_metrics": deadlock_metrics,
					"elapsed": round(perf_counter() - t0, 3),
				}
				_dump(f"{pass_name.lower()}_stopped.json", out)
				return out
			gates = _gates()
			gate_ok = (
				int(gates.get("i1") or 0) == 0
				and int(gates.get("neg_stock") or 0) == 0
				and int(gates.get("broken_bin") or 0) == 0
				and int(gates.get("broken_gl") or 0) == 0
			)
			if not gate_ok:
				out = {
					"pass": pass_name,
					"status": "STOPPED_HARD_GATE",
					"at_identity": i,
					"gates": gates,
					"failed_n": len(failed),
					"failed_sample": failed[:5],
					"before_econ": before_econ,
					"deadlock_metrics": deadlock_metrics,
					"elapsed": round(perf_counter() - t0, 3),
				}
				_dump(f"{pass_name.lower()}_stopped.json", out)
				return out
			_dump(
				f"{pass_name.lower()}_progress.json",
				{
					"done": i,
					"total": len(idents),
					"failed_n": len(failed),
					"gates": gates,
					"deadlock_metrics": {
						k: deadlock_metrics[k]
						for k in (
							"deadlocks_encountered",
							"db_1020_encountered",
							"lock_wait_timeouts",
							"retry_success",
							"retry_exhaustion",
						)
					},
				},
			)

	frappe.db.commit()
	if batch_names:
		tmp = CampaignRivRegistry(f"L6:{pass_name}:final_batch")
		for n in batch_names:
			tmp.register(n)
		barrier = wait_registry(
			tmp,
			timeout_s=300.0,
			poll_s=0.5,
			require_workers=not sync_execution,
		)
		if not barrier.get("ready"):
			out = {
				"pass": pass_name,
				"status": "RIV_SETTLEMENT_BLOCKED",
				"barrier": barrier,
				"deadlock_metrics": deadlock_metrics,
				"elapsed": round(perf_counter() - t0, 3),
			}
			_dump(f"{pass_name.lower()}_stopped.json", out)
			return out

	final_barrier = wait_registry(
		registry,
		timeout_s=300.0,
		poll_s=0.5,
		require_workers=not sync_execution,
	)
	if not final_barrier.get("ready"):
		out = {
			"pass": pass_name,
			"status": "RIV_SETTLEMENT_BLOCKED",
			"barrier": final_barrier,
			"deadlock_metrics": deadlock_metrics,
			"elapsed": round(perf_counter() - t0, 3),
		}
		_dump(f"{pass_name.lower()}_stopped.json", out)
		return out

	quiet = assert_campaign_queue_quiescent(
		registry.names(),
		also_forbid_global_open=True,
	)
	if not quiet.get("ready"):
		out = {
			"pass": pass_name,
			"status": "CAMPAIGN_QUEUE_NOT_QUIESCENT",
			"quiescence": quiet,
			"worker_preflight": wp,
			"deadlock_metrics": deadlock_metrics,
			"elapsed": round(perf_counter() - t0, 3),
		}
		_dump(f"{pass_name.lower()}_stopped.json", out)
		return out

	after_fp = fingerprint()
	after_econ = _economic_fingerprint()
	gates = _gates()
	canaries = _canaries()
	status = "PASS" if _hard_gate_ok(gates) and not failed else "FAIL"
	out = {
		"pass": pass_name,
		"status": status,
		"identity_n": len(idents),
		"completed_n": sum(1 for r in results if r.get("status") not in ("EXCEPTION", "FAILED", "ERROR")),
		"failed_n": len(failed),
		"failed_sample": failed[:10],
		"gates": gates,
		"canaries": canaries,
		"worker_preflight": {
			"status": wp.get("status"),
			"ready": wp.get("ready"),
			"mode": wp.get("mode") or ("ASYNC" if not sync_execution else "FOREGROUND_SYNC_REPORT_ONLY"),
			"sync_execution": sync_execution,
		},
		"riv_settlement": {
			"status": final_barrier.get("status"),
			"tracked_n": final_barrier.get("tracked_n"),
			"ready": final_barrier.get("ready"),
		},
		"quiescence": quiet,
		"direct_lifecycle_overrides": 0,
		"deadlock_metrics": deadlock_metrics,
		"before_fingerprint": before_fp,
		"after_fingerprint": after_fp,
		"before_econ": before_econ,
		"after_econ": after_econ,
		"econ_sle_unchanged": before_econ["sle_tip_sha256"] == after_econ["sle_tip_sha256"],
		"econ_bin_unchanged": before_econ["bin_sha256"] == after_econ["bin_sha256"],
		"mfg_qty_unchanged": before_econ["mfg_qty_sum"] == after_econ["mfg_qty_sum"],
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump(f"{pass_name.lower()}_result.json", out)
	return out


def run_l6_idempotency() -> dict:
	"""L6 #2 — second full native repost; expect no material economic drift."""
	first_econ = None
	path = OUT / "l6_1_result.json"
	if path.exists():
		first_econ = json.loads(path.read_text()).get("after_econ")
	# Preflight may still be READY after L6#1
	second = run_l6(pass_name="L6_2", require_preflight=False)
	second["vs_l6_1"] = {
		"sle_tip_match": (first_econ or {}).get("sle_tip_sha256")
		== (second.get("after_econ") or {}).get("sle_tip_sha256"),
		"bin_match": (first_econ or {}).get("bin_sha256")
		== (second.get("after_econ") or {}).get("bin_sha256"),
		"mfg_qty_match": (first_econ or {}).get("mfg_qty_sum")
		== (second.get("after_econ") or {}).get("mfg_qty_sum"),
	}
	if second.get("status") == "PASS" and all(second["vs_l6_1"].values()):
		second["idempotency"] = "PASS"
	else:
		second["idempotency"] = "FAIL"
	_dump("l6_2_result.json", second)
	return second


def probe_canary_index() -> dict:
	"""Locate canary identity ordinal in L6 identity list (diagnostic)."""
	idents = _identities()
	hits = []
	for i, row in enumerate(idents, 1):
		if row.item == "15010444" or (row.voucher_no if hasattr(row, "voucher_no") else None) == "MAT-PRE-2026-00793-1":
			hits.append({"i": i, "item": row.item, "warehouse": row.warehouse, "posting_date": str(row.posting_date), "posting_time": str(row.posting_time)})
		# also match by earliest sle voucher if present on row
	# Fallback: find by warehouse of canary
	wh = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": "MAT-PRE-2026-00793-1", "item_code": "15010444", "is_cancelled": 0},
		"warehouse",
	)
	for i, row in enumerate(idents, 1):
		if row.item == "15010444" and row.warehouse == wh:
			hits.append({"i": i, "item": row.item, "warehouse": row.warehouse, "posting_date": str(row.posting_date)})
			break
	return {"total": len(idents), "hits": hits, "warehouse": wh}

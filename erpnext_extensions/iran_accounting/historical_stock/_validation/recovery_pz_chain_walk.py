# Copyright (c) 2026 — walk WR/ZR patient-zero chains to true roots; classify repairability
from __future__ import annotations

import json
from collections import Counter, defaultdict


def run(*, max_depth=12, sample=40):
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row, READY_STATUSES
	import frappe

	company = "اسپاد فارمد دارو"
	wrong = scan_wrong_rates(company=company, limit=5000)
	rows = wrong.get("rows") or []
	by_v = {r.get("voucher"): r for r in rows if r.get("voucher")}

	def _pz(r):
		p = r.get("patient_zero")
		if isinstance(p, dict):
			return p.get("voucher_no")
		return p

	waiting = [r for r in rows if "WAITING" in str(r.get("planner_status") or "")]
	chains = []
	true_roots = Counter()
	root_meta = {}

	for r in waiting[: int(sample) * 5]:
		path = [r.get("voucher")]
		cur = r
		seen = {r.get("voucher")}
		for _ in range(int(max_depth)):
			pz = _pz(cur)
			if not pz or pz in seen:
				break
			path.append(pz)
			seen.add(pz)
			nxt = by_v.get(pz)
			if not nxt:
				# root not in scan — probe SLE / evaluate
				true_roots[f"OUT_OF_SCAN::{pz}"] += 1
				root_meta.setdefault(
					pz,
					{
						"kind": "OUT_OF_SCAN",
						"deps": [],
						"path_sample": path,
					},
				)
				root_meta[pz]["deps"].append(r.get("voucher"))
				break
			cur = nxt
			ps = str(cur.get("planner_status") or "")
			if ps in READY_STATUSES or ps.startswith("READY"):
				true_roots[f"READY::{pz}"] += 1
				break
			if "WAITING" not in ps:
				true_roots[f"{ps}::{pz}"] += 1
				root_meta.setdefault(
					pz,
					{
						"kind": ps,
						"conf": cur.get("confidence"),
						"source": cur.get("source") or cur.get("source_of_truth"),
						"expected": cur.get("expected") or cur.get("proposed_rate"),
						"current": cur.get("current") or cur.get("current_rate"),
						"eligible": cur.get("eligible"),
						"sql": cur.get("sql_updates"),
						"flags": cur.get("flags"),
						"reason": (cur.get("reason") or "")[:200],
						"item": cur.get("item"),
						"warehouse": cur.get("warehouse"),
						"deps": [],
						"path_sample": path,
					},
				)
				root_meta[pz]["deps"].append(r.get("voucher"))
				break
		else:
			true_roots["MAX_DEPTH"] += 1
		if len(chains) < int(sample):
			chains.append({"start": r.get("voucher"), "path": path, "end_ps": str((by_v.get(path[-1]) or {}).get("planner_status") or "OUT")})

	# Fresh-evaluate OUT_OF_SCAN and MANUAL/AMBIGUOUS EXACT-looking roots
	fresh = []
	for pz, meta in list(root_meta.items())[:30]:
		row = by_v.get(pz)
		if row:
			fr = evaluate_row(dict(row))
			fresh.append(
				{
					"pz": pz,
					"kind": meta.get("kind"),
					"stamped": row.get("planner_status"),
					"fresh_ps": fr.get("planner_status"),
					"fresh_eligible": fr.get("eligible"),
					"fresh_sql": fr.get("sql_updates"),
					"conf": fr.get("confidence") or row.get("confidence"),
					"source": fr.get("source") or row.get("source"),
					"expected": fr.get("expected") or row.get("expected"),
					"reason": (fr.get("reason") or "")[:180],
				}
			)
		else:
			# synthesize minimal probe from SLE
			sles = frappe.db.sql(
				"""
				SELECT name, item_code, warehouse, voucher_no, actual_qty, incoming_rate,
				       valuation_rate, stock_value_difference, qty_after_transaction, posting_date, posting_time
				FROM `tabStock Ledger Entry`
				WHERE voucher_no=%s AND is_cancelled=0
				ORDER BY posting_date, posting_time, creation
				LIMIT 8
				""",
				pz,
				as_dict=True,
			)
			fresh.append({"pz": pz, "kind": "OUT_OF_SCAN", "n_sle": len(sles), "sles": sles[:4]})

	# Classify roots for engine vs operator
	classifiable = []
	for pz, meta in root_meta.items():
		kind = meta.get("kind") or ""
		conf = meta.get("conf")
		src = meta.get("source")
		exp = float(meta.get("expected") or 0)
		blocker = "OPERATOR_DECISION"
		if kind.startswith("OUT") or kind == "OUT_OF_SCAN":
			blocker = "NO_EVIDENCE"
		elif conf == "EXACT" and exp > 0 and src and src not in ("manual", "bin", "scan_flag"):
			blocker = "ENGINE_LIMITATION_CHAIN_NOT_WALKED"
		elif conf == "AMBIGUOUS" or "AMBIGUOUS" in kind:
			blocker = "MATHEMATICAL_AMBIGUITY"
		elif conf == "LIKELY":
			blocker = "OPERATOR_DECISION"
		elif "MANUAL" in kind:
			blocker = "OPERATOR_DECISION"
		classifiable.append(
			{
				"pz": pz,
				"blocker": blocker,
				"kind": kind,
				"conf": conf,
				"source": src,
				"expected": meta.get("expected"),
				"n_deps": len(meta.get("deps") or []),
				"reason": meta.get("reason"),
				"item": meta.get("item"),
			}
		)

	by_blocker = Counter(c["blocker"] for c in classifiable)
	out = {
		"n_waiting": len(waiting),
		"true_root_counts": dict(true_roots.most_common(40)),
		"n_unique_roots_meta": len(root_meta),
		"by_blocker": dict(by_blocker),
		"engine_limited_roots": [c for c in classifiable if c["blocker"] == "ENGINE_LIMITATION_CHAIN_NOT_WALKED"],
		"ambiguous_roots": [c for c in classifiable if c["blocker"] == "MATHEMATICAL_AMBIGUITY"][:15],
		"operator_roots": [c for c in classifiable if c["blocker"] == "OPERATOR_DECISION"][:15],
		"no_evidence_roots": [c for c in classifiable if c["blocker"] == "NO_EVIDENCE"][:15],
		"fresh_sample": fresh[:20],
		"chain_samples": chains[:12],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out

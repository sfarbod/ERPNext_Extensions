
from __future__ import annotations
import frappe
from collections import Counter

def run(company=None):
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
	from erpnext_extensions.iran_accounting.historical_stock.root_graph import build_root_cause_graph

	company = company or "اسپاد فارمد دارو"
	i1 = scan_i1_negative_rate(company=company)
	i1_n = len(i1.get("rows") or i1.get("vouchers") or []) if isinstance(i1, dict) else 0
	if isinstance(i1, dict) and "count" in i1:
		i1_n = i1["count"]
	plan = build_master_repair_plan(company=company)
	classes = plan.get("classes") or {}
	class_summary = {}
	for name, blob in classes.items() if isinstance(classes, dict) else []:
		if not isinstance(blob, dict):
			class_summary[name] = blob
			continue
		class_summary[name] = {
			k: blob.get(k)
			for k in (
				"raw", "actionable", "findings", "finding_count", "unique_roots",
				"root_count", "patient_zero_count", "count", "rows",
			)
			if k in blob or k == "rows"
		}
		if "rows" in class_summary[name] and isinstance(blob.get("rows"), list):
			class_summary[name]["rows"] = len(blob["rows"])
	# root graph
	try:
		rg = build_root_cause_graph(company=company)
	except TypeError:
		rg = build_root_cause_graph(plan) if False else plan.get("root_cause_graph") or {}
	except Exception as exc:
		rg = {"error": str(exc)}

	rg_out = {}
	if isinstance(rg, dict):
		rg_out["keys"] = list(rg.keys())[:40]
		for k in ("finding_count", "unique_roots", "edge_count", "cycle_count"):
			if k in rg:
				rg_out[k] = rg[k]
		cls = rg.get("classes") or {}
		if isinstance(cls, dict):
			rg_out["classes"] = {
				n: ({k: v for k, v in b.items() if k != "rows"} if isinstance(b, dict) else b)
				for n, b in cls.items()
			}

	safety = frappe.db.sql("""
		SELECT
		  SUM(CASE WHEN valuation_rate < 0 THEN 1 ELSE 0 END) neg_val,
		  SUM(CASE WHEN incoming_rate < 0 THEN 1 ELSE 0 END) neg_in,
		  SUM(CASE WHEN actual_qty > 0 AND valuation_rate < 0 THEN 1 ELSE 0 END) neg_fg
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND company=%s
	""", (company,), as_dict=True)[0]
	return {
		"i1": {"count": i1_n, "keys": list(i1.keys())[:20] if isinstance(i1, dict) else type(i1).__name__},
		"classes": class_summary,
		"root_graph": rg_out,
		"safety": {
			"neg_valuation": int(safety.neg_val or 0),
			"neg_incoming": int(safety.neg_in or 0),
			"neg_fg": int(safety.neg_fg or 0),
			"active_riv": frappe.db.count("Repost Item Valuation", {"status": ("in", ["Queued", "In Progress"])}),
		},
		"blockers": {
			"open_shortage": frappe.db.count("Historical Repair Blocker", {"issue_type":"HISTORICAL_NEGATIVE_STOCK","status":"OPEN","lane":"USER_ACTION_REQUIRED"}),
			"open_user": frappe.db.count("Historical Repair Blocker", {"lane":"USER_ACTION_REQUIRED","status":"OPEN"}),
		},
	}

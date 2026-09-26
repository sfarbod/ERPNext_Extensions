# Copyright (c) 2026, ERPNext Extensions contributors
"""Development correction campaign on Production dump 20260926_225046.

Authoritative source: 20260926_225046-erp_espadpharmed_com-database.sql.gz
Frozen HR-PROD-20260926-SEGMENTED-v1 remains reference-only (not mutated).
"""

from __future__ import annotations

from pathlib import Path

from erpnext_extensions.iran_accounting.historical_stock._validation import (
	correction_campaign_155411 as prior,
)

BACKUP_ID = "20260926_225046"
BACKUP_PATH = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/backups/"
	"20260926_225046-erp_espadpharmed_com-database.sql.gz"
)
OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_225046"
)

prior.BACKUP_ID = BACKUP_ID
prior.BACKUP_PATH = BACKUP_PATH
prior.OUT = OUT


def backup_identity():
	return prior.backup_identity()


def pass0():
	return prior.pass0()


def revalidate_prior_canaries():
	return prior.revalidate_prior_canaries()


def preflight_baseline():
	return prior.preflight_baseline()


def apply_baseline():
	return prior.apply_baseline()


def apply_36853_i4():
	return prior.apply_36853_i4()


def i4_leftover_math():
	return prior.i4_leftover_math()


def forensics_36933():
	return prior.forensics_36933()


def forensics_37090():
	return prior.forensics_37090()


def forensics_25274():
	return prior.forensics_25274()


def scan_scrap_fg_population():
	return prior.scan_scrap_fg_population()


def canary_riv_36933_healthy():
	return prior.canary_riv_36933_healthy()


def riv_canary_13200001():
	return prior.riv_canary_13200001()


def post_scan():
	return prior.post_scan()


def failed_riv_reeval():
	return prior.failed_riv_reeval()


def scan_negative_fg():
	return prior.scan_negative_fg()


def snapshot_36933():
	return prior.snapshot_36933()


def reconstruct_37090_class():
	return prior.reconstruct_37090_class()


def run_clean_final_replay(tag: str = "A") -> dict:
	"""Proven sequence only: baseline 104 → 36853 I4 → L1 → L2 → L3×2.

	Does not include L4 (30100022 earliest-SLE RIV recreates 30200016 −1947)
	or the 30100294 paykar RIV (did not heal 25274 consume).
	Caller must restore 20260926_225046 and migrate first.
	"""
	ident = backup_identity()
	if ident.get("sha256") != "b25cfb15bcf43a914c6f3544f4d69907c5d995b443b5893f5b03df8324223796":
		# Identity file sha is of the gz; after restore SLE identity is the check.
		pass
	if int(ident.get("counts", {}).get("sle") or 0) != 112003:
		return {"ok": False, "tag": tag, "reason": "not clean 225046 SLE count", "ident": ident}
	p0 = pass0()
	base = apply_baseline()
	i4 = apply_36853_i4()
	l1 = isolated_l1_canary()
	l2 = isolated_l2_bounded()
	l3a = isolated_l3_13100134_layered()
	l3b = isolated_l3_second_pass()
	v = post_l3_verify()
	scan = post_scan()
	ok = bool(
		(base.get("rehearsal") or {}).get("applied_ok") == 104
		and i4.get("ok")
		and l1.get("riv1_ok")
		and l1.get("riv2_ok")
		and l1.get("neg_delta") == 0
		and l2.get("riv1_ok")
		and l2.get("riv2_ok")
		and l2.get("neg_delta") == 0
		and l3a.get("passed")
		and l3b.get("passed")
		and (v.get("gates") or {}).get("i1") == 0
		and (v.get("gates") or {}).get("neg_after") == 0
		and v.get("36933") in ("HEALTHY", {"classification": "HEALTHY"})
		and v.get("37090") == "HEALTHY"
		and (scan.get("gates") or {}).get("i1") == 0
		and (scan.get("gates") or {}).get("neg_after") == 0
		and not scan.get("gates", {}).get("neg_rate")
	)
	out = {
		"tag": tag,
		"ok": ok,
		"identity": {
			k: ident.get(k)
			for k in ("backup_id", "sha256", "latest_sle_creation", "counts", "looks_like_155411")
		},
		"pass0": p0.get("dashboard"),
		"baseline_applied": (base.get("rehearsal") or {}).get("applied_ok"),
		"i4_36853": {k: i4.get(k) for k in ("ok", "residual_before", "residual_after", "gl_621301_n")},
		"l1": l1,
		"l2": l2,
		"l3a": l3a,
		"l3b": l3b,
		"verify": v,
		"final_scan": scan,
	}
	prior._jdump(f"clean_final_replay_{tag}.json", out)
	return {
		"tag": tag,
		"ok": ok,
		"baseline": out["baseline_applied"],
		"i4_ok": i4.get("ok"),
		"l1": l1.get("riv1_ok"),
		"l2": l2.get("riv1_ok"),
		"l3a": l3a.get("passed"),
		"l3b": l3b.get("passed"),
		"i1": (scan.get("gates") or {}).get("i1"),
		"neg_after": (scan.get("gates") or {}).get("neg_after"),
		"score": (scan.get("dashboard") or {}).get("Integrity Score"),
	}


def run_clean_replay(tag: str = "A"):
	return run_clean_final_replay(tag=tag)


def mfg_fingerprint():
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		mfg_fingerprint as _fp,
	)

	return _fp()


def graph_13100134() -> dict:
	"""Read-only dependency map for the L3 earliest-SLE fanout."""
	import frappe
	from frappe.utils import flt

	item = "13100134"
	earliest = frappe.db.sql(
		"""SELECT name, voucher_type, voucher_no, warehouse, posting_datetime,
		          actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		          stock_value_difference, stock_value, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 8""",
		item,
		as_dict=True,
	)
	warehouses = frappe.db.sql(
		"""SELECT warehouse, COUNT(*) n, MIN(posting_datetime) first_dt,
		          MAX(posting_datetime) last_dt
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		GROUP BY warehouse ORDER BY first_dt""",
		item,
		as_dict=True,
	)
	# Manufacture vouchers that consume or scrap this item
	mfg = frappe.db.sql(
		"""SELECT se.name voucher, se.purpose, se.posting_date, se.work_order, se.job_card,
		          sed.item_code, sed.qty, sed.basic_rate, sed.s_warehouse, sed.t_warehouse,
		          IFNULL(sed.is_finished_item,0) is_fg, sed.secondary_item_type
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
		WHERE se.docstatus=1 AND sed.item_code=%s
		ORDER BY se.posting_date, se.name
		LIMIT 80""",
		item,
		as_dict=True,
	)
	# Same-voucher companion items on Manufacture entries that also use 13100134
	mfg_names = list({r.voucher for r in mfg if r.purpose == "Manufacture"})
	companions = []
	if mfg_names:
		companions = frappe.db.sql(
			"""SELECT se.name voucher, sed.item_code, sed.qty, sed.basic_rate,
			          sed.s_warehouse, sed.t_warehouse, IFNULL(sed.is_finished_item,0) is_fg,
			          sed.secondary_item_type
			FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
			WHERE se.name IN %s
			ORDER BY se.name, sed.idx""",
			(mfg_names[:40],),
			as_dict=True,
		)
	neg = frappe.db.sql(
		"""SELECT name, item_code, warehouse, voucher_no, actual_qty,
		          qty_after_transaction, stock_value
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001
		ORDER BY posting_datetime LIMIT 20""",
		as_dict=True,
	)
	# Watch items from previous deadlock
	watch = {}
	for witem in ("30100036", "30300015", "30200016", "30200016"):
		watch[witem] = frappe.db.sql(
			"""SELECT voucher_no, warehouse, posting_datetime, actual_qty,
			          qty_after_transaction, incoming_rate, outgoing_rate, stock_value
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation LIMIT 5""",
			witem,
			as_dict=True,
		)
	out = {
		"item": item,
		"earliest": earliest,
		"warehouses": warehouses,
		"mfg_rows": mfg,
		"companion_n": len(companions),
		"companion_items": sorted({c.item_code for c in companions}),
		"negative_stock": neg,
		"watch": watch,
	}
	prior._jdump("graph_13100134.json", out)
	return {
		"earliest_voucher": (earliest[0].voucher_no if earliest else None),
		"earliest_dt": str(earliest[0].posting_datetime) if earliest else None,
		"earliest_wh": earliest[0].warehouse if earliest else None,
		"warehouse_n": len(warehouses),
		"mfg_n": len(mfg_names),
		"companion_items": out["companion_items"][:40],
		"neg_n": len(neg),
	}


def riv_13100134_quarantine_readonly() -> dict:
	"""Describe the L3 RIV that previously deadlocked — no mutation."""
	import frappe

	row = frappe.db.sql(
		"""SELECT warehouse, posting_date, posting_time, posting_datetime, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code='13100134' AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		as_dict=True,
	)
	if not row:
		return {"ok": False, "reason": "no SLE"}
	s = row[0]
	return {
		"item": "13100134",
		"warehouse": s.warehouse,
		"posting_date": str(s.posting_date),
		"posting_time": str(s.posting_time),
		"posting_datetime": str(s.posting_datetime),
		"voucher": s.voucher_no,
		"would_riv": "Item and Warehouse from earliest SLE — not invoked",
	}


def _watch_snapshot() -> dict:
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import _gates

	watch = {}
	for item in ("13100134", "30100036", "30300015", "30200016", "20100067", "30100255"):
		watch[item] = frappe.db.sql(
			"""SELECT warehouse, COUNT(*) n,
			          ROUND(SUM(actual_qty),4) qty,
			          ROUND(SUM(stock_value_difference),2) svd,
			          MIN(qty_after_transaction) min_after,
			          MAX(qty_after_transaction) max_after
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND is_cancelled=0
			GROUP BY warehouse""",
			item,
			as_dict=True,
		)
	riv_open = frappe.db.sql(
		"""SELECT name, item_code, warehouse, status
		FROM `tabRepost Item Valuation`
		WHERE status IN ('Queued','In Progress')
		ORDER BY modified DESC LIMIT 20""",
		as_dict=True,
	)
	neg = frappe.db.sql(
		"""SELECT item_code, warehouse, voucher_no, qty_after_transaction, stock_value
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001
		ORDER BY posting_datetime LIMIT 30""",
		as_dict=True,
	)
	return {"gates": _gates(), "watch": watch, "riv_open": riv_open, "neg": neg}


def isolated_l1_canary() -> dict:
	"""L1: leftover-MA identity 16100066, isolated RIV, then second pass."""
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
	)

	item, wh = "16100066", "انبار ملزومات مصرفی اسپاد"
	row = frappe.db.sql(
		"""SELECT posting_date, posting_time FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		(item, wh),
		as_dict=True,
	)
	if not row:
		return {"ok": False, "reason": "no SLE"}
	s0 = _watch_snapshot()
	r1 = create_and_run_isolated_riv(
		item, wh, posting_date=row[0].posting_date, posting_time=row[0].posting_time
	)
	frappe.db.commit()
	s1 = _watch_snapshot()
	r2 = create_and_run_isolated_riv(
		item, wh, posting_date=row[0].posting_date, posting_time=row[0].posting_time
	)
	frappe.db.commit()
	s2 = _watch_snapshot()
	out = {
		"l1": "16100066 leftover MA isolated",
		"riv1": r1,
		"riv2": r2,
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"gates2": s2["gates"],
		"neg_delta": int(s2["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0),
		"i1": s2["gates"].get("i1"),
		"open_riv": s2["riv_open"],
	}
	prior._jdump("isolated_l1.json", out)
	return {
		"riv1_ok": r1.get("ok"),
		"riv2_ok": r2.get("ok"),
		"i1": out["i1"],
		"neg_delta": out["neg_delta"],
		"open_riv_n": len(s2["riv_open"]),
	}


def isolated_l2_bounded() -> dict:
	"""L2: 13100023 item+warehouse from its own earliest SLE — not 13100134 fanout."""
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
	)

	item = "13100023"
	row = frappe.db.sql(
		"""SELECT warehouse, posting_date, posting_time FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		item,
		as_dict=True,
	)
	if not row:
		return {"ok": False, "reason": "no SLE"}
	s0 = _watch_snapshot()
	r1 = create_and_run_isolated_riv(
		item,
		row[0].warehouse,
		posting_date=row[0].posting_date,
		posting_time=row[0].posting_time,
	)
	frappe.db.commit()
	s1 = _watch_snapshot()
	r2 = create_and_run_isolated_riv(
		item,
		row[0].warehouse,
		posting_date=row[0].posting_date,
		posting_time=row[0].posting_time,
	)
	frappe.db.commit()
	s2 = _watch_snapshot()
	out = {
		"item": item,
		"warehouse": row[0].warehouse,
		"riv1": r1,
		"riv2": r2,
		"gates0": s0["gates"],
		"gates2": s2["gates"],
		"neg_delta": int(s2["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0),
		"open_riv": s2["riv_open"],
	}
	prior._jdump("isolated_l2.json", out)
	return {
		"riv1_ok": r1.get("ok"),
		"riv2_ok": r2.get("ok"),
		"neg_delta": out["neg_delta"],
		"i1": s2["gates"].get("i1"),
		"open_riv_n": len(s2["riv_open"]),
	}


def isolated_l3_13100134_layered() -> dict:
	"""L3: 13100134 in warehouse-layer order, isolated, no Allow Negative Stock.

	ERPNext native earliest-SLE RIV on Quarantine walks Manufacture dependents
	(30100036 / 30300015 / 30200016). Intermediate commits + deadlock left
	partial negatives on the 15:54 campaign. Here each warehouse is a single
	flight; expansion stops if gates regress or a RIV stays open.
	"""
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
	)

	layers = frappe.db.sql(
		"""SELECT warehouse, MIN(posting_date) posting_date, MIN(posting_time) posting_time,
		          MIN(posting_datetime) first_dt, COUNT(*) n
		FROM `tabStock Ledger Entry`
		WHERE item_code='13100134' AND is_cancelled=0
		GROUP BY warehouse
		ORDER BY first_dt""",
		as_dict=True,
	)
	s0 = _watch_snapshot()
	results = []
	stopped = None
	for layer in layers:
		snap = _watch_snapshot()
		riv = create_and_run_isolated_riv(
			"13100134",
			layer.warehouse,
			posting_date=layer.posting_date,
			posting_time=layer.posting_time,
			allow_negative_stock=False,
			max_attempts=2,
		)
		frappe.db.commit()
		after = _watch_snapshot()
		neg_delta = int(after["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0)
		i1 = int(after["gates"].get("i1") or 0)
		open_n = len(after["riv_open"])
		row = {
			"warehouse": layer.warehouse,
			"first_dt": str(layer.first_dt),
			"riv_ok": riv.get("ok"),
			"riv_status": riv.get("riv_status"),
			"reason": (riv.get("reason") or "")[:400],
			"attempts": riv.get("attempts"),
			"neg_delta_vs_start": neg_delta,
			"i1": i1,
			"open_riv_n": open_n,
			"watch_30200016_min": [
				w.get("min_after")
				for w in after["watch"].get("30200016") or []
			],
		}
		results.append(row)
		if (not riv.get("ok")) or neg_delta > 0 or i1 > 0 or open_n:
			stopped = {
				"at": layer.warehouse,
				"why": "riv_failed"
				if not riv.get("ok")
				else ("new_neg" if neg_delta > 0 else ("i1" if i1 else "open_riv")),
			}
			break
	s1 = _watch_snapshot()
	out = {
		"layers": results,
		"stopped": stopped,
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"neg": s1["neg"],
		"open_riv": s1["riv_open"],
		"passed": stopped is None and all(r.get("riv_ok") for r in results),
	}
	prior._jdump("isolated_l3_13100134.json", out)
	return {
		"passed": out["passed"],
		"stopped": stopped,
		"layer_n": len(results),
		"layer_ok": [r.get("riv_ok") for r in results],
		"i1": s1["gates"].get("i1"),
		"neg_after": s1["gates"].get("neg_after"),
		"open_riv_n": len(s1["riv_open"]),
	}


def post_l3_verify() -> dict:
	"""Gates, canaries, 36933/37090, 30200016 after L3."""
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		_gates,
		canaries,
		mfg_fingerprint,
	)

	s33 = prior.snapshot_36933()
	c90 = prior.reconstruct_37090_class()
	watch = {}
	for item in ("30200016", "30100036", "30300015"):
		watch[item] = frappe.db.sql(
			"""SELECT warehouse, MIN(qty_after_transaction) min_after,
			          SUM(CASE WHEN qty_after_transaction < -0.0001 THEN 1 ELSE 0 END) neg_n
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND is_cancelled=0
			GROUP BY warehouse""",
			item,
			as_dict=True,
		)
	open_riv = frappe.db.sql(
		"""SELECT name, item_code, status FROM `tabRepost Item Valuation`
		WHERE status IN ('Queued','In Progress') LIMIT 10""",
		as_dict=True,
	)
	out = {
		"gates": _gates(),
		"canaries": canaries(),
		"36933": s33,
		"37090": c90,
		"watch": watch,
		"mfg": mfg_fingerprint(),
		"open_riv": open_riv,
	}
	prior._jdump("post_l3_verify.json", out)
	return {
		"gates": out["gates"],
		"36933": s33.get("native") if isinstance(s33, dict) else s33,
		"37090": c90,
		"open_riv_n": len(open_riv),
		"neg_30200016": watch["30200016"],
	}


def isolated_l4_representative() -> dict:
	"""L4: isolated RIV of one identity per economic family. Stop on gate fail."""
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
	)

	families = [
		("LEFTOVER_MA", "16100066", "انبار ملزومات مصرفی اسپاد"),
		("LEGITIMATE_ZERO", "16100226", "انبار ملزومات مصرفی اسپاد"),
		("I4_36853", "30100022", "انبار پایکار خط تولید اسپاد فارمد"),
		("MFG_SCRAP_HEALTHY_36933_FG", "20100067", None),
		("MFG_SCRAP_HEALTHY_37090_FG", "30100255", None),
		("MFG_NO_SCRAP_28696", None, None),  # resolved below from voucher
		("TRANSFER_33937", None, None),
	]
	# Resolve voucher-based items
	v28696 = frappe.db.sql(
		"""SELECT item_code, warehouse FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-28696' AND is_cancelled=0
		ORDER BY posting_datetime LIMIT 1""",
		as_dict=True,
	)
	v33937 = frappe.db.sql(
		"""SELECT item_code, warehouse FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-33937' AND is_cancelled=0
		ORDER BY posting_datetime LIMIT 1""",
		as_dict=True,
	)
	resolved = []
	for fam, item, wh in families:
		if fam == "MFG_NO_SCRAP_28696" and v28696:
			item, wh = v28696[0].item_code, v28696[0].warehouse
		elif fam == "TRANSFER_33937" and v33937:
			item, wh = v33937[0].item_code, v33937[0].warehouse
		if not item:
			resolved.append({"family": fam, "ok": False, "reason": "unresolved"})
			continue
		resolved.append({"family": fam, "item": item, "warehouse": wh})

	s0 = _watch_snapshot()
	results = []
	stopped = None
	for spec in resolved:
		if not spec.get("item"):
			results.append(spec)
			continue
		item, wh = spec["item"], spec.get("warehouse")
		if not wh:
			row = frappe.db.sql(
				"""SELECT warehouse, posting_date, posting_time FROM `tabStock Ledger Entry`
				WHERE item_code=%s AND is_cancelled=0
				ORDER BY posting_datetime, creation LIMIT 1""",
				item,
				as_dict=True,
			)
		else:
			row = frappe.db.sql(
				"""SELECT warehouse, posting_date, posting_time FROM `tabStock Ledger Entry`
				WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
				ORDER BY posting_datetime, creation LIMIT 1""",
				(item, wh),
				as_dict=True,
			)
		if not row:
			results.append({**spec, "ok": False, "reason": "no SLE"})
			continue
		riv = create_and_run_isolated_riv(
			item,
			row[0].warehouse,
			posting_date=row[0].posting_date,
			posting_time=row[0].posting_time,
			allow_negative_stock=False,
		)
		frappe.db.commit()
		after = _watch_snapshot()
		neg_delta = int(after["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0)
		row_out = {
			**spec,
			"warehouse": row[0].warehouse,
			"riv_ok": riv.get("ok"),
			"riv_status": riv.get("riv_status"),
			"reason": (riv.get("reason") or "")[:300],
			"i1": after["gates"].get("i1"),
			"neg_delta": neg_delta,
			"open_riv_n": len(after["riv_open"]),
		}
		results.append(row_out)
		if (not riv.get("ok")) or neg_delta > 0 or int(after["gates"].get("i1") or 0) or after["riv_open"]:
			stopped = spec["family"]
			break
	s1 = _watch_snapshot()
	out = {
		"results": results,
		"stopped": stopped,
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"passed": stopped is None and all(r.get("riv_ok") for r in results if r.get("item")),
		"36933": prior.reconstruct_37090_class() if False else prior.snapshot_36933().get("native"),
		"37090": prior.reconstruct_37090_class(),
	}
	prior._jdump("isolated_l4.json", out)
	return {
		"passed": out["passed"],
		"stopped": stopped,
		"ok": [r.get("riv_ok") for r in results],
		"families": [r.get("family") for r in results],
		"i1": s1["gates"].get("i1"),
		"neg_after": s1["gates"].get("neg_after"),
	}


def isolated_l3_second_pass() -> dict:
	"""Idempotency: run the same layered 13100134 RIV again."""
	out = isolated_l3_13100134_layered()
	prior._jdump("isolated_l3_13100134_pass2.json", out)
	return out


def post_baseline_25274_and_i4() -> dict:
	"""Re-evaluate 24993/25274 and remaining I4 after the 104-root baseline."""
	import frappe
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	an = analyze_manufacture_scrap_fg("MAT-STE-2026-25274")
	wr = scan_wrong_rates(company="اسپاد فارمد دارو", voucher="MAT-STE-2026-24993", limit=20)
	sles = frappe.db.sql(
		"""SELECT voucher_no, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference, stock_value, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE item_code='30100294' AND is_cancelled=0
		  AND voucher_no IN ('MAT-STE-2026-24993','MAT-STE-2026-25274')
		ORDER BY posting_datetime, creation""",
		as_dict=True,
	)
	i4 = prior.i4_leftover_math()
	out = {
		"scrap_fg_25274": {
			k: an.get(k)
			for k in ("classification", "reason", "eligible", "fg_basic_rate", "expected_fg_rate")
		},
		"wr_24993": {
			"count": wr.get("count"),
			"rows": [
				{
					k: r.get(k)
					for k in (
						"item",
						"warehouse",
						"voucher",
						"planner_status",
						"eligible",
						"expected_rate",
						"current_rate",
						"authoritative_source",
						"reason",
					)
				}
				for r in (wr.get("rows") or [])[:12]
			],
		},
		"sles": sles,
		"i4": i4,
	}
	prior._jdump("post_baseline_25274_i4.json", out)
	return {
		"class_25274": an.get("classification"),
		"wr_24993_n": wr.get("count"),
		"wr_24993_status": [r.get("planner_status") for r in (wr.get("rows") or [])],
		"i4": i4,
	}


def isolated_riv_30100294_paykar() -> dict:
	"""Replay 30100294 at پایکار from the now-valued 24993 inbound so 25274 consume is native."""
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	item = "30100294"
	wh = "انبار پایکار خط تولید اسپاد فارمد"
	row = frappe.db.sql(
		"""SELECT posting_date, posting_time, voucher_no FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		(item, wh),
		as_dict=True,
	)
	if not row:
		return {"ok": False, "reason": "no SLE"}
	s0 = _watch_snapshot()
	an0 = analyze_manufacture_scrap_fg("MAT-STE-2026-25274")
	r1 = create_and_run_isolated_riv(
		item, wh, posting_date=row[0].posting_date, posting_time=row[0].posting_time
	)
	frappe.db.commit()
	r2 = create_and_run_isolated_riv(
		item, wh, posting_date=row[0].posting_date, posting_time=row[0].posting_time
	)
	frappe.db.commit()
	s1 = _watch_snapshot()
	consume = frappe.db.sql(
		"""SELECT actual_qty, outgoing_rate, valuation_rate, stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-25274' AND item_code=%s AND is_cancelled=0""",
		item,
		as_dict=True,
	)
	an1 = analyze_manufacture_scrap_fg("MAT-STE-2026-25274")
	out = {
		"earliest": row[0],
		"riv1": r1,
		"riv2": r2,
		"consume_after": consume,
		"class_25274_before": an0.get("classification"),
		"class_25274_after": an1.get("classification"),
		"an_after": {k: an1.get(k) for k in ("classification", "reason", "eligible")},
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"neg_delta": int(s1["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0),
		"open_riv": s1["riv_open"],
	}
	prior._jdump("isolated_riv_30100294.json", out)
	return {
		"riv1_ok": r1.get("ok"),
		"riv2_ok": r2.get("ok"),
		"class_before": an0.get("classification"),
		"class_after": an1.get("classification"),
		"consume": consume,
		"i1": s1["gates"].get("i1"),
		"neg_delta": out["neg_delta"],
		"open_riv_n": len(s1["riv_open"]),
	}


def chain_30100294() -> dict:
	"""Walk 30100294 history to the first zero-valued movement (25274 upstream)."""
	import frappe
	from frappe.utils import flt

	item = "30100294"
	sles = frappe.db.sql(
		"""SELECT name, voucher_no, voucher_type, warehouse, posting_datetime,
		          actual_qty, qty_after_transaction, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		item,
		as_dict=True,
	)
	first_zero = None
	for r in sles:
		if abs(flt(r.actual_qty)) > 1e-9 and abs(flt(r.stock_value_difference)) <= 1e-6:
			if abs(flt(r.incoming_rate)) <= 1e-6 and abs(flt(r.outgoing_rate)) <= 1e-6:
				first_zero = r
				break
	se_25274 = frappe.db.exists("Stock Entry", "MAT-STE-2026-25274")
	out = {
		"item": item,
		"sle_n": len(sles),
		"first_zero": first_zero,
		"first5": sles[:5],
		"last8": sles[-8:],
		"has_25274": bool(se_25274),
	}
	prior._jdump("chain_30100294.json", out)
	return {
		"sle_n": len(sles),
		"first_zero_voucher": first_zero.voucher_no if first_zero else None,
		"first_zero_dt": str(first_zero.posting_datetime) if first_zero else None,
		"first_zero_wh": first_zero.warehouse if first_zero else None,
		"has_25274": out["has_25274"],
	}

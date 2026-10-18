# Copyright (c) 2026, ERPNext Extensions contributors
"""Single-RIV chronological historical rebuild (v5.6.1 strategy change).

Rules:
- MAX_ACTIVE_RIV = 1
- No mass RIV pre-creation
- No uncontrolled RQ enqueue (continue_reposting=False + empty queues)
- Coverage only after Completed session RIV + SLE continuity verification
- Stop on I3/I4 / genuine valuation failures for that identity (fix then retry)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	execute_reposting_entry,
)

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_SKIPPED,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	inspect_sle_chain,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	COVERED_BY_REPOSTED_AND_VERIFIED,
	DIRECT_REPOSTED_AND_VERIFIED,
	FINAL_SUCCESS_CLASSES,
	REPOST_FAILED,
	REPOST_PLANNED,
	SESSION_RIV_REMARK_PREFIX,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	verify_voucher_basic,
)

MAX_ACTIVE_RIV = 1
CACHE_SEQ = "hrc561_sequential_session"


def _ensure_bootstrap() -> None:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()


def _session_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"hrc_sequential_rebuild",
	)
	os.makedirs(path, exist_ok=True)
	return path


def _empty_rq() -> None:
	try:
		from frappe.utils.background_jobs import get_queue

		for name in ("default", "long", "short"):
			try:
				get_queue(name).empty()
			except Exception:
				pass
	except Exception:
		pass


def delete_all_rivs(*, reason: str = "sequential_rebuild_reset") -> dict[str, Any]:
	"""Delete every Repost Item Valuation row on this DEVELOPMENT site.

	Authorized cleanup only. Does not touch SLE/GL/business vouchers.
	"""
	_assert_dev()
	before = cint(frappe.db.count("Repost Item Valuation"))
	# Detach File attachments pointing at RIV
	frappe.db.sql(
		"""
		delete from `tabFile`
		where attached_to_doctype='Repost Item Valuation'
		"""
	)
	# Direct delete of RIV table (no business vouchers)
	frappe.db.sql("delete from `tabRepost Item Valuation`")
	frappe.db.commit()
	after = cint(frappe.db.count("Repost Item Valuation"))
	audit = {
		"reason": reason,
		"before": before,
		"after": after,
		"at": str(now_datetime()),
		"site": frappe.local.site,
	}
	path = os.path.join(_session_dir(), f"riv_delete_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(audit, fh, indent=2)
	if after != 0:
		frappe.throw(f"RIV cleanup incomplete: remaining={after}")
	return {"ok": True, "deleted": before, "remaining": after, "audit_path": path}


def _assert_dev() -> None:
	site = frappe.local.site or ""
	if "espadpharmed.com" in site and "development" not in site:
		frappe.throw(f"Refused RIV mass-delete on production-like site: {site}")
	if not (
		frappe.conf.get("developer_mode")
		or frappe.conf.get("e2e_marker")
		or frappe.conf.get("restored_from_backup")
	):
		frappe.throw("Refused: site not marked as development/test")


def reset_campaign_planned(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Force all campaign items to Pending / REPOST_PLANNED; clear covering_riv."""
	# Preserve prior manifest status snapshot
	snap = frappe.db.sql(
		"""
		select item_status, verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s
		group by item_status, verification_class
		""",
		campaign,
		as_dict=True,
	)
	path = os.path.join(
		_session_dir(), f"manifest_before_reset_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json"
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump({"campaign": campaign, "counts": snap}, fh, indent=2, default=str)

	frappe.db.sql(
		"""
		update `tabHistorical Rebuild Campaign Item`
		set item_status=%s,
		    verification_class=%s,
		    covering_riv=NULL,
		    riv_names=NULL,
		    error_message=%s,
		    last_processed_at=%s
		where campaign=%s
		""",
		(
			ITEM_PENDING,
			REPOST_PLANNED,
			"sequential_rebuild_reset",
			now_datetime(),
			campaign,
		),
	)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"status": "Paused", "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	total = cint(frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign}))
	pending = cint(
		frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_PENDING})
	)
	return {"campaign": campaign, "total": total, "pending": pending, "snapshot": path}


def start_sequential_session(campaign: str = "HRC-00001") -> dict[str, Any]:
	sid = now_datetime().strftime("%Y%m%d%H%M%S")
	payload = {
		"session_id": sid,
		"campaign": campaign,
		"remark": f"{SESSION_RIV_REMARK_PREFIX}SEQ:{sid}",
		"started_at": str(now_datetime()),
		"riv_created": [],
		"riv_completed": [],
		"riv_failed": [],
		"identities_done": [],
		"max_active_riv": MAX_ACTIVE_RIV,
		"strategy": "sequential_chronological",
	}
	_save_seq(payload)
	return payload


def _save_seq(payload: dict) -> None:
	frappe.cache().set_value(CACHE_SEQ, payload, expires_in_sec=60 * 60 * 24 * 14)
	path = os.path.join(_session_dir(), f"session_{payload['session_id']}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def get_seq_session() -> dict | None:
	return frappe.cache().get_value(CACHE_SEQ)


def restore_session_from_file(session_id: str | None = None) -> dict[str, Any]:
	"""Reload sequential session JSON into cache (survives process restarts)."""
	directory = _session_dir()
	path = None
	if session_id:
		path = os.path.join(directory, f"session_{session_id}.json")
	else:
		candidates = sorted(
			(
				os.path.join(directory, name)
				for name in os.listdir(directory)
				if name.startswith("session_") and name.endswith(".json")
			),
			key=os.path.getmtime,
			reverse=True,
		)
		path = candidates[0] if candidates else None
	if not path or not os.path.exists(path):
		frappe.throw(f"No sequential session file found (session_id={session_id})")
	with open(path, encoding="utf-8") as fh:
		payload = json.load(fh)
	_save_seq(payload)
	return {"ok": True, "session_id": payload.get("session_id"), "path": path, "payload": payload}


def chronological_identities(campaign: str) -> list[dict[str, Any]]:
	"""Distinct Item+WH ordered by earliest campaign SLE posting."""
	return (
		frappe.db.sql(
			"""
			select sle.item_code, sle.warehouse,
			       min(sle.posting_date) as posting_date,
			       substring_index(
			         group_concat(sle.posting_time order by sle.posting_date, sle.posting_time, sle.creation),
			         ',', 1
			       ) as posting_time,
			       count(distinct concat(i.voucher_type,'/',i.voucher_no)) as voucher_count
			from `tabHistorical Rebuild Campaign Item` i
			inner join `tabStock Ledger Entry` sle
			  on sle.voucher_type=i.voucher_type and sle.voucher_no=i.voucher_no
			 and sle.is_cancelled=0
			where i.campaign=%s
			  and sle.item_code is not null and sle.item_code!=''
			  and sle.warehouse is not null and sle.warehouse!=''
			group by sle.item_code, sle.warehouse
			order by posting_date asc, posting_time asc, sle.item_code asc, sle.warehouse asc
			""",
			campaign,
			as_dict=True,
		)
		or []
	)


def _assert_no_active_riv() -> None:
	n = cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		)
	)
	if n > 0:
		frappe.throw(f"Active RIV present ({n}); sequential policy forbids starting another")


def execute_one_identity(
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	*,
	company: str,
) -> dict[str, Any]:
	"""Create exactly one RIV, execute sync, verify chain — terminal before return."""
	_ensure_bootstrap()
	_empty_rq()
	_assert_no_active_riv()
	sess = get_seq_session()
	if not sess:
		frappe.throw("No sequential session — call start_sequential_session()")

	# Skip if already done in this session
	key = f"{item_code}|{warehouse}"
	if key in (sess.get("identities_done") or []):
		return {"ok": True, "skipped_done": True, "item_code": item_code, "warehouse": warehouse}

	create_repost_item_valuation_entry(
		frappe._dict(
			based_on="Item and Warehouse",
			item_code=item_code,
			warehouse=warehouse,
			posting_date=posting_date,
			posting_time=posting_time or "00:00:00",
			company=company,
			allow_zero_rate=False,
			allow_negative_stock=True,
		)
	)
	name = frappe.db.get_value(
		"Repost Item Valuation",
		{
			"based_on": "Item and Warehouse",
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": posting_date,
			"docstatus": 1,
		},
		"name",
		order_by="creation desc",
	)
	if not name:
		return {"ok": False, "reason": "create_failed", "item_code": item_code, "warehouse": warehouse}

	sess.setdefault("riv_created", []).append(name)
	_save_seq(sess)
	frappe.db.commit()

	t0 = time.time()
	try:
		_empty_rq()
		execute_reposting_entry(name, continue_reposting=False)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		status = frappe.db.get_value("Repost Item Valuation", name, "status")
		sess.setdefault("riv_failed", []).append(name)
		_save_seq(sess)
		return {
			"ok": False,
			"riv": name,
			"status": status,
			"error": str(e)[:500],
			"item_code": item_code,
			"warehouse": warehouse,
			"duration": round(time.time() - t0, 3),
		}

	status = frappe.db.get_value("Repost Item Valuation", name, "status")
	duration = round(time.time() - t0, 3)
	if status != "Completed":
		err = (frappe.db.get_value("Repost Item Valuation", name, "error_log") or "")[:500]
		sess.setdefault("riv_failed", []).append(name)
		_save_seq(sess)
		return {
			"ok": False,
			"riv": name,
			"status": status,
			"error": err,
			"item_code": item_code,
			"warehouse": warehouse,
			"duration": duration,
			"stop_chain": True,
		}

	# Verify SLE continuity from RIV anchor forward
	chain = inspect_sle_chain(
		item_code, warehouse, company=company, from_date=posting_date, from_time=posting_time
	)
	if chain.get("dirty"):
		sess.setdefault("riv_failed", []).append(name)
		_save_seq(sess)
		return {
			"ok": False,
			"riv": name,
			"status": "Completed",
			"verify_failed": True,
			"issue_count": chain.get("issue_count"),
			"issues_sample": (chain.get("issues") or [])[:5],
			"item_code": item_code,
			"warehouse": warehouse,
			"duration": duration,
			"stop_chain": True,
		}

	sess.setdefault("riv_completed", []).append(name)
	sess.setdefault("identities_done", []).append(key)
	_save_seq(sess)
	return {
		"ok": True,
		"riv": name,
		"status": "Completed",
		"item_code": item_code,
		"warehouse": warehouse,
		"duration": duration,
		"sle_count": chain.get("sle_count"),
	}


def mark_campaign_coverage(campaign: str, *, limit: int = 5000) -> dict[str, Any]:
	"""Mark Pending items COVERED when every SLE identity is in identities_done
	with a session Completed RIV at/before posting.
	"""
	sess = get_seq_session() or {}
	done = set(sess.get("identities_done") or [])
	completed = list(dict.fromkeys(sess.get("riv_completed") or []))
	if not completed and not done:
		return {"covered": 0, "pending": 0, "detail": "no session progress"}

	# Index earliest session Completed RIV per identity
	index: dict[tuple[str, str], tuple[str, str, str]] = {}
	if completed:
		ph = ",".join(["%s"] * len(completed))
		rows = frappe.db.sql(
			f"""
			select name, item_code, warehouse, posting_date, posting_time
			from `tabRepost Item Valuation`
			where name in ({ph}) and status='Completed' and docstatus=1
			order by posting_date asc, posting_time asc
			""",
			tuple(completed),
			as_dict=True,
		) or []
		for r in rows:
			key = (r.item_code, r.warehouse)
			if key not in index:
				index[key] = (r.name, str(r.posting_date), str(r.posting_time or "00:00:00"))

	items = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign, "item_status": ITEM_PENDING},
		fields=["name", "voucher_type", "voucher_no", "company"],
		order_by="ordinal asc",
		limit_page_length=cint(limit) or 5000,
	)
	covered = 0
	still = 0
	direct = 0
	for it in items:
		sles = (
			frappe.db.sql(
				"""
				select distinct item_code, warehouse, posting_date, posting_time
				from `tabStock Ledger Entry`
				where is_cancelled=0 and voucher_type=%s and voucher_no=%s
				  and item_code is not null and item_code!=''
				  and warehouse is not null and warehouse!=''
				""",
				(it.voucher_type, it.voucher_no),
				as_dict=True,
			)
			or []
		)
		if not sles:
			basic = verify_voucher_basic(it.voucher_type, it.voucher_no)
			if basic.get("ok"):
				_mark(it.name, DIRECT_REPOSTED_AND_VERIFIED, None, {"reason": "no_sle"})
				covered += 1
				direct += 1
			else:
				still += 1
			continue
		ok = True
		covers = []
		for sle in sles:
			info = index.get((sle.item_code, sle.warehouse))
			if not info:
				ok = False
				break
			# chronological: RIV posting <= SLE posting
			if (info[1], info[2]) > (str(sle.posting_date), str(sle.posting_time or "00:00:00")):
				ok = False
				break
			covers.append(info[0])
		if not ok:
			still += 1
			continue
		_mark(
			it.name,
			COVERED_BY_REPOSTED_AND_VERIFIED,
			covers[0],
			{"covering_rivs": list(dict.fromkeys(covers)), "reason": "sequential_session_cover"},
		)
		covered += 1
	frappe.db.commit()
	return {"covered": covered, "still_pending": still, "direct_no_sle": direct, "scanned": len(items)}


def _mark(name: str, vclass: str, riv: str | None, detail: dict) -> None:
	fields = {
		"item_status": ITEM_SKIPPED if vclass in FINAL_SUCCESS_CLASSES else ITEM_PENDING,
		"verification_class": vclass,
		"covering_riv": riv,
		"error_message": "",
		"last_processed_at": now_datetime(),
	}
	meta = frappe.get_meta(ITEM_DOCTYPE)
	if meta.has_field("verification_detail"):
		fields["verification_detail"] = json.dumps(
			{**detail, "at": str(now_datetime())}, ensure_ascii=False, default=str
		)[:2000]
	if vclass == REPOST_FAILED:
		fields["item_status"] = ITEM_FAILED
	frappe.db.set_value(ITEM_DOCTYPE, name, fields, update_modified=False)


def run_sequential(
	campaign: str = "HRC-00001",
	*,
	max_identities: int | None = None,
	stop_on_failure: bool = True,
	mark_every: int = 10,
) -> dict[str, Any]:
	"""Process identities one-by-one chronologically until done or blocked."""
	t0 = time.time()
	_ensure_bootstrap()
	_empty_rq()
	sess = get_seq_session() or start_sequential_session(campaign)
	company = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	idents = chronological_identities(campaign)
	done_keys = set(sess.get("identities_done") or [])
	todo = [r for r in idents if f"{r.item_code}|{r.warehouse}" not in done_keys]
	if max_identities is not None:
		todo = todo[: cint(max_identities)]

	results = []
	completed = 0
	failed = 0
	blocked_at = None
	for i, row in enumerate(todo):
		_empty_rq()
		out = execute_one_identity(
			row.item_code,
			row.warehouse,
			row.posting_date,
			row.posting_time,
			company=company,
		)
		results.append(out)
		if out.get("ok"):
			completed += 1
		else:
			failed += 1
			if stop_on_failure and out.get("stop_chain"):
				blocked_at = {
					"index": i,
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"riv": out.get("riv"),
					"error": out.get("error") or out.get("issues_sample"),
				}
				break
		if mark_every and (i + 1) % cint(mark_every) == 0:
			mark_campaign_coverage(campaign, limit=20000)

	cover = mark_campaign_coverage(campaign, limit=20000)
	# Direct-mark remaining no-SLE
	cover2 = mark_campaign_coverage(campaign, limit=20000)

	vc = frappe.db.sql(
		"""
		select verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s group by verification_class
		""",
		campaign,
		as_dict=True,
	)
	success = sum(cint(r.c) for r in vc if r.verification_class in FINAL_SUCCESS_CLASSES)
	total = sum(cint(r.c) for r in vc)
	sess = get_seq_session() or sess
	return {
		"session_id": sess.get("session_id"),
		"runtime_seconds": round(time.time() - t0, 3),
		"identities_total": len(idents),
		"identities_attempted": len(results),
		"identities_completed": completed,
		"identities_failed": failed,
		"blocked_at": blocked_at,
		"cover": cover,
		"cover2": cover2,
		"verification_class_counts": {r.verification_class: cint(r.c) for r in vc},
		"session_riv_created": len(sess.get("riv_created") or []),
		"session_riv_completed": len(sess.get("riv_completed") or []),
		"session_riv_failed": len(sess.get("riv_failed") or []),
		"success_entries": success,
		"total_entries": total,
		"gates_ok": success == total and total > 0 and not blocked_at,
		"results_sample": results[:20],
	}


def prepare_clean_start(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Delete all RIVs, reset campaign, start sequential session."""
	_empty_rq()
	deleted = delete_all_rivs(reason="sequential_rebuild_clean_start")
	reset = reset_campaign_planned(campaign)
	session = start_sequential_session(campaign)
	return {"deleted": deleted, "reset": reset, "session": session}

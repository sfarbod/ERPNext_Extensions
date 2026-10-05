# Copyright (c) 2026, ERPNext Extensions contributors
"""UI disposition prefill contract (v5.5.2).

Maps authoritative Scan suggestion fields onto editable Golden Rule decision
buckets. Mirrors ``jcsr_prefill_from_suggestion`` in the Job Card Stock Rebuild
page JS. Not a second suggestion engine — no inference from raw issued /
returned / scrap quantities.
"""

from __future__ import annotations

from frappe.utils import flt


# Concrete dispositions that may prefill editable fields (incl. LOW confidence).
CONCRETE_ACTIONS = frozenset(
	{
		"CONSUMED",
		"SCRAP",
		"COMPONENT_SCRAP",
		"RETURN",
		"STILL_IN_WIP",
		"STILL_WIP",
		"STILL IN WIP",
	}
)

# No defensible recommendation — leave allocation unset/zero.
NON_CONCRETE_ACTIONS = frozenset(
	{
		"",
		"MANUAL REVIEW",
		"MANUAL_REVIEW",
		"AMBIGUOUS",
		"BLOCKED",
		"NONE",
		"INSUFFICIENT_EVIDENCE",
	}
)


def _normalize_action(action) -> str:
	raw = str(action or "").strip().upper()
	return raw.replace(" ", "_") if raw else ""


def prefill_from_suggestion(row: dict | None) -> dict:
	"""Return editable decision quantities from a Scan row.

	Keys: consumed, scrap, return, still (UI field names).
	"""
	r = row or {}
	action_raw = str(r.get("suggested_action") or r.get("disposition") or "").strip().upper()
	action = _normalize_action(action_raw)
	qty = flt(r.get("suggested_qty"))
	out = {"consumed": 0.0, "scrap": 0.0, "return": 0.0, "still": 0.0}

	if action == "CONSUMED" and qty > 0:
		out["consumed"] = qty
		return out
	if action in ("SCRAP", "COMPONENT_SCRAP") and qty > 0:
		out["scrap"] = qty
		return out
	if action == "RETURN" and qty > 0:
		out["return"] = qty
		return out
	if action in ("STILL_IN_WIP", "STILL_WIP") and qty > 0:
		out["still"] = qty
		return out
	# Canonical backend spelling with spaces (before normalize) also handled above
	# via STILL_IN_WIP after normalize of "STILL IN WIP".

	# Fallback: trust server-filled proposed_* when already allocated.
	pc = flt(r.get("proposed_consumed"))
	ps = flt(r.get("proposed_scrap"))
	pr = flt(r.get("proposed_return"))
	pw = flt(r.get("proposed_still_in_wip"))
	if pc + ps + pr + pw > 0:
		out["consumed"] = pc
		out["scrap"] = ps
		out["return"] = pr
		out["still"] = pw
	return out


def row_key(item_code, batch_no="") -> str:
	return f"{item_code or ''}\0{batch_no or ''}"


class DecisionState:
	"""Mirrors page JS: Scan initializes; user edits persist until new Scan."""

	def __init__(self):
		self.decisions: dict[str, dict] = {}

	def init_from_scan(self, rows: list[dict]):
		self.decisions = {}
		for r in rows or []:
			self.decisions[row_key(r.get("item_code"), r.get("batch_no"))] = prefill_from_suggestion(r)

	def get(self, item_code, batch_no="") -> dict:
		return self.decisions.get(row_key(item_code, batch_no), {
			"consumed": 0.0,
			"scrap": 0.0,
			"return": 0.0,
			"still": 0.0,
		})

	def set_user_edit(self, item_code, batch_no="", **kwargs):
		key = row_key(item_code, batch_no)
		cur = dict(self.get(item_code, batch_no))
		for k in ("consumed", "scrap", "return", "still"):
			if k in kwargs:
				cur[k] = flt(kwargs[k])
		self.decisions[key] = cur

	def values_for_render(self, row: dict) -> dict:
		"""Re-render must not overwrite user edits with fresh suggestion."""
		key = row_key(row.get("item_code"), row.get("batch_no"))
		if key in self.decisions:
			return self.decisions[key]
		d = prefill_from_suggestion(row)
		self.decisions[key] = d
		return d

	def dry_run_payload_row(self, item_code, batch_no="") -> dict:
		d = self.get(item_code, batch_no)
		return {
			"item_code": item_code,
			"batch_no": batch_no or "",
			"proposed_consumed": flt(d["consumed"]),
			"proposed_scrap": flt(d["scrap"]),
			"proposed_return": flt(d["return"]),
			"proposed_still_in_wip": flt(d["still"]),
		}

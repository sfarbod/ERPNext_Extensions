# Copyright (c) 2026, ERPNext Extensions contributors
"""SABB root vs descendant. Authority is batch inward SABB, never nearest rate."""


def sabb_role(*, warehouse: str, has_source_side_same_batch: bool, source: str, confidence: str) -> str:
	src = str(source or "")
	if "batch_inward_sabb" not in src or str(confidence).upper() != "EXACT":
		return "NOT_SABB"
	wh = str(warehouse or "")
	if "پایکار" in wh and has_source_side_same_batch:
		return "DESCENDANT"
	return "ROOT"

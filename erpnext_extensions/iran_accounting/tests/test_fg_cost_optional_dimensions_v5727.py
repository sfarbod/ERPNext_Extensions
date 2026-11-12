# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: optional Department/Cost Center filters + overlap non-duplication (v5.7.27)."""

from __future__ import annotations

import unittest
from decimal import Decimal

from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import (
	GLLineSnapshot,
	RuleSpec,
	assign_gl_lines_to_rules,
	detect_overlapping_rules,
	dimension_filter_matches,
	rule_matches_gl,
	rules_can_overlap,
)


def _rule(name, account, department="", cost_center=""):
	return RuleSpec(
		name=name,
		account=account,
		department=department or "",
		cost_center=cost_center or "",
		enabled=1,
	)


def _gl(name, account, department="", cost_center="", net="100"):
	amt = Decimal(net)
	return GLLineSnapshot(
		gl_entry=name,
		voucher_type="Journal Entry",
		voucher_no="JV-1",
		posting_date="2026-04-01",
		account=account,
		cost_center=cost_center or "",
		department=department or "",
		debit=amt if amt >= 0 else Decimal("0"),
		credit=(-amt) if amt < 0 else Decimal("0"),
		net_amount=amt,
	)


class TestOptionalDimensionFilters(unittest.TestCase):
	def test_blank_cost_center_matches_any_cost_center(self):
		rule = _rule("R1", "611102", department="Dept A", cost_center="")
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1200 - Overhead")
		)
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1110 - Bio")
		)
		self.assertTrue(rule_matches_gl(rule, account="611102", department="Dept A", cost_center=""))

	def test_populated_cost_center_matches_only_exact(self):
		rule = _rule("R1", "611102", department="Dept A", cost_center="1200 - Overhead")
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1200 - Overhead")
		)
		self.assertFalse(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1110 - Bio")
		)
		self.assertFalse(rule_matches_gl(rule, account="611102", department="Dept A", cost_center=""))

	def test_blank_department_matches_any_department(self):
		rule = _rule("R1", "611102", department="", cost_center="1200 - Overhead")
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1200 - Overhead")
		)
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept B", cost_center="1200 - Overhead")
		)
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="", cost_center="1200 - Overhead")
		)

	def test_populated_department_matches_only_exact(self):
		rule = _rule("R1", "611102", department="Dept A", cost_center="1200 - Overhead")
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1200 - Overhead")
		)
		self.assertFalse(
			rule_matches_gl(rule, account="611102", department="Dept B", cost_center="1200 - Overhead")
		)

	def test_both_dimensions_blank_match_account_only(self):
		rule = _rule("R1", "611102", department="", cost_center="")
		self.assertTrue(
			rule_matches_gl(rule, account="611102", department="Dept A", cost_center="1200")
		)
		self.assertTrue(rule_matches_gl(rule, account="611102", department="", cost_center=""))
		self.assertFalse(
			rule_matches_gl(rule, account="611999", department="Dept A", cost_center="1200")
		)

	def test_account_always_exact(self):
		rule = _rule("R1", "611102", department="Dept A", cost_center="")
		self.assertFalse(
			rule_matches_gl(rule, account="611103", department="Dept A", cost_center="1200")
		)

	def test_dimension_filter_helper(self):
		self.assertTrue(dimension_filter_matches("", "anything"))
		self.assertTrue(dimension_filter_matches(None, "anything"))
		self.assertTrue(dimension_filter_matches("A", "A"))
		self.assertFalse(dimension_filter_matches("A", "B"))
		self.assertFalse(dimension_filter_matches("A", ""))


class TestOverlapPolicy(unittest.TestCase):
	def test_blank_cc_overlaps_specific_cc_same_dept(self):
		a = _rule("A", "611102", department="Dept A", cost_center="")
		b = _rule("B", "611102", department="Dept A", cost_center="1200")
		self.assertTrue(rules_can_overlap(a, b))
		pairs = detect_overlapping_rules([a, b])
		self.assertEqual(len(pairs), 1)

	def test_different_accounts_do_not_overlap(self):
		a = _rule("A", "611102", department="Dept A", cost_center="")
		b = _rule("B", "611103", department="Dept A", cost_center="")
		self.assertFalse(rules_can_overlap(a, b))

	def test_disjoint_departments_do_not_overlap(self):
		a = _rule("A", "611102", department="Dept A", cost_center="")
		b = _rule("B", "611102", department="Dept B", cost_center="")
		self.assertFalse(rules_can_overlap(a, b))

	def test_overlapping_rules_do_not_duplicate_gl_amount(self):
		rules = [
			_rule("A", "611102", department="Dept A", cost_center=""),
			_rule("B", "611102", department="Dept A", cost_center="1200"),
		]
		lines = [
			_gl("GLE-1", "611102", department="Dept A", cost_center="1200", net="500"),
			_gl("GLE-2", "611102", department="Dept A", cost_center="1110", net="200"),
		]
		by_rule, overlaps, matched, unmatched = assign_gl_lines_to_rules(rules, lines)

		# GLE-1 matches both → withheld (not in any pool)
		self.assertEqual(len(overlaps), 1)
		self.assertEqual(overlaps[0]["gl_entry"], "GLE-1")
		self.assertNotIn("GLE-1", [x.gl_entry for xs in by_rule.values() for x in xs])

		# GLE-2 matches only blank-CC rule A
		self.assertEqual([x.gl_entry for x in by_rule.get("A", [])], ["GLE-2"])
		self.assertEqual(by_rule.get("B", []), [])

		# Amounts: matched 200 only; unmatched includes conflicted 500
		self.assertEqual(matched, Decimal("200"))
		self.assertEqual(unmatched, Decimal("500"))
		# No double count: total of pool lines + unmatched = all GL nets
		pool_sum = sum((x.net_amount for xs in by_rule.values() for x in xs), Decimal("0"))
		self.assertEqual(pool_sum + unmatched, Decimal("700"))
		self.assertEqual(pool_sum, matched)

	def test_single_rule_blank_cc_all_gl_go_to_one_pool(self):
		rules = [_rule("A", "611102", department="Dept A", cost_center="")]
		lines = [
			_gl("GLE-1", "611102", department="Dept A", cost_center="1200", net="100"),
			_gl("GLE-2", "611102", department="Dept A", cost_center="1110", net="50"),
			_gl("GLE-3", "611102", department="Dept B", cost_center="1200", net="999"),
		]
		by_rule, overlaps, matched, unmatched = assign_gl_lines_to_rules(rules, lines)
		self.assertEqual(overlaps, [])
		self.assertEqual({x.gl_entry for x in by_rule["A"]}, {"GLE-1", "GLE-2"})
		self.assertEqual(matched, Decimal("150"))
		self.assertEqual(unmatched, Decimal("999"))

	def test_four_scenarios_matrix(self):
		"""Documented matrix from the correction brief."""
		acct = "611102"
		cases = [
			# (rule_dept, rule_cc, gl_dept, gl_cc, expect)
			("Dept A", "", "Dept A", "1200", True),
			("Dept A", "", "Dept A", "1110", True),
			("Dept A", "", "Dept B", "1200", False),
			("", "1200", "Dept A", "1200", True),
			("", "1200", "Dept B", "1200", True),
			("", "1200", "Dept A", "1110", False),
			("Dept A", "1200", "Dept A", "1200", True),
			("Dept A", "1200", "Dept A", "1110", False),
			("Dept A", "1200", "Dept B", "1200", False),
			("", "", "Dept A", "1200", True),
			("", "", "Dept B", "1110", True),
		]
		for i, (rd, rc, gd, gc, expect) in enumerate(cases):
			rule = _rule(f"R{i}", acct, department=rd, cost_center=rc)
			got = rule_matches_gl(rule, account=acct, department=gd, cost_center=gc)
			self.assertEqual(got, expect, msg=f"case {i}: rule=({rd!r},{rc!r}) gl=({gd!r},{gc!r})")


class TestPoolReconciliationIdentity(unittest.TestCase):
	def test_pool_line_sum_equals_matched_without_duplication(self):
		rules = [
			_rule("A", "611102", department="Dept A", cost_center=""),
			_rule("B", "611102", department="Dept B", cost_center=""),
		]
		lines = [
			_gl("1", "611102", department="Dept A", cost_center="1200", net="10"),
			_gl("2", "611102", department="Dept A", cost_center="1110", net="20"),
			_gl("3", "611102", department="Dept B", cost_center="1200", net="30"),
			_gl("4", "611999", department="Dept A", cost_center="1200", net="40"),
		]
		by_rule, overlaps, matched, unmatched = assign_gl_lines_to_rules(rules, lines)
		self.assertEqual(overlaps, [])
		pool_a = sum((x.net_amount for x in by_rule["A"]), Decimal("0"))
		pool_b = sum((x.net_amount for x in by_rule["B"]), Decimal("0"))
		self.assertEqual(pool_a, Decimal("30"))
		self.assertEqual(pool_b, Decimal("30"))
		self.assertEqual(matched, Decimal("60"))
		# GLE-4 different account → unmatched relative to these rules' assignment
		# (assign_gl_lines only considers provided lines; account mismatch → unmatched)
		self.assertEqual(unmatched, Decimal("40"))
		self.assertEqual(matched + unmatched, sum((l.net_amount for l in lines), Decimal("0")))


if __name__ == "__main__":
	unittest.main()

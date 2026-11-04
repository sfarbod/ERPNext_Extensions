# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.33 savepoint dry-run tests retired.

Architecture replaced in v5.6.34 with strict read-only validation.
See ``test_delivery_note_check_submission_v5634.py``.
"""

from __future__ import annotations

import unittest


class TestDeliveryNoteCheckSubmissionV5633Retired(unittest.TestCase):
	def test_retired_in_favor_of_v5634(self):
		self.skipTest("Replaced by strict read-only suite test_delivery_note_check_submission_v5634")

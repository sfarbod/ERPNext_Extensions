# Copyright (c) 2025, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PDCJournalReference(Document):
	def before_insert(self):
		from erpnext_extensions.cheque_management.pdc_journal_reference_guard import (
			reject_unauthorized_journal_reference_insert,
		)

		reject_unauthorized_journal_reference_insert(self)

	def validate(self):
		from erpnext_extensions.cheque_management.pdc_journal_reference_guard import (
			reject_unauthorized_journal_reference_field_edit,
		)

		reject_unauthorized_journal_reference_field_edit(self)

	def on_trash(self):
		from erpnext_extensions.cheque_management.pdc_journal_reference_guard import (
			reject_unauthorized_journal_reference_trash,
		)

		reject_unauthorized_journal_reference_trash(self)

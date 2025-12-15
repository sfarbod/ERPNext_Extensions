import frappe
from frappe.model.document import Document


class ChequeSettings(Document):
	def validate(self):
		"""Validate that accounts are valid if provided"""
		# Validate bank account type
		if self.default_bank_account:
			account_type = frappe.db.get_value("Account", self.default_bank_account, "account_type")
			if account_type != "Bank":
				frappe.throw("Default Bank Account must be of type Bank")
		
		# Validate that company is selected
		if not self.company:
			frappe.throw("Company is required")


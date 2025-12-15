import frappe
from frappe.model.document import Document
from frappe.utils import getdate, flt

from erpnext_extensions.cheque_management.utils import (
	ReceivableChequeStatus,
	PayableChequeStatus,
	JournalEntryPurpose,
	ChequeType,
)


@frappe.whitelist()
def mark_waiting_for_sayad(name):
	"""Whitelisted method for Mark Waiting For Sayad"""
	doc = frappe.get_doc("Cheque", name)
	doc.mark_waiting_for_sayad()
	return doc


@frappe.whitelist()
def mark_registered_in_sayad(name):
	"""Whitelisted method for Mark Registered In Sayad"""
	doc = frappe.get_doc("Cheque", name)
	doc.mark_registered_in_sayad()
	return doc


@frappe.whitelist()
def move_to_box(name):
	"""Whitelisted method for Move To Box"""
	doc = frappe.get_doc("Cheque", name)
	doc.move_to_box()
	return doc


@frappe.whitelist()
def assign_to_bank(name, posting_date=None):
	"""Whitelisted method for Assign To Bank"""
	doc = frappe.get_doc("Cheque", name)
	return doc.assign_to_bank(posting_date)


@frappe.whitelist()
def mark_as_collected(name, posting_date=None, bank_account=None):
	"""Whitelisted method for Mark As Collected"""
	doc = frappe.get_doc("Cheque", name)
	return doc.mark_as_collected(posting_date, bank_account)


@frappe.whitelist()
def mark_as_returned_from_bank(name, posting_date=None):
	"""Whitelisted method for Mark As Returned From Bank"""
	doc = frappe.get_doc("Cheque", name)
	return doc.mark_as_returned_from_bank(posting_date)


@frappe.whitelist()
def return_to_customer(name):
	"""Whitelisted method for Return To Customer"""
	doc = frappe.get_doc("Cheque", name)
	doc.return_to_customer()
	return doc


@frappe.whitelist()
def reassign_to_bank(name, posting_date=None):
	"""Whitelisted method for Reassign To Bank"""
	doc = frappe.get_doc("Cheque", name)
	return doc.reassign_to_bank(posting_date)


@frappe.whitelist()
def return_not_registered_to_customer(name):
	"""Whitelisted method for Return Not Registered To Customer"""
	doc = frappe.get_doc("Cheque", name)
	doc.return_not_registered_to_customer()
	return doc


@frappe.whitelist()
def return_registered_to_customer(name):
	"""Whitelisted method for Return Registered To Customer"""
	doc = frappe.get_doc("Cheque", name)
	doc.return_registered_to_customer()
	return doc


@frappe.whitelist()
def retrieve_from_bank(name):
	"""Whitelisted method for Retrieve From Bank"""
	doc = frappe.get_doc("Cheque", name)
	doc.retrieve_from_bank()
	return doc


@frappe.whitelist()
def move_back_to_box_from_retrieved(name):
	"""Whitelisted method for Move Back To Box From Retrieved"""
	doc = frappe.get_doc("Cheque", name)
	doc.move_back_to_box_from_retrieved()
	return doc


# ========== Payable Cheque Whitelisted Methods ==========

@frappe.whitelist()
def select_bank(name):
	"""Whitelisted method for Select Bank"""
	doc = frappe.get_doc("Cheque", name)
	doc.select_bank()
	return doc


@frappe.whitelist()
def issue_cheque(name, posting_date=None):
	"""Whitelisted method for Issue Cheque"""
	doc = frappe.get_doc("Cheque", name)
	return doc.issue_cheque(posting_date)


@frappe.whitelist()
def mark_as_printed(name):
	"""Whitelisted method for Mark As Printed"""
	doc = frappe.get_doc("Cheque", name)
	doc.mark_as_printed()
	return doc


@frappe.whitelist()
def first_signature_done(name):
	"""Whitelisted method for First Signature Done"""
	doc = frappe.get_doc("Cheque", name)
	doc.first_signature_done()
	return doc


@frappe.whitelist()
def second_signature_done(name):
	"""Whitelisted method for Second Signature Done"""
	doc = frappe.get_doc("Cheque", name)
	doc.second_signature_done()
	return doc


@frappe.whitelist()
def notify_supplier(name):
	"""Whitelisted method for Notify Supplier"""
	doc = frappe.get_doc("Cheque", name)
	doc.notify_supplier()
	return doc


@frappe.whitelist()
def deliver_to_supplier(name):
	"""Whitelisted method for Deliver To Supplier"""
	doc = frappe.get_doc("Cheque", name)
	doc.deliver_to_supplier()
	return doc


@frappe.whitelist()
def mark_registered_in_sayad_payable(name):
	"""Whitelisted method for Mark Registered In Sayad (Payable)"""
	doc = frappe.get_doc("Cheque", name)
	doc.mark_registered_in_sayad_payable()
	return doc


@frappe.whitelist()
def mark_sayad_success(name):
	"""Whitelisted method for Mark Sayad Success"""
	doc = frappe.get_doc("Cheque", name)
	doc.mark_sayad_success()
	return doc


@frappe.whitelist()
def mark_as_void(name):
	"""Whitelisted method for Mark As Void"""
	doc = frappe.get_doc("Cheque", name)
	doc.mark_as_void()
	return doc


class Cheque(Document):
	def before_insert(self):
		"""Set default status based on cheque_type"""
		if not self.status:
			if self.cheque_type == ChequeType.RECEIVABLE:
				self.status = ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER
			elif self.cheque_type == ChequeType.PAYABLE:
				self.status = PayableChequeStatus.PAYMENT_REQUEST_CREATED
	
	def validate(self):
		"""Validate cheque data"""
		# Set default status if not set
		if not self.status:
			if self.cheque_type == ChequeType.RECEIVABLE:
				self.status = ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER
			elif self.cheque_type == ChequeType.PAYABLE:
				self.status = PayableChequeStatus.PAYMENT_REQUEST_CREATED
		
		# Set party_type based on cheque_type if not set
		if not self.party_type:
			if self.cheque_type == ChequeType.RECEIVABLE:
				self.party_type = "Customer"
			elif self.cheque_type == ChequeType.PAYABLE:
				self.party_type = "Supplier"
		
		# Validate duplicate cheque number in same company
		if self.cheque_no and self.company:
			duplicate = frappe.db.exists("Cheque", {
				"cheque_no": self.cheque_no,
				"company": self.company,
				"name": ["!=", self.name]
			})
			if duplicate:
				frappe.throw(f"Cheque Number {self.cheque_no} already exists for company {self.company}")
		
		# Validate party is required
		if not self.party:
			frappe.throw(f"Party is required for {self.cheque_type} cheque")
		
		# Validate bank account for Payable cheques when issuing
		if self.cheque_type == ChequeType.PAYABLE and self.status == PayableChequeStatus.SELECT_BANK:
			if not self.bank_account:
				frappe.throw("Bank Account is required for Payable cheques")
		
		# Prevent Cheque User from manually changing status
		# Status should only be changed through action methods
		if self._doc_before_save and hasattr(self._doc_before_save, 'status'):
			if self.status != self._doc_before_save.status:
				# Allow if user has submit permission (Cheque Manager)
				if not frappe.has_permission("Cheque", "submit", self.name):
					frappe.throw("You do not have permission to change status. Please use action buttons.", frappe.PermissionError)
	
	def after_insert(self):
		"""Create Journal Entry when cheque is first created"""
		# For Receivable cheques in "Received From Customer" status, create Receive Journal Entry
		if (self.cheque_type == ChequeType.RECEIVABLE and 
			self.status == ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER):
			try:
				je = self.create_receive_entry()
				if je:
					# Save to persist journal_references
					self.save(ignore_permissions=True)
					frappe.db.commit()
					frappe.msgprint({
						"message": f"Journal Entry <a href='/app/journal-entry/{je.name}'>{je.name}</a> created for receiving cheque.",
						"indicator": "green"
					})
			except Exception as e:
				frappe.log_error(f"Error creating Receive Journal Entry: {str(e)}", "Cheque Receive Entry Error")
				# Don't block cheque creation if JE creation fails
				frappe.msgprint(f"Warning: Could not create Journal Entry: {str(e)}", indicator="orange")
	
	def get_cheque_settings(self):
		"""Get Cheque Settings for the company"""
		# Try to get by company filter first
		settings_name = frappe.db.get_value(
			"Cheque Settings",
			{"company": self.company},
			"name"
		)
		
		if settings_name:
			return frappe.get_doc("Cheque Settings", settings_name)
		
		# If not found, try to get single (if it's truly single)
		try:
			settings = frappe.get_single("Cheque Settings")
			if settings.company == self.company:
				return settings
		except frappe.DoesNotExistError:
			pass
		
		frappe.throw(f"Cheque Settings not found for company {self.company}. Please create Cheque Settings first.")
	
	def has_receive_entry(self):
		"""Check if Receive Journal Entry already exists for this cheque"""
		for ref in self.journal_references:
			if ref.purpose == JournalEntryPurpose.RECEIVE:
				return True
		return False
	
	def create_receive_entry(self, posting_date=None):
		"""
		Create Journal Entry for receiving cheque from customer
		For Receivable cheques only
		Debit: Receivable Cheque Account
		Credit: Customer Account (Accounts Receivable)
		"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("Receive entry is only for Receivable cheques")
		
		# Don't create if already exists
		if self.has_receive_entry():
			return None
		
		settings = self.get_cheque_settings()
		
		if not settings.default_receivable_cheque_account:
			frappe.throw("Default Receivable Cheque Account is not set in Cheque Settings")
		
		# Get customer account
		party_account = frappe.db.get_value(
			"Party Account",
			{"parent": self.party, "company": self.company},
			"account"
		)
		if not party_account:
			frappe.throw(f"Party Account not found for {self.party_type} {self.party} in company {self.company}")
		
		posting_date = posting_date or getdate()
		
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = self.company
		je.voucher_type = "Journal Entry"
		je.cheque_no = self.cheque_no
		je.user_remark = f"Cheque {self.cheque_no} received from customer"
		
		# Debit: Receivable Cheque Account
		je.append("accounts", {
			"account": settings.default_receivable_cheque_account,
			"debit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		# Credit: Customer Account (Accounts Receivable)
		je.append("accounts", {
			"account": party_account,
			"credit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		je.save()
		je.submit()
		
		# Add to journal references
		self.append("journal_references", {
			"journal_entry": je.name,
			"purpose": JournalEntryPurpose.RECEIVE,
			"posting_date": posting_date,
			"amount": self.cheque_amount,
		})
		
		return je
	
	def create_under_collection_entry(self, posting_date=None):
		"""
		Create Journal Entry for moving cheque to Under Collection
		For Receivable cheques only
		"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("Under Collection entry is only for Receivable cheques")
		
		settings = self.get_cheque_settings()
		
		if not settings.default_receivable_cheque_account:
			frappe.throw("Default Receivable Cheque Account is not set in Cheque Settings")
		if not settings.default_under_collection_account:
			frappe.throw("Default Under Collection Account is not set in Cheque Settings")
		
		posting_date = posting_date or getdate()
		
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = self.company
		je.voucher_type = "Journal Entry"
		je.cheque_no = self.cheque_no
		je.user_remark = f"Cheque {self.cheque_no} moved to Under Collection"
		
		# Debit: Under Collection Account
		je.append("accounts", {
			"account": settings.default_under_collection_account,
			"debit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		# Credit: Receivable Cheque Account
		je.append("accounts", {
			"account": settings.default_receivable_cheque_account,
			"credit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		je.save()
		je.submit()
		
		# Add to journal references
		self.append("journal_references", {
			"journal_entry": je.name,
			"purpose": JournalEntryPurpose.UNDER_COLLECTION,
			"posting_date": posting_date,
			"amount": self.cheque_amount,
		})
		
		self.status = ReceivableChequeStatus.UNDER_COLLECTION
		self.save()
		
		return je
	
	def create_collection_entry(self, posting_date=None, bank_account=None):
		"""
		Create Journal Entry for collecting the cheque
		For Receivable cheques only
		"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("Collection entry is only for Receivable cheques")
		
		settings = self.get_cheque_settings()
		
		if not settings.default_under_collection_account:
			frappe.throw("Default Under Collection Account is not set in Cheque Settings")
		
		bank_account = bank_account or settings.default_bank_account
		if not bank_account:
			frappe.throw("Bank Account is required. Set it in Cheque Settings or pass as parameter")
		
		posting_date = posting_date or getdate()
		
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = self.company
		je.voucher_type = "Journal Entry"
		je.cheque_no = self.cheque_no
		je.user_remark = f"Cheque {self.cheque_no} collected"
		
		# Debit: Bank Account
		je.append("accounts", {
			"account": bank_account,
			"debit_in_account_currency": self.cheque_amount,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		# Credit: Under Collection Account
		je.append("accounts", {
			"account": settings.default_under_collection_account,
			"credit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		je.save()
		je.submit()
		
		# Add to journal references
		self.append("journal_references", {
			"journal_entry": je.name,
			"purpose": JournalEntryPurpose.COLLECTED,
			"posting_date": posting_date,
			"amount": self.cheque_amount,
		})
		
		self.status = ReceivableChequeStatus.COLLECTED
		self.save()
		
		return je
	
	def create_return_entry(self, posting_date=None):
		"""
		Create Journal Entry for returning the cheque
		For Receivable cheques only
		"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("Return entry is only for Receivable cheques")
		
		settings = self.get_cheque_settings()
		
		if not settings.default_returned_cheque_account:
			frappe.throw("Default Returned Cheque Account is not set in Cheque Settings")
		
		# Determine source account based on current status
		source_account = None
		if self.status == ReceivableChequeStatus.UNDER_COLLECTION:
			if not settings.default_under_collection_account:
				frappe.throw("Default Under Collection Account is not set in Cheque Settings")
			source_account = settings.default_under_collection_account
		elif self.status == ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER:
			if not settings.default_receivable_cheque_account:
				frappe.throw("Default Receivable Cheque Account is not set in Cheque Settings")
			source_account = settings.default_receivable_cheque_account
		else:
			frappe.throw(f"Cannot return cheque from status: {self.status}")
		
		posting_date = posting_date or getdate()
		
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = self.company
		je.voucher_type = "Journal Entry"
		je.cheque_no = self.cheque_no
		je.user_remark = f"Cheque {self.cheque_no} returned"
		
		# Debit: Returned Cheque Account
		je.append("accounts", {
			"account": settings.default_returned_cheque_account,
			"debit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		# Credit: Source Account (Under Collection or Receivable)
		je.append("accounts", {
			"account": source_account,
			"credit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		je.save()
		je.submit()
		
		# Add to journal references
		self.append("journal_references", {
			"journal_entry": je.name,
			"purpose": JournalEntryPurpose.RETURNED,
			"posting_date": posting_date,
			"amount": self.cheque_amount,
		})
		
		self.status = ReceivableChequeStatus.RETURNED
		self.save()
		
		return je
	
	def create_payable_issue_entry(self, posting_date=None, bank_account=None):
		"""
		Create Journal Entry for issuing payable cheque
		For Payable cheques only
		"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("Payable Issue entry is only for Payable cheques")
		
		settings = self.get_cheque_settings()
		
		if not settings.default_payable_cheque_account:
			frappe.throw("Default Payable Cheque Account is not set in Cheque Settings")
		
		bank_account = bank_account or settings.default_bank_account
		if not bank_account:
			frappe.throw("Bank Account is required. Set it in Cheque Settings or pass as parameter")
		
		posting_date = posting_date or getdate()
		
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = self.company
		je.voucher_type = "Journal Entry"
		je.cheque_no = self.cheque_no
		je.user_remark = f"Payable Cheque {self.cheque_no} issued"
		
		# Debit: Payable Cheque Account
		je.append("accounts", {
			"account": settings.default_payable_cheque_account,
			"debit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		# Credit: Bank Account
		je.append("accounts", {
			"account": bank_account,
			"credit_in_account_currency": self.cheque_amount,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		je.save()
		je.submit()
		
		# Add to journal references
		self.append("journal_references", {
			"journal_entry": je.name,
			"purpose": JournalEntryPurpose.PAYABLE_ISSUE,
			"posting_date": posting_date,
			"amount": self.cheque_amount,
		})
		
		self.status = PayableChequeStatus.ISSUED
		self.save()
		
		return je
	
	def create_payable_clear_entry(self, posting_date=None):
		"""
		Create Journal Entry for clearing payable cheque
		For Payable cheques only
		"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("Payable Clear entry is only for Payable cheques")
		
		settings = self.get_cheque_settings()
		
		if not settings.default_payable_cheque_account:
			frappe.throw("Default Payable Cheque Account is not set in Cheque Settings")
		
		posting_date = posting_date or getdate()
		
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = self.company
		je.voucher_type = "Journal Entry"
		je.cheque_no = self.cheque_no
		je.user_remark = f"Payable Cheque {self.cheque_no} cleared"
		
		# Debit: Party Account (Supplier)
		party_account = frappe.db.get_value(
			"Party Account",
			{"parent": self.party, "company": self.company},
			"account"
		)
		if not party_account:
			frappe.throw(f"Party Account not found for {self.party_type} {self.party} in company {self.company}")
		
		je.append("accounts", {
			"account": party_account,
			"debit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		# Credit: Payable Cheque Account
		je.append("accounts", {
			"account": settings.default_payable_cheque_account,
			"credit_in_account_currency": self.cheque_amount,
			"party_type": self.party_type,
			"party": self.party,
			"reference_type": "Cheque",
			"reference_name": self.name,
		})
		
		je.save()
		je.submit()
		
		# Add to journal references
		self.append("journal_references", {
			"journal_entry": je.name,
			"purpose": JournalEntryPurpose.PAYABLE_CLEAR,
			"posting_date": posting_date,
			"amount": self.cheque_amount,
		})
		
		self.status = PayableChequeStatus.CLEARED
		self.save()
		
		return je
	
	# ========== Receivable Cheque Action Methods ==========
	
	def mark_waiting_for_sayad(self):
		"""Mark cheque as Waiting For Sayad"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark as Waiting For Sayad from status: {self.status}")
		
		self.status = ReceivableChequeStatus.WAITING_FOR_SAYAD
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} marked as Waiting For Sayad")
	
	def mark_registered_in_sayad(self):
		"""Mark cheque as Registered In Sayad"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.WAITING_FOR_SAYAD,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark as Registered In Sayad from status: {self.status}")
		
		self.status = ReceivableChequeStatus.REGISTERED_IN_SAYAD
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} marked as Registered In Sayad")
	
	def move_to_box(self):
		"""Move cheque to Box"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.REGISTERED_IN_SAYAD,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot move to Box from status: {self.status}")
		
		self.status = ReceivableChequeStatus.MOVE_TO_BOX
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} moved to Box")
	
	def assign_to_bank(self, posting_date=None):
		"""Assign cheque to bank (creates Under Collection Journal Entry)"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER,
			ReceivableChequeStatus.MOVE_TO_BOX,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot assign to bank from status: {self.status}")
		
		je = self.create_under_collection_entry(posting_date)
		frappe.msgprint({
			"message": f"Cheque {self.cheque_no} assigned to bank. Journal Entry <a href='/app/journal-entry/{je.name}'>{je.name}</a> created.",
			"indicator": "green"
		})
		return {"name": je.name, "message": je}
	
	def mark_as_collected(self, posting_date=None, bank_account=None):
		"""Mark cheque as collected (creates Collection Journal Entry)"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		# Check permission - only Cheque Manager can mark as collected
		if not frappe.has_permission("Cheque", "submit", self.name):
			frappe.throw("Only Cheque Manager can mark cheques as collected", frappe.PermissionError)
		
		allowed_statuses = [
			ReceivableChequeStatus.UNDER_COLLECTION,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark as collected from status: {self.status}")
		
		je = self.create_collection_entry(posting_date, bank_account)
		frappe.msgprint({
			"message": f"Cheque {self.cheque_no} marked as collected. Journal Entry <a href='/app/journal-entry/{je.name}'>{je.name}</a> created.",
			"indicator": "green"
		})
		return {"name": je.name, "message": je}
	
	def mark_as_returned_from_bank(self, posting_date=None):
		"""Mark cheque as returned from bank (creates Return Journal Entry)"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.UNDER_COLLECTION,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark as returned from bank from status: {self.status}")
		
		je = self.create_return_entry(posting_date)
		self.status = ReceivableChequeStatus.RETURNED_FROM_BANK
		self.save()
		frappe.msgprint({
			"message": f"Cheque {self.cheque_no} marked as returned from bank. Journal Entry <a href='/app/journal-entry/{je.name}'>{je.name}</a> created.",
			"indicator": "orange"
		})
		return {"name": je.name, "message": je}
	
	def return_to_customer(self):
		"""Return cheque to customer"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.RETURNED_FROM_BANK,
			ReceivableChequeStatus.RETURNED,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot return to customer from status: {self.status}")
		
		self.status = ReceivableChequeStatus.RETURN_TO_CUSTOMER
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} returned to customer")
	
	def reassign_to_bank(self, posting_date=None):
		"""Reassign cheque to bank (creates Under Collection Journal Entry)"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.RETURNED_FROM_BANK,
			ReceivableChequeStatus.RETURNED,
			ReceivableChequeStatus.RETRIEVED_FROM_BANK,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot reassign to bank from status: {self.status}")
		
		je = self.create_under_collection_entry(posting_date)
		frappe.msgprint({
			"message": f"Cheque {self.cheque_no} reassigned to bank. Journal Entry <a href='/app/journal-entry/{je.name}'>{je.name}</a> created.",
			"indicator": "green"
		})
		return {"name": je.name, "message": je}
	
	def return_not_registered_to_customer(self):
		"""Return cheque to customer when customer doesn't register in Sayad"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.WAITING_FOR_SAYAD,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot return not registered cheque from status: {self.status}")
		
		self.status = ReceivableChequeStatus.RETURNED_NOT_REGISTERED
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} returned to customer (not registered in Sayad)")
	
	def return_registered_to_customer(self):
		"""Return registered cheque to customer (cancellation/return request)"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.REGISTERED_IN_SAYAD,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot return registered cheque from status: {self.status}")
		
		self.status = ReceivableChequeStatus.RETURNED_REGISTERED_TO_CUSTOMER
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} returned to customer (registered in Sayad)")
	
	def retrieve_from_bank(self):
		"""Retrieve cheque from bank without action (no collection, no return)"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.UNDER_COLLECTION,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot retrieve from bank from status: {self.status}")
		
		# Reverse the Under Collection Journal Entry
		# Find the most recent Under Collection JE
		under_collection_je = None
		for ref in self.journal_references:
			if ref.purpose == JournalEntryPurpose.UNDER_COLLECTION:
				under_collection_je = ref.journal_entry
				break
		
		if under_collection_je:
			# Cancel the JE if not already cancelled
			je_doc = frappe.get_doc("Journal Entry", under_collection_je)
			if je_doc.docstatus == 1:
				je_doc.cancel()
				frappe.msgprint(f"Under Collection Journal Entry {under_collection_je} cancelled")
		
		self.status = ReceivableChequeStatus.RETRIEVED_FROM_BANK
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} retrieved from bank")
	
	def move_back_to_box_from_retrieved(self):
		"""Move cheque back to box after retrieving from bank"""
		if self.cheque_type != ChequeType.RECEIVABLE:
			frappe.throw("This action is only for Receivable cheques")
		
		allowed_statuses = [
			ReceivableChequeStatus.RETRIEVED_FROM_BANK,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot move to box from status: {self.status}")
		
		self.status = ReceivableChequeStatus.MOVE_TO_BOX
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} moved back to Box")
	
	# ========== Payable Cheque Action Methods ==========
	
	def select_bank(self):
		"""Select bank for payable cheque"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.PAYMENT_REQUEST_CREATED,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot select bank from status: {self.status}")
		
		if not self.bank_account:
			frappe.throw("Bank Account is required. Please select a bank account first.")
		
		self.status = PayableChequeStatus.SELECT_BANK
		self.save()
		frappe.msgprint(f"Bank {self.bank_account} selected for cheque {self.cheque_no}")
	
	def issue_cheque(self, posting_date=None):
		"""Issue payable cheque (creates Payable Issue Journal Entry)"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.SELECT_BANK,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot issue cheque from status: {self.status}")
		
		if not self.bank_account:
			frappe.throw("Bank Account is required")
		
		je = self.create_payable_issue_entry(posting_date, self.bank_account)
		frappe.msgprint({
			"message": f"Cheque {self.cheque_no} issued. Journal Entry <a href='/app/journal-entry/{je.name}'>{je.name}</a> created.",
			"indicator": "green"
		})
		return {"name": je.name, "message": je}
	
	def mark_as_printed(self):
		"""Mark cheque as printed"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.ISSUED,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark as printed from status: {self.status}")
		
		self.status = PayableChequeStatus.MARKED_AS_PRINTED
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} marked as printed")
	
	def first_signature_done(self):
		"""Mark first signature as done"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.MARKED_AS_PRINTED,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark first signature from status: {self.status}")
		
		self.status = PayableChequeStatus.FIRST_SIGNATURE_DONE
		self.save()
		frappe.msgprint(f"First signature done for cheque {self.cheque_no}")
	
	def second_signature_done(self):
		"""Mark second signature as done"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.FIRST_SIGNATURE_DONE,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark second signature from status: {self.status}")
		
		self.status = PayableChequeStatus.SECOND_SIGNATURE_DONE
		self.save()
		frappe.msgprint(f"Second signature done for cheque {self.cheque_no}")
	
	def notify_supplier(self):
		"""Notify supplier about cheque"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.SECOND_SIGNATURE_DONE,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot notify supplier from status: {self.status}")
		
		if not self.party:
			frappe.throw("Supplier is required")
		
		self.status = PayableChequeStatus.NOTIFY_SUPPLIER
		self.save()
		frappe.msgprint(f"Supplier {self.party} notified about cheque {self.cheque_no}")
	
	def deliver_to_supplier(self):
		"""Mark cheque as delivered to supplier"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.NOTIFY_SUPPLIER,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot deliver to supplier from status: {self.status}")
		
		self.status = PayableChequeStatus.DELIVER_TO_SUPPLIER
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} delivered to supplier")
	
	def mark_registered_in_sayad_payable(self):
		"""Mark payable cheque as registered in Sayad"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.DELIVER_TO_SUPPLIER,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark as registered in Sayad from status: {self.status}")
		
		self.status = PayableChequeStatus.REGISTERED_IN_SAYAD
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} marked as registered in Sayad")
	
	def mark_sayad_success(self):
		"""Mark Sayad registration as successful"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		allowed_statuses = [
			PayableChequeStatus.REGISTERED_IN_SAYAD,
		]
		
		if self.status not in allowed_statuses:
			frappe.throw(f"Cannot mark Sayad success from status: {self.status}")
		
		self.status = PayableChequeStatus.SAYAD_SUCCESS
		self.save()
		frappe.msgprint(f"Sayad registration successful for cheque {self.cheque_no}")
	
	def mark_as_void(self):
		"""Mark cheque as void (before clearing)"""
		if self.cheque_type != ChequeType.PAYABLE:
			frappe.throw("This action is only for Payable cheques")
		
		# Check permission - only Cheque Manager can void cheques
		if not frappe.has_permission("Cheque", "cancel", self.name):
			frappe.throw("Only Cheque Manager can void cheques", frappe.PermissionError)
		
		# Cannot void if already cleared
		if self.status == PayableChequeStatus.CLEARED:
			frappe.throw("Cannot void a cleared cheque")
		
		# Cannot void if already cancelled
		if self.status == PayableChequeStatus.CANCELLED:
			frappe.throw("Cheque is already cancelled")
		
		self.status = PayableChequeStatus.VOID
		self.save()
		frappe.msgprint(f"Cheque {self.cheque_no} marked as void")


def on_cheque_update(doc, method=None):
	"""
	Hook called when Cheque document is updated
	Handles workflow state changes and creates Journal Entries automatically
	"""
	# Only process if workflow_state has changed
	if not hasattr(doc, '_doc_before_save') or not doc._doc_before_save:
		return
	
	old_workflow_state = doc._doc_before_save.get('workflow_state') if doc._doc_before_save else None
	new_workflow_state = doc.get('workflow_state')
	
	# If workflow_state changed, update status and create Journal Entry if needed
	if old_workflow_state != new_workflow_state and new_workflow_state:
		# Map workflow_state to status
		workflow_to_status_map = {
			"Received From Customer": ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER,
			"Waiting For Sayad": ReceivableChequeStatus.WAITING_FOR_SAYAD,
			"Registered In Sayad": ReceivableChequeStatus.REGISTERED_IN_SAYAD,
			"Move To Box": ReceivableChequeStatus.MOVE_TO_BOX,
			"Under Collection": ReceivableChequeStatus.UNDER_COLLECTION,
			"Collected": ReceivableChequeStatus.COLLECTED,
			"Returned From Bank": ReceivableChequeStatus.RETURNED_FROM_BANK,
			"Returned": ReceivableChequeStatus.RETURNED,
			"Return To Customer": ReceivableChequeStatus.RETURN_TO_CUSTOMER,
			"Retrieved From Bank": ReceivableChequeStatus.RETRIEVED_FROM_BANK,
			"Payment Request Created": PayableChequeStatus.PAYMENT_REQUEST_CREATED,
			"Select Bank": PayableChequeStatus.SELECT_BANK,
			"Issued": PayableChequeStatus.ISSUED,
			"Mark As Printed": PayableChequeStatus.MARKED_AS_PRINTED,
			"First Signature Done": PayableChequeStatus.FIRST_SIGNATURE_DONE,
			"Second Signature Done": PayableChequeStatus.SECOND_SIGNATURE_DONE,
			"Notify Supplier": PayableChequeStatus.NOTIFY_SUPPLIER,
			"Deliver To Supplier": PayableChequeStatus.DELIVER_TO_SUPPLIER,
			"Mark Registered In Sayad": PayableChequeStatus.REGISTERED_IN_SAYAD,
			"Mark Sayad Success": PayableChequeStatus.SAYAD_SUCCESS,
			"Cleared": PayableChequeStatus.CLEARED,
			"Mark As Void": PayableChequeStatus.VOID,
		}
		
		# Update status field
		if new_workflow_state in workflow_to_status_map:
			doc.status = workflow_to_status_map[new_workflow_state]
		
		# Create Journal Entry based on workflow state change
		create_je_for_workflow_state(doc, old_workflow_state, new_workflow_state)


def create_je_for_workflow_state(doc, old_state, new_state):
	"""
	Create Journal Entry automatically when workflow state changes to specific states
	"""
	try:
		# Receivable Cheque Journal Entry creation
		if doc.cheque_type == ChequeType.RECEIVABLE:
			if new_state == "Under Collection" and old_state != "Under Collection":
				# Assign to bank - create Under Collection JE
				doc.create_under_collection_entry()
				# Submit the document if not already submitted
				if doc.docstatus == 0:
					doc.submit()
			
			elif new_state == "Collected" and old_state != "Collected":
				# Mark as collected - create Collection JE
				doc.create_collection_entry()
				# Submit the document if not already submitted
				if doc.docstatus == 0:
					doc.submit()
			
			elif new_state == "Returned From Bank" and old_state != "Returned From Bank":
				# Mark as returned - create Return JE
				doc.create_return_entry()
				# Submit the document if not already submitted
				if doc.docstatus == 0:
					doc.submit()
			
			elif new_state == "Received From Customer" and old_state != "Received From Customer":
				# Create Receive JE if not exists
				if not doc.has_receive_entry():
					doc.create_receive_entry()
					# Submit the document if not already submitted
					if doc.docstatus == 0:
						doc.submit()
		
		# Payable Cheque Journal Entry creation
		elif doc.cheque_type == ChequeType.PAYABLE:
			if new_state == "Issued" and old_state != "Issued":
				# Issue cheque - create Payable Issue JE
				doc.create_payable_issue_entry(posting_date=None, bank_account=doc.bank_account)
				# Submit the document if not already submitted
				if doc.docstatus == 0:
					doc.submit()
			
			elif new_state == "Cleared" and old_state != "Cleared":
				# Clear cheque - create Payable Clear JE
				doc.create_payable_clear_entry()
				# Submit the document if not already submitted
				if doc.docstatus == 0:
					doc.submit()
	
	except Exception as e:
		frappe.log_error(f"Error creating Journal Entry for workflow state change: {str(e)}", "Cheque Workflow JE Error")
		frappe.msgprint(f"Warning: Could not create Journal Entry automatically: {str(e)}", indicator="orange")


def before_cheque_delete(doc, method=None):
	"""
	Hook called before Cheque document is deleted
	Prevents deletion of submitted/finalized documents
	"""
	# Prevent deletion if document is submitted
	if doc.docstatus == 1:
		frappe.throw("Cannot delete a submitted Cheque document. Please cancel it first.", frappe.ValidationError)
	
	# Prevent deletion if document has Journal Entries
	if doc.journal_references and len(doc.journal_references) > 0:
		frappe.throw("Cannot delete a Cheque that has Journal Entries. Please cancel the Journal Entries first.", frappe.ValidationError)
	
	# Prevent deletion if in certain final states
	final_states = [
		ReceivableChequeStatus.COLLECTED,
		ReceivableChequeStatus.RETURN_TO_CUSTOMER,
		PayableChequeStatus.CLEARED,
		PayableChequeStatus.VOID,
	]
	
	if doc.status in final_states:
		frappe.throw(f"Cannot delete a Cheque in final state: {doc.status}", frappe.ValidationError)


def on_cheque_update_after_submit(doc, method=None):
	"""
	Hook called when Cheque document is updated after submission
	Handles workflow state changes for submitted documents
	"""
	# Only process if workflow_state has changed
	if not hasattr(doc, '_doc_before_save') or not doc._doc_before_save:
		return
	
	old_workflow_state = doc._doc_before_save.get('workflow_state') if doc._doc_before_save else None
	new_workflow_state = doc.get('workflow_state')
	
	# If workflow_state changed, update status
	if old_workflow_state != new_workflow_state and new_workflow_state:
		# Map workflow_state to status
		workflow_to_status_map = {
			"Received From Customer": ReceivableChequeStatus.RECEIVED_FROM_CUSTOMER,
			"Waiting For Sayad": ReceivableChequeStatus.WAITING_FOR_SAYAD,
			"Registered In Sayad": ReceivableChequeStatus.REGISTERED_IN_SAYAD,
			"Move To Box": ReceivableChequeStatus.MOVE_TO_BOX,
			"Under Collection": ReceivableChequeStatus.UNDER_COLLECTION,
			"Collected": ReceivableChequeStatus.COLLECTED,
			"Returned From Bank": ReceivableChequeStatus.RETURNED_FROM_BANK,
			"Returned": ReceivableChequeStatus.RETURNED,
			"Return To Customer": ReceivableChequeStatus.RETURN_TO_CUSTOMER,
			"Retrieved From Bank": ReceivableChequeStatus.RETRIEVED_FROM_BANK,
			"Payment Request Created": PayableChequeStatus.PAYMENT_REQUEST_CREATED,
			"Select Bank": PayableChequeStatus.SELECT_BANK,
			"Issued": PayableChequeStatus.ISSUED,
			"Mark As Printed": PayableChequeStatus.MARKED_AS_PRINTED,
			"First Signature Done": PayableChequeStatus.FIRST_SIGNATURE_DONE,
			"Second Signature Done": PayableChequeStatus.SECOND_SIGNATURE_DONE,
			"Notify Supplier": PayableChequeStatus.NOTIFY_SUPPLIER,
			"Deliver To Supplier": PayableChequeStatus.DELIVER_TO_SUPPLIER,
			"Mark Registered In Sayad": PayableChequeStatus.REGISTERED_IN_SAYAD,
			"Mark Sayad Success": PayableChequeStatus.SAYAD_SUCCESS,
			"Cleared": PayableChequeStatus.CLEARED,
			"Mark As Void": PayableChequeStatus.VOID,
		}
		
		# Update status field
		if new_workflow_state in workflow_to_status_map:
			doc.status = workflow_to_status_map[new_workflow_state]
		
		# Create Journal Entry based on workflow state change
		create_je_for_workflow_state(doc, old_workflow_state, new_workflow_state)


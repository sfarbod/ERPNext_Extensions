"""
Utility module for Cheque Management
Defines status constants and helper functions
"""

from enum import Enum


class ReceivableChequeStatus:
	"""Status constants for Receivable Cheques"""
	RECEIVED_FROM_CUSTOMER = "Received From Customer"
	WAITING_FOR_SAYAD = "Waiting For Sayad"
	REGISTERED_IN_SAYAD = "Registered In Sayad"
	MOVE_TO_BOX = "Move To Box"
	UNDER_COLLECTION = "Under Collection"
	COLLECTED = "Collected"
	RETURNED_FROM_BANK = "Returned From Bank"
	RETURNED = "Returned"
	RETURN_TO_CUSTOMER = "Return To Customer"
	RETURNED_NOT_REGISTERED = "Returned Not Registered"
	RETURNED_REGISTERED_TO_CUSTOMER = "Returned Registered To Customer"
	RETRIEVED_FROM_BANK = "Retrieved From Bank"
	CANCELLED = "Cancelled"

	@classmethod
	def get_all(cls):
		"""Get all valid receivable cheque statuses"""
		return [
			cls.RECEIVED_FROM_CUSTOMER,
			cls.WAITING_FOR_SAYAD,
			cls.REGISTERED_IN_SAYAD,
			cls.MOVE_TO_BOX,
			cls.UNDER_COLLECTION,
			cls.COLLECTED,
			cls.RETURNED_FROM_BANK,
			cls.RETURNED,
			cls.RETURN_TO_CUSTOMER,
			cls.RETURNED_NOT_REGISTERED,
			cls.RETURNED_REGISTERED_TO_CUSTOMER,
			cls.RETRIEVED_FROM_BANK,
			cls.CANCELLED,
		]

	@classmethod
	def is_valid(cls, status):
		"""Check if status is valid for receivable cheques"""
		return status in cls.get_all()


class PayableChequeStatus:
	"""Status constants for Payable Cheques"""
	PAYMENT_REQUEST_CREATED = "Payment Request Created"
	SELECT_BANK = "Select Bank"
	ISSUED = "Issued"
	MARKED_AS_PRINTED = "Mark As Printed"
	FIRST_SIGNATURE_DONE = "First Signature Done"
	SECOND_SIGNATURE_DONE = "Second Signature Done"
	NOTIFY_SUPPLIER = "Notify Supplier"
	DELIVER_TO_SUPPLIER = "Deliver To Supplier"
	REGISTERED_IN_SAYAD = "Mark Registered In Sayad"
	SAYAD_SUCCESS = "Mark Sayad Success"
	CLEARED = "Cleared"
	VOID = "Mark As Void"
	CANCELLED = "Cancelled"

	@classmethod
	def get_all(cls):
		"""Get all valid payable cheque statuses"""
		return [
			cls.PAYMENT_REQUEST_CREATED,
			cls.SELECT_BANK,
			cls.ISSUED,
			cls.MARKED_AS_PRINTED,
			cls.FIRST_SIGNATURE_DONE,
			cls.SECOND_SIGNATURE_DONE,
			cls.NOTIFY_SUPPLIER,
			cls.DELIVER_TO_SUPPLIER,
			cls.REGISTERED_IN_SAYAD,
			cls.SAYAD_SUCCESS,
			cls.CLEARED,
			cls.VOID,
			cls.CANCELLED,
		]

	@classmethod
	def is_valid(cls, status):
		"""Check if status is valid for payable cheques"""
		return status in cls.get_all()


class JournalEntryPurpose:
	"""Purpose constants for Journal Entry references"""
	RECEIVE = "Receive"
	UNDER_COLLECTION = "Under Collection"
	COLLECTED = "Collected"
	RETURNED = "Returned"
	PAYABLE_ISSUE = "Payable Issue"
	PAYABLE_CLEAR = "Payable Clear"
	CANCEL = "Cancel"

	@classmethod
	def get_all(cls):
		"""Get all valid journal entry purposes"""
		return [
			cls.RECEIVE,
			cls.UNDER_COLLECTION,
			cls.COLLECTED,
			cls.RETURNED,
			cls.PAYABLE_ISSUE,
			cls.PAYABLE_CLEAR,
			cls.CANCEL,
		]


class ChequeType:
	"""Cheque type constants"""
	RECEIVABLE = "Receivable"
	PAYABLE = "Payable"

	@classmethod
	def get_all(cls):
		"""Get all valid cheque types"""
		return [cls.RECEIVABLE, cls.PAYABLE]


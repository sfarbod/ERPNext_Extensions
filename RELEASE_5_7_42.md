# erpnext_extensions 5.7.42

## Multiple Advance Received and Advance Paid Accounts

Version **5.7.42** allows a company to use **multiple** separate-party Advance Received and Advance Paid accounts, while preserving existing Company and Party default configuration.

---

## New Features

### Account-level advance classification

Accounts can be classified with **Advance Account Type**:

| Value | Meaning |
| --- | --- |
| *(blank)* | Not a separate-party advance account |
| Advance Received | Eligible for customer advance receipts |
| Advance Paid | Eligible for supplier advance payments |

**Advance Received** accounts must be ledger accounts with Root Type **Liability** and Account Type **Receivable**, enabled, belonging to the company, and in company currency.

**Advance Paid** accounts must be ledger accounts with Root Type **Asset** and Account Type **Payable**, with the same company and currency rules.

Invalid classifications are rejected on save.

### Multi-account advance recognition

Any correctly classified advance account can be used on Payment Entry. Advances booked to different eligible accounts remain discoverable for invoice allocation and Payment Reconciliation (not limited to a single Company default).

---

## Improvements

### Payment Entry

When **Book Advance Payments in Separate Party Account** is enabled on Company:

| Scenario | Party account field | Account used | Advance checkbox |
| --- | --- | --- | --- |
| Customer receipt (normal) | Paid From | Ordinary Receivable (e.g. Debtors) | Off |
| Customer advance | Paid From | Advance Received | On (automatic) |
| Supplier payment (normal) | Paid To | Ordinary Payable (e.g. Creditors) | Off |
| Supplier advance | Paid To | Advance Paid | On (automatic) |

- The Payment Entry advance checkbox remains **read-only** and is computed by the server.
- An eligible advance account selected by the user is **not** overwritten by the Company default during validation.
- Using an Advance Paid account on a Receive entry (or Advance Received on a Pay entry) is rejected.
- With the Company feature **off**, standard ERPNext behaviour is unchanged.

### Invoice advance allocation

- Sales Invoice can discover advances from **multiple** Advance Received accounts for the same customer.
- Purchase Invoice can discover advances from **multiple** Advance Paid accounts for the same supplier.
- Order-linked advances, partial allocation, and existing advance GL reclassification paths are preserved (ERPNext’s separate-party advance mechanism is reused; no parallel GL engine).

### Payment Reconciliation

Payment Reconciliation can consider all eligible classified advance accounts for the party/company, in addition to the manually selected default advance account field where applicable.

---

## Compatibility

- **Company defaults** (`Default Advance Received Account`, `Default Advance Paid Account`) remain supported and continue to act as the preferred defaults.
- **Party / Party Group** advance accounts remain supported.
- **Journal Entry** advances continue to use `Is Advance` on normal Receivable/Payable party accounts. Posting Journal Entries against separate-party advance accounts still shows a warning and is not treated as the Payment Entry advance workflow. The warning now covers all classified separate advance accounts. Hard blocking is not applied, so Post Dated Cheque (PDC) Journal Entries that use advance accounts continue to work.
- **PDC** helpers validate document-level advance account overrides against classification where present, and settlement / application validation can consider the set of eligible advance accounts rather than only a single Company default.

---

## Technical Changes

- Custom Field `advance_account_type` on **Account**
- Runtime helpers for eligible-account resolution (classified accounts ∪ configured Company/Party defaults for backward compatibility)
- Payment Entry, Journal Entry, and Payment Reconciliation behaviour extensions via supported hooks
- Widened advance Payment Entry discovery query for multi-account eligibility
- Install/migrate seed for existing configured Company and Party advance accounts (sets classification when empty and structurally valid; does not silently overwrite conflicting classifications)
- Client scripts for Account and Payment Entry UX hints

---

## Migration Notes

1. Run `bench migrate` (and clear cache) so the Account field, seed patch, and runtime hooks are applied.
2. Review Chart of Accounts: set **Advance Account Type** on any additional advance ledgers that should participate in separate-party advances.
3. Confirm Company default advance accounts remain set when the Company feature is enabled.
4. No historical General Ledger or submitted voucher data is rewritten by this release.

---

## Examples (fictional)

| Role | Example |
| --- | --- |
| Company | Example Company |
| Customer | Example Customer |
| Supplier | Example Supplier |
| Advance Received ledger | ADV-RECEIVED-001 |
| Advance Paid ledger | ADV-PAID-001 |
| Ordinary receivable | Debtors |
| Ordinary payable | Creditors |

---

## Known Limitations

- Journal Entry is **not** converted to the Payment Entry separate-party advance workflow; JE advances remain the classic `Is Advance` model on AR/AP.
- Full interactive Payment Reconciliation multi-account allocation in the Desk UI should be verified in the target environment after upgrade.
- End-to-end Sales Invoice / Purchase Invoice allocate → unreconcile → cancel cycles should be verified in the target environment after upgrade (automated coverage focused on classification, Payment Entry flag behaviour, account preservation, GL posting for advances, and multi-account discovery).

---

## Upgrade Checklist

1. Upgrade to **erpnext_extensions 5.7.42**
2. `bench migrate` and clear cache
3. Classify additional advance accounts as needed
4. Spot-check: Receive with Advance Received, Pay with Advance Paid, normal Debtors/Creditors with Company feature on
5. Spot-check: Get Advances on Example Sales Invoice / Example Purchase Invoice when multiple advance Payment Entries exist

# decimal_expand/patches/post_model_sync/expand_currency_precision.py
import frappe

TARGET_PRECISION = 30  # M
TARGET_SCALE = 9       # D

TABLES_AND_COLUMNS = {
    # Parent
    "tabJournal Entry": [
        "total_debit", "total_credit", "difference", "total_amount"
    ],
    # Child
    "tabJournal Entry Account": [
        "debit", "credit",
        "debit_in_account_currency", "credit_in_account_currency"
       
    ],
    # GL Entry (recommended)
    "tabGL Entry": [
        "debit", "credit",
        "debit_in_account_currency", "credit_in_account_currency",
        "debit_in_transaction_currency", "credit_in_transaction_currency"
    ],
    # Payment Entry
    "tabPayment Entry": [
        "paid_from_account_balance",
        "paid_to_account_balance",
        "party_balance"
    ],
}

def execute():
    db = frappe.db
   # Current database name
    db_name = db.sql("SELECT DATABASE()")[0][0]

    def current_numeric_pd(table, col):
        row = db.sql("""
            SELECT NUMERIC_PRECISION, NUMERIC_SCALE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """, (db_name, table, col), as_dict=True)
        return (row[0]["NUMERIC_PRECISION"], row[0]["NUMERIC_SCALE"]) if row else (None, None)

    def ensure_decimal(table, col, p, s):
        cur_p, cur_s = current_numeric_pd(table, col)

        frappe.logger().info(
        f"Checking {table}.{col}: current=({cur_p},{cur_s}), target=({TARGET_PRECISION},{TARGET_SCALE})"
         )
        # If the column does not exist or is not of type DECIMAL, try to directly MODIFY it
        # If it exists but is smaller than the target, enlarge it
        if (cur_p is None) or (cur_p < TARGET_PRECISION) or (cur_s is None) or (cur_s < TARGET_SCALE):
            db.sql(f"""
                ALTER TABLE `{table}`
                MODIFY `{col}` DECIMAL({TARGET_PRECISION},{TARGET_SCALE}) NOT NULL DEFAULT 0
            """)

    # ============================================
    # Task 1: Expand decimal precision for numeric fields
    # ============================================
    frappe.logger().info("Starting Task 1: Expanding decimal precision for numeric fields")
    for table, cols in TABLES_AND_COLUMNS.items():
        for col in cols:
            ensure_decimal(table, col, TARGET_PRECISION, TARGET_SCALE)
    frappe.logger().info("Completed Task 1: Decimal precision expansion")

    # ============================================
    # Task 2: Increase total_amount_in_words field length
    # ============================================
    # This is separate from Task 1 - it handles text field length, not numeric precision
    # Default is 140, increasing to 300 to handle very large numbers in words
    # (300 is sufficient for numbers up to 30 digits precision)
    frappe.logger().info("Starting Task 2: Increasing total_amount_in_words field length")
    TARGET_TEXT_LENGTH = 300
    text_field_table = "tabJournal Entry"
    text_field_column = "total_amount_in_words"
    
    # Check current character length in database
    current_length_row = db.sql("""
        SELECT CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
    """, (db_name, text_field_table, text_field_column), as_dict=True)
    
    if current_length_row:
        current_length = current_length_row[0].get("CHARACTER_MAXIMUM_LENGTH")
        frappe.logger().info(
            f"Checking {text_field_table}.{text_field_column}: current_length={current_length}, target={TARGET_TEXT_LENGTH}"
        )
        
        if current_length is None or current_length < TARGET_TEXT_LENGTH:
            db.sql(f"""
                ALTER TABLE `{text_field_table}`
                MODIFY `{text_field_column}` VARCHAR({TARGET_TEXT_LENGTH})
            """)
            frappe.logger().info(
                f"Updated {text_field_table}.{text_field_column} length to {TARGET_TEXT_LENGTH}"
            )
        else:
            frappe.logger().info(
                f"{text_field_table}.{text_field_column} already has sufficient length ({current_length})"
            )
    else:
        frappe.logger().warning(
            f"Column {text_field_table}.{text_field_column} not found, skipping length update"
        )
    
    # Also update the doctype field definition to match database
    try:
        doctype_name = "Journal Entry"
        field_name = "total_amount_in_words"
        
        if frappe.db.exists("DocType", doctype_name):
            doctype = frappe.get_doc("DocType", doctype_name)
            field_found = False
            
            for field in doctype.fields:
                if field.fieldname == field_name:
                    if field.max_length is None or field.max_length < TARGET_TEXT_LENGTH:
                        field.max_length = TARGET_TEXT_LENGTH
                        field_found = True
                        frappe.logger().info(
                            f"Updated doctype field {doctype_name}.{field_name} max_length to {TARGET_TEXT_LENGTH}"
                        )
                        break
            
            if field_found:
                doctype.save(ignore_permissions=True)
                frappe.db.commit()
            else:
                frappe.logger().info(
                    f"Doctype field {doctype_name}.{field_name} already has sufficient max_length or not found"
                )
    except Exception as e:
        frappe.logger().warning(
            f"Could not update doctype field definition: {str(e)}"
        )
    
    frappe.logger().info("Completed Task 2: Text field length update")
    db.commit()

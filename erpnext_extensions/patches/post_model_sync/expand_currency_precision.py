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
        # If the column does not exist or is not of type DECIMAL, try to directly MODIFY it
        # If it exists but is smaller than the target, enlarge it
        if (cur_p is None) or (cur_p < p) or (cur_s is None) or (cur_s < s):
            db.sql(f"""
                ALTER TABLE `{table}`
                MODIFY `{col}` DECIMAL({p},{s}) NOT NULL DEFAULT 0
            """)

    # Execute changes
    for table, cols in TABLES_AND_COLUMNS.items():
        for col in cols:
            ensure_decimal(table, col, TARGET_PRECISION, TARGET_SCALE)

    db.commit()

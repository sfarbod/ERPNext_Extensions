# راهنمای اجرای تست‌های Cheque Management

## پیش‌نیازها

1. ✅ سرور Frappe در حال اجرا باشد (`bench start`)
2. ✅ Migrate انجام شده باشد (`bench --site frontend migrate`)
3. ✅ حداقل یک Company، Customer و Supplier وجود داشته باشد

## روش 1: اجرای تست از طریق Frappe Console

### مرحله 1: باز کردن Console

```bash
bench --site frontend console
```

### مرحله 2: اجرای تست

در Console که باز شده است:

```python
# Import test script
exec(open('apps/erpnext_extensions/erpnext_extensions/cheque_management/test_cheque_management.py').read())

# یا مستقیماً:
from erpnext_extensions.cheque_management.test_cheque_management import run_all_tests
run_all_tests()
```

## روش 2: اجرای تست از طریق UI

### مرحله 1: ایجاد Cheque Settings

1. به `http://mysite.localhost:8000` بروید
2. وارد سیستم شوید
3. به **Cheque Management > Cheque Settings** بروید
4. Company را انتخاب کنید
5. حساب‌های مورد نیاز را تنظیم کنید:
   - Default Receivable Cheque Account
   - Default Under Collection Account
   - Default Returned Cheque Account
   - Default Payable Cheque Account
   - Default Bank Account
6. Save کنید

### مرحله 2: ایجاد چک دریافتنی

1. به **Cheque Management > Cheque** بروید
2. New را بزنید
3. نوع را "Receivable" انتخاب کنید
4. اطلاعات را پر کنید:
   - Company
   - Cheque No
   - Cheque Date
   - Cheque Amount
   - Party (Customer)
5. Save کنید
6. دکمه‌های action را تست کنید:
   - Mark Waiting For Sayad
   - Mark Registered In Sayad
   - Move To Box
   - Assign To Bank (باید Journal Entry ایجاد شود)
   - Mark As Collected (نیاز به Cheque Manager)

### مرحله 3: ایجاد چک پرداختنی

1. به **Cheque Management > Cheque** بروید
2. New را بزنید
3. نوع را "Payable" انتخاب کنید
4. اطلاعات را پر کنید:
   - Company
   - Cheque No
   - Cheque Date
   - Cheque Amount
   - Party (Supplier)
   - Bank Account
5. Save کنید
6. دکمه‌های action را تست کنید:
   - Select Bank
   - Issue Cheque (باید Journal Entry ایجاد شود)
   - Mark As Printed
   - First Signature Done
   - Second Signature Done
   - Notify Supplier
   - Deliver To Supplier
   - Mark Registered In Sayad
   - Mark Sayad Success

## روش 3: اجرای تست از طریق Python Script

### در ترمینال:

```bash
bench --site frontend execute erpnext_extensions.cheque_management.test_cheque_management.run_all_tests
```

## بررسی نتایج

### 1. بررسی Journal Entries

- به **Accounting > Journal Entry** بروید
- باید Journal Entry های ایجاد شده را ببینید
- هر Journal Entry باید reference به Cheque داشته باشد

### 2. بررسی Cheque Records

- به **Cheque Management > Cheque** بروید
- چک‌های ایجاد شده را ببینید
- در جدول "Journal References" باید Journal Entry ها لینک شده باشند

### 3. بررسی Status Indicator

- فیلد Status باید رنگی باشد
- رنگ‌ها باید بر اساس وضعیت تغییر کنند

### 4. بررسی Permissions

- با نقش "Cheque User" وارد شوید
- نباید دکمه‌های "Mark As Collected" و "Mark As Void" را ببینید
- با نقش "Cheque Manager" باید همه دکمه‌ها را ببینید

## Troubleshooting

### خطا: "Cheque Settings not found"
**راه حل**: Cheque Settings را به صورت دستی ایجاد کنید

### خطا: "Account not found"
**راه حل**: حساب‌های مورد نیاز را در Chart of Accounts ایجاد کنید:
- Receivable Cheque Account (Asset)
- Under Collection Account (Asset)
- Returned Cheque Account (Asset)
- Payable Cheque Account (Liability)
- Bank Account (Bank)

### خطا: "Party Account not found"
**راه حل**: مطمئن شوید که Customer/Supplier دارای Party Account برای Company است

### خطا: "Permission denied"
**راه حل**: مطمئن شوید که کاربر نقش مناسب دارد (Cheque Manager برای عملیات حساس)

## دستورات مفید

```bash
# Migrate
bench --site frontend migrate

# Clear cache
bench --site frontend clear-cache

# Restart
bench restart

# View logs
bench --site frontend logs

# Console
bench --site frontend console
```


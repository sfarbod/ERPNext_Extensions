# خلاصه پشتیبانی Workflow دیاگرام

## ✅ وضعیت‌های جدید اضافه شده:

### 1. Returned Not Registered
- **از دیاگرام**: برگشت چک به مشتری (ثبت نشد)
- **از وضعیت**: `Waiting For Sayad`
- **به وضعیت**: `Returned Not Registered`
- **Action**: `return_not_registered_to_customer()`

### 2. Returned Registered To Customer
- **از دیاگرام**: عودت چک ثبت‌شده و تحویل به مشتری
- **از وضعیت**: `Registered In Sayad`
- **به وضعیت**: `Returned Registered To Customer`
- **Action**: `return_registered_to_customer()`

### 3. Retrieved From Bank
- **از دیاگرام**: پس گرفتن چک از بانک (بدون اقدام)
- **از وضعیت**: `Under Collection`
- **به وضعیت**: `Retrieved From Bank`
- **Action**: `retrieve_from_bank()` (cancel JE)
- **بعد می‌تواند**: به `Move To Box` برگردد

## 📋 تمام وضعیت‌های پشتیبانی شده (14 وضعیت):

| # | وضعیت (انگلیسی) | در دیاگرام | پشتیبانی |
|---|----------------|-----------|---------|
| 1 | Received From Customer | ✅ | ✅ موجود |
| 2 | Waiting For Sayad | ✅ | ✅ موجود |
| 3 | Returned Not Registered | ✅ | ✅ **جدید** |
| 4 | Registered In Sayad | ✅ | ✅ موجود |
| 5 | Returned Registered To Customer | ✅ | ✅ **جدید** |
| 6 | Move To Box | ✅ | ✅ موجود |
| 7 | Under Collection | ✅ | ✅ موجود |
| 8 | Retrieved From Bank | ✅ | ✅ **جدید** |
| 9 | Collected | ✅ | ✅ موجود |
| 10 | Returned From Bank | ✅ | ✅ موجود |
| 11 | Return To Customer | ✅ | ✅ موجود |
| 12 | Reassign To Bank | ✅ | ✅ موجود (از Returned/Retrieved) |

## 🔄 تمام Action‌های پشتیبانی شده:

### Flow اصلی:
1. ✅ `Mark Waiting For Sayad` (از Received From Customer)
2. ✅ `Mark Registered In Sayad` (از Waiting For Sayad)
3. ✅ `Move To Box` (از Registered In Sayad)
4. ✅ `Assign To Bank` (از Received/Move To Box) - ایجاد JE
5. ✅ `Mark As Collected` (از Under Collection) - ایجاد JE
6. ✅ `Mark As Returned From Bank` (از Under Collection) - ایجاد JE
7. ✅ `Return To Customer` (از Returned From Bank)

### Flow‌های برگشت:
8. ✅ `Return Not Registered To Customer` (از Waiting For Sayad) - **جدید**
9. ✅ `Return Registered To Customer` (از Registered In Sayad) - **جدید**
10. ✅ `Retrieve From Bank` (از Under Collection) - **جدید** (cancel JE)
11. ✅ `Move Back To Box From Retrieved` (از Retrieved From Bank) - **جدید**
12. ✅ `Reassign To Bank` (از Returned/Retrieved From Bank) - ایجاد JE - **به‌روز شده**

## 📝 تغییرات انجام شده:

### 1. `utils.py`
- اضافه شد: `RETURNED_NOT_REGISTERED`
- اضافه شد: `RETURNED_REGISTERED_TO_CUSTOMER`
- اضافه شد: `RETRIEVED_FROM_BANK`

### 2. `cheque.py`
- اضافه شد: `return_not_registered_to_customer()`
- اضافه شد: `return_registered_to_customer()`
- اضافه شد: `retrieve_from_bank()` (با cancel JE)
- اضافه شد: `move_back_to_box_from_retrieved()`
- به‌روز شد: `reassign_to_bank()` (پشتیبانی از Retrieved From Bank)

### 3. `cheque.json`
- اضافه شد: وضعیت‌های جدید به options field

### 4. `client_script.json`
- اضافه شد: دکمه‌های جدید برای action‌های جدید
- اضافه شد: رنگ‌های جدید برای وضعیت‌های جدید
- به‌روز شد: دکمه Reassign To Bank برای پشتیبانی از Retrieved From Bank

## ✅ نتیجه:

**تمام 14 وضعیت از دیاگرام پشتیبانی می‌شوند!**

سیستم اکنون قابلیت کامل workflow دیاگرام را دارد.


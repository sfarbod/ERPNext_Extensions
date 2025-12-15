# مقایسه Workflow دیاگرام با وضعیت‌های موجود

## وضعیت‌های موجود در دیاگرام (به انگلیسی):

| # | وضعیت در دیاگرام | وضعیت موجود در کد | وضعیت |
|---|------------------|-------------------|-------|
| 1 | دریافت چک از مشتری | `Received From Customer` | ✅ موجود |
| 2 | در انتظار ثبت صیاد توسط مشتری | `Waiting For Sayad` | ✅ موجود |
| 3 | برگشت چک به مشتری (ثبت نشد) | `Return To Customer` | ⚠️ متفاوت |
| 4 | ثبت شده در سامانه صیاد | `Registered In Sayad` | ✅ موجود |
| 5 | چک در صندوق | `Move To Box` | ✅ موجود |
| 6 | عودت چک ثبت‌شده و تحویل به مشتری | - | ❌ ندارد |
| 7 | واگذاری به بانک | `Under Collection` (پس از assign) | ✅ موجود |
| 8 | در بانک (انتظار وصول) | `Under Collection` | ✅ موجود |
| 9 | پس گرفتن چک از بانک (بدون اقدام) | - | ❌ ندارد |
| 10 | وصول چک | `Collected` | ✅ موجود |
| 11 | برگشت خوردن چک | `Returned From Bank` | ✅ موجود |
| 12 | تحویل چک برگشتی از بانک | `Returned From Bank` | ✅ موجود |
| 13 | واگذاری مجدد به بانک | `Under Collection` (پس از reassign) | ✅ موجود |
| 14 | استرداد چک برگشتی به مشتری | `Return To Customer` | ✅ موجود |

## وضعیت‌های کمبود:

### 1. "Returned To Customer (Not Registered)"
- **دیاگرام**: برگشت چک به مشتری (ثبت نشد) - از "Waiting For Sayad"
- **نیاز**: وضعیت برای زمانی که مشتری چک را ثبت نمی‌کند
- **پیشنهاد نام انگلیسی**: `Returned To Customer (Not Registered)` یا `Returned (Not Registered)`

### 2. "Returned Registered To Customer"
- **دیاگرام**: عودت چک ثبت‌شده و تحویل به مشتری - از "Registered In Sayad"
- **نیاز**: زمانی که چک در صیاد ثبت شده اما درخواست عودت/لغو می‌شود
- **پیشنهاد نام انگلیسی**: `Returned Registered To Customer` یا `Returned (Registered)`

### 3. "Retrieved From Bank"
- **دیاگرام**: پس گرفتن چک از بانک (بدون اقدام) - از "Under Collection"
- **نیاز**: زمانی که چک را از بانک برمی‌گردانیم بدون اینکه وصول یا برگشت بخورد
- **پیشنهاد نام انگلیسی**: `Retrieved From Bank` یا `Withdrawn From Bank`

## Action های کمبود:

### 1. "Return To Customer (Not Registered)"
- از وضعیت: `Waiting For Sayad`
- به وضعیت: `Returned To Customer (Not Registered)`

### 2. "Return Registered To Customer"
- از وضعیت: `Registered In Sayad`
- به وضعیت: `Returned Registered To Customer`

### 3. "Retrieve From Bank"
- از وضعیت: `Under Collection`
- به وضعیت: `Retrieved From Bank`
- سپس می‌تواند به `Move To Box` برگردد

## وضعیت‌های موجود که در دیاگرام نیست:

- `Returned` (وضعیت عمومی برگشت)
- `Cancelled` (لغو شده)

## خلاصه:

✅ **10 از 14 وضعیت** موجود است
❌ **3 وضعیت** کمبود دارد
⚠️ **1 وضعیت** نیاز به تفکیک دارد (Return To Customer)


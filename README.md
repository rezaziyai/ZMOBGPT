# ZMOBGPT

تستر حرفه‌ای کانفیگ‌های V2Ray/Xray برای ویندوز با رابط فارسی.

## قابلیت‌ها
- دریافت Subscription به‌صورت Plain/Base64 و چند لینک هم‌زمان
- حذف Duplicate بر اساس هویت واقعی اتصال، نه نام کانفیگ
- پیش‌فیلتر سریع برای حذف سرورهای مرده
- تست واقعی با xray-core به‌جای TCP-only
- پشتیبانی VMess، VLESS، Reality، XHTTP، gRPC، WebSocket، Trojan، Shadowsocks و Hysteria2
- تست Median Ping و پایداری/Jitter در مرحله تست اصلی
- Cache برای اجرای سریع‌تر تست‌های بعدی و تأیید مجدد کانفیگ‌های سالم
- نوار پیشرفت، ETA، Start/Stop و گزارش زنده
- تولید خودکار `CONFING.txt` شامل کانفیگ‌های سالم

## اجرا
```powershell
pip install -r requirements.txt
python app.py
```

فایل `xray.exe`، `geoip.dat`، `geosite.dat` و DLLهای همراه باید کنار برنامه باشند.

## ساخت EXE
```powershell
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name ZMOBGPT app.py
```

هسته تست از پروژه‌های متن‌باز تستر Xray ایده گرفته و در ZMOBGPT با رابط فارسی و تنظیمات مناسب این برنامه یکپارچه شده است.

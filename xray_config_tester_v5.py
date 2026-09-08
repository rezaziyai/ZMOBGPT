#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تست‌کننده‌ی کانفیگ V2Ray/Xray — نسخه‌ی ۳
========================================
نسبت به نسخه‌ی قبل دو قابلیت اضافه شده:
  - پشتیبانی xhttp برای VLESS (نجات کانفیگ‌های مدرن Reality+XHTTP)
  - پشتیبانی hysteria2 (با معافیت از پیش‌فیلتر TCP چون UDP/QUIC است)

پایپ‌لاین:
  گام ۱) جمع‌آوری کانفیگ‌ها
  گام ۲) حذف تکراری بر اساس مشخصات واقعی اتصال
  مرحله A) پیش‌فیلتر سریع TCP (حذف سرورهای مرده)
  مرحله B) تست دقیق و سخت‌گیرانه با xray-core
  ذخیره) ریمارک ثابت روی کانفیگ‌های سالم

پیش‌نیاز:
  - xray-core (نسخه‌ی جدید، برای hysteria2): https://github.com/XTLS/Xray-core/releases
  - pip install requests pysocks
"""

import os
import re
import json
import time
import base64
import queue
import socket
import tempfile
import hashlib
import threading
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

import requests

# ============================================================
# تنظیمات
# ============================================================
XRAY_PATH = r"C:\xray\xray.exe"   # مسیر فایل اجرایی xray در ویندوز
TEST_URL = "http://cp.cloudflare.com/generate_204"

MAX_DELAY_MS = 300        # حداکثر تأخیر قابل‌قبول (میانه) — درجه یک
PING_COUNT = 2            # تعداد پینگ اندازه‌گیری‌شده (همه باید موفق شوند)
JITTER_FACTOR = 1.5       # بدترین پینگ نباید از این ضریب × آستانه بیشتر باشد
REQUEST_TIMEOUT = 4       # مهلت هر درخواست (ثانیه)
STARTUP_TIMEOUT = 3.0     # حداکثر صبر برای بالا آمدن هسته (ثانیه)
MAX_WORKERS = 30          # تست هم‌زمان مرحله‌ی B (تا 50 هم می‌توانی ببری)
LOCAL_PORT_BASE = 20000   # پورت لوکال شروع برای inbound های SOCKS

# --- مرحله‌ی A: پیش‌فیلتر سریع TCP ---
PREFILTER = True
PREFILTER_WORKERS = 100
PREFILTER_TIMEOUT = 1.5

REMARK_TAG = "@alijk"     # ← ریمارک ثابت همه‌ی کانفیگ‌ها

# پرچم توقف (پنجره‌ی گرافیکی این را ست می‌کند تا مرحله‌های طولانی متوقف شوند)
STOP_EVENT = threading.Event()

# --- حافظه‌ی بین اجراها (cache) ---
USE_CACHE = True                 # روشن/خاموش کردن سیستم cache
CACHE_FILE = "tester_cache.json" # کنار همین برنامه ساخته می‌شود
RETEST_GOOD = True               # کانفیگ‌های سالمِ قبلی همیشه دوباره تست شوند (تأیید مجدد)
RETEST_DEAD_AFTER_HOURS = 48     # کانفیگ مرده‌ی قبلی بعد از این مدت یک شانس دوباره می‌گیرد
#   کوچک‌تر = زنده‌شده‌ها زودتر گیر می‌افتند ولی اجرا کندتر
#   بزرگ‌تر  = اجرا سریع‌تر ولی زنده‌شده‌ها دیرتر گیر می‌افتند

# --- فایل خروجی تجمیعی و چرخه‌ی بازنشانی ---
CONFING_FILE = "CONFING.txt"     # فایل نهایی که هر بار به‌روزرسانی می‌شود
RESET_EVERY_HOURS = 120          # هر ۵ روز (۱۲۰ ساعت) لیست از صفر شروع می‌شود


# ============================================================
# ابزارهای کمکی
# ============================================================
def b64_decode(data: str) -> str:
    data = data.strip()
    data += "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    except Exception:
        return base64.b64decode(data).decode("utf-8", errors="ignore")


def is_probably_base64(text: str) -> bool:
    clean = "".join(text.split())
    if not clean or len(clean) % 4 != 0:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_\-]+", clean))


# ============================================================
# پارس کردن کانفیگ‌ها → outbound استاندارد xray
# ============================================================
def build_stream_settings(net, security, host, path, sni,
                          header_type="", pbk="", sid="", fp="",
                          alpn="", flow="", mode="", spx="") -> dict:
    net = net or "tcp"
    security = security or "none"
    ss = {"network": net, "security": security}

    if security == "tls":
        ss["tlsSettings"] = {
            "serverName": sni or host or "",
            "allowInsecure": True,
            "fingerprint": fp or "chrome",
        }
        if alpn:
            ss["tlsSettings"]["alpn"] = [a for a in alpn.split(",") if a]
    elif security == "reality":
        ss["realitySettings"] = {
            "serverName": sni or "",
            "publicKey": pbk or "",
            "shortId": sid or "",
            "fingerprint": fp or "chrome",
        }
        if spx:
            ss["realitySettings"]["spiderX"] = spx

    if net == "ws":
        ss["wsSettings"] = {"path": path or "/", "headers": ({"Host": host} if host else {})}
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": path or ""}
    elif net in ("h2", "http"):
        ss["httpSettings"] = {"path": path or "/", "host": ([host] if host else [])}
    elif net == "xhttp":   # ← قابلیت جدید: VLESS با XHTTP
        xs = {"path": path or "/", "mode": mode or "auto"}
        if host:
            xs["host"] = host
        ss["xhttpSettings"] = xs
    elif net == "tcp" and header_type == "http":
        ss["tcpSettings"] = {
            "header": {"type": "http",
                       "request": {"path": [path or "/"],
                                   "headers": {"Host": [host] if host else []}}}
        }
    return ss


def parse_vmess(link: str) -> Optional[dict]:
    try:
        info = json.loads(b64_decode(link[len("vmess://"):]))
        return {
            "protocol": "vmess",
            "settings": {"vnext": [{
                "address": info["add"], "port": int(info["port"]),
                "users": [{"id": info["id"],
                           "alterId": int(info.get("aid", 0) or 0),
                           "security": info.get("scy", "auto")}],
            }]},
            "streamSettings": build_stream_settings(
                net=info.get("net", "tcp"), security=info.get("tls", ""),
                host=info.get("host", ""), path=info.get("path", ""),
                sni=info.get("sni", ""), header_type=info.get("type", ""),
                alpn=info.get("alpn", "")),
        }
    except Exception:
        return None


def parse_vless(link: str) -> Optional[dict]:
    try:
        u = urlparse(link)
        uuid, host, port = unquote(u.username or ""), u.hostname, u.port
        if not (uuid and host and port):
            return None
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        user = {"id": uuid, "encryption": q.get("encryption", "none")}
        if q.get("flow"):
            user["flow"] = q["flow"]
        return {
            "protocol": "vless",
            "settings": {"vnext": [{"address": host, "port": int(port), "users": [user]}]},
            "streamSettings": build_stream_settings(
                net=q.get("type", "tcp"), security=q.get("security", "none"),
                host=q.get("host", ""), path=q.get("path", ""),
                sni=q.get("sni", ""), header_type=q.get("headerType", ""),
                pbk=q.get("pbk", ""), sid=q.get("sid", ""), fp=q.get("fp", ""),
                alpn=q.get("alpn", ""), flow=q.get("flow", ""),
                mode=q.get("mode", ""), spx=q.get("spx", "")),
        }
    except Exception:
        return None


def parse_trojan(link: str) -> Optional[dict]:
    try:
        u = urlparse(link)
        password, host, port = unquote(u.username or ""), u.hostname, u.port
        if not (password and host and port):
            return None
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        return {
            "protocol": "trojan",
            "settings": {"servers": [{"address": host, "port": int(port), "password": password}]},
            "streamSettings": build_stream_settings(
                net=q.get("type", "tcp"), security=q.get("security", "tls"),
                host=q.get("host", ""), path=q.get("path", ""),
                sni=q.get("sni", ""), header_type=q.get("headerType", ""),
                fp=q.get("fp", ""), alpn=q.get("alpn", ""),
                mode=q.get("mode", ""), spx=q.get("spx", "")),
        }
    except Exception:
        return None


def parse_shadowsocks(link: str) -> Optional[dict]:
    try:
        body = link[len("ss://"):].split("#", 1)[0]
        if "@" in body:
            userinfo, server = body.split("@", 1)
            method, password = b64_decode(userinfo).split(":", 1)
            host, port = server.rsplit(":", 1)
        else:
            creds, server = b64_decode(body).split("@", 1)
            method, password = creds.split(":", 1)
            host, port = server.rsplit(":", 1)
        return {
            "protocol": "shadowsocks",
            "settings": {"servers": [{"address": host, "port": int(port),
                                      "method": method, "password": password}]},
            "streamSettings": {"network": "tcp"},
        }
    except Exception:
        return None


def parse_hysteria2(link: str) -> Optional[dict]:
    """Hysteria2 (UDP/QUIC). نیازمند xray نسخه‌ی جدید با پشتیبانی hysteria."""
    try:
        u = urlparse(link)
        auth = unquote(u.username or "")
        if u.password:
            auth = f"{unquote(u.username)}:{unquote(u.password)}"
        host, port = u.hostname, u.port   # port-hopping/range → fail و رد می‌شود
        if not (host and port and auth):
            return None
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        sni = q.get("sni", "") or q.get("peer", "")
        return {
            "protocol": "hysteria",
            "settings": {"version": 2, "address": host, "port": int(port)},
            "streamSettings": {
                "network": "hysteria",
                "security": "tls",
                "tlsSettings": {"serverName": sni or host, "allowInsecure": True},
                "hysteriaSettings": {"version": 2, "auth": auth},
            },
        }
    except Exception:
        return None


def parse_config(link: str) -> Optional[dict]:
    link = link.strip()
    if link.startswith("vmess://"):
        return parse_vmess(link)
    if link.startswith("vless://"):
        return parse_vless(link)
    if link.startswith("trojan://"):
        return parse_trojan(link)
    if link.startswith("ss://"):
        return parse_shadowsocks(link)
    if link.startswith(("hysteria2://", "hy2://")):
        return parse_hysteria2(link)
    return None


def is_udp_based(link: str) -> bool:
    """پروتکل‌های مبتنی بر UDP که نباید با پیش‌فیلتر TCP رد شوند."""
    return link.strip().lower().startswith(("hysteria2://", "hy2://"))


# ============================================================
# گام ۲: حذف تکراری بر اساس مشخصات واقعی اتصال
# ============================================================
def config_signature(link: str) -> str:
    ob = parse_config(link)
    if not ob:
        return link.split("#", 1)[0].strip()

    ss = ob.get("streamSettings", {})
    net = ss.get("network", "")
    sec = ss.get("security", "")
    host = path = sni = ""
    if "wsSettings" in ss:
        path = ss["wsSettings"].get("path", "")
        host = ss["wsSettings"].get("headers", {}).get("Host", "")
    elif "grpcSettings" in ss:
        path = ss["grpcSettings"].get("serviceName", "")
    elif "httpSettings" in ss:
        path = ss["httpSettings"].get("path", "")
    elif "xhttpSettings" in ss:
        path = ss["xhttpSettings"].get("path", "")
        host = ss["xhttpSettings"].get("host", "")
    if "tlsSettings" in ss:
        sni = ss["tlsSettings"].get("serverName", "")
    elif "realitySettings" in ss:
        sni = ss["realitySettings"].get("serverName", "")

    st = ob.get("settings", {})
    try:
        if "vnext" in st:
            v = st["vnext"][0]
            addr, port, auth = v["address"], v["port"], v["users"][0].get("id", "")
        elif "servers" in st:
            s = st["servers"][0]
            addr, port = s["address"], s["port"]
            auth = s.get("password", "") or s.get("method", "")
        elif "address" in st:   # hysteria2
            addr, port = st["address"], st["port"]
            auth = ss.get("hysteriaSettings", {}).get("auth", "")
        else:
            return link.split("#", 1)[0].strip()
    except Exception:
        return link.split("#", 1)[0].strip()

    return f"{ob['protocol']}|{addr}|{port}|{auth}|{net}|{sec}|{host}|{path}|{sni}"


def dedup_configs(configs: List[str]) -> List[str]:
    seen, out = set(), []
    for c in configs:
        sig = config_signature(c)
        if sig not in seen:
            seen.add(sig)
            out.append(c)
    return out


# ============================================================
# مرحله‌ی A: پیش‌فیلتر سریع TCP
# ============================================================
def get_endpoint(link: str) -> Optional[Tuple[str, int]]:
    ob = parse_config(link)
    if not ob:
        return None
    st = ob.get("settings", {})
    try:
        if "vnext" in st:
            return st["vnext"][0]["address"], int(st["vnext"][0]["port"])
        if "servers" in st:
            return st["servers"][0]["address"], int(st["servers"][0]["port"])
        if "address" in st:   # hysteria2
            return st["address"], int(st["port"])
    except Exception:
        return None
    return None


def tcp_alive(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def prefilter(configs: List[str], progress=None) -> List[str]:
    alive = []
    total = len(configs)

    def check(link):
        if is_udp_based(link):       # UDP/QUIC → مستقیم به مرحله‌ی B
            return link, True
        ep = get_endpoint(link)
        if not ep:
            return link, False
        return link, tcp_alive(ep[0], ep[1], PREFILTER_TIMEOUT)

    print(f"\n⚡ مرحله‌ی A — پیش‌فیلتر TCP روی {total} کانفیگ "
          f"(کانکارنسی {PREFILTER_WORKERS})")
    t0 = time.time()
    last_emit = 0.0
    with ThreadPoolExecutor(max_workers=PREFILTER_WORKERS) as ex:
        futures = [ex.submit(check, c) for c in configs]
        done = 0
        for fut in as_completed(futures):
            done += 1
            link, ok = fut.result()
            if ok:
                alive.append(link)
            if done % 1000 == 0:
                rate = done / max(time.time() - t0, 0.001)
                print(f"   {done}/{total}  زنده: {len(alive)}  ({rate:.0f}/ثانیه)")

            now = time.time()
            if progress and (now - last_emit > 0.5 or done == 1 or done == total):
                last_emit = now
                rate = done / max(now - t0, 0.001)
                eta = (total - done) / max(rate, 0.001)
                progress({"done": done, "total": total,
                          "good": len(alive), "eta_sec": eta})

            if STOP_EVENT.is_set():
                for f in futures:
                    f.cancel()
                print("⛔ توقف توسط کاربر (زنده‌یابی)")
                break

    print(f"✅ مرحله‌ی A تمام شد در {time.time()-t0:.0f}s — "
          f"{len(alive)} زنده از {total}")
    return alive


# ============================================================
# تغییر ریمارک (اسم) کانفیگ
# ============================================================
def set_remark(link: str, name: str) -> str:
    link = link.strip()
    if link.startswith("vmess://"):
        try:
            info = json.loads(b64_decode(link[len("vmess://"):]))
            info["ps"] = name
            raw = base64.b64encode(
                json.dumps(info, ensure_ascii=False).encode("utf-8")).decode()
            return "vmess://" + raw
        except Exception:
            return link
    return link.split("#", 1)[0] + "#" + quote(name)


# ============================================================
# گام ۱: گرفتن کانفیگ‌ها
# ============================================================
def fetch_configs(urls: List[str]) -> List[str]:
    all_cfgs = []
    for url in urls:
        try:
            print(f"📥 دریافت از: {url}")
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            content = r.text.strip()
            if is_probably_base64(content):
                content = b64_decode(content)
            cfgs = [ln.strip() for ln in content.splitlines()
                    if re.match(r"^(vmess|vless|trojan|ss|hysteria2|hy2)://", ln.strip())]
            print(f"✅ {len(cfgs)} کانفیگ از این لینک")
            all_cfgs.extend(cfgs)
        except Exception as e:
            print(f"❌ خطا در {url}: {e}")
    return all_cfgs


# ============================================================
# سیستم cache: تشخیص آپدیت لینک‌ها و تصمیم برای تست
# ============================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_since(iso_str: str) -> float:
    try:
        then = datetime.fromisoformat(iso_str)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0
    except Exception:
        return 1e9   # خیلی قدیمی → دوباره تست شود


def _fresh_cache() -> dict:
    return {"links": {}, "configs": {}, "created": _now_iso()}


def load_cache() -> dict:
    if not USE_CACHE:
        return _fresh_cache()
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
            c.setdefault("links", {})
            c.setdefault("configs", {})
            c.setdefault("created", _now_iso())
            return c
    except Exception:
        return _fresh_cache()


def save_cache(cache: dict):
    if not USE_CACHE:
        return
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ ذخیره‌ی cache ناموفق: {e}")


def fetch_with_report(urls: List[str], cache: dict) -> Tuple[List[str], List[dict]]:
    """دریافت کانفیگ‌ها + گزارش وضعیت آپدیت هر لینک (بر اساس hash محتوا)."""
    all_cfgs, report = [], []
    for url in urls:
        item = {"url": url, "status": "error", "count": 0}
        try:
            print(f"📥 دریافت از: {url}")
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            content = r.text.strip()

            h = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()
            old = cache["links"].get(url, {})
            if not old:
                item["status"] = "new"          # لینک تازه
            elif old.get("hash") != h:
                item["status"] = "changed"       # محتوا عوض شده
            else:
                item["status"] = "unchanged"     # دست‌نخورده

            if is_probably_base64(content):
                content = b64_decode(content)
            cfgs = [ln.strip() for ln in content.splitlines()
                    if re.match(r"^(vmess|vless|trojan|ss|hysteria2|hy2)://", ln.strip())]
            item["count"] = len(cfgs)
            all_cfgs.extend(cfgs)
            cache["links"][url] = {"hash": h, "last_seen": _now_iso(), "count": len(cfgs)}
            print(f"✅ {len(cfgs)} کانفیگ — وضعیت: {item['status']}")
        except Exception as e:
            item["error"] = str(e)
            print(f"❌ خطا در {url}: {e}")
        report.append(item)
    return all_cfgs, report


def classify_by_cache(configs: List[str], cache: dict) -> Tuple[List[str], List[str], dict]:
    """تقسیم به: تست‌شونده‌ها و رد‌شده‌ها (مرده‌ی تازه). نگاشت link→signature هم برمی‌گرداند."""
    to_test, skipped = [], []
    sig_map = {}
    for c in configs:
        sig = config_signature(c)
        sig_map[c] = sig
        rec = cache["configs"].get(sig)
        if rec is None:
            to_test.append(c)                       # جدید
        elif rec.get("result") == "good":
            if RETEST_GOOD:
                to_test.append(c)                   # سالمِ قبلی → تأیید مجدد
            else:
                skipped.append(c)
        else:  # bad
            if _hours_since(rec.get("last_tested", "")) >= RETEST_DEAD_AFTER_HOURS:
                to_test.append(c)                   # مرده‌ی قدیمی → شانس دوباره
            else:
                skipped.append(c)                   # مرده‌ی تازه → رد
    return to_test, skipped, sig_map


def write_update_report(report: List[dict], stats: dict, filename: str = None):
    if not filename:
        filename = f"update_report_{datetime.now():%Y%m%d_%H%M%S}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# گزارش آپدیت لینک‌ها | {datetime.now()}\n\n")
        for it in report:
            line = f"[{it['status']:>9}] {it['count']:>6} کانفیگ — {it['url']}"
            if it.get("error"):
                line += f"  (خطا: {it['error']})"
            f.write(line + "\n")
        f.write("\n" + "=" * 50 + "\n")
        for k, v in stats.items():
            f.write(f"{k}: {v}\n")
    print(f"💾 گزارش آپدیت: {filename}")


def write_confing_from_cache(cache: dict, tag: str):
    """فایل CONFING را از روی همه‌ی کانفیگ‌های «سالمِ فعلی» داخل cache بازنویسی می‌کند.
    تجمیع، حذف تکراری و حذف مرده‌ها خودکار است (چون فقط good ها نوشته می‌شوند)."""
    goods = []
    for sig, rec in cache["configs"].items():
        if rec.get("result") == "good" and rec.get("link"):
            goods.append((rec["link"], rec.get("ping", 999999)))
    goods.sort(key=lambda x: x[1])   # مرتب بر اساس پینگ (سریع‌ترین اول)

    with open(CONFING_FILE, "w", encoding="utf-8") as f:
        f.write(f"# CONFING — کانفیگ‌های سالمِ فعلی\n")
        f.write(f"# تعداد: {len(goods)} | به‌روزرسانی: {datetime.now()}\n\n")
        for link, ping in goods:
            f.write(set_remark(link, tag) + "\n")
            f.write(f"# {ping:.0f}ms\n\n")
    print(f"💾 فایل تجمیعی به‌روز شد: {CONFING_FILE}  ({len(goods)} کانفیگ سالم)")


# ============================================================
# مرحله‌ی B: تست واقعی و سخت‌گیرانه با xray-core
# ============================================================
def make_xray_config(outbound: dict, socks_port: int) -> dict:
    return {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen": "127.0.0.1", "port": socks_port,
                      "protocol": "socks",
                      "settings": {"udp": False, "auth": "noauth"}}],
        "outbounds": [outbound],
    }


def wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def test_one(link: str, socks_port: int) -> Tuple[str, float, bool, str]:
    ob = parse_config(link)
    if not ob:
        return link, -1, False, "parse_failed"

    cfg = make_xray_config(ob, socks_port)
    fd, cfg_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)

    proc = None
    try:
        proc = subprocess.Popen([XRAY_PATH, "run", "-c", cfg_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for_port(socks_port, STARTUP_TIMEOUT):
            return link, -1, False, "xray_no_start"
        if proc.poll() is not None:
            return link, -1, False, "xray_crashed"

        proxies = {"http": f"socks5h://127.0.0.1:{socks_port}",
                   "https": f"socks5h://127.0.0.1:{socks_port}"}

        # warmup (محاسبه نمی‌شود)
        try:
            w = requests.get(TEST_URL, proxies=proxies, timeout=REQUEST_TIMEOUT)
            if w.status_code not in (200, 204):
                return link, -1, False, f"http_{w.status_code}"
        except requests.exceptions.RequestException:
            return link, -1, False, "no_tunnel"

        # پینگ‌های اندازه‌گیری‌شده (همه باید موفق شوند)
        pings = []
        for _ in range(PING_COUNT):
            try:
                start = time.time()
                r = requests.get(TEST_URL, proxies=proxies, timeout=REQUEST_TIMEOUT)
                if r.status_code not in (200, 204):
                    return link, -1, False, f"http_{r.status_code}"
                pings.append((time.time() - start) * 1000)
            except requests.exceptions.RequestException:
                return link, -1, False, "unstable"

        pings.sort()
        median = pings[len(pings) // 2]
        worst = pings[-1]
        jitter = pings[-1] - pings[0]

        if median <= MAX_DELAY_MS and worst <= MAX_DELAY_MS * JITTER_FACTOR:
            return link, median, True, f"ok(j{jitter:.0f})"
        return link, median, False, "too_slow"

    except Exception as e:
        return link, -1, False, f"err_{type(e).__name__}"
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        try:
            os.unlink(cfg_path)
        except Exception:
            pass


def test_all(configs: List[str], progress=None) -> Tuple[List, List]:
    good, bad = [], []
    port_pool: "queue.Queue[int]" = queue.Queue()
    for p in range(LOCAL_PORT_BASE, LOCAL_PORT_BASE + MAX_WORKERS):
        port_pool.put(p)

    def worker(link):
        port = port_pool.get()
        try:
            return test_one(link, port)
        finally:
            port_pool.put(port)

    print(f"\n🔄 مرحله‌ی B — تست دقیق {len(configs)} کانفیگ "
          f"(آستانه {MAX_DELAY_MS}ms، {PING_COUNT} پینگ)")
    print(f"🌐 URL: {TEST_URL}")
    print("=" * 60)
    t0 = time.time()
    total = len(configs)
    last_emit = 0.0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(worker, c): c for c in configs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            link, delay, ok, reason = fut.result()
            if ok:
                good.append((link, delay, reason))
                print(f"   ✅ {delay:6.0f}ms  {reason}")
            else:
                bad.append((link, delay, reason))

            elapsed = time.time() - t0
            rate = done / max(elapsed, 0.001)
            eta = (total - done) / max(rate, 0.001)

            if done % 100 == 0:
                print(f"⏳ {done}/{total}  (درجه‌یک: {len(good)})  "
                      f"{rate:.0f}/ث  ETA ~{eta/60:.1f} دقیقه")

            # گزارش پیشرفت لحظه‌ای به بیرون (حداکثر هر نیم‌ثانیه)
            now = time.time()
            if progress and (now - last_emit > 0.5 or done == 1 or done == total):
                last_emit = now
                progress({"done": done, "total": total,
                          "good": len(good), "eta_sec": eta})

            if STOP_EVENT.is_set():
                for f in futures:
                    f.cancel()
                print("⛔ توقف توسط کاربر (تست اصلی)")
                break

    good.sort(key=lambda x: x[1])
    print(f"\n📊 نتیجه: {len(good)} درجه‌یک، {len(bad)} رد")
    return good, bad


# ============================================================
# ذخیره‌ی نتایج با ریمارک‌گذاری
# ============================================================
def save_results(good: List, tag: str, filename: str = None):
    if not filename:
        filename = f"top_configs_{datetime.now():%Y%m%d_%H%M%S}.txt"

    renamed = [(set_remark(link, tag), delay) for (link, delay, _) in good]

    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# کانفیگ‌های درجه‌یک (پینگ ≤ {MAX_DELAY_MS}ms، پایدار)\n")
        f.write(f"# تعداد: {len(renamed)} | زمان: {datetime.now()}\n\n")
        for link, delay in renamed:
            f.write(f"{link}\n# {delay:.0f}ms\n\n")
    print(f"💾 ذخیره شد: {filename}")

    plain = filename.replace(".txt", "_plain.txt")
    with open(plain, "w", encoding="utf-8") as f:
        for link, _ in renamed:
            f.write(link + "\n")
    print(f"💾 نسخه‌ی قابل import در v2ray: {plain}")


# ============================================================
# اجرای کامل
# ============================================================
def check_xray_binary():
    if not (os.path.isfile(XRAY_PATH) or XRAY_PATH in ("xray", "xray.exe")):
        print(f"⚠️  فایل xray پیدا نشد: {XRAY_PATH}")
        print("    از https://github.com/XTLS/Xray-core/releases دانلودش کن.\n")


def run(urls: List[str]):
    print("🚀 تست کانفیگ VPN — نسخه‌ی ۵ (cache + فایل تجمیعی + چرخه‌ی ۵ روزه)")
    print("=" * 60)
    check_xray_binary()

    cache = load_cache()

    # چرخه‌ی بازنشانی: هر RESET_EVERY_HOURS لیست از صفر شروع می‌شود
    if _hours_since(cache.get("created", "")) >= RESET_EVERY_HOURS:
        print(f"♻️  چرخه‌ی {RESET_EVERY_HOURS} ساعته رسید — لیست از صفر شروع می‌شود.")
        cache = _fresh_cache()

    # گام ۱: جمع‌آوری + گزارش وضعیت آپدیت هر لینک
    configs, link_report = fetch_with_report(urls, cache)
    if not configs:
        print("❌ هیچ کانفیگی دریافت نشد!")
        save_cache(cache)
        return
    print(f"\n📦 مجموع خام: {len(configs)}")

    # گام ۲: حذف تکراری
    configs = dedup_configs(configs)
    print(f"🧹 بعد از حذف تکراری: {len(configs)}")

    # گام cache: تصمیم اینکه کدام‌ها تست شوند
    to_test, skipped, sig_map = classify_by_cache(configs, cache)
    print(f"🧠 cache → تست: {len(to_test)}  |  رد (مرده‌ی تازه/سالمِ خاموش): {len(skipped)}")

    # مرحله‌ی A: پیش‌فیلتر سریع TCP فقط روی تست‌شونده‌ها
    candidates = to_test
    if PREFILTER:
        candidates = prefilter(to_test)
    dead_by_prefilter = set(to_test) - set(candidates)

    # مرحله‌ی B: تست دقیق
    good, bad = ([], [])
    if candidates:
        good, bad = test_all(candidates)

    # به‌روزرسانی cache بر اساس نتیجه (لینک کانفیگ‌های سالم هم ذخیره می‌شود)
    now = _now_iso()
    good_map = {link: d for (link, d, _) in good}
    for c in candidates:
        sig = sig_map.get(c) or config_signature(c)
        if c in good_map:
            cache["configs"][sig] = {"result": "good", "ping": round(good_map[c]),
                                     "last_tested": now, "link": c}
        else:
            cache["configs"][sig] = {"result": "bad", "last_tested": now}
    for c in dead_by_prefilter:                      # سرور مرده در پیش‌فیلتر
        sig = sig_map.get(c) or config_signature(c)
        cache["configs"][sig] = {"result": "bad", "last_tested": now}
    save_cache(cache)

    # فایل تجمیعی CONFING: از روی همه‌ی کانفیگ‌های سالمِ فعلیِ داخل cache بازنویسی می‌شود
    write_confing_from_cache(cache, REMARK_TAG)

    # گزارش آپدیت
    total_good_now = sum(1 for r in cache["configs"].values() if r.get("result") == "good")
    stats = {
        "مجموع یکتا (این اجرا)": len(configs),
        "تست‌شده": len(candidates),
        "رد توسط cache": len(skipped),
        "مرده در پیش‌فیلتر": len(dead_by_prefilter),
        "سالمِ این اجرا": len(good),
        "مجموع سالم در CONFING": total_good_now,
    }
    write_update_report(link_report, stats)

    print(f"\n📋 خلاصه: {len(good)} سالمِ این اجرا | {total_good_now} کل سالم در {CONFING_FILE} | "
          f"{len(candidates)} تست‌شده | {len(skipped)} رد‌شده از cache")


def main():
    urls = [
        "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/all_sub.txt",
        "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/super-sub.txt",
        "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/filtered/subs/hysteria2.txt",
        "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/filtered/subs/vmess.txt",
        "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/filtered/subs/vless.txt",
        "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/filtered/subs/ss.txt",
        "https://raw.githubusercontent.com/rtwo2/FastNodes/main/sub/everything.txt",
        "https://raw.githubusercontent.com/rtwo2/FastNodes/main/sub/countries/IR.txt",
        "https://raw.githubusercontent.com/awesome-vpn/awesome-vpn/master/all",
        "https://ghp.ci/https://raw.githubusercontent.com/free18/v2ray/refs/heads/main/v.txt",
        "https://www.xrayvip.com/free.txt",
    ]
    run(urls)


if __name__ == "__main__":
    main()

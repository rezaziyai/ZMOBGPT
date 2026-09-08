# -*- coding: utf-8 -*-
import sys
import time
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
import xray_config_tester_v5 as core

core.XRAY_PATH = str(APP_DIR / "xray.exe")
core.MAX_DELAY_MS = 1200
core.PING_COUNT = 3
core.JITTER_FACTOR = 2.0
core.REQUEST_TIMEOUT = 6
core.MAX_WORKERS = 12
core.PREFILTER_WORKERS = 80
core.REMARK_TAG = "ZMOBGPT"

msg_queue = queue.Queue()
STOP = threading.Event()


def hms():
    return datetime.now().strftime("%H:%M:%S")


def duration(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s} ثانیه"
    return f"{s//60} دقیقه و {s%60} ثانیه"


def log(text):
    msg_queue.put(("log", str(text)))


def progress(info, phase):
    done = info.get("done", 0)
    total = info.get("total", 0)
    pct = (done / total * 100) if total else 0
    eta = info.get("eta_sec", 0)
    eta_text = f"{eta/60:.0f} دقیقه" if eta >= 60 else f"{eta:.0f} ثانیه"
    msg_queue.put(("progress", f"{phase}: {done:,}/{total:,}  ({pct:.0f}٪)  | سالم: {info.get('good', 0)} | حدود {eta_text}"))


def run_pipeline(urls):
    old = sys.stdout
    try:
        sys.stdout = type("W", (), {"write": lambda self, x: log(x) if x else None, "flush": lambda self: None})()
        STOP.clear()
        core.STOP_EVENT.clear()
        started = time.time()
        log("=" * 64)
        log(f"شروع تست: {hms()}")
        log("نسخه ارتقایافته با موتور xray-core و تست واقعی")
        log("=" * 64)
        core.check_xray_binary()
        cache = core.load_cache()
        if core._hours_since(cache.get("created", "")) >= core.RESET_EVERY_HOURS:
            log("چرخه cache تمام شده؛ حافظه از نو ساخته شد.")
            cache = core._fresh_cache()

        log("[1/5] دریافت Subscription ها...")
        configs, reports = core.fetch_with_report(urls, cache)
        if not configs:
            log("هیچ کانفیگی دریافت نشد.")
            return
        log(f"دریافت اولیه: {len(configs):,} کانفیگ")

        configs = core.dedup_configs(configs)
        log(f"[2/5] بعد از حذف Duplicate واقعی: {len(configs):,}")

        to_test, skipped, sig_map = core.classify_by_cache(configs, cache)
        log(f"[3/5] تست جدید/نیازمند تأیید: {len(to_test):,} | از cache رد شد: {len(skipped):,}")

        candidates = to_test
        dead = []
        if core.PREFILTER and candidates:
            log("[4/5] پیش‌فیلتر سریع TCP برای حذف سرورهای مرده...")
            candidates = core.prefilter(candidates, progress=lambda x: progress(x, "پیش‌فیلتر"))
            dead = list(set(to_test) - set(candidates))
            log(f"پیش‌فیلتر: {len(candidates):,} نامزد باقی ماند.")

        if STOP.is_set():
            return
        if candidates:
            log("[5/5] تست واقعی با Xray: handshake + درخواست HTTP + کنترل پایداری")
            good, bad = core.test_all(candidates, progress=lambda x: progress(x, "تست واقعی"))
        else:
            good, bad = [], []

        now = core._now_iso()
        for link, delay, _ in good:
            sig = sig_map.get(link) or core.config_signature(link)
            cache["configs"][sig] = {"result": "good", "ping": round(delay), "last_tested": now, "link": link}
        for link, _, _ in bad:
            sig = sig_map.get(link) or core.config_signature(link)
            cache["configs"][sig] = {"result": "bad", "last_tested": now}
        if not STOP.is_set():
            for link in dead:
                sig = sig_map.get(link) or core.config_signature(link)
                cache["configs"][sig] = {"result": "bad", "last_tested": now}
        core.save_cache(cache)
        core.write_confing_from_cache(cache, core.REMARK_TAG)

        elapsed = duration(time.time() - started)
        log(f"پایان تست واقعی: {len(good):,} سالم | {len(bad):,} خراب | زمان: {elapsed}")
        log(f"CONFING.txt به‌روز شد؛ cache نیز ذخیره شد.")
    except Exception:
        log("خطا در اجرای تست:\n" + traceback.format_exc())
    finally:
        sys.stdout = old
        msg_queue.put(("done", None))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ZMOBGPT — تستر حرفه‌ای V2Ray/Xray")
        self.geometry("1250x760")
        self.events = queue.Queue()
        self.running = False
        self.build()
        self.after(100, self.drain)

    def build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="لینک‌های Subscription — هر خط یک لینک", font=("Tahoma", 11, "bold")).pack(anchor="w")
        self.urls = tk.Text(top, height=5, font=("Tahoma", 10))
        self.urls.pack(fill="x", pady=6)

        bar = ttk.Frame(top)
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="▶ شروع تست", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ توقف", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.status = ttk.Label(bar, text="آماده")
        self.status.pack(side="right")

        self.details = scrolledtext.ScrolledText(self, height=9, font=("Consolas", 9), wrap="word")
        self.details.pack(fill="x", padx=10, pady=(4, 0))

        cols = ("phase", "done", "total", "good", "time")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=9)
        for c, h, w in [("phase", "مرحله", 260), ("done", "انجام‌شده", 110), ("total", "کل", 110), ("good", "سالم", 110), ("time", "وضعیت", 450)]:
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(self, text="ویژگی‌های اضافه‌شده: حذف Duplicate واقعی، cache، پیش‌فیلتر سریع، تست واقعی با xray-core، کنترل Median/Jitter و خروجی CONFING.txt", padding=8).pack(fill="x")

    def start(self):
        urls = [x.strip() for x in self.urls.get("1.0", "end").splitlines() if x.strip()]
        if not urls:
            messagebox.showwarning("ZMOBGPT", "حداقل یک لینک Subscription وارد کنید.")
            return
        if self.running:
            return
        self.running = True
        STOP.clear()
        core.STOP_EVENT.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.config(text="در حال اجرا...")
        self.details.delete("1.0", "end")
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=run_pipeline, args=(urls,), daemon=True).start()

    def stop(self):
        STOP.set()
        core.STOP_EVENT.set()
        self.status.config(text="در حال توقف...")

    def add_progress(self, text):
        parts = text.split("|")
        vals = [p.strip() for p in parts]
        phase = vals[0] if vals else ""
        self.details.insert("end", text + "\n")
        self.details.see("end")
        self.status.config(text=phase[:90])

    def drain(self):
        try:
            while True:
                typ, data = msg_queue.get_nowait()
                if typ == "log":
                    self.details.insert("end", data)
                    self.details.see("end")
                elif typ == "progress":
                    self.add_progress(data)
                elif typ == "done":
                    self.running = False
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.config(text="متوقف شد" if STOP.is_set() else "پایان ✔")
        except queue.Empty:
            pass
        self.after(120, self.drain)


if __name__ == "__main__":
    App().mainloop()

# -*- coding: utf-8 -*-
import os, sys, time, base64, json, queue, socket, tempfile, subprocess, threading
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, parse_qs, unquote
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
try:
    import qrcode
    from PIL import ImageTk
except Exception:
    qrcode = None
    ImageTk = None

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import xray_config_tester_v5 as core
core.XRAY_PATH = str(APP_DIR / "xray.exe")
core.MAX_DELAY_MS = 1200
core.PING_COUNT = 3
core.JITTER_FACTOR = 2.0
core.REQUEST_TIMEOUT = 8
core.MAX_WORKERS = 8
core.PREFILTER_WORKERS = 50
core.REMARK_TAG = "ZMOBGPT"

DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=5000000"
UPLOAD_BYTES = 5_000_000
UPLOAD_URL = "https://speed.cloudflare.com/__up"

@dataclass
class Row:
    index: int
    raw: str
    name: str
    protocol: str
    host: str
    port: int
    active: bool = False
    latency: float = 0.0
    jitter: float = 0.0
    download: float = 0.0
    upload: float = 0.0
    status: str = ""


def display_name(link, idx):
    try:
        u = urlsplit(link)
        frag = unquote(u.fragment or "").strip()
        q = parse_qs(u.query)
        return unquote(q.get("remarks", [""])[0]).strip() or frag or f"Config {idx}"
    except Exception:
        return f"Config {idx}"


def make_row(link, idx):
    u = urlsplit(link)
    return Row(idx, link, display_name(link, idx), u.scheme.lower(), u.hostname or "", u.port or 0)


def start_xray(row, local_port):
    outbound = core.parse_config(row.raw)
    if not outbound:
        raise ValueError("Parse failed")
    cfg = core.make_xray_config(outbound, local_port)
    fd, path = tempfile.mkstemp(prefix="zmob_", suffix=".json", dir=APP_DIR)
    os.close(fd)
    Path(path).write_text(json.dumps(cfg), encoding="utf-8")
    proc = subprocess.Popen([core.XRAY_PATH, "run", "-c", path], cwd=APP_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not core.wait_for_port(local_port, 8):
        try: proc.kill()
        except Exception: pass
        try: os.unlink(path)
        except Exception: pass
        raise RuntimeError("Xray start failed")
    return proc, path


def stop_xray(proc, path):
    try:
        if proc and proc.poll() is None:
            proc.terminate(); proc.wait(timeout=2)
    except Exception:
        try: proc.kill()
        except Exception: pass
    try: os.unlink(path)
    except Exception: pass

def latency_real(row):
    port = 25000 + (row.index % 400)
    proc = path = None
    try:
        proc, path = start_xray(row, port)
        vals = []
        proxy = f"socks5h://127.0.0.1:{port}"
        for _ in range(4):
            t = time.perf_counter()
            r = subprocess.run([
                "curl.exe", "--proxy", proxy, "--connect-timeout", "5",
                "--max-time", "10", "-sS", "-o", "NUL", "-w",
                "%{http_code} %{time_total}", "https://www.cloudflare.com/cdn-cgi/trace"
            ], capture_output=True, text=True, timeout=12)
            p = r.stdout.strip().split()
            if len(p) >= 2 and p[0].startswith("2"):
                vals.append(float(p[1]) * 1000.0)
            elif r.returncode == 0:
                vals.append((time.perf_counter() - t) * 1000.0)
        if not vals:
            return False, 0.0, 0.0, "Proxy FAIL"
        vals.sort()
        med = vals[len(vals)//2]
        return True, med, max(vals) - min(vals), "REAL PROXY"
    except Exception as e:
        return False, 0.0, 0.0, str(e)[:32]
    finally:
        stop_xray(proc, path)


def transfer_test(row, direction):
    port = 26000 + (row.index % 400)
    proc = path = tmp = None
    try:
        proc, path = start_xray(row, port)
        proxy = f"socks5h://127.0.0.1:{port}"
        if direction == "download":
            cmd = ["curl.exe", "--proxy", proxy, "--connect-timeout", "6",
                   "--max-time", "25", "-sS", "-o", "NUL", "-w",
                   "%{http_code} %{speed_download} %{time_total}", DOWNLOAD_URL]
        else:
            fd, tmp = tempfile.mkstemp(prefix="zmob_up_", suffix=".bin", dir=APP_DIR)
            os.close(fd)
            with open(tmp, "wb") as f:
                f.write(os.urandom(UPLOAD_BYTES))
            cmd = ["curl.exe", "--proxy", proxy, "--connect-timeout", "6",
                   "--max-time", "25", "-sS", "-o", "NUL", "-X", "POST",
                   "--data-binary", f"@{tmp}", "-w",
                   "%{http_code} %{speed_upload} %{time_total}", UPLOAD_URL]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        p = r.stdout.strip().split()
        if len(p) < 2 or not p[0].startswith("2"):
            return 0.0, f"{direction.upper()} FAIL ({p[0] if p else '000'})"
        bps = float(p[1])
        return bps * 8.0 / 1_000_000.0, f"{direction.upper()} {p[0]}"
    except Exception as e:
        return 0.0, f"{direction.upper()} {str(e)[:24]}"
    finally:
        stop_xray(proc, path)
        if tmp:
            try: os.unlink(tmp)
            except Exception: pass


def parse_jitter(reason):
    try:
        if "j" in reason:
            return float(reason.split("j", 1)[1].rstrip(")"))
    except Exception:
        pass
    return 0.0
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ZMOBGPT — تستر حرفه‌ای V2Ray/Xray")
        self.geometry("1400x820")
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.rows = []
        self.top_rows = []
        self.do_download = tk.BooleanVar(value=False)
        self.do_upload = tk.BooleanVar(value=False)
        self.qr_image = None
        self.build_ui()
        self.after(100, self.drain)

    def build_ui(self):
        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        ttk.Label(top, text="لینک‌های Subscription — هر خط یک لینک",
                  font=("Tahoma", 11, "bold")).pack(anchor="w")
        self.urls = tk.Text(top, height=4, font=("Tahoma", 10))
        self.urls.pack(fill="x", pady=6)
        bar = ttk.Frame(top); bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="▶ شروع تست کامل", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ توقف", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=7)
        ttk.Checkbutton(bar, text="تست Download", variable=self.do_download).pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="تست Upload", variable=self.do_upload).pack(side="left", padx=6)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.status = ttk.Label(bar, text="آماده")
        self.status.pack(side="right")
        self.logbox = scrolledtext.ScrolledText(self, height=7, font=("Consolas", 9))
        self.logbox.pack(fill="x", padx=10, pady=(3, 7))

        cols = ("#", "name", "protocol", "server", "active", "latency",
                "jitter", "download", "upload", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        headers = {
            "#":"#", "name":"نام", "protocol":"پروتکل", "server":"سرور",
            "active":"فعال", "latency":"Latency ms", "jitter":"Jitter ms",
            "download":"Download Mbps", "upload":"Upload Mbps", "status":"وضعیت"
        }
        widths = {"#":45,"name":260,"protocol":90,"server":220,"active":65,
                  "latency":90,"jitter":85,"download":120,"upload":110,"status":240}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="center")
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.tree.bind("<<TreeviewSelect>>", self.tree_selected)

        ttk.Label(self, text="🏆 TOP 10 Upload", font=("Tahoma", 11, "bold")).pack(anchor="e", padx=10, pady=(7, 2))
        self.topbox = tk.Text(self, height=8, font=("Tahoma", 10), cursor="hand2")
        self.topbox.pack(fill="x", padx=10, pady=(2, 3))
        self.topbox.bind("<Button-1>", self.top_click)

        detail = ttk.Frame(self)
        detail.pack(fill="both", expand=True, padx=10, pady=(4, 6))
        left = ttk.Frame(detail)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="کانفیگ انتخاب‌شده", font=("Tahoma", 10, "bold")).pack(anchor="w")
        self.config_box = scrolledtext.ScrolledText(left, height=7, font=("Consolas", 9), wrap="word")
        self.config_box.pack(fill="both", expand=True, pady=(3, 0))
        right = ttk.Frame(detail, width=280)
        right.pack(side="right", fill="y", padx=(10, 0))
        ttk.Label(right, text="QR Code", font=("Tahoma", 10, "bold")).pack()
        self.qr_label = ttk.Label(right, text="یک کانفیگ را انتخاب کنید", anchor="center")
        self.qr_label.pack(fill="both", expand=True, pady=4)
        ttk.Label(self, text="تست Download و Upload کاملاً انتخابی است. بعد از پایان تست Upload، جدول و TOP10 بر اساس سرعت Upload مرتب می‌شوند. روی TOP10 یا یک ردیف کلیک کنید تا لینک و QR نمایش داده شود.",
                  padding=8).pack(fill="x")

    def log(self, text):
        self.events.put(("log", str(text)))

    def start(self):
        urls = [x.strip() for x in self.urls.get("1.0", "end").splitlines() if x.strip()]
        if not urls:
            messagebox.showwarning("ZMOBGPT", "حداقل یک لینک Subscription وارد کنید.")
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.config(text="در حال تست...")
        self.progress["value"] = 0
        self.logbox.delete("1.0", "end")
        self.topbox.delete("1.0", "end")
        self.config_box.delete("1.0", "end")
        self.qr_label.config(text="یک کانفیگ را انتخاب کنید", image="")
        self.qr_image = None
        self.tree.delete(*self.tree.get_children())
        self.stop_event.clear()
        threading.Thread(target=self.worker, args=(urls,), daemon=True).start()

    def stop(self):
        self.stop_event.set()
        core.STOP_EVENT.set()
        self.status.config(text="در حال توقف...")

    def add_row(self, row):
        vals = (row.index, row.name, row.protocol, row.host,
                "بله" if row.active else "خیر",
                f"{row.latency:.0f}" if row.latency else "-",
                f"{row.jitter:.0f}" if row.jitter else "-",
                f"{row.download:.2f}" if row.download else "-",
                f"{row.upload:.2f}" if row.upload else "-",
                row.status)
        iid = str(row.index)
        if self.tree.exists(iid): self.tree.item(iid, values=vals)
        else: self.tree.insert("", "end", iid=iid, values=vals)

    def worker(self, urls):
        old_stdout = sys.stdout
        try:
            sys.stdout = type("Logger", (), {
                "write": lambda s, x: self.log(x) if x else None,
                "flush": lambda s: None
            })()
            core.STOP_EVENT.clear()
            self.log("=" * 72)
            self.log("شروع تست کامل: Latency واقعی + Jitter + Download + Upload")
            self.log("=" * 72)

            configs = []
            for url in urls:
                if self.stop_event.is_set(): return
                try:
                    req = core.requests.get(url, timeout=20,
                                            headers={"User-Agent":"ZMOBGPT/2.0"})
                    text = req.text
                    configs.extend(core._extract_links_from_text(text))
                    self.log(f"Subscription دریافت شد: {len(configs):,} کانفیگ")
                except Exception as e:
                    self.log(f"خطا در دریافت Subscription: {e}")
            # fallback to core fetcher when helper is unavailable
            if not configs:
                cache = core._fresh_cache()
                configs, _ = core.fetch_with_report(urls, cache)
            configs = core.dedup_configs(configs)
            self.rows = [make_row(c, i) for i, c in enumerate(configs, 1)]
            self.events.put(("reset", self.rows))
            self.progress["maximum"] = max(1, len(self.rows))
            self.log(f"تعداد نهایی برای تست: {len(self.rows):,}")

            # Stage 1: real proxy latency, not TCP ping
            def latency_job(r):
                return latency_real(r)
            with ThreadPoolExecutor(max_workers=core.MAX_WORKERS) as ex:
                futs = {ex.submit(latency_job, r): r for r in self.rows}
                done = 0
                for fut in as_completed(futs):
                    if self.stop_event.is_set(): break
                    r = futs[fut]
                    r.active, r.latency, r.jitter, st = fut.result()
                    r.status = st
                    done += 1
                    self.events.put(("row", r)); self.events.put(("progress", done))
            active = [r for r in self.rows if r.active]
            self.log(f"Latency واقعی تمام شد: {len(active)}/{len(self.rows)} سالم")

            # Stage 2: optional real bandwidth tests.
            selected = []
            if self.do_download.get(): selected.append("download")
            if self.do_upload.get(): selected.append("upload")
            for direction in selected:
                self.log(f"شروع تست واقعی {direction.upper()} برای {len(active)} سرور فعال...")
                with ThreadPoolExecutor(max_workers=core.MAX_WORKERS) as ex:
                    futs = {ex.submit(transfer_test, r, direction): r for r in active}
                    done = 0
                    for fut in as_completed(futs):
                        if self.stop_event.is_set(): break
                        r = futs[fut]
                        speed, st = fut.result()
                        if direction == "download": r.download = speed
                        else: r.upload = speed
                        r.status = st
                        done += 1
                        self.events.put(("row", r))
                        self.events.put(("summary", f"{direction.upper()}: {done}/{len(active)}"))
                if self.stop_event.is_set(): break

            # Final ordering: Upload descending when Upload was selected.
            if self.do_upload.get():
                self.events.put(("sort_upload", None))
                tops = sorted([r for r in active if r.upload > 0],
                              key=lambda r: r.upload, reverse=True)[:10]
                self.top_rows = tops
                lines = ["TOP 10 Upload واقعی", ""]
                for n, r in enumerate(tops, 1):
                    lines.append(f"{n:02d}. {r.upload:.2f} Mbps | {r.host}:{r.port}")
                    lines.append(f"    {r.name}")
                    lines.append(f"    {r.raw}")
                    lines.append("")
                Path(APP_DIR / "TOP10_UPLOAD.txt").write_text("\n".join(lines), encoding="utf-8")
                self.events.put(("top", tops))
                self.log(f"TOP10_UPLOAD.txt ساخته شد: {len(tops)} کانفیگ")
            else:
                self.top_rows = []
                self.events.put(("top", []))
                self.log("تست Upload انتخاب نشده؛ TOP10 Upload ساخته نشد.")
            self.log("تست کامل به پایان رسید.")
        except Exception:
            self.log("خطا:\n" + __import__("traceback").format_exc())
        finally:
            sys.stdout = old_stdout
            self.events.put(("done", None))

    def drain(self):
        try:
            while True:
                typ, data = self.events.get_nowait()
                if typ == "log":
                    self.logbox.insert("end", data)
                    self.logbox.see("end")
                elif typ == "reset":
                    for r in data: self.add_row(r)
                elif typ == "row":
                    self.add_row(data)
                elif typ == "progress":
                    self.progress["value"] = data
                elif typ == "summary":
                    self.status.config(text=data)
                elif typ == "top":
                    self.show_top(data)
                elif typ == "sort_upload":
                    self.sort_tree_upload()
                elif typ == "done":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    if self.stop_event.is_set():
                        self.status.config(text="تست متوقف شد")
                    else:
                        self.status.config(text="پایان ✓")
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def sort_tree_upload(self):
        rows = sorted(self.rows, key=lambda r: r.upload, reverse=True)
        for pos, r in enumerate(rows):
            iid = str(r.index)
            if self.tree.exists(iid): self.tree.move(iid, "", pos)

    def show_top(self, rows):
        self.topbox.delete("1.0", "end")
        self.top_rows = rows or []
        if not self.top_rows:
            self.topbox.insert("end", "برای ساخت TOP10، گزینه «تست Upload» را تیک بزنید.\n")
            return
        for n, r in enumerate(self.top_rows, 1):
            start = self.topbox.index("end-1c")
            self.topbox.insert("end", f"{n:02d}. {r.upload:.2f} Mbps | {r.host}:{r.port} | {r.name}\n")
            end = self.topbox.index("end-1c")
            tag = f"top{n}"
            self.topbox.tag_add(tag, start, end)
            self.topbox.tag_config(tag, underline=True)

    def top_click(self, event):
        idxs = self.topbox.tag_names(f"@{event.x},{event.y}")
        for tag in idxs:
            if tag.startswith("top") and tag[3:].isdigit():
                i = int(tag[3:]) - 1
                if 0 <= i < len(self.top_rows):
                    self.select_row(self.top_rows[i])
                return "break"

    def tree_selected(self, event=None):
        sel = self.tree.selection()
        if not sel: return
        try:
            idx = int(sel[0])
            row = next(r for r in self.rows if r.index == idx)
            self.select_row(row)
        except Exception:
            pass

    def select_row(self, row):
        self.config_box.delete("1.0", "end")
        self.config_box.insert("end", row.raw)
        self.config_box.tag_add("sel", "1.0", "end")
        if qrcode is None or ImageTk is None:
            self.qr_label.config(text="برای QR، qrcode و Pillow نصب کنید", image="")
            return
        try:
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=7, border=3)
            qr.add_data(row.raw)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            img.thumbnail((260, 260))
            self.qr_image = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self.qr_image, text="")
        except Exception as e:
            self.qr_label.config(text=f"QR ساخته نشد: {e}", image="")

if __name__ == "__main__":
    App().mainloop()

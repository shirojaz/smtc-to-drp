"""
SMTC Discord RPC
Shows whatever you're playing (via SMTC Bridge by nutty) as a Spotify-style
"Listening to ..." card on Discord. Small settings window + system tray.
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import asyncio
    from pypresence import ActivityType, Presence
except ImportError:  # handled in main() with a friendly message
    Presence = ActivityType = None

APP_NAME = "SMTC Discord RPC"
APP_VERSION = "1.0.0"
DEV_PORTAL = "https://discord.com/developers/applications"

DEFAULT_BRIDGE = "http://127.0.0.1:5000/now-playing"
POLL_SECONDS = 2          # how often to ask the bridge
MIN_UPDATE_GAP = 15       # Discord accepts ~1 presence update / 15s
SEEK_TOLERANCE_MS = 3000  # position jump larger than this counts as a seek
PLAYING = 4               # SMTC PlaybackStatus.PLAYING
TYPE_VIDEO = 2            # SMTC PlaybackType.VIDEO

DEFAULTS = {
    "client_id": "",
    "bridge_url": DEFAULT_BRIDGE,
    "music_only": False,
    "use_itunes_art": True,
    "ignore_apps": "",
    "run_on_boot": False,
    "auto_start": True,
}

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SMTCDiscordRPC"

COLORS = {"ok": "#3ba55d", "warn": "#faa61a", "error": "#ed4245", "idle": "#80848e"}


# ----------------------------- helpers -----------------------------
def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_FILE.read_text("utf-8"))
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")
    except Exception:
        pass


def valid_id(s):
    return bool(re.fullmatch(r"\d{17,20}", (s or "").strip()))


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{os.path.abspath(__file__)}" --minimized'


def set_autostart(enabled):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


_mutex = None


def single_instance():
    global _mutex
    if os.name != "nt":
        return True
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SMTCDiscordRPC")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bridge_endpoint(s):
    url = (s.get("bridge_url") or DEFAULT_BRIDGE).strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    if not url.endswith("now-playing"):
        url += "/now-playing"
    return url


def parse_iso(s):
    """Tolerant ISO 8601 parser (handles 'Z' and >6 fractional digits)."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        i = 0
        while i < len(rest) and rest[i].isdigit():
            i += 1
        s = head + "." + rest[:i][:6].ljust(6, "0") + rest[i:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clip(s, fallback="Unknown"):
    s = (s or "").strip() or fallback
    s = s[:128]
    return s if len(s) >= 2 else s + "\u200b"


def pick_session(data, s):
    """Prefer the bridge's current session, fall back to any playing one."""
    ignore = [x.strip().lower() for x in (s.get("ignore_apps") or "").split(",") if x.strip()]
    sessions = data.get("sessions") or []
    current = data.get("current_session_id")
    ordered = [x for x in sessions if current and x.get("source_app_id") == current] + sessions
    for sess in ordered:
        app = (sess.get("source_app_id") or "").lower()
        if any(x in app for x in ignore):
            continue
        pi = sess.get("playback_info") or {}
        if pi.get("PlaybackStatus") != PLAYING:
            continue
        if s.get("music_only") and pi.get("PlaybackType") == TYPE_VIDEO:
            continue
        if not ((sess.get("media_properties") or {}).get("Title") or "").strip():
            continue
        return sess
    return None


def extract(session):
    mp = session.get("media_properties") or {}
    pi = session.get("playback_info") or {}
    tl = session.get("timeline_properties") or {}

    start = tl.get("StartTime") or 0
    end = tl.get("EndTime") or 0
    duration = max(0, end - start)

    pos = tl.get("Position") or 0
    last = parse_iso(tl.get("LastUpdatedTime"))
    if last:
        drift = (datetime.now(timezone.utc) - last).total_seconds() * 1000
        drift *= pi.get("PlaybackRate") or 1.0
        if drift >= 0 and (duration == 0 or pos + drift <= duration):
            pos += drift
    if duration:
        pos = min(max(pos, 0), duration)

    return {
        "title": (mp.get("Title") or "").strip(),
        "artist": (mp.get("Artist") or mp.get("AlbumArtist") or "").strip(),
        "album": (mp.get("AlbumTitle") or "").strip(),
        "duration": duration,
        "position": pos,
    }


_art_cache = {}


def find_art(title, artist):
    key = (title.lower(), artist.lower())
    if key in _art_cache:
        return _art_cache[key]
    url = None
    try:
        q = urllib.parse.urlencode(
            {"term": f"{artist} {title}".strip(), "entity": "song", "limit": 5}
        )
        data = get_json("https://itunes.apple.com/search?" + q, timeout=5)
        a = artist.lower().replace(" - topic", "")
        for r in data.get("results", []):
            ra = (r.get("artistName") or "").lower()
            if not a or a in ra or ra in a:
                url = (r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
                break
    except Exception:
        pass
    _art_cache[key] = url or None
    return _art_cache[key]


# ----------------------------- worker -----------------------------
class Worker(threading.Thread):
    """Polls the bridge and keeps the Discord presence in sync."""

    def __init__(self, settings, notify):
        super().__init__(daemon=True)
        self.s = dict(settings)
        self.client_id = settings["client_id"].strip()
        self.notify = notify
        self.stop_evt = threading.Event()
        self._last_status = None

    def update_settings(self, s):
        self.s = dict(s)  # client_id changes need a restart

    def stop(self):
        self.stop_evt.set()

    def _set(self, kind, text):
        if (kind, text) != self._last_status:
            self._last_status = (kind, text)
            self.notify(self, kind, text)

    def _connect(self):
        first = True
        while not self.stop_evt.is_set():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                rpc = Presence(self.client_id, loop=loop)
                rpc.connect()
                return rpc
            except Exception:
                if first:
                    self._set("warn", "Waiting for Discord... (is the desktop app open?)")
                    first = False
                self.stop_evt.wait(5)
        return None

    def run(self):
        rpc = None
        try:
            rpc = self._connect()
            if rpc is None:
                return
            self._set("ok", "Connected to Discord")

            last_key = last_start = None
            last_push = 0.0
            shown = False

            while not self.stop_evt.is_set():
                s = self.s
                try:
                    data = get_json(bridge_endpoint(s))
                except Exception:
                    data = None

                session = pick_session(data, s) if data else None

                if session is None:
                    if shown:
                        try:
                            rpc.clear()
                        except Exception:
                            rpc = self._connect()
                            if rpc is None:
                                return
                        shown, last_key, last_start = False, None, None
                    if data is None:
                        self._set("warn", "Can't reach SMTC Bridge. Is it running?")
                    else:
                        self._set("ok", "Connected. Nothing playing right now.")
                    self.stop_evt.wait(POLL_SECONDS)
                    continue

                info = extract(session)
                est_start = time.time() * 1000 - info["position"]
                key = (info["title"], info["artist"], info["album"])
                changed = (
                    key != last_key
                    or last_start is None
                    or abs(est_start - last_start) > SEEK_TOLERANCE_MS
                )

                if changed and time.time() - last_push >= MIN_UPDATE_GAP:
                    art = find_art(info["title"], info["artist"]) if s.get("use_itunes_art") else None
                    payload = {
                        "activity_type": ActivityType.LISTENING,
                        "details": clip(info["title"]),
                        "state": clip(info["artist"], "Unknown artist"),
                        "start": int(est_start / 1000),
                    }
                    if info["duration"]:
                        payload["end"] = int((est_start + info["duration"]) / 1000)
                    if art:
                        payload["large_image"] = art
                        payload["large_text"] = clip(info["album"] or info["title"])
                    try:
                        rpc.update(**payload)
                        last_key, last_start, last_push, shown = key, est_start, time.time(), True
                        label = f"{info['artist']} - {info['title']}" if info["artist"] else info["title"]
                        self._set("ok", "Showing: " + label[:60])
                    except Exception:
                        self._set("warn", "Lost connection to Discord, reconnecting...")
                        rpc = self._connect()
                        if rpc is None:
                            return
                        self._set("ok", "Connected to Discord")
                        last_key = None

                self.stop_evt.wait(POLL_SECONDS)
        finally:
            if rpc is not None:
                try:
                    rpc.clear()
                    rpc.close()
                except Exception:
                    pass


# ------------------------------- GUI -------------------------------
class App:
    def __init__(self, root, start_hidden=False):
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()
        self.worker = None
        self.tray = None
        self._quitting = False
        self._tray_hint_shown = False

        root.title(APP_NAME)
        root.resizable(False, False)
        try:
            root.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass
        try:
            ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
        except Exception:
            pass

        self._build()
        self.tray = self._make_tray()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(200, self._poll)

        if self.cfg["run_on_boot"] and os.name == "nt":
            try:
                set_autostart(True)  # keeps the registry path fresh if the exe moved
            except Exception:
                pass

        if start_hidden:
            self._hide(first_time_hint=False)
        if self.cfg["auto_start"] and valid_id(self.cfg["client_id"]):
            self.start()

    # ---- layout ----
    def _build(self):
        c = self.cfg
        self.v_id = tk.StringVar(value=c["client_id"])
        self.v_music = tk.BooleanVar(value=c["music_only"])
        self.v_art = tk.BooleanVar(value=c["use_itunes_art"])
        self.v_boot = tk.BooleanVar(value=c["run_on_boot"])
        self.v_auto = tk.BooleanVar(value=c["auto_start"])
        self.v_ignore = tk.StringVar(value=c["ignore_apps"])
        self.v_bridge = tk.StringVar(value=c["bridge_url"])

        f = ttk.Frame(self.root, padding=(24, 20, 24, 20))
        f.grid()
        f.columnconfigure(0, weight=1)

        title_font = ("Segoe UI", 15, "bold") if os.name == "nt" else None
        ttk.Label(f, text=APP_NAME, font=title_font).grid(row=0, column=0, sticky="w")
        ttk.Label(f, text="Show what you're playing on Discord, Spotify-style.",
                  foreground="#6b6f76").grid(row=1, column=0, sticky="w", pady=(0, 16))

        ttk.Label(f, text="Discord Application ID").grid(row=2, column=0, sticky="w")
        self.id_entry = ttk.Entry(f, textvariable=self.v_id, width=44)
        self.id_entry.grid(row=3, column=0, sticky="ew", pady=(4, 2))
        link = ttk.Label(f, text="Where do I find this?", foreground="#0b6bcb", cursor="hand2")
        link.grid(row=4, column=0, sticky="w", pady=(0, 14))
        link.bind("<Button-1>", lambda e: webbrowser.open(DEV_PORTAL))

        opts = [
            (self.v_music, "Music only (ignore video like YouTube / Twitch)"),
            (self.v_art, "Fetch album art (iTunes lookup)"),
            (self.v_auto, "Start presence automatically when the app opens"),
        ]
        r = 5
        for var, text in opts:
            ttk.Checkbutton(f, text=text, variable=var, command=self._changed).grid(
                row=r, column=0, sticky="w", pady=2)
            r += 1
        boot = ttk.Checkbutton(f, text="Start with Windows (in the tray)",
                               variable=self.v_boot, command=self._boot_changed)
        boot.grid(row=r, column=0, sticky="w", pady=2)
        if os.name != "nt":
            boot.state(["disabled"])
        r += 1

        ttk.Label(f, text="Ignore apps (comma-separated, e.g. spotify, chrome)").grid(
            row=r, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(f, textvariable=self.v_ignore).grid(row=r + 1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(f, text="SMTC Bridge URL").grid(row=r + 2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(f, textvariable=self.v_bridge).grid(row=r + 3, column=0, sticky="ew", pady=(4, 0))

        for v in (self.v_id, self.v_ignore, self.v_bridge):
            v.trace_add("write", lambda *a: self._changed())

        ttk.Separator(f).grid(row=r + 4, column=0, sticky="ew", pady=16)

        bottom = ttk.Frame(f)
        bottom.grid(row=r + 5, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)
        self.dot = ttk.Label(bottom, text="\u25CF", foreground=COLORS["idle"])
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self.status = ttk.Label(bottom, text="Stopped", wraplength=250, justify="left")
        self.status.grid(row=0, column=1, sticky="w")
        self.btn = ttk.Button(bottom, text="Start", width=10, command=self.toggle)
        self.btn.grid(row=0, column=2, padx=(10, 0))

    # ---- settings ----
    def collect(self):
        return {
            "client_id": self.v_id.get().strip(),
            "bridge_url": self.v_bridge.get().strip() or DEFAULT_BRIDGE,
            "music_only": self.v_music.get(),
            "use_itunes_art": self.v_art.get(),
            "ignore_apps": self.v_ignore.get().strip(),
            "run_on_boot": self.v_boot.get(),
            "auto_start": self.v_auto.get(),
        }

    def _changed(self):
        s = self.collect()
        save_config(s)
        if self.worker:
            self.worker.update_settings(s)

    def _boot_changed(self):
        try:
            set_autostart(self.v_boot.get())
        except Exception as e:
            self.v_boot.set(not self.v_boot.get())
            messagebox.showerror(APP_NAME, f"Couldn't change the startup setting:\n{e}")
        self._changed()

    # ---- start / stop ----
    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop()
        else:
            self.start()

    def start(self):
        s = self.collect()
        if not valid_id(s["client_id"]):
            self._status("error", "Enter a valid Application ID (17-20 digits).")
            self._show()
            self.id_entry.focus_set()
            return
        save_config(s)
        self.worker = Worker(s, lambda w, k, t: self.q.put(("status", w, k, t)))
        self.worker.start()
        self.btn.config(text="Stop")
        self._status("warn", "Connecting to Discord...")

    def stop(self, quiet=False):
        w, self.worker = self.worker, None
        if w:
            w.stop()
            w.join(timeout=3)
        self.btn.config(text="Start")
        if not quiet:
            self._status("idle", "Stopped")

    def _status(self, kind, text):
        self.dot.config(foreground=COLORS.get(kind, COLORS["idle"]))
        self.status.config(text=text)

    # ---- tray ----
    def _make_tray(self):
        try:
            import pystray
            from PIL import Image
        except Exception:
            return None
        try:
            img = Image.open(resource_path("icon.ico")).convert("RGBA")
        except Exception:
            img = Image.new("RGBA", (64, 64), "#5865F2")
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda icon, item: self.q.put(("ui", "show")), default=True),
            pystray.MenuItem("Quit", lambda icon, item: self.q.put(("ui", "quit"))),
        )
        try:
            icon = pystray.Icon("smtc-discord-rpc", img, APP_NAME, menu)
            icon.run_detached()
            return icon
        except Exception:
            return None

    def _hide(self, first_time_hint=True):
        self.root.withdraw()
        if first_time_hint and self.tray and not self._tray_hint_shown:
            self._tray_hint_shown = True
            try:
                self.tray.notify("Still running in the tray. Right-click the icon to quit.", APP_NAME)
            except Exception:
                pass

    def _show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.tray:
            self._hide()
        else:
            self.quit()

    def quit(self):
        self._quitting = True
        save_config(self.collect())
        self.stop(quiet=True)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---- queue pump ----
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "status":
                    _, w, kind, text = msg
                    if w is self.worker:
                        self._status(kind, text)
                elif msg[1] == "show":
                    self._show()
                elif msg[1] == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.btn.config(text="Start")
        if not self._quitting:
            self.root.after(200, self._poll)


def fatal(msg):
    r = tk.Tk()
    r.withdraw()
    messagebox.showerror(APP_NAME, msg, parent=r)
    r.destroy()


def main():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SMTCDiscordRPC.App")
        except Exception:
            pass

    if Presence is None or ActivityType is None:
        return fatal('pypresence 4.3.0 or newer is required.\n\nRun: pip install -U "pypresence>=4.3.0"')
    if not single_instance():
        return fatal("SMTC Discord RPC is already running. Check your system tray.")

    cfg = load_config()
    hidden = "--minimized" in sys.argv and valid_id(cfg["client_id"])
    root = tk.Tk()
    App(root, start_hidden=hidden)
    root.mainloop()


if __name__ == "__main__":
    main()

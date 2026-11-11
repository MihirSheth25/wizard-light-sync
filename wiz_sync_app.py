"""
WiZ Screen Sync
A desktop app that makes Philips WiZ bulbs follow the colors on your screen.

Unofficial. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.

Run:   python wiz_sync_app.py
Needs: pip install customtkinter mss numpy

Copyright (c) 2026 Mihir Sheth. MIT License, see LICENSE.
"""

import colorsys
import copy
import json
import os
import queue
import socket
import sys
import threading
import time

import numpy as np

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    sys.exit("Missing package. Run:  pip install customtkinter mss numpy")

try:  # system tray support is optional
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except Exception:  # missing package, or no tray backend on this system
    TRAY_OK = False

APP_NAME = "WiZ Screen Sync"
WIZ_PORT = 38899

ZONES = ["Full screen", "Left half", "Right half", "Top half", "Bottom half", "Center",
         "Top left", "Top right", "Bottom left", "Bottom right"]
COLOR_MODES = ["Vivid", "Average", "Dominant"]
BRIGHTNESS_MODES = ["Follow screen", "Fixed"]
DARK_SCENE = ["Dim warm white", "Keep last color", "Turn off"]

DEFAULTS = {
    "monitor": 1,
    "fps": 10,
    "smoothing": 0.5,
    "saturation": 2.0,
    "color_mode": "Vivid",
    "change_threshold": 3,
    "brightness_mode": "Follow screen",
    "min_brightness": 15,
    "max_brightness": 100,
    "brightness_gain": 3.0,
    "fixed_brightness": 80,
    "dark_scene": "Dim warm white",
    "dark_threshold": 0.03,
    "ignore_black_bars": True,
    "crop_top": 0,
    "crop_bottom": 0,
    "crop_left": 0,
    "crop_right": 0,
    "sample_step": 8,
    "start_sync_on_launch": False,
    "launch_at_login": False,
    "restore_on_stop": True,
    "close_to_tray": True,
    "bulbs": {},  # ip -> {"name", "enabled", "zone", "brightness_scale"}
}

PRESETS = {
    "Movie": {"smoothing": 0.7, "saturation": 1.6, "fps": 10, "color_mode": "Vivid",
              "brightness_mode": "Follow screen", "max_brightness": 80, "brightness_gain": 2.5},
    "Gaming": {"smoothing": 0.2, "saturation": 2.2, "fps": 20, "color_mode": "Vivid",
               "brightness_mode": "Follow screen", "max_brightness": 100, "brightness_gain": 3.0},
    "Chill": {"smoothing": 0.9, "saturation": 1.3, "fps": 6, "color_mode": "Average",
              "brightness_mode": "Fixed", "fixed_brightness": 50},
    "Party": {"smoothing": 0.0, "saturation": 3.0, "fps": 20, "color_mode": "Dominant",
              "brightness_mode": "Follow screen", "max_brightness": 100, "brightness_gain": 4.0},
}


# ======================================================================= storage

def settings_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "WiZScreenSync")
    os.makedirs(path, exist_ok=True)
    return path


SETTINGS_FILE = os.path.join(settings_dir(), "settings.json")


def load_settings():
    data = copy.deepcopy(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in data:
                data[k] = v
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# ======================================================================= bulbs (UDP)

def discover_bulbs(timeout=2.5):
    """Broadcast getPilot and return {ip: result} for every WiZ bulb that answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    msg = json.dumps({"method": "getPilot", "params": {}}).encode()
    found, end, next_send = {}, time.time() + timeout, 0.0
    while time.time() < end:
        if time.time() >= next_send:
            try:
                sock.sendto(msg, ("255.255.255.255", WIZ_PORT))
            except OSError:
                pass
            next_send = time.time() + 0.7
        try:
            data, (ip, _) = sock.recvfrom(4096)
            reply = json.loads(data.decode())
        except (socket.timeout, ValueError, OSError):
            continue
        if reply.get("method") == "getPilot" and "result" in reply:
            found[ip] = reply["result"]
    sock.close()
    return found


def bulb_send(sock, ip, params):
    try:
        sock.sendto(json.dumps({"method": "setPilot", "params": params}).encode(), (ip, WIZ_PORT))
    except OSError:
        pass


def bulb_get_state(sock, ip):
    msg = json.dumps({"method": "getPilot", "params": {}}).encode()
    for _ in range(3):
        try:
            sock.sendto(msg, (ip, WIZ_PORT))
            data, _ = sock.recvfrom(4096)
            return json.loads(data.decode()).get("result")
        except (socket.timeout, ValueError, OSError):
            continue
    return None


def restore_params(state):
    if not state:
        return None
    if not state.get("state", True):
        return {"state": False}
    params = {"state": True}
    if "dimming" in state:
        params["dimming"] = state["dimming"]
    if state.get("sceneId"):
        params["sceneId"] = state["sceneId"]
        if "speed" in state:
            params["speed"] = state["speed"]
    elif "temp" in state:
        params["temp"] = state["temp"]
    elif all(k in state for k in ("r", "g", "b")):
        for k in ("r", "g", "b", "c", "w"):
            if k in state:
                params[k] = state[k]
    return params


# ======================================================================= color math

def crop_zone(frame, zone):
    h, w = frame.shape[:2]
    hh, hw = h // 2, w // 2
    return {
        "Left half": frame[:, :hw], "Right half": frame[:, hw:],
        "Top half": frame[:hh], "Bottom half": frame[hh:],
        "Center": frame[h // 4: 3 * h // 4, w // 4: 3 * w // 4],
        "Top left": frame[:hh, :hw], "Top right": frame[:hh, hw:],
        "Bottom left": frame[hh:, :hw], "Bottom right": frame[hh:, hw:],
    }.get(zone, frame)


def crop_margins(frame, s):
    h, w = frame.shape[:2]
    t = int(h * s["crop_top"] / 100)
    b = h - int(h * s["crop_bottom"] / 100)
    l = int(w * s["crop_left"] / 100)
    r = w - int(w * s["crop_right"] / 100)
    if b - t < 4 or r - l < 4:
        return frame
    return frame[t:b, l:r]


def strip_black_bars(frame, threshold=12):
    """Remove letterbox / pillarbox bars so movies do not read as darker than they are."""
    lum = frame.max(axis=2)
    rows = np.where(lum.mean(axis=1) > threshold)[0]
    cols = np.where(lum.mean(axis=0) > threshold)[0]
    if len(rows) < frame.shape[0] * 0.2 or len(cols) < frame.shape[1] * 0.2:
        return frame
    return frame[rows[0]: rows[-1] + 1, cols[0]: cols[-1] + 1]


def pick_color(pixels, mode="Vivid", saturation_boost=2.0):
    """Return (r, g, b, brightness 0..1)."""
    px = pixels.reshape(-1, 3).astype(np.float32) / 255.0
    if px.size == 0:
        return 0, 0, 0, 0.0
    mx = px.max(axis=1)
    mn = px.min(axis=1)
    brightness = float(mx.mean())
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)

    if mode == "Average":
        rgb = px.mean(axis=0)
    elif mode == "Dominant":
        # Group pixels by hue (12 buckets), weight by how vivid they are, take the strongest group.
        hue = np.zeros_like(mx)
        d = np.maximum(mx - mn, 1e-6)
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        hue = np.where(mx == r, ((g - b) / d) % 6, hue)
        hue = np.where(mx == g, (b - r) / d + 2, hue)
        hue = np.where(mx == b, (r - g) / d + 4, hue)
        bucket = (hue * 2).astype(int) % 12
        weight = sat * mx
        totals = np.bincount(bucket, weights=weight, minlength=12)
        if totals.max() < 1e-3:
            rgb = px.mean(axis=0)
        else:
            sel = bucket == int(totals.argmax())
            wsel = weight[sel] + 1e-6
            rgb = (px[sel] * wsel[:, None]).sum(axis=0) / wsel.sum()
    else:  # Vivid
        weight = (sat ** 2) * mx + 0.02
        rgb = (px * weight[:, None]).sum(axis=0) / weight.sum()

    if rgb.max() < 1e-4:
        return 0, 0, 0, brightness
    h, s, _ = colorsys.rgb_to_hsv(*rgb)
    s = min(1.0, s * saturation_boost)
    r, g, b = (int(round(v * 255)) for v in colorsys.hsv_to_rgb(h, s, 1.0))
    return r, g, b, brightness


# ======================================================================= sync engine

class SyncEngine(threading.Thread):
    """Captures the screen and drives the bulbs. Reads settings live every frame."""

    def __init__(self, get_settings):
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.stop_event = threading.Event()
        self.current = {}        # ip -> (r, g, b, dimming) last sent
        self.preview = None      # small RGB thumbnail of the captured screen
        self.hold_until = {}     # ip -> time; the engine leaves that bulb alone until then
        self.status = "Starting"
        self.error = None
        self.actual_fps = 0.0
        self.saved_states = {}

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            import mss
        except ImportError:
            self.error = "Missing package 'mss'. Run: pip install mss"
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        smooth, last_sent = {}, {}
        frames, fps_t0 = 0, time.perf_counter()
        try:
            grabber = getattr(mss, "MSS", None) or mss.mss
            with grabber() as sct:
                self.status = "Syncing"
                while not self.stop_event.is_set():
                    t0 = time.perf_counter()
                    s = self.get_settings()
                    bulbs = {ip: b for ip, b in s["bulbs"].items() if b.get("enabled", True)}
                    for ip in bulbs:
                        if ip not in self.saved_states:
                            self.saved_states[ip] = bulb_get_state(sock, ip)

                    mon_idx = s["monitor"] if 0 < s["monitor"] < len(sct.monitors) else 1
                    step = max(1, int(s["sample_step"]))
                    shot = np.asarray(sct.grab(sct.monitors[mon_idx]))
                    frame = shot[::step, ::step, 2::-1]
                    k = max(1, shot.shape[1] // 240)
                    self.preview = shot[::k, ::k, 2::-1]   # small copy for the UI thumbnail
                    frame = crop_margins(frame, s)
                    if s["ignore_black_bars"]:
                        frame = strip_black_bars(frame)

                    for ip, b in bulbs.items():
                        if self.hold_until.get(ip, 0) > time.time():
                            last_sent.pop(ip, None)   # resend a color as soon as the hold ends
                            continue
                        r, g, bl, bright = pick_color(crop_zone(frame, b.get("zone", "Full screen")),
                                                      s["color_mode"], s["saturation"])
                        a = max(0.0, min(0.95, float(s["smoothing"])))
                        new = np.array([r, g, bl, bright], dtype=np.float32)
                        prev = smooth.get(ip)
                        val = new if prev is None else prev * a + new * (1 - a)
                        smooth[ip] = val
                        sr, sg, sb, sbright = (float(x) for x in val)

                        if s["brightness_mode"] == "Fixed":
                            dim = s["fixed_brightness"]
                        else:
                            lo, hi = s["min_brightness"], max(s["min_brightness"], s["max_brightness"])
                            dim = lo + (hi - lo) * min(1.0, sbright * s["brightness_gain"])
                        dim = dim * b.get("brightness_scale", 100) / 100
                        dim = int(max(10, min(100, dim)))

                        is_dark = sbright < s["dark_threshold"] or max(sr, sg, sb) < 1
                        if is_dark and s["dark_scene"] == "Keep last color" and ip in last_sent:
                            continue
                        if is_dark and s["dark_scene"] == "Turn off":
                            key = ("off",)
                            params = {"state": False}
                            shown = (0, 0, 0, 0)
                        elif is_dark:
                            key = ("dark", dim)
                            params = {"state": True, "temp": 2700, "dimming": 10}
                            shown = (255, 170, 90, 10)
                        else:
                            color = (int(sr), int(sg), int(sb), dim)
                            key = color
                            params = {"state": True, "r": color[0], "g": color[1], "b": color[2],
                                      "c": 0, "w": 0, "dimming": dim}
                            shown = color
                        old = last_sent.get(ip)
                        thr = s["change_threshold"]
                        both_colors = (old is not None and len(key) == 4 and len(old) == 4
                                       and isinstance(key[0], int) and isinstance(old[0], int))
                        if both_colors:
                            changed = max(abs(x - y) for x, y in zip(key, old)) >= max(1, thr)
                        else:
                            changed = key != old
                        if changed:
                            bulb_send(sock, ip, params)
                            last_sent[ip] = key
                            self.current[ip] = shown

                    frames += 1
                    now = time.perf_counter()
                    if now - fps_t0 >= 1.0:
                        self.actual_fps = frames / (now - fps_t0)
                        frames, fps_t0 = 0, now
                    fps = max(1.0, min(20.0, float(s["fps"])))
                    self.stop_event.wait(max(0.0, 1.0 / fps - (time.perf_counter() - t0)))
        except Exception as exc:  # show any capture/network error in the UI
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            s = self.get_settings()
            if s.get("restore_on_stop", True):
                for ip, state in self.saved_states.items():
                    params = restore_params(state)
                    if params:
                        for _ in range(3):
                            bulb_send(sock, ip, params)
                            time.sleep(0.04)
            sock.close()
            self.status = "Stopped"


# ======================================================================= launch at login (Windows)

def startup_shortcut_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        "WiZ Screen Sync.vbs")


def set_launch_at_login(enabled):
    path = startup_shortcut_path()
    if not path:
        return False
    if not enabled:
        if os.path.exists(path):
            os.remove(path)
        return True
    if getattr(sys, "frozen", False):          # built .exe
        cmd = f'""{sys.executable}"" --autostart'
    else:                                       # running the .py with Python
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        cmd = f'""{exe}"" ""{os.path.abspath(__file__)}"" --autostart'
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\n')
    return True


# ======================================================================= UI

import tkinter as tk
from tkinter import font as tkfont

try:
    from PIL import Image as PILImage, ImageDraw as PILDraw, ImageFilter, ImageTk, ImageChops
    PIL_OK = True
except Exception:
    PIL_OK = False

# Night-bedroom palette
NIGHT = "#16121F"       # the wall at night: window background
SURFACE = "#211B2E"     # panels
RAISED = "#2C2540"      # inputs, menus, hover
LINE = "#3A3250"        # hairlines and slider tracks
MOON = "#ECE6F5"        # main text
MUTED = "#948BAA"       # secondary text
FILAMENT = "#F2A65A"    # warm tungsten amber: primary action and sliders
FILAMENT_HOVER = "#F7BC7E"
ROSE = "#E5677A"        # stop / remove
ROSE_HOVER = "#EE8494"
OFF_BULB = (58, 51, 80)


def hex_color(rgb):
    r, g, b = (max(0, min(255, int(x))) for x in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgb(hexstr):
    hexstr = hexstr.lstrip("#")
    return tuple(int(hexstr[i:i + 2], 16) for i in (0, 2, 4))


def bulb_side(zone, index):
    if "left" in zone.lower():
        return "left"
    if "right" in zone.lower():
        return "right"
    return "left" if index % 2 == 0 else "right"


def zone_box(zone, x0, y0, x1, y1):
    """Rectangle of the screen area a bulb follows, inside the thumbnail box."""
    w, h = x1 - x0, y1 - y0
    fr = {
        "Left half": (0, 0, .5, 1), "Right half": (.5, 0, 1, 1), "Top half": (0, 0, 1, .5),
        "Bottom half": (0, .5, 1, 1), "Center": (.25, .25, .75, .75), "Top left": (0, 0, .5, .5),
        "Top right": (.5, 0, 1, .5), "Bottom left": (0, .5, .5, 1), "Bottom right": (.5, .5, 1, 1),
    }.get(zone, (0, 0, 1, 1))
    return (x0 + fr[0] * w, y0 + fr[1] * h, x0 + fr[2] * w, y0 + fr[3] * h)


def render_room(w, h, bulbs, thumb, scale=1.0):
    """
    Draw the room scene: the monitor in the middle with the live screen, bulbs on either
    side throwing their color onto the wall. bulbs: list of dicts with x, y, color or None, dim.
    Returns a PIL image.
    """
    bg = _rgb(NIGHT)
    q = 4  # paint the light wash at quarter size, then blur and scale up (fast and soft)
    wash = PILImage.new("RGB", (max(1, w // q), max(1, h // q)), bg)
    for b in bulbs:
        if not b["color"]:
            continue
        strength = 0.22 + 0.38 * (b["dim"] / 100)
        col = tuple(int(c * strength) for c in b["color"])
        layer = PILImage.new("RGB", wash.size, (0, 0, 0))
        d = PILDraw.Draw(layer)
        cx, cy, r = b["x"] / q, b["y"] / q, (95 * scale) / q
        d.ellipse((cx - r, cy - r * 1.2, cx + r, cy + r * 1.2), fill=col)
        layer = layer.filter(ImageFilter.GaussianBlur(radius=r * 1.1))
        wash = ImageChops.add(wash, layer)
    # let the light fade into the wall at the top and bottom edges
    fade = PILImage.linear_gradient("L").resize((1, wash.size[1]))
    fade = PILImage.eval(fade, lambda v: int(255 * (1 - abs(v - 127.5) / 127.5) ** 0.55))
    fade = fade.resize(wash.size)
    wash = PILImage.composite(wash, PILImage.new("RGB", wash.size, bg), fade)
    img = wash.resize((w, h), PILImage.BILINEAR)
    d = PILDraw.Draw(img)

    # monitor
    mw, mh = int(250 * scale), int(141 * scale)
    mx0, my0 = (w - mw) // 2, int(h * 0.40 - mh / 2)
    bez = max(2, int(5 * scale))
    d.rounded_rectangle((mx0 - bez, my0 - bez, mx0 + mw + bez, my0 + mh + bez),
                        radius=int(7 * scale), fill=(10, 8, 15), outline=(70, 62, 92), width=max(1, int(scale)))
    if thumb is not None:
        t = PILImage.fromarray(np.ascontiguousarray(thumb)).resize((mw, mh), PILImage.BILINEAR)
        img.paste(t, (mx0, my0))
    else:
        d.rectangle((mx0, my0, mx0 + mw, my0 + mh), fill=(24, 20, 34))
    # stand
    sx = w // 2
    d.rectangle((sx - int(7 * scale), my0 + mh + bez, sx + int(7 * scale), my0 + mh + int(24 * scale)),
                fill=(52, 46, 70))
    d.rounded_rectangle((sx - int(46 * scale), my0 + mh + int(22 * scale),
                         sx + int(46 * scale), my0 + mh + int(28 * scale)),
                        radius=int(3 * scale), fill=(52, 46, 70))

    # outline of the screen area each bulb follows, in that bulb's color
    for i, b in enumerate(bulbs):
        if not b["color"] or thumb is None:
            continue
        inset = i * max(2, int(2 * scale))
        x0, y0, x1, y1 = zone_box(b["zone"], mx0, my0, mx0 + mw, my0 + mh)
        d.rectangle((x0 + inset, y0 + inset, x1 - inset - 1, y1 - inset - 1),
                    outline=b["color"], width=max(2, int(2 * scale)))

    # bulbs
    for b in bulbs:
        cx, cy = b["x"], b["y"]
        r = int(20 * scale)
        if b["color"]:
            core = tuple(int(c + (255 - c) * 0.35) for c in b["color"])
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=b["color"])
            r2 = int(r * 0.55)
            d.ellipse((cx - r2, cy - r2 - int(3 * scale), cx + r2, cy + r2 - int(3 * scale)), fill=core)
        else:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=OFF_BULB, outline=(84, 75, 110),
                      width=max(1, int(scale)))
        # screw base
        d.rounded_rectangle((cx - int(9 * scale), cy + r - int(2 * scale), cx + int(9 * scale),
                             cy + r + int(9 * scale)), radius=int(2 * scale), fill=(110, 102, 128))
    return img, (mx0, my0, mw, mh)


class App(ctk.CTk):
    PAGES = ("Bulbs", "Color", "Brightness", "Screen", "App")

    def __init__(self, autostart=False):
        super().__init__(fg_color=NIGHT)
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self._set_window_icon()
        self.geometry("1080x780")
        self.minsize(960, 680)

        self._pick_fonts()
        self.settings = load_settings()
        self.lock = threading.Lock()
        self.engine = None
        self.bulb_rows = {}
        self.slider_widgets = {}
        self._save_job = None
        self.ui_queue = queue.Queue()
        self.tray = None
        self._tray_notified = False
        self._quitting = False
        self._hero_img = None
        self._last_hero = 0.0

        self._build_header()
        self._build_room()
        self._build_body()
        self._refresh_bulb_rows()
        self._show_page("Bulbs")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._tick)

        if not self.settings["bulbs"]:
            self.after(400, self._discover)
        if autostart or self.settings["start_sync_on_launch"]:
            self.after(1500, self._start)
        self._setup_tray()
        if autostart:
            self.after(100, self.withdraw if self.tray else self.iconify)

    def _set_window_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # inside the .exe or next to the .py
        path = os.path.join(base, "wiz.ico")
        if os.path.exists(path) and sys.platform == "win32":
            try:
                self.iconbitmap(path)
                self.after(250, lambda: self.iconbitmap(path))  # customtkinter sets its own icon shortly after start
            except Exception:
                pass

    # ------------------------------------------------------------ type
    def _pick_fonts(self):
        fams = set(tkfont.families(self))

        def first(*names):
            for n in names:
                if n in fams:
                    return n
            return names[-1]

        display = first("Bahnschrift", "Barlow", "Roboto Condensed", "Segoe UI")
        display_bold = first("Bahnschrift SemiBold", "Barlow SemiBold", "Roboto Condensed Medium", display)
        body = first("Segoe UI Variable Text", "Segoe UI", "Open Sans", "DejaVu Sans")
        bold_w = "normal" if display_bold != display else "bold"
        self.f_title = ctk.CTkFont(family=display_bold, size=22, weight=bold_w)
        self.f_head = ctk.CTkFont(family=display_bold, size=17, weight=bold_w)
        self.f_num = ctk.CTkFont(family=display, size=15)
        self.f_nav = ctk.CTkFont(family=display, size=16)
        self.f_body = ctk.CTkFont(family=body, size=13)
        self.f_small = ctk.CTkFont(family=body, size=12)
        self.f_button = ctk.CTkFont(family=display_bold, size=15, weight=bold_w)
        self.display_family, self.display_bold = display, display_bold

    # ------------------------------------------------------------ settings helpers
    def get_settings(self):
        with self.lock:
            return copy.deepcopy(self.settings)

    def set(self, key, value):
        with self.lock:
            self.settings[key] = value
        self._schedule_save()

    def set_bulb(self, ip, key, value):
        with self.lock:
            self.settings["bulbs"].setdefault(ip, {})[key] = value
        self._schedule_save()

    def _schedule_save(self):
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(500, lambda: save_settings(self.get_settings()))

    # ------------------------------------------------------------ styled widget helpers
    def _label(self, parent, text, muted=False, font=None, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=MUTED if muted else MOON,
                            font=font or (self.f_small if muted else self.f_body), **kw)

    def _menu(self, parent, values, command, width=170):
        return ctk.CTkOptionMenu(parent, values=values, command=command, width=width,
                                 fg_color=RAISED, button_color=RAISED, button_hover_color=LINE,
                                 text_color=MOON, dropdown_fg_color=RAISED, dropdown_hover_color=LINE,
                                 dropdown_text_color=MOON, font=self.f_body, dropdown_font=self.f_body,
                                 corner_radius=8)

    def _ghost_button(self, parent, text, command, color=MUTED, width=80):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=30,
                             fg_color="transparent", hover_color=RAISED, text_color=color,
                             font=self.f_small, corner_radius=8)

    def _slider_widget(self, parent, lo, hi, steps, command, width=None):
        kw = {"width": width} if width else {}
        return ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, command=command,
                             fg_color=LINE, progress_color=FILAMENT, button_color=FILAMENT,
                             button_hover_color=FILAMENT_HOVER, height=16, **kw)

    # ------------------------------------------------------------ header
    def _build_header(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=28, pady=(20, 0))
        ctk.CTkLabel(top, text=APP_NAME, font=self.f_title, text_color=MOON).pack(side="left")

        self.start_btn = ctk.CTkButton(top, text="Start sync", width=150, height=42, corner_radius=21,
                                       font=self.f_button, fg_color=FILAMENT, hover_color=FILAMENT_HOVER,
                                       text_color=NIGHT, command=self._toggle)
        self.start_btn.pack(side="right")
        self.preset_menu = self._menu(top, ["Presets"] + list(PRESETS), self._apply_preset, width=140)
        self.preset_menu.set("Presets")
        self.preset_menu.pack(side="right", padx=14)

    # ------------------------------------------------------------ the room scene
    def _build_room(self):
        self.room_h = 250
        self.room = tk.Canvas(self, height=self.room_h, bg=NIGHT, highlightthickness=0, bd=0)
        self.room.pack(fill="x", padx=0, pady=(10, 0))
        self.room.bind("<Configure>", lambda e: self._draw_room(force=True))

    def _room_bulbs(self, w, h, scale):
        bulbs = [(ip, b) for ip, b in sorted(self.settings["bulbs"].items()) if b.get("enabled", True)]
        sides = {"left": [], "right": []}
        for i, (ip, b) in enumerate(bulbs):
            sides[bulb_side(b.get("zone", "Full screen"), i)].append((ip, b))
        out = []
        syncing = self._is_syncing()
        cur = self.engine.current if self.engine else {}
        mon_half = 125 * scale
        for side, items in sides.items():
            for j, (ip, b) in enumerate(items):
                gap = 150 * scale + j * 150 * scale
                x = w / 2 - mon_half - gap if side == "left" else w / 2 + mon_half + gap
                col = cur.get(ip) if syncing else None
                color = tuple(col[:3]) if col and col[3] else None
                out.append({"ip": ip, "name": b.get("name", ip), "zone": b.get("zone", "Full screen"),
                            "x": int(x), "y": int(h * 0.36), "color": color,
                            "dim": col[3] if col else 0})
        return out

    def _draw_room(self, force=False):
        c = self.room
        w = max(200, c.winfo_width())
        h = max(150, c.winfo_height())
        scale = h / self.room_h
        bulbs = self._room_bulbs(w, h, scale)
        c.delete("all")
        thumb = self.engine.preview if (self._is_syncing() and self.engine) else None
        if PIL_OK:
            img, (mx0, my0, mw, mh) = render_room(w, h, bulbs, thumb, scale)
            self._hero_img = ImageTk.PhotoImage(img)
            c.create_image(0, 0, image=self._hero_img, anchor="nw")
        else:
            mw, mh = int(250 * scale), int(141 * scale)
            mx0, my0 = (w - mw) // 2, int(h * 0.40 - mh / 2)
            c.create_rectangle(mx0, my0, mx0 + mw, my0 + mh, fill=SURFACE, outline=LINE)
            for b in bulbs:
                r = 20 * scale
                c.create_oval(b["x"] - r, b["y"] - r, b["x"] + r, b["y"] + r,
                              fill=hex_color(b["color"]) if b["color"] else hex_color(OFF_BULB), outline="")

        # words on the scene
        name_font = (self.display_bold, 13)
        info_font = (self.f_small.cget("family"), 10)
        for b in bulbs:
            c.create_text(b["x"], b["y"] + 44 * scale, text=b["name"], fill=MOON, font=name_font)
            detail = b["zone"] if not b["color"] else f"{b['zone']}, {b['dim']}%"
            c.create_text(b["x"], b["y"] + 64 * scale, text=detail, fill="#C9C0DC", font=info_font)
        if self._is_syncing():
            caption = f"Syncing at {self.engine.actual_fps:.0f} updates a second"
        elif self.engine and self.engine.error:
            caption = f"Stopped: {self.engine.error}"
        elif not self.settings["bulbs"]:
            caption = "No bulbs yet. Find them on the Bulbs page."
        else:
            caption = "Not syncing"
        c.create_text(w / 2, my0 + mh + 46 * scale, text=caption, fill=MUTED, font=info_font)
        if thumb is None:
            c.create_text(w / 2, my0 + mh / 2, text="Your screen shows here while syncing",
                          fill=MUTED, font=info_font, width=mw - 20, justify="center")

    # ------------------------------------------------------------ body: sidebar + pages
    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(6, 20))

        nav = ctk.CTkFrame(body, fg_color="transparent", width=170)
        nav.pack(side="left", fill="y", padx=(0, 14))
        self.nav_buttons = {}
        for name in self.PAGES:
            row = ctk.CTkFrame(nav, fg_color="transparent")
            row.pack(fill="x", pady=2)
            bar = ctk.CTkFrame(row, width=3, height=34, corner_radius=2, fg_color="transparent")
            bar.pack(side="left", padx=(0, 8))
            btn = ctk.CTkButton(row, text=name, anchor="w", width=150, height=40, corner_radius=10,
                                fg_color="transparent", hover_color=SURFACE, text_color=MUTED,
                                font=self.f_nav, command=lambda n=name: self._show_page(n))
            btn.pack(side="left", fill="x")
            self.nav_buttons[name] = (btn, bar)

        self.page_host = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=16)
        self.page_host.pack(side="left", fill="both", expand=True)
        self.pages = {}
        builders = {"Bulbs": self._build_bulbs_page, "Color": self._build_color_page,
                    "Brightness": self._build_brightness_page, "Screen": self._build_screen_page,
                    "App": self._build_app_page}
        for name in self.PAGES:
            frame = ctk.CTkFrame(self.page_host, fg_color="transparent")
            builders[name](frame)
            self.pages[name] = frame

    def _show_page(self, name):
        for n, f in self.pages.items():
            f.pack_forget()
        self.pages[name].pack(fill="both", expand=True, padx=6, pady=6)
        for n, (btn, bar) in self.nav_buttons.items():
            on = n == name
            btn.configure(text_color=MOON if on else MUTED, fg_color=SURFACE if on else "transparent")
            bar.configure(fg_color=FILAMENT if on else "transparent")

    def _scroll(self, parent):
        sf = ctk.CTkScrollableFrame(parent, fg_color="transparent", scrollbar_button_color=RAISED,
                                    scrollbar_button_hover_color=LINE)
        sf.pack(fill="both", expand=True)
        return sf

    def _group(self, parent, title, note=None):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=18, pady=(14, 4))
        ctk.CTkLabel(frame, text=title, font=self.f_head, text_color=MOON).pack(anchor="w")
        if note:
            self._label(frame, note, muted=True, wraplength=620, justify="left").pack(anchor="w", pady=(0, 4))
        return frame

    def _slider(self, parent, label, key, lo, hi, steps, fmt="{:.0f}", note=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure(1, weight=1)
        self._label(row, label, width=180, anchor="w").grid(row=0, column=0, sticky="w")
        value_lbl = ctk.CTkLabel(row, text=fmt.format(self.settings[key]), width=70, anchor="e",
                                 font=self.f_num, text_color=FILAMENT)
        value_lbl.grid(row=0, column=2, sticky="e")

        def changed(v, k=key, lbl=value_lbl, f=fmt, is_int=isinstance(DEFAULTS[key], int)):
            v = int(round(v)) if is_int else round(float(v), 2)
            lbl.configure(text=f.format(v))
            self.set(k, v)

        sl = self._slider_widget(row, lo, hi, steps, changed)
        sl.set(self.settings[key])
        sl.grid(row=0, column=1, sticky="ew", padx=14)
        if note:
            self._label(row, note, muted=True, anchor="w", justify="left", wraplength=420).grid(
                row=1, column=1, sticky="w", padx=14)
        self.slider_widgets[key] = (sl, value_lbl, fmt)
        return sl

    def _choice(self, parent, label, key, values, note=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._label(row, label, width=180, anchor="w").grid(row=0, column=0, sticky="w")
        seg = ctk.CTkSegmentedButton(row, values=values, command=lambda v, k=key: self.set(k, v),
                                     fg_color=RAISED, selected_color=FILAMENT,
                                     selected_hover_color=FILAMENT_HOVER, unselected_color=RAISED,
                                     unselected_hover_color=LINE, text_color=MOON,
                                     font=self.f_body, corner_radius=10, height=32)
        seg.set(self.settings[key])
        seg.grid(row=0, column=1, sticky="w", padx=14)
        self._recolor_segment(seg)
        seg.configure(command=lambda v, k=key, s=seg: (self.set(k, v), self._recolor_segment(s)))
        if note:
            self._label(row, note, muted=True, anchor="w", justify="left", wraplength=480).grid(
                row=1, column=1, sticky="w", padx=14, pady=(4, 0))
        self.slider_widgets[key] = (seg, None, None)
        return seg

    def _recolor_segment(self, seg):
        """Dark text on the amber selected option, light text on the rest."""
        cur = seg.get()
        for value, btn in getattr(seg, "_buttons_dict", {}).items():
            btn.configure(text_color=NIGHT if value == cur else MOON)

    def _switch(self, parent, label, key, command=None):
        var = ctk.BooleanVar(value=bool(self.settings[key]))

        def changed(k=key, v=var):
            self.set(k, bool(v.get()))
            if command:
                command(bool(v.get()))

        sw = ctk.CTkSwitch(parent, text=label, variable=var, command=changed, font=self.f_body,
                           text_color=MOON, fg_color=LINE, progress_color=FILAMENT,
                           button_color=MOON, button_hover_color="#ffffff", switch_width=42, switch_height=22)
        sw.pack(anchor="w", pady=7)
        self.slider_widgets[key] = (sw, var, None)
        return sw

    # ---- Bulbs page
    def _build_bulbs_page(self, page):
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(bar, text="Your bulbs", font=self.f_head, text_color=MOON).pack(side="left")
        self.find_btn = ctk.CTkButton(bar, text="Find bulbs", width=110, height=32, corner_radius=10,
                                      fg_color=RAISED, hover_color=LINE, text_color=MOON,
                                      font=self.f_body, command=self._discover)
        self.find_btn.pack(side="right")
        ctk.CTkButton(bar, text="Add", width=60, height=32, corner_radius=10, fg_color=RAISED,
                      hover_color=LINE, text_color=MOON, font=self.f_body,
                      command=self._add_ip).pack(side="right", padx=(6, 12))
        self.ip_entry = ctk.CTkEntry(bar, width=150, height=32, corner_radius=10, fg_color=RAISED,
                                     border_width=0, text_color=MOON, placeholder_text="Add by IP address",
                                     placeholder_text_color=MUTED, font=self.f_body)
        self.ip_entry.pack(side="right")
        self.discover_label = self._label(page, "Pick the part of the screen each bulb follows. "
                                                "Left half and Right half split the screen between two bulbs.",
                                          muted=True, anchor="w", justify="left", wraplength=640)
        self.discover_label.pack(fill="x", padx=18, pady=(4, 6))
        self.bulb_list = self._scroll(page)

    def _refresh_bulb_rows(self):
        for w in self.bulb_list.winfo_children():
            w.destroy()
        self.bulb_rows.clear()
        bulbs = self.settings["bulbs"]
        if not bulbs:
            self._label(self.bulb_list, "No bulbs found yet. Click Find bulbs, or add one by its IP address.",
                        muted=True).pack(pady=40)
        for i, (ip, b) in enumerate(sorted(bulbs.items())):
            b.setdefault("name", f"Bulb {i + 1}")
            b.setdefault("enabled", True)
            b.setdefault("zone", "Full screen")
            b.setdefault("brightness_scale", 100)

            row = ctk.CTkFrame(self.bulb_list, fg_color=NIGHT, corner_radius=12)
            row.pack(fill="x", padx=12, pady=5)
            row.grid_columnconfigure(3, weight=1)

            dot = ctk.CTkFrame(row, width=18, height=18, corner_radius=9, fg_color=hex_color(OFF_BULB))
            dot.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=14)
            name = ctk.CTkEntry(row, width=170, height=30, fg_color="transparent", border_width=0,
                                text_color=MOON, font=ctk.CTkFont(family=self.display_bold, size=15))
            name.insert(0, b["name"])
            name.bind("<KeyRelease>", lambda e, ip=ip, w=name: self.set_bulb(ip, "name", w.get()))
            name.grid(row=0, column=1, sticky="w", pady=(10, 0))
            self._label(row, ip, muted=True).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(0, 10))

            zone = self._menu(row, ZONES, lambda v, ip=ip: self.set_bulb(ip, "zone", v), width=150)
            zone.set(b["zone"])
            zone.grid(row=0, column=2, rowspan=2, padx=10)

            bri = ctk.CTkFrame(row, fg_color="transparent")
            bri.grid(row=0, column=3, rowspan=2, sticky="ew", padx=6)
            bri.grid_columnconfigure(0, weight=1)
            scale_lbl = ctk.CTkLabel(bri, text=f"{b['brightness_scale']}%", width=44, anchor="e",
                                     font=self.f_num, text_color=FILAMENT)
            sl = self._slider_widget(bri, 10, 100, 18, lambda v, ip=ip, l=scale_lbl: (
                l.configure(text=f"{int(v)}%"), self.set_bulb(ip, "brightness_scale", int(v))))
            sl.set(b["brightness_scale"])
            sl.grid(row=0, column=0, sticky="ew")
            scale_lbl.grid(row=0, column=1, padx=(6, 0))
            self._label(bri, "Brightness limit", muted=True).grid(row=1, column=0, sticky="w")

            en = ctk.BooleanVar(value=b["enabled"])
            ctk.CTkSwitch(row, text="", variable=en, width=46, switch_width=42, switch_height=22,
                          fg_color=LINE, progress_color=FILAMENT, button_color=MOON,
                          command=lambda ip=ip, v=en: self.set_bulb(ip, "enabled", bool(v.get()))
                          ).grid(row=0, column=4, rowspan=2, padx=(10, 0))
            acts = ctk.CTkFrame(row, fg_color="transparent")
            acts.grid(row=0, column=5, rowspan=2, padx=(0, 10))
            self._ghost_button(acts, "Identify", lambda ip=ip: self._identify(ip), width=70).pack()
            self._ghost_button(acts, "Remove", lambda ip=ip: self._remove_bulb(ip), color=ROSE,
                               width=70).pack()
            self.bulb_rows[ip] = {"swatch": dot}

    def _discover(self):
        self.find_btn.configure(state="disabled", text="Searching")
        self.discover_label.configure(text="Searching your network for WiZ bulbs.")

        def work():
            found = discover_bulbs()
            self.after(0, lambda: self._discover_done(found))

        threading.Thread(target=work, daemon=True).start()

    def _discover_done(self, found):
        self.find_btn.configure(state="normal", text="Find bulbs")
        with self.lock:
            known = self.settings["bulbs"]
            new = [ip for ip in found if ip not in known]
            for ip in sorted(new):
                known[ip] = {"name": f"Bulb {len(known) + 1}", "enabled": True,
                             "zone": "Full screen", "brightness_scale": 100}
        self._schedule_save()
        if found:
            self.discover_label.configure(text=f"Found {len(found)} bulbs, {len(new)} of them new.")
        else:
            self.discover_label.configure(
                text="No bulbs answered. Check that this PC and the bulbs are on the same Wi-Fi, "
                     "and that local communication is on in the WiZ app.")
        self._refresh_bulb_rows()
        self._draw_room(force=True)

    def _add_ip(self):
        ip = self.ip_entry.get().strip()
        try:
            socket.inet_aton(ip)
        except OSError:
            self.discover_label.configure(text=f"{ip or 'That'} is not an IP address. It should look like 192.168.1.50.")
            return
        with self.lock:
            self.settings["bulbs"].setdefault(ip, {"name": f"Bulb {len(self.settings['bulbs']) + 1}",
                                                    "enabled": True, "zone": "Full screen",
                                                    "brightness_scale": 100})
        self._schedule_save()
        self.ip_entry.delete(0, "end")
        self._refresh_bulb_rows()

    def _remove_bulb(self, ip):
        with self.lock:
            self.settings["bulbs"].pop(ip, None)
        self._schedule_save()
        self._refresh_bulb_rows()

    def _identify(self, ip):
        """Flash the bulb soft yellow at 50% once. Works while syncing too."""
        name = self.settings["bulbs"].get(ip, {}).get("name", ip)
        self.discover_label.configure(text=f"{name} flashed soft yellow.")
        engine = self.engine if self._is_syncing() else None
        flash_for = 0.7
        if engine:
            engine.hold_until[ip] = time.time() + flash_for + 0.1

        def work():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            state = None if engine else bulb_get_state(sock, ip)
            bulb_send(sock, ip, {"state": True, "r": 255, "g": 200, "b": 80, "c": 0, "w": 0,
                                 "dimming": 50})   # soft yellow, half brightness
            time.sleep(flash_for)
            if engine:
                engine.hold_until.pop(ip, None)    # the sync takes the bulb back
            else:
                params = restore_params(state)
                if params:
                    bulb_send(sock, ip, params)
            sock.close()

        threading.Thread(target=work, daemon=True).start()

    # ---- Color page
    def _build_color_page(self, page):
        sf = self._scroll(page)
        g = self._group(sf, "Color", "How the app turns your screen into one color for each bulb.")
        self._choice(g, "Color mode", "color_mode", COLOR_MODES,
                     "Vivid lets strong colors win over gray. Average uses the true "
                     "average of the screen. Dominant uses the most common strong color.")
        self._slider(g, "Color intensity", "saturation", 0.5, 3.0, 25, "{:.1f}x",
                     "1.0x shows the screen's real colors. Higher makes pale colors bolder.")
        g = self._group(sf, "Motion", "How quickly the bulbs react.")
        self._slider(g, "Smoothing", "smoothing", 0.0, 0.95, 19, "{:.2f}",
                     "0 reacts instantly. 0.9 fades slowly and calmly.")
        self._slider(g, "Updates per second", "fps", 1, 20, 19, "{:.0f}",
                     "WiZ bulbs keep up with 10 to 20.")
        self._slider(g, "Ignore small changes", "change_threshold", 0, 20, 20, "{:.0f}",
                     "Skips tiny shifts to reduce flicker and Wi-Fi traffic.")

    # ---- Brightness page
    def _build_brightness_page(self, page):
        sf = self._scroll(page)
        g = self._group(sf, "Brightness")
        self._choice(g, "Mode", "brightness_mode", BRIGHTNESS_MODES)
        self._slider(g, "Lowest", "min_brightness", 10, 100, 90, "{:.0f}%",
                     "How dim the bulbs get in dark scenes.")
        self._slider(g, "Highest", "max_brightness", 10, 100, 90, "{:.0f}%")
        self._slider(g, "Response", "brightness_gain", 0.5, 6.0, 22, "{:.1f}x",
                     "Higher reaches full brightness on dimmer scenes.")
        self._slider(g, "Fixed level", "fixed_brightness", 10, 100, 90, "{:.0f}%",
                     "Used when Brightness is set to Fixed.")
        g = self._group(sf, "Dark scenes", "What the bulbs do when the screen is almost black.")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._label(row, "When the screen is dark", width=180, anchor="w").pack(side="left")
        om = self._menu(row, DARK_SCENE, lambda v: self.set("dark_scene", v), width=180)
        om.set(self.settings["dark_scene"])
        om.pack(side="left", padx=14)
        self.slider_widgets["dark_scene"] = (om, None, None)
        self._slider(g, "Counts as dark below", "dark_threshold", 0.0, 0.2, 20, "{:.2f}")

    # ---- Screen page
    def _build_screen_page(self, page):
        sf = self._scroll(page)
        g = self._group(sf, "Monitor")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._label(row, "Capture", width=180, anchor="w").pack(side="left")
        names = self._monitor_names()
        self.monitor_menu = self._menu(row, names, self._pick_monitor, width=280)
        idx = self.settings["monitor"]
        self.monitor_menu.set(names[idx - 1] if 0 < idx <= len(names) else names[0])
        self.monitor_menu.pack(side="left", padx=14)
        self._ghost_button(row, "Refresh list", self._refresh_monitors, color=MOON, width=100).pack(side="left")

        g = self._group(sf, "Capture area", "Leave out parts of the screen, such as the taskbar.")
        self._switch(g, "Ignore black bars on movies", "ignore_black_bars")
        self._slider(g, "Ignore top", "crop_top", 0, 40, 40, "{:.0f}%")
        self._slider(g, "Ignore bottom", "crop_bottom", 0, 40, 40, "{:.0f}%")
        self._slider(g, "Ignore left", "crop_left", 0, 40, 40, "{:.0f}%")
        self._slider(g, "Ignore right", "crop_right", 0, 40, 40, "{:.0f}%")

        g = self._group(sf, "Performance")
        self._slider(g, "Pixel sampling", "sample_step", 2, 24, 22, "1 in {:.0f}",
                     "Reads every Nth pixel. Higher uses less CPU and looks the same.")

    def _monitor_names(self):
        try:
            import mss
            grabber = getattr(mss, "MSS", None) or mss.mss
            with grabber() as sct:
                mons = sct.monitors[1:]
            names = [f"Monitor {i}, {m['width']} x {m['height']}" + (", main" if m["left"] == 0 and m["top"] == 0 else "")
                     for i, m in enumerate(mons, start=1)]
            return names or ["Monitor 1"]
        except Exception:
            return ["Monitor 1"]

    def _pick_monitor(self, name):
        try:
            self.set("monitor", int(name.split(",")[0].split()[-1]))
        except ValueError:
            self.set("monitor", 1)

    def _refresh_monitors(self):
        names = self._monitor_names()
        self.monitor_menu.configure(values=names)
        idx = self.settings["monitor"]
        self.monitor_menu.set(names[idx - 1] if 0 < idx <= len(names) else names[0])

    # ---- App page
    def _build_app_page(self, page):
        sf = self._scroll(page)
        g = self._group(sf, "Startup")
        self._switch(g, "Start syncing when the app opens", "start_sync_on_launch")
        self._switch(g, "Open at Windows login, hidden and syncing", "launch_at_login",
                     command=self._toggle_login)
        self._switch(g, "Put the bulbs back the way they were when syncing stops", "restore_on_stop")

        g = self._group(sf, "Closing the window")
        sw = self._switch(g, "Keep running in the system tray when I close the window", "close_to_tray")
        if TRAY_OK:
            note = "Right-click the tray icon near the clock to open the app, start or stop syncing, or exit."
        else:
            note = "The tray needs two packages. Run  pip install pystray pillow  and restart the app."
            sw.configure(state="disabled")
        self._label(g, note, muted=True, wraplength=620, justify="left").pack(anchor="w")
        ctk.CTkButton(g, text="Exit app", width=110, height=32, corner_radius=10, fg_color=RAISED,
                      hover_color=LINE, text_color=ROSE, font=self.f_body,
                      command=self._quit).pack(anchor="w", pady=(10, 0))

        g = self._group(sf, "Settings")
        self._label(g, f"Saved automatically to {SETTINGS_FILE}", muted=True, wraplength=620,
                    justify="left").pack(anchor="w")
        ctk.CTkButton(g, text="Reset all settings", width=150, height=32, corner_radius=10,
                      fg_color=RAISED, hover_color=LINE, text_color=ROSE, font=self.f_body,
                      command=self._reset).pack(anchor="w", pady=(10, 16))

    def _toggle_login(self, enabled):
        try:
            set_launch_at_login(enabled)
        except OSError as exc:
            self.discover_label.configure(text=f"Could not change the login setting: {exc}")

    def _reset(self):
        with self.lock:
            bulbs = self.settings["bulbs"]
            self.settings = copy.deepcopy(DEFAULTS)
            self.settings["bulbs"] = {ip: {"name": b.get("name", ip), "enabled": True,
                                           "zone": "Full screen", "brightness_scale": 100}
                                      for ip, b in bulbs.items()}
        self._sync_widgets()
        self._refresh_bulb_rows()
        self._toggle_login(False)
        self._schedule_save()

    # ------------------------------------------------------------ presets
    def _apply_preset(self, name):
        if name not in PRESETS:
            return
        with self.lock:
            self.settings.update(PRESETS[name])
        self._sync_widgets()
        self._schedule_save()
        self.preset_menu.set(name)

    def _sync_widgets(self):
        for key, (w, extra, fmt) in self.slider_widgets.items():
            v = self.settings[key]
            if isinstance(w, ctk.CTkSlider):
                w.set(v)
                extra.configure(text=fmt.format(v))
            elif isinstance(w, ctk.CTkSwitch):
                extra.set(bool(v))
            elif isinstance(w, ctk.CTkSegmentedButton):
                w.set(v)
                self._recolor_segment(w)
            else:
                w.set(v)

    # ------------------------------------------------------------ run / stop
    def _is_syncing(self):
        return bool(self.engine and self.engine.is_alive())

    def _toggle(self):
        if self._is_syncing():
            self._stop()
        else:
            self._start()

    def _start(self):
        if self._is_syncing():
            return
        if not any(b.get("enabled", True) for b in self.settings["bulbs"].values()):
            self.discover_label.configure(text="Turn on at least one bulb first.")
            self._show_page("Bulbs")
            return
        self.engine = SyncEngine(self.get_settings)
        self.engine.start()
        self.start_btn.configure(text="Stop sync", fg_color=RAISED, hover_color=LINE, text_color=ROSE)
        self._update_tray()

    def _stop(self):
        if self.engine:
            self.engine.stop()
            self.engine.join(timeout=3)
        self.start_btn.configure(text="Start sync", fg_color=FILAMENT, hover_color=FILAMENT_HOVER,
                                 text_color=NIGHT)
        for r in self.bulb_rows.values():
            r["swatch"].configure(fg_color=hex_color(OFF_BULB))
        self._update_tray()
        self._draw_room(force=True)

    def _tick(self):
        while True:
            try:
                self.ui_queue.get_nowait()()
            except queue.Empty:
                break
        if self._quitting:
            return
        eng = self.engine
        if eng and eng.is_alive():
            for ip, color in list(eng.current.items()):
                if ip in self.bulb_rows:
                    c = hex_color(color) if color[3] else hex_color(OFF_BULB)
                    self.bulb_rows[ip]["swatch"].configure(fg_color=c)
            if time.time() - self._last_hero > 0.18 and self.winfo_viewable():
                self._last_hero = time.time()
                self._draw_room()
        elif eng and eng.error:
            self._stop()
        self.after(150, self._tick)

    # ------------------------------------------------------------ system tray
    def _setup_tray(self):
        if not TRAY_OK:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open WiZ Screen Sync", lambda: self.ui_queue.put(self._show), default=True),
            pystray.MenuItem(lambda item: "Stop sync" if self._is_syncing() else "Start sync",
                             lambda: self.ui_queue.put(self._toggle)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda: self.ui_queue.put(self._quit)),
        )
        try:
            self.tray = pystray.Icon("wiz_screen_sync", tray_image(False), APP_NAME, menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def _update_tray(self):
        if self.tray:
            try:
                syncing = self._is_syncing()
                self.tray.icon = tray_image(syncing)
                self.tray.title = f"{APP_NAME}, {'syncing' if syncing else 'not syncing'}"
                self.tray.update_menu()
            except Exception:
                pass

    def _show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close(self):
        if self.tray and self.settings.get("close_to_tray", True):
            self.withdraw()
            if not self._tray_notified:
                self._tray_notified = True
                try:
                    self.tray.notify("Still syncing in the system tray. Right-click the icon to exit.", APP_NAME)
                except Exception:
                    pass
            return
        self._quit()

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        self._stop()
        save_settings(self.get_settings())
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()


def tray_image(syncing):
    """A small bulb icon: colorful while syncing, gray when stopped."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if syncing:
        for i, col in enumerate([(255, 60, 60), (255, 170, 0), (60, 200, 90), (60, 120, 255)]):
            d.pieslice((6, 2, 58, 54), 90 * i - 135, 90 * i - 45, fill=col)
    else:
        d.ellipse((6, 2, 58, 54), fill=(120, 128, 140))
    d.rectangle((22, 50, 42, 62), fill=(200, 200, 205))
    return img


def main():
    import signal
    app = App(autostart="--autostart" in sys.argv)
    # Ctrl+C in the terminal: exit cleanly (stop syncing, restore bulbs) instead of breaking the app.
    signal.signal(signal.SIGINT, lambda *_: app.ui_queue.put(app._quit))
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app._quit()


if __name__ == "__main__":
    main()

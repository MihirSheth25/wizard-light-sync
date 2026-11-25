"""
Core of WiZard: the WiZ bulb protocol, settings storage, screen and music sync engines,
fades, and helpers. Has no user interface code, so the app window and tests can share it.

Unofficial. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.
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

APP_NAME = "WiZard"
SINGLE_INSTANCE_PORT = 38917   # only one WiZard runs at a time; a second launch opens the first
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
    "sidebar_open": True,
    "sync_source": "Screen",          # what Start sync follows: Screen or Music
    "music_mode": "Calm rhythm",
    "music_rhythm_low": 20,           # Calm rhythm: brightness just before each beat
    "music_rhythm_high": 100,         # Calm and Dynamic rhythm: brightness on the beat
    "music_beats_per_color": 8,       # how many beats one color lasts
    "music_on_length": 50,            # Dynamic rhythm: share of each beat the light stays on, in %
    "music_palette": "Rainbow",
    "music_sensitivity": 1.0,
    "music_min_brightness": 20,
    "music_max_brightness": 100,
    "music_fps": 12,
    "music_smoothing": 0.3,
    "music_split_colors": True,
    "music_profiles": {},             # music style -> its own copy of every MUSIC_KEYS setting
    "music_user_defaults": {},        # music style -> the settings saved with Set as default
    "control_brightness": 80,
    "control_kelvin": 3000,
    "control_color": [255, 140, 60],
    "scene_speed": 100,
    "scene_brightness": 80,
    "schedules": [],                  # see ScheduleDialog for the fields
    "rooms": [],                      # {"id", "name", "lights": [ip, ...]}
    "bulbs": {},  # ip -> {"name", "enabled", "zone", "brightness_scale"}
    "screen_presets": {},             # name -> every screen sync setting (SCREEN_KEYS)
    "screen_preset": "Default",       # the preset picked last
}

# The settings a screen sync preset holds (everything on that page except which monitor)
SCREEN_KEYS = ("color_mode", "saturation", "smoothing", "fps", "change_threshold", "brightness_mode",
               "min_brightness", "max_brightness", "brightness_gain", "fixed_brightness", "dark_scene",
               "dark_threshold", "ignore_black_bars", "crop_top", "crop_bottom", "crop_left", "crop_right",
               "sample_step")
# The music settings each style keeps for itself (everything on the Music sync page but the style)
MUSIC_KEYS = tuple(k for k in DEFAULTS if k.startswith("music_")
                   and k not in ("music_mode", "music_profiles", "music_user_defaults"))

# The presets the app starts with; each only lists what differs from the defaults
STARTER_PRESETS = {
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
    path = os.path.join(base, "WiZard")
    os.makedirs(path, exist_ok=True)
    old = os.path.join(base, "WiZScreenSync", "settings.json")   # carry settings over from the old name
    new = os.path.join(path, "settings.json")
    if os.path.exists(old) and not os.path.exists(new):
        try:
            import shutil
            shutil.copyfile(old, new)
        except OSError:
            pass
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
    base = {k: DEFAULTS[k] for k in SCREEN_KEYS}
    if not data["screen_presets"]:      # first run with presets: keep today's settings as Default
        data["screen_presets"] = {"Default": {k: data[k] for k in SCREEN_KEYS}}
        for name, diff in STARTER_PRESETS.items():
            data["screen_presets"][name] = {**base, **diff}
        data["screen_preset"] = "Default"
    for name, values in data["screen_presets"].items():
        data["screen_presets"][name] = {**base, **{k: v for k, v in values.items() if k in SCREEN_KEYS}}
    if data["screen_preset"] not in data["screen_presets"]:
        data["screen_preset"] = next(iter(data["screen_presets"]), "Default")
    # each music style keeps its own settings; styles seen for the first time start from today's values
    for mode in MUSIC_MODES:
        prof = data["music_profiles"].setdefault(mode, {})
        for k in MUSIC_KEYS:
            prof.setdefault(k, data[k])
    if data["music_mode"] not in MUSIC_MODES:
        data["music_mode"] = DEFAULTS["music_mode"]
    data.update(data["music_profiles"][data["music_mode"]])
    for i, sc in enumerate(data["schedules"]):    # every schedule needs an id to edit or delete it
        sc.setdefault("id", f"s{i}")
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


# ======================================================================= WiZ scenes and whites

# The built-in WiZ scenes (scene id -> name). Speed applies to the moving ones.
SCENES = {
    1: "Ocean", 2: "Romance", 3: "Sunset", 4: "Party", 5: "Fireplace", 6: "Cozy", 7: "Forest",
    8: "Pastel colors", 9: "Wake up", 10: "Bedtime", 11: "Warm white", 12: "Daylight",
    13: "Cool white", 14: "Night light", 15: "Focus", 16: "Relax", 17: "True colors",
    18: "TV time", 19: "Plant growth", 20: "Spring", 21: "Summer", 22: "Fall", 23: "Deep dive",
    24: "Jungle", 25: "Mojito", 26: "Club", 27: "Christmas", 28: "Halloween", 29: "Candlelight",
    30: "Golden white", 31: "Pulse", 32: "Steampunk",
}
SCENE_IDS = {name: sid for sid, name in SCENES.items()}
# Grouping used on the Scenes page
SCENE_GROUPS = [
    ("Moving color", [1, 2, 3, 4, 5, 6, 7, 8, 20, 21, 22, 23, 24, 25, 26, 27, 28, 31, 32]),
    ("Whites and daily", [11, 12, 13, 14, 15, 16, 17, 18, 19, 29, 30, 9, 10]),
]
# Rough two-tone look of each scene, used for the chips and the room picture only
SCENE_LOOK = {
    1: ((20, 90, 200), (40, 200, 210)), 2: ((230, 60, 110), (150, 40, 160)),
    3: ((255, 110, 40), (230, 50, 70)), 4: ((255, 40, 120), (60, 90, 255)),
    5: ((255, 90, 20), (255, 170, 40)), 6: ((255, 140, 60), (240, 90, 60)),
    7: ((40, 160, 60), (150, 200, 60)), 8: ((250, 170, 200), (160, 200, 255)),
    9: ((255, 150, 60), (255, 230, 190)), 10: ((255, 130, 50), (120, 60, 30)),
    11: ((255, 190, 120), (255, 200, 140)), 12: ((255, 245, 230), (240, 245, 255)),
    13: ((220, 235, 255), (200, 220, 255)), 14: ((255, 120, 40), (160, 70, 20)),
    15: ((240, 245, 255), (255, 250, 240)), 16: ((255, 180, 100), (255, 200, 140)),
    17: ((255, 235, 215), (255, 240, 225)), 18: ((120, 90, 200), (255, 150, 80)),
    19: ((230, 60, 200), (140, 40, 220)), 20: ((150, 220, 90), (255, 180, 200)),
    21: ((255, 200, 50), (60, 200, 200)), 22: ((220, 110, 30), (170, 60, 30)),
    23: ((20, 40, 140), (40, 120, 200)), 24: ((40, 150, 50), (200, 200, 40)),
    25: ((130, 230, 80), (230, 255, 150)), 26: ((200, 30, 255), (30, 200, 255)),
    27: ((220, 30, 30), (30, 170, 60)), 28: ((255, 110, 0), (130, 40, 170)),
    29: ((255, 140, 40), (255, 100, 20)), 30: ((255, 200, 110), (255, 215, 140)),
    31: ((255, 60, 60), (60, 60, 255)), 32: ((200, 120, 50), (90, 160, 160)),
}
STATIC_SCENES = {11, 12, 13, 14, 15, 16, 17, 18, 19, 29, 30}

MIN_KELVIN, MAX_KELVIN = 2200, 6500


def kelvin_to_rgb(k):
    """Approximate color of a white at k kelvin (Tanner Helland's fit)."""
    t = max(1000, min(40000, k)) / 100.0
    if t <= 66:
        r = 255
        g = 99.4708025861 * np.log(t) - 161.1195681661
        b = 0 if t <= 19 else 138.5177312231 * np.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255
    return tuple(int(max(0, min(255, v))) for v in (r, g, b))


def state_to_display(state):
    """Turn a getPilot result into (rgb or None, dimming, words) for the UI."""
    if not state:
        return None, 0, "Not answering"
    if not state.get("state", True):
        return None, 0, "Off"
    dim = int(state.get("dimming", 100))
    sid = state.get("sceneId", 0)
    if sid:
        look = SCENE_LOOK.get(sid, ((255, 200, 140), (255, 200, 140)))[0]
        return look, dim, SCENES.get(sid, f"Scene {sid}")
    if "temp" in state and state.get("temp"):
        return kelvin_to_rgb(state["temp"]), dim, f"White {state['temp']}K"
    if all(k in state for k in ("r", "g", "b")):
        return (state["r"], state["g"], state["b"]), dim, "Color"
    return (255, 200, 140), dim, "On"


def color_cmd(rgb, dim):
    r, g, b = (int(x) for x in rgb[:3])
    return {"state": True, "r": r, "g": g, "b": b, "c": 0, "w": 0, "dimming": dim}, ((r, g, b), dim, "Color")


def white_cmd(kelvin, dim):
    return {"state": True, "temp": kelvin, "dimming": dim}, (kelvin_to_rgb(kelvin), dim, f"White {kelvin}K")


def scene_cmd(sid, dim, speed):
    params = {"state": True, "sceneId": sid, "dimming": dim}
    if sid not in STATIC_SCENES:
        params["speed"] = speed
    return params, (SCENE_LOOK[sid][0], dim, SCENES[sid])


def on_cmd(dim):
    return {"state": True, "dimming": dim}, ((255, 200, 140), dim, "On")


OFF_CMD = ({"state": False}, (None, 0, "Off"))


# ======================================================================= engines

class BulbEngine(threading.Thread):
    """
    Base for anything that drives the bulbs continuously (screen sync, music sync).
    Remembers each bulb's state first and puts it back when stopped.
    Subclasses implement loop(sock).
    """
    kind = "sync"

    def __init__(self, get_settings):
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.stop_event = threading.Event()
        self.current = {}        # ip -> (r, g, b, dimming) last sent
        self.hold_until = {}     # ip -> time; leave that bulb alone until then (Identify)
        self.error = None
        self.actual_fps = 0.0
        self.saved_states = {}
        self._sock = None

    def stop(self):
        self.stop_event.set()

    def enabled_bulbs(self, s):
        return {ip: b for ip, b in s["bulbs"].items() if b.get("enabled", True)}

    def remember(self, ip):
        if ip not in self.saved_states:
            self.saved_states[ip] = bulb_get_state(self._sock, ip)

    def held(self, ip):
        return self.hold_until.get(ip, 0) > time.time()

    def send_color(self, ip, rgb, dim, last_sent, threshold):
        """Send a color if it changed enough since the last one. Returns True if sent."""
        key = (int(rgb[0]), int(rgb[1]), int(rgb[2]), int(dim))
        old = last_sent.get(ip)
        if old is not None and len(old) == 4 and isinstance(old[0], int):
            if max(abs(x - y) for x, y in zip(key, old)) < max(1, threshold):
                return False
        bulb_send(self._sock, ip, color_cmd(key[:3], key[3])[0])
        last_sent[ip] = key
        self.current[ip] = key
        return True

    def run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        try:
            self.loop(self._sock)
        except Exception as exc:  # shown in the UI
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            s = self.get_settings()
            if s.get("restore_on_stop", True):
                for ip, state in self.saved_states.items():
                    params = restore_params(state)
                    if params:
                        for _ in range(3):
                            bulb_send(self._sock, ip, params)
                            time.sleep(0.04)
            self._sock.close()

    def loop(self, sock):
        raise NotImplementedError


class ScreenEngine(BulbEngine):
    """Captures the screen and drives the bulbs. Reads settings live every frame."""
    kind = "Screen"

    def loop(self, sock):
        try:
            import mss
        except ImportError:
            self.error = "Screen capture needs the mss package. Run: pip install mss"
            return
        smooth, last_sent = {}, {}
        frames, fps_t0 = 0, time.perf_counter()
        grabber = getattr(mss, "MSS", None) or mss.mss
        with grabber() as sct:
            while not self.stop_event.is_set():
                t0 = time.perf_counter()
                s = self.get_settings()
                bulbs = self.enabled_bulbs(s)
                for ip in bulbs:
                    self.remember(ip)

                mon_idx = s["monitor"] if 0 < s["monitor"] < len(sct.monitors) else 1
                step = max(1, int(s["sample_step"]))
                shot = np.asarray(sct.grab(sct.monitors[mon_idx]))
                frame = shot[::step, ::step, 2::-1]
                frame = crop_margins(frame, s)
                if s["ignore_black_bars"]:
                    frame = strip_black_bars(frame)

                for ip, b in bulbs.items():
                    if self.held(ip):
                        last_sent.pop(ip, None)
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
                    dim = int(max(10, min(100, dim * b.get("brightness_scale", 100) / 100)))

                    is_dark = sbright < s["dark_threshold"] or max(sr, sg, sb) < 1
                    if is_dark and s["dark_scene"] == "Keep last color" and ip in last_sent:
                        continue
                    if is_dark and s["dark_scene"] == "Turn off":
                        if last_sent.get(ip) != ("off",):
                            bulb_send(sock, ip, {"state": False})
                            last_sent[ip] = ("off",)
                            self.current[ip] = (0, 0, 0, 0)
                    elif is_dark:
                        if last_sent.get(ip) != ("dark",):
                            bulb_send(sock, ip, {"state": True, "temp": 2700, "dimming": 10})
                            last_sent[ip] = ("dark",)
                            self.current[ip] = (255, 170, 90, 10)
                    else:
                        self.send_color(ip, (sr, sg, sb), dim, last_sent, s["change_threshold"])

                frames += 1
                now = time.perf_counter()
                if now - fps_t0 >= 1.0:
                    self.actual_fps = frames / (now - fps_t0)
                    frames, fps_t0 = 0, now
                fps = max(1.0, min(20.0, float(s["fps"])))
                self.stop_event.wait(max(0.0, 1.0 / fps - (time.perf_counter() - t0)))


# ---------------------------------------------------------------- music

class LoopbackAudio:
    """
    Records whatever the PC is playing (WASAPI loopback, via the soundcard package)
    on its own thread, so a silent PC never blocks the light loop.
    """

    def __init__(self, samplerate=44100, block=1024):
        self.samplerate, self.block = samplerate, block
        self.buffer = queue.Queue(maxsize=16)
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def read_all(self):
        """Every block of mono samples that arrived since the last call (empty during silence)."""
        out = []
        while True:
            try:
                out.append(self.buffer.get_nowait())
            except queue.Empty:
                return out

    def _run(self):
        try:
            import warnings
            import soundcard as sc
            warnings.filterwarnings("ignore", message=".*discontinuity.*")
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            with mic.recorder(samplerate=self.samplerate, blocksize=self.block) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=self.block)
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    if self.buffer.full():
                        try:
                            self.buffer.get_nowait()
                        except queue.Empty:
                            pass
                    self.buffer.put(mono.astype(np.float32))
        except ImportError:
            self.error = "Music sync needs the soundcard package. Run: pip install soundcard"
        except Exception as exc:
            self.error = f"Could not listen to your PC's audio: {exc}"


class MusicAnalyzer:
    """
    Turns audio blocks into loudness, bass/mid/treble and onsets (sudden rises in energy,
    usually drum hits). Onsets use spectral flux, which ignores steady loud sounds and
    reacts to new ones.
    """

    def __init__(self, samplerate=44100):
        self.sr = samplerate
        self.peak = 1e-3
        self.band_peaks = np.full(3, 1e-3)
        self.prev_spec = None
        self.flux_hist = []          # recent flux values for an adaptive threshold
        self.kick_hist = []
        self.prev_low = None
        self.last_onset = 0.0
        self.last_kick = 0.0
        self.slow_level = 0.0        # loudness over the last several seconds
        self.fast_level = 0.0        # loudness over the last fraction of a second
        self.heard = 0               # blocks of real sound so far, so the slow average can settle
        self.surging = False
        self.last_surge = 0.0

    def analyze(self, mono, sensitivity=1.0, now=None):
        now = time.time() if now is None else now
        if mono is None or len(mono) < 64:
            self.fast_level *= 0.9
            return {"level": 0.0, "bands": np.zeros(3), "beat": False, "onset": False, "strength": 0.0,
                    "surge": False, "kick": False}
        rms = float(np.sqrt(np.mean(mono ** 2)))
        self.peak = max(rms, self.peak * 0.997, 1e-3)
        level = 0.0 if rms < 2e-4 else min(1.0, rms / self.peak)
        self.fast_level += (level - self.fast_level) * 0.35
        self.slow_level += (level - self.slow_level) * 0.004
        if level > 0.05:
            self.heard += 1

        spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        freqs = np.fft.rfftfreq(len(mono), 1.0 / self.sr)
        edges = [(20, 250), (250, 2000), (2000, 8000)]
        bands = np.array([spec[(freqs >= lo) & (freqs < hi)].mean() if np.any((freqs >= lo) & (freqs < hi))
                          else 0.0 for lo, hi in edges])
        self.band_peaks = np.maximum(bands, self.band_peaks * 0.995)
        norm = np.clip(bands / np.maximum(self.band_peaks, 1e-6), 0, 1)
        if rms < 2e-4:
            norm[:] = 0

        # spectral flux, weighted toward the kick drum and snare range
        low = spec[(freqs >= 30) & (freqs < 300)]
        mid = spec[(freqs >= 300) & (freqs < 4000)]
        cur = np.concatenate([low * 2.0, mid * 0.5])
        cur = np.log1p(cur * 50)
        flux = 0.0 if self.prev_spec is None else float(np.maximum(cur - self.prev_spec, 0).sum())
        self.prev_spec = cur
        self.flux_hist.append(flux)
        if len(self.flux_hist) > 64:          # about 1.5 seconds of blocks
            self.flux_hist.pop(0)
        hist = np.array(self.flux_hist)
        mean, std = float(hist.mean()), float(hist.std())
        k = 1.6 / max(0.4, sensitivity)
        onset = (flux > mean + k * std and flux > 1e-3 and level > 0.08 and now - self.last_onset > 0.15)
        strength = 0.0
        if onset:
            self.last_onset = now
            strength = min(1.0, (flux - mean) / (3 * std + 1e-6))
        # a surge is the moment the music jumps clearly louder, like a drop or a chorus
        loud_now = self.heard > 250 and self.fast_level > max(0.3, self.slow_level * 1.8)
        # kick drum: flux in the lowest band alone, which hi-hats and vocals barely touch
        kick_spec = np.log1p(spec[(freqs >= 30) & (freqs < 160)] * 50)
        kflux = 0.0 if self.prev_low is None else float(np.maximum(kick_spec - self.prev_low, 0).sum())
        self.prev_low = kick_spec
        self.kick_hist.append(kflux)
        if len(self.kick_hist) > 64:
            self.kick_hist.pop(0)
        kh = np.array(self.kick_hist)
        kick = (kflux > kh.mean() + (1.4 / max(0.4, sensitivity)) * kh.std() and kflux > 1e-3
                and level > 0.08 and now - self.last_kick > 0.25)
        if kick:
            self.last_kick = now
        surge = loud_now and not self.surging and now - self.last_surge > 8.0
        self.surging = loud_now
        if surge:
            self.last_surge = now
        return {"level": level, "bands": norm, "beat": onset, "onset": onset, "strength": strength,
                "surge": surge, "kick": kick}


class RhythmTracker:
    """
    Follows the tempo of the music from its onsets, so the lights keep a steady pulse
    even when a beat is soft or missing. Beats come out evenly spaced, locked to the drums.
    """

    MIN_PERIOD, MAX_PERIOD = 0.3, 1.0        # 60 to 200 beats per minute

    def __init__(self):
        self.onsets = []                      # recent onset times
        self.period = 0.5                     # seconds per beat, starts at 120 BPM
        self.confidence = 0.0
        self.next_beat = None
        self.last_beat = 0.0
        self.count = 0                        # beats since the color last changed

    @property
    def bpm(self):
        return 60.0 / self.period

    def _estimate(self):
        """Pick the beat period that best explains the gaps between recent onsets."""
        if len(self.onsets) < 4:
            return
        times = np.array(self.onsets)
        gaps = []
        for i in range(len(times)):
            for j in range(i + 1, min(i + 5, len(times))):
                gaps.append(times[j] - times[i])
        gaps = np.array(gaps)
        best, best_score = None, 0.0
        for period in np.arange(self.MIN_PERIOD, self.MAX_PERIOD, 0.005):
            ratio = gaps / period
            near = np.abs(ratio - np.round(ratio))
            ok = (np.round(ratio) >= 1) & (np.round(ratio) <= 4)
            score = float(np.sum(np.exp(-(near / 0.06) ** 2) * ok / np.maximum(np.round(ratio), 1)))
            score *= 1.0 + 0.35 * np.exp(-((period - 0.55) / 0.15) ** 2)   # prefer 80 to 150 BPM
            if score > best_score:
                best, best_score = period, score
        if best:
            change = abs(best - self.period) / self.period
            self.period = best if change > 0.25 or self.confidence < 0.3 else self.period * 0.8 + best * 0.2
            self.confidence = min(1.0, best_score / (len(times) * 0.9))

    def update(self, now, kick, loud):
        """Feed one analysis step with whether a kick drum hit. Returns True when a beat lands now."""
        if kick:
            self.onsets.append(now)
            self.onsets = [t for t in self.onsets if now - t < 8.0]
            self._estimate()
        if not loud:
            self.next_beat = None
            return False
        if self.next_beat is None or len(self.onsets) < 4:
            # no tempo yet: every kick is a beat
            if kick and now - self.last_beat > self.MIN_PERIOD:
                self.last_beat = now
                self.next_beat = now + self.period
                return True
            return False
        since = now - self.last_beat
        if kick and since < self.period * 0.3:
            # the kick came a little after the beat we already showed: shift the grid, no extra beat
            self.last_beat = now
            self.next_beat = now + self.period
            return False
        if kick and abs(now - self.next_beat) < self.period * 0.3:
            self.next_beat = now          # the kick came a little early: take the beat now
        if now >= self.next_beat - 0.012:
            self.last_beat = now
            self.next_beat = max(self.next_beat, now) + self.period
            return True
        return False

    def phase(self, now):
        """0 right on a beat, rising toward 1 just before the next one."""
        if self.next_beat is None:
            return 1.0
        return max(0.0, min(1.0, (now - self.last_beat) / self.period))


MUSIC_MODES = ["Calm rhythm", "Dynamic rhythm", "Beat pulse", "Spectrum", "Mellow"]
RHYTHM_MODES = ("Calm rhythm", "Dynamic rhythm")
MUSIC_PALETTES = {"Rainbow": (0.0, 1.0), "Warm": (0.93, 0.17), "Cool": (0.45, 0.80), "Neon": (0.75, 1.05)}


def palette_hue(palette, t):
    """Map t in [0, 1) onto the hue range of a palette, sweeping back and forth."""
    lo, hi = MUSIC_PALETTES.get(palette, (0.0, 1.0))
    if palette == "Rainbow":
        return t % 1.0
    if hi < lo:  # range wraps past red
        hi += 1.0
    tri = 1 - abs((t % 1.0) * 2 - 1)   # 0 -> 1 -> 0
    return (lo + (hi - lo) * tri) % 1.0


class MusicEngine(BulbEngine):
    """Listens to the PC's audio and drives the bulbs."""
    kind = "Music"
    TICK = 1 / 40                     # rhythm timing runs at 40 steps a second

    def __init__(self, get_settings, audio_factory=None, clock=time.time):
        super().__init__(get_settings)
        self.audio_factory = audio_factory or (lambda: LoopbackAudio().start())
        self.clock = clock
        self.bpm = 0.0
        self.beats = 0
        self.beat_times = []

    def loop(self, sock):
        audio = self.audio_factory()
        analyzer, rhythm = MusicAnalyzer(), RhythmTracker()
        hue_t, flash = 0.0, 0.0
        smooth, last_sent, last_send_t = {}, {}, {}
        quiet_since = None
        frames, fps_t0 = 0, time.perf_counter()
        try:
            while not self.stop_event.is_set():
                t0 = time.perf_counter()
                if audio.error:
                    self.error = audio.error
                    return
                s = self.get_settings()
                bulbs = self.enabled_bulbs(s)
                for ip in bulbs:
                    self.remember(ip)
                mode, palette = s["music_mode"], s["music_palette"]
                now = self.clock()

                blocks = audio.read_all()
                info = None
                for block in blocks or [None]:      # every block counts, so no drum hit is skipped
                    step = analyzer.analyze(block, s["music_sensitivity"], now)
                    if info is not None:
                        for key in ("onset", "beat", "surge", "kick"):
                            step[key] = step[key] or info[key]
                    info = step

                if mode in RHYTHM_MODES:
                    loud = info["level"] > 0.06 or analyzer.fast_level > 0.06
                    quiet_since = None if loud else (quiet_since or now)
                    beat = rhythm.update(now, info["kick"], loud or (now - (quiet_since or now)) < 1.0)
                    self.bpm = rhythm.bpm if rhythm.confidence > 0.25 else 0.0
                    if beat:
                        self.beats += 1
                        self.beat_times.append(now)
                        del self.beat_times[:-64]
                        rhythm.count += 1
                        per = max(1, int(s["music_beats_per_color"]))
                        if rhythm.count >= per or (info["surge"] and rhythm.count >= 2):
                            rhythm.count = 0
                            hue_t += 0.618034          # golden-ratio steps give well spread colors
                    self._rhythm_frame(sock, s, bulbs, mode, palette, hue_t, rhythm, now, beat,
                                       quiet_since is not None and now - quiet_since > 1.0,
                                       last_sent, last_send_t)
                else:
                    self._classic_frame(s, bulbs, mode, palette, info, smooth, last_sent, flash, hue_t)
                    if mode == "Beat pulse":
                        if info["beat"]:
                            hue_t += 0.13
                            flash = 1.0
                        flash *= 0.80
                    elif mode == "Mellow":
                        hue_t += 0.002 + 0.01 * info["level"]

                frames += 1
                tnow = time.perf_counter()
                if tnow - fps_t0 >= 1.0:
                    self.actual_fps = frames / (tnow - fps_t0)
                    frames, fps_t0 = 0, tnow
                if mode in RHYTHM_MODES:
                    wait = self.TICK
                else:
                    wait = 1.0 / max(4.0, min(20.0, float(s["music_fps"])))
                self.stop_event.wait(max(0.0, wait - (time.perf_counter() - t0)))
        finally:
            audio.stop()

    def _color_for(self, palette, hue_t, i, split):
        offset = (0.5 if palette == "Rainbow" else 0.35) * i if split else 0.0
        r, g, b = colorsys.hsv_to_rgb(palette_hue(palette, hue_t + offset), 1.0, 1.0)
        return int(r * 255), int(g * 255), int(b * 255)

    def _rhythm_frame(self, sock, s, bulbs, mode, palette, hue_t, rhythm, now, beat, silent,
                      last_sent, last_send_t):
        """
        Calm rhythm: hold one color, swing brightness from high on each beat down to low
        before the next. Dynamic rhythm: hold one color, flash it on with each beat and
        switch the light off between beats. The color moves on after a set number of beats,
        or early when the music suddenly gets louder.
        """
        lo = int(s["music_rhythm_low"])
        hi = max(lo, int(s["music_rhythm_high"]))
        phase = rhythm.phase(now)
        for i, (ip, b) in enumerate(sorted(bulbs.items())):
            if self.held(ip):
                last_sent.pop(ip, None)
                continue
            rgb = self._color_for(palette, hue_t, i, s["music_split_colors"])
            scale = b.get("brightness_scale", 100) / 100
            if silent:
                level = lo
                on = True
            elif mode == "Calm rhythm":
                ease = 0.5 + 0.5 * np.cos(np.pi * min(1.0, phase / 0.85))   # 1 on the beat, 0 before the next
                level = lo + (hi - lo) * ease
                on = True
            else:  # Dynamic rhythm
                on = phase < max(0.15, min(0.9, s["music_on_length"] / 100)) and rhythm.next_beat is not None
                level = hi
            dim = int(max(10, min(100, level * scale)))
            if not on:
                if last_sent.get(ip) != ("off",):
                    bulb_send(sock, ip, {"state": False})
                    last_sent[ip] = ("off",)
                    self.current[ip] = (0, 0, 0, 0)
                continue
            # brightness ramps are capped at about 16 messages a second per light
            if not beat and now - last_send_t.get(ip, 0) < 0.06 and last_sent.get(ip) != ("off",):
                continue
            if beat or last_sent.get(ip) == ("off",):
                last_sent.pop(ip, None)         # the first frame of a beat always goes out
            if self.send_color(ip, rgb, dim, last_sent, 2):
                last_send_t[ip] = now

    def _classic_frame(self, s, bulbs, mode, palette, info, smooth, last_sent, flash, hue_t):
        if mode == "Beat pulse":
            energy = min(1.0, 0.55 * info["level"] + 0.6 * (1.0 if info["beat"] else flash * 0.8))
        else:
            energy = info["level"]
        lo, hi = s["music_min_brightness"], max(s["music_min_brightness"], s["music_max_brightness"])
        for i, (ip, b) in enumerate(sorted(bulbs.items())):
            if self.held(ip):
                last_sent.pop(ip, None)
                continue
            if mode == "Spectrum":
                bass, mid, treble = info["bands"]
                rgb = np.array([bass, mid * 0.8, treble]) + 1e-6
                if s["music_split_colors"] and i % 2:
                    rgb = rgb[::-1]
                h, sat, _ = colorsys.rgb_to_hsv(*rgb)
                r, g, bl = colorsys.hsv_to_rgb(h, min(1.0, sat * 1.6), 1.0)
                r, g, bl = r * 255, g * 255, bl * 255
            else:
                r, g, bl = self._color_for(palette, hue_t, i, s["music_split_colors"])
            a = max(0.0, min(0.9, float(s["music_smoothing"])))
            new = np.array([r, g, bl, energy], dtype=np.float32)
            prev = smooth.get(ip)
            val = new if prev is None else prev * a + new * (1 - a)
            if mode == "Beat pulse" and info["beat"]:
                val = new   # beats hit instantly
            smooth[ip] = val
            dim = lo + (hi - lo) * float(val[3])
            dim = int(max(10, min(100, dim * b.get("brightness_scale", 100) / 100)))
            self.send_color(ip, val[:3], dim, last_sent, 3)


# ---------------------------------------------------------------- fades (wake up, bedtime, sleep timer)

class FadeJob(threading.Thread):
    """
    Smoothly moves bulbs from one brightness/white to another over some minutes.
    Wake up: warm and dim -> bright daylight. Bedtime: current -> off.
    """

    def __init__(self, ips, minutes, start_dim, end_dim, start_k, end_k, turn_off_at_end=False,
                 on_done=None):
        super().__init__(daemon=True)
        self.ips, self.duration = list(ips), max(0.05, minutes) * 60
        self.start_dim, self.end_dim = start_dim, end_dim
        self.start_k, self.end_k = start_k, end_k
        self.turn_off_at_end = turn_off_at_end
        self.on_done = on_done
        self.cancel_event = threading.Event()
        self.progress = 0.0

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0 = time.time()
        step = min(2.0, max(0.25, self.duration / 120))
        try:
            while not self.cancel_event.is_set():
                p = min(1.0, (time.time() - t0) / self.duration)
                self.progress = p
                dim = int(round(self.start_dim + (self.end_dim - self.start_dim) * p))
                k = int(round(self.start_k + (self.end_k - self.start_k) * p))
                for ip in self.ips:
                    bulb_send(sock, ip, {"state": True, "temp": k, "dimming": max(10, min(100, dim))})
                if p >= 1.0:
                    break
                self.cancel_event.wait(step)
            if self.turn_off_at_end and not self.cancel_event.is_set():
                for ip in self.ips:
                    bulb_send(sock, ip, {"state": False})
        finally:
            sock.close()
            if self.on_done and not self.cancel_event.is_set():
                self.on_done()




# ======================================================================= launch at login (Windows)

def startup_shortcut_path(name="WiZard.vbs"):
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", name)


def set_launch_at_login(enabled):
    path = startup_shortcut_path()
    if not path:
        return False
    old = startup_shortcut_path("WiZ Screen Sync.vbs")   # from before the rename
    if old and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
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
        cmd = f'""{exe}"" ""{os.path.abspath(sys.argv[0])}"" --autostart'
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\n')
    return True


# ======================================================================= schedules

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ACTIONS = ["Turn on", "Turn off", "Set white", "Set color", "Play scene", "Wake-up fade",
           "Bedtime fade", "Start screen sync", "Start music sync", "Stop sync"]
QUICK_COLORS = [(255, 40, 40), (255, 120, 20), (255, 200, 40), (60, 220, 90), (40, 210, 200),
                (40, 110, 255), (130, 60, 255), (230, 50, 200)]
QUICK_WHITES = [2200, 2700, 4000, 6500]


def days_text(days):
    days = sorted(set(days))
    if len(days) == 7:
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if days == [5, 6]:
        return "Weekends"
    if not days:
        return "Never"
    return ", ".join(DAY_NAMES[d] for d in days)


def time_text(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def action_text(sc):
    a, v = sc["action"], sc.get("value", {})
    if a == "Turn on":
        return f"Turn on at {v.get('brightness', 80)}%"
    if a == "Set white":
        return f"White {v.get('kelvin', 3000)}K at {v.get('brightness', 80)}%"
    if a == "Set color":
        return f"Color at {v.get('brightness', 80)}%"
    if a == "Play scene":
        return f"{SCENES.get(v.get('scene', 1), 'Scene')} scene at {v.get('brightness', 80)}%"
    if a in ("Wake-up fade", "Bedtime fade"):
        return f"{a} over {v.get('minutes', 30)} min"
    return a



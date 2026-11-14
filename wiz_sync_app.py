"""
WiZard
A desktop app for Philips WiZ bulbs: screen sync, music sync, manual control,
WiZ scenes, schedules and a sleep timer. Talks to the bulbs directly over Wi-Fi.

Unofficial. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.

Run:   python wiz_sync_app.py
Needs: pip install customtkinter mss numpy pystray pillow soundcard

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
    "sync_source": "Screen",          # what Start sync follows: Screen or Music
    "music_mode": "Beat pulse",
    "music_palette": "Rainbow",
    "music_sensitivity": 1.0,
    "music_min_brightness": 20,
    "music_max_brightness": 100,
    "music_fps": 12,
    "music_smoothing": 0.3,
    "music_split_colors": True,
    "control_target": "All bulbs",
    "control_brightness": 80,
    "control_kelvin": 3000,
    "control_color": [255, 140, 60],
    "scene_speed": 100,
    "scene_brightness": 80,
    "schedules": [],                  # see ScheduleDialog for the fields
    "rooms": [],                      # {"id", "name", "lights": [ip, ...]}
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
        self.preview = None      # screen thumbnail (screen) or spectrum bars (music)
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
        bulb_send(self._sock, ip, {"state": True, "r": key[0], "g": key[1], "b": key[2],
                                   "c": 0, "w": 0, "dimming": key[3]})
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
                k = max(1, shot.shape[1] // 240)
                self.preview = shot[::k, ::k, 2::-1]
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
        self.buffer = queue.Queue(maxsize=8)
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def read(self):
        """Latest block of mono samples, or None if nothing arrived (silence)."""
        data = None
        while True:
            try:
                data = self.buffer.get_nowait()
            except queue.Empty:
                return data

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
    """Turns audio blocks into loudness, bass/mid/treble, beats and spectrum bars."""

    BARS = 24

    def __init__(self, samplerate=44100):
        self.sr = samplerate
        self.peak = 1e-3
        self.band_peaks = np.full(3, 1e-3)
        self.bass_avg = 0.0
        self.last_beat = 0.0
        self.bar_peaks = np.full(self.BARS, 1e-3)

    def analyze(self, mono, sensitivity=1.0, now=None):
        now = time.time() if now is None else now
        if mono is None or len(mono) < 64:
            # silence: let everything decay
            self.bass_avg *= 0.95
            return {"level": 0.0, "bands": np.zeros(3), "beat": False, "bars": np.zeros(self.BARS)}
        rms = float(np.sqrt(np.mean(mono ** 2)))
        self.peak = max(rms, self.peak * 0.997, 1e-3)
        level = 0.0 if rms < 2e-4 else min(1.0, rms / self.peak)

        spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        freqs = np.fft.rfftfreq(len(mono), 1.0 / self.sr)
        edges = [(20, 250), (250, 2000), (2000, 8000)]
        bands = np.array([spec[(freqs >= lo) & (freqs < hi)].mean() if np.any((freqs >= lo) & (freqs < hi))
                          else 0.0 for lo, hi in edges])
        self.band_peaks = np.maximum(bands, self.band_peaks * 0.995)
        norm = np.clip(bands / np.maximum(self.band_peaks, 1e-6), 0, 1)
        if rms < 2e-4:
            norm[:] = 0

        # beat: bass jumps clearly above its recent average
        bass = bands[0]
        threshold = 1.5 / max(0.3, sensitivity)
        beat = (bass > self.bass_avg * threshold and level > 0.15 and now - self.last_beat > 0.18)
        if beat:
            self.last_beat = now
        self.bass_avg = self.bass_avg * 0.9 + bass * 0.1 if self.bass_avg else bass

        # spectrum bars on a log scale for the room picture
        bar_edges = np.geomspace(40, 12000, self.BARS + 1)
        bars = np.array([spec[(freqs >= bar_edges[i]) & (freqs < bar_edges[i + 1])].mean()
                         if np.any((freqs >= bar_edges[i]) & (freqs < bar_edges[i + 1])) else 0.0
                         for i in range(self.BARS)])
        self.bar_peaks = np.maximum(bars, self.bar_peaks * 0.99)
        bars = np.clip(bars / np.maximum(self.bar_peaks, 1e-6), 0, 1) * (1.0 if rms >= 2e-4 else 0)
        return {"level": level, "bands": norm, "beat": beat, "bars": bars}


MUSIC_MODES = ["Beat pulse", "Spectrum", "Mellow"]
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

    def __init__(self, get_settings, audio_factory=None):
        super().__init__(get_settings)
        self.audio_factory = audio_factory or (lambda: LoopbackAudio().start())

    def loop(self, sock):
        audio = self.audio_factory()
        analyzer = MusicAnalyzer()
        hue_t, flash = 0.0, 0.0
        smooth, last_sent = {}, {}
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
                info = analyzer.analyze(audio.read(), s["music_sensitivity"])
                self.preview = info["bars"]
                mode, palette = s["music_mode"], s["music_palette"]

                if mode == "Beat pulse":
                    if info["beat"]:
                        hue_t += 0.13
                        flash = 1.0
                    flash *= 0.80
                    energy = min(1.0, 0.55 * info["level"] + 0.6 * flash)
                elif mode == "Spectrum":
                    energy = info["level"]
                else:  # Mellow
                    hue_t += 0.002 + 0.01 * info["level"]
                    energy = info["level"]

                lo, hi = s["music_min_brightness"], max(s["music_min_brightness"], s["music_max_brightness"])
                for i, (ip, b) in enumerate(sorted(bulbs.items())):
                    if self.held(ip):
                        last_sent.pop(ip, None)
                        continue
                    offset = (0.5 if palette == "Rainbow" else 0.35) * i if s["music_split_colors"] else 0.0
                    if mode == "Spectrum":
                        bass, mid, treble = info["bands"]
                        rgb = np.array([bass, mid * 0.8, treble]) + 1e-6
                        if s["music_split_colors"] and i % 2:
                            rgb = rgb[::-1]
                        h, sat, _ = colorsys.rgb_to_hsv(*rgb)
                        r, g, bl = colorsys.hsv_to_rgb(h, min(1.0, sat * 1.6), 1.0)
                    else:
                        r, g, bl = colorsys.hsv_to_rgb(palette_hue(palette, hue_t + offset), 1.0, 1.0)
                    a = max(0.0, min(0.9, float(s["music_smoothing"])))
                    new = np.array([r * 255, g * 255, bl * 255, energy], dtype=np.float32)
                    prev = smooth.get(ip)
                    val = new if prev is None else prev * a + new * (1 - a)
                    if mode == "Beat pulse" and info["beat"]:
                        val = new   # beats hit instantly
                    smooth[ip] = val
                    dim = lo + (hi - lo) * float(val[3])
                    dim = int(max(10, min(100, dim * b.get("brightness_scale", 100) / 100)))
                    self.send_color(ip, val[:3], dim, last_sent, 3)

                frames += 1
                now = time.perf_counter()
                if now - fps_t0 >= 1.0:
                    self.actual_fps = frames / (now - fps_t0)
                    frames, fps_t0 = 0, now
                fps = max(4.0, min(20.0, float(s["music_fps"])))
                self.stop_event.wait(max(0.0, 1.0 / fps - (time.perf_counter() - t0)))
        finally:
            audio.stop()


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


SyncEngine = ScreenEngine


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
ROW_HOVER = "#231D32"   # a row under the mouse: lifted, but darker than its buttons


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
    if thumb is not None and np.ndim(thumb) == 3:
        t = PILImage.fromarray(np.ascontiguousarray(thumb)).resize((mw, mh), PILImage.BILINEAR)
        img.paste(t, (mx0, my0))
    else:
        d.rectangle((mx0, my0, mx0 + mw, my0 + mh), fill=(24, 20, 34))
        if thumb is not None:  # music: spectrum bars in the bulbs' colors
            n = len(thumb)
            lit = [b["color"] for b in bulbs if b["color"]] or [_rgb(FILAMENT)]
            gap = max(1, int(2 * scale))
            bw = (mw - gap * (n + 1)) / n
            for i, v in enumerate(thumb):
                col = lit[0] if len(lit) == 1 else tuple(
                    int(lit[0][c] + (lit[-1][c] - lit[0][c]) * i / max(1, n - 1)) for c in range(3))
                bh = max(int(2 * scale), int((mh - 12 * scale) * float(v)))
                x0 = mx0 + gap + i * (bw + gap)
                d.rectangle((x0, my0 + mh - 6 * scale - bh, x0 + bw, my0 + mh - 6 * scale), fill=col)
    # stand
    sx = w // 2
    d.rectangle((sx - int(7 * scale), my0 + mh + bez, sx + int(7 * scale), my0 + mh + int(24 * scale)),
                fill=(52, 46, 70))
    d.rounded_rectangle((sx - int(46 * scale), my0 + mh + int(22 * scale),
                         sx + int(46 * scale), my0 + mh + int(28 * scale)),
                        radius=int(3 * scale), fill=(52, 46, 70))

    # outline of the screen area each bulb follows, in that bulb's color
    for i, b in enumerate(bulbs):
        if not b["color"] or thumb is None or np.ndim(thumb) != 3 or not b.get("sync", True):
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


def wheel_image(size):
    """Hue around the circle, white in the middle, as an RGBA PIL image."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2
    dx, dy = x - c, c - y
    r = np.sqrt(dx ** 2 + dy ** 2) / c
    hue = (np.arctan2(dy, dx) / (2 * np.pi)) % 1.0
    sat = np.clip(r, 0, 1)
    i = np.floor(hue * 6).astype(int) % 6
    f = hue * 6 - np.floor(hue * 6)
    p, q, t = 1 - sat, 1 - sat * f, 1 - sat * (1 - f)
    v = np.ones_like(sat)
    rgb = np.zeros((size, size, 3), np.float32)
    for k, (a, b, cc) in enumerate([(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]):
        m = i == k
        rgb[m, 0], rgb[m, 1], rgb[m, 2] = a[m], b[m], cc[m]
    alpha = np.clip((1.0 - r) * c * 1.5 + 0.5, 0, 1)   # soft anti-aliased edge
    out = np.dstack([rgb * 255, alpha * 255]).astype(np.uint8)
    return PILImage.fromarray(out, "RGBA")


class ColorWheel(tk.Canvas):
    """Click or drag to pick a color. Calls command((r, g, b)) while dragging."""

    def __init__(self, master, size=220, command=None, bg=SURFACE):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.size, self.command = size, command
        self._img = ImageTk.PhotoImage(wheel_image(size)) if PIL_OK else None
        if self._img:
            self.create_image(0, 0, image=self._img, anchor="nw")
        self.marker = self.create_oval(0, 0, 0, 0, outline="#ffffff", width=3)
        self.bind("<Button-1>", self._pick)
        self.bind("<B1-Motion>", self._pick)

    def _pick(self, e):
        c = (self.size - 1) / 2
        dx, dy = e.x - c, c - e.y
        r = min(1.0, (dx * dx + dy * dy) ** 0.5 / c)
        hue = (np.arctan2(dy, dx) / (2 * np.pi)) % 1.0
        rgb = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(hue, r, 1.0))
        self._place(hue, r)
        if self.command:
            self.command(rgb)

    def _place(self, hue, sat):
        c = (self.size - 1) / 2
        x = c + np.cos(hue * 2 * np.pi) * sat * c
        y = c - np.sin(hue * 2 * np.pi) * sat * c
        self.coords(self.marker, x - 9, y - 9, x + 9, y + 9)

    def set_color(self, rgb):
        h, s, _ = colorsys.rgb_to_hsv(*(v / 255 for v in rgb))
        self._place(h, s)


def kelvin_strip(width, height):
    img = PILImage.new("RGB", (width, height))
    d = PILDraw.Draw(img)
    for x in range(width):
        k = MIN_KELVIN + (MAX_KELVIN - MIN_KELVIN) * x / max(1, width - 1)
        d.line((x, 0, x, height), fill=kelvin_to_rgb(k))
    return img


def scene_chip(sid, size=26):
    a, b = SCENE_LOOK.get(sid, ((255, 200, 140), (255, 200, 140)))
    s4 = size * 4
    img = PILImage.new("RGBA", (s4, s4), (0, 0, 0, 0))
    d = PILDraw.Draw(img)
    d.pieslice((0, 0, s4 - 1, s4 - 1), 90, 270, fill=a)
    d.pieslice((0, 0, s4 - 1, s4 - 1), 270, 450, fill=b)
    return img.resize((size, size), PILImage.LANCZOS)


class SwapArea(ctk.CTkFrame):
    """
    Holds stacked layers. New content is built in a layer underneath the visible one,
    drawn while hidden, and then raised in one step, so the old and new screens never mix.
    """

    def __init__(self, master, bg):
        super().__init__(master, fg_color=bg, corner_radius=0)
        self.bg = bg
        self.current = None

    def new_layer(self):
        layer = ctk.CTkFrame(self, fg_color=self.bg, corner_radius=0)
        layer.place(relx=0, rely=0, relwidth=1, relheight=1)
        if self.current is not None:
            layer.lower(self.current)
        return layer

    def commit(self, layer):
        old, self.current = self.current, layer
        self.update_idletasks()          # let the hidden layer finish drawing
        layer.lift()
        if old is not None:
            self.after(60, old.destroy)


class App(ctk.CTk):
    NAV = [("Lights", "Rooms"), ("Screen sync", "Music sync"), ("App",)]

    def __init__(self, autostart=False):
        super().__init__(fg_color=NIGHT)
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self._set_window_icon()
        self.geometry("1120x820")
        self.minsize(1000, 720)

        self._pick_fonts()
        self.settings = load_settings()
        self.lock = threading.Lock()
        self.engine = None
        self.fade_job = None
        self.fade_label = ""
        self.sleep_timers = {}       # target key -> time when its lights go off
        self.detail_key = None       # the light or room whose page is open
        self.detail_tab = "Control"
        self.light_dots = {}
        self._scene_imgs = {}
        self._scene_btns = {}
        self._current_scene = None
        self.display = {}            # ip -> (rgb or None, dim, words) when nothing is syncing
        self.status_text = ""
        self.bulb_rows = {}
        self.slider_widgets = {}
        self._save_job = None
        self.ui_queue = queue.Queue()
        self.tray = None
        self._tray_notified = False
        self._quitting = False
        self._hero_img = None
        self._last_hero = 0.0
        self._last_sched_check = 0.0
        self._fired = set()
        self._pending_cmd = None
        self._cmd_job = None
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(0.5)

        self._build_header()
        self._build_room()
        self._build_body()
        self._show_page("Lights")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._tick)
        self.after(300, self._poll_states)

        if not self.settings["bulbs"]:
            self.after(400, self._discover)
        if autostart or self.settings["start_sync_on_launch"]:
            self.after(1500, self._start)
        self._setup_tray()
        if autostart:
            self.after(100, self.withdraw if self.tray else self.iconify)

    def _dialog_icon(self, win):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "wiz.ico")
        if os.path.exists(path) and sys.platform == "win32":
            win.after(260, lambda: win.iconbitmap(path))   # after customtkinter sets its own

    def _center_dialog(self, win):
        """Size the dialog to its content and center it over the app."""
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + max(20, (self.winfo_height() - h) // 3)
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _set_window_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "wiz.ico")
        if os.path.exists(path) and sys.platform == "win32":
            try:
                self.iconbitmap(path)
                self.after(250, lambda: self.iconbitmap(path))
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
        self.f_big = ctk.CTkFont(family=display_bold, size=24, weight=bold_w)
        self.f_num = ctk.CTkFont(family=display, size=15)
        self.f_nav = ctk.CTkFont(family=display, size=16)
        self.f_body = ctk.CTkFont(family=body, size=13)
        self.f_small = ctk.CTkFont(family=body, size=12)
        self.f_button = ctk.CTkFont(family=display_bold, size=15, weight=bold_w)
        self.f_row = ctk.CTkFont(family=display_bold, size=16, weight=bold_w)
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

    def _button(self, parent, text, command, width=110, primary=False, color=None):
        if primary:
            return ctk.CTkButton(parent, text=text, command=command, width=width, height=34,
                                 corner_radius=10, fg_color=FILAMENT, hover_color=FILAMENT_HOVER,
                                 text_color=NIGHT, font=self.f_button)
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=34, corner_radius=10,
                             fg_color=RAISED, hover_color=LINE, text_color=color or MOON, font=self.f_body)

    def _ghost_button(self, parent, text, command, color=MUTED, width=80):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=30,
                             fg_color="transparent", hover_color=RAISED, text_color=color,
                             font=self.f_small, corner_radius=8)

    def _slider_widget(self, parent, lo, hi, steps, command, width=None):
        kw = {"width": width} if width else {}
        return ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, command=command,
                             fg_color=LINE, progress_color=FILAMENT, button_color=FILAMENT,
                             button_hover_color=FILAMENT_HOVER, height=16, **kw)

    def _segmented(self, parent, values, command, current):
        seg = ctk.CTkSegmentedButton(parent, values=values, fg_color=RAISED, selected_color=FILAMENT,
                                     selected_hover_color=FILAMENT_HOVER, unselected_color=RAISED,
                                     unselected_hover_color=LINE, text_color=MOON, font=self.f_body,
                                     corner_radius=10, height=32)
        seg.set(current)
        seg.configure(command=lambda v, s=seg: (command(v), self._recolor_segment(s)))
        self._recolor_segment(seg)
        return seg

    def _recolor_segment(self, seg):
        cur = seg.get()
        for value, btn in getattr(seg, "_buttons_dict", {}).items():
            btn.configure(text_color=NIGHT if value == cur else MOON)

    # ------------------------------------------------------------ header
    def _build_header(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=28, pady=(20, 0))
        ctk.CTkLabel(top, text=APP_NAME, font=self.f_title, text_color=MOON).pack(side="left")

        self.start_btn = ctk.CTkButton(top, text="Start sync", width=150, height=42, corner_radius=21,
                                       font=self.f_button, fg_color=FILAMENT, hover_color=FILAMENT_HOVER,
                                       text_color=NIGHT, command=self._toggle)
        self.start_btn.pack(side="right")
        self.source_seg = self._segmented(top, ["Screen", "Music"], self._pick_source,
                                          self.settings["sync_source"])
        self.source_seg.pack(side="right", padx=14)
        self._label(top, "Sync to", muted=True).pack(side="right")

    def _pick_source(self, value):
        self.set("sync_source", value)
        if self.source_seg.get() != value:
            self.source_seg.set(value)
        self._recolor_segment(self.source_seg)
        if self._is_syncing():           # switch over right away
            self._stop(restore=False)
            self._start()

    # ------------------------------------------------------------ the room scene
    def _build_room(self):
        self.room_h = 250
        self.room = tk.Canvas(self, height=self.room_h, bg=NIGHT, highlightthickness=0, bd=0)
        self.room.pack(fill="x", pady=(10, 0))
        self.room.bind("<Configure>", lambda e: self._draw_room())

    def _bulb_look(self, ip):
        """(rgb or None, dimming, words) for a bulb right now."""
        if self._is_syncing() and self.settings["bulbs"].get(ip, {}).get("enabled", True):
            col = self.engine.current.get(ip)
            if col and col[3]:
                return tuple(col[:3]), col[3], None
            return None, 0, None
        return self.display.get(ip, (None, 0, ""))

    def _room_bulbs(self, w, h, scale):
        bulbs = sorted(self.settings["bulbs"].items())
        sides = {"left": [], "right": []}
        for i, (ip, b) in enumerate(bulbs):
            sides[bulb_side(b.get("zone", "Full screen"), i)].append((ip, b))
        out = []
        mon_half = 125 * scale
        for side, items in sides.items():
            for j, (ip, b) in enumerate(items):
                gap = 150 * scale + j * 150 * scale
                x = w / 2 - mon_half - gap if side == "left" else w / 2 + mon_half + gap
                color, dim, words = self._bulb_look(ip)
                out.append({"ip": ip, "name": b.get("name", ip), "zone": b.get("zone", "Full screen"),
                            "x": int(x), "y": int(h * 0.36), "color": color, "dim": dim, "words": words,
                            "sync": b.get("enabled", True)})
        return out

    def _draw_room(self):
        c = self.room
        w = max(200, c.winfo_width())
        h = max(150, c.winfo_height())
        scale = h / self.room_h
        bulbs = self._room_bulbs(w, h, scale)
        c.delete("all")
        thumb = self.engine.preview if self._is_syncing() else None
        if PIL_OK:
            img, (mx0, my0, mw, mh) = render_room(w, h, bulbs, thumb, scale)
            self._hero_img = ImageTk.PhotoImage(img)
            c.create_image(0, 0, image=self._hero_img, anchor="nw")
        else:
            mw, mh = int(250 * scale), int(141 * scale)
            mx0, my0 = (w - mw) // 2, int(h * 0.40 - mh / 2)
            c.create_rectangle(mx0, my0, mx0 + mw, my0 + mh, fill=SURFACE, outline=LINE)

        name_font = (self.display_bold, 13)
        info_font = (self.f_small.cget("family"), 10)
        syncing = self._is_syncing()
        for b in bulbs:
            c.create_text(b["x"], b["y"] + 44 * scale, text=b["name"], fill=MOON, font=name_font)
            if syncing and b["sync"]:
                detail = b["zone"] if self.engine.kind == "Screen" else "Music"
                if b["color"]:
                    detail += f", {b['dim']}%"
            else:
                detail = b["words"] or ""
                if b["color"] and b["words"] not in ("Off", ""):
                    detail = f"{b['words']}, {b['dim']}%"
            c.create_text(b["x"], b["y"] + 64 * scale, text=detail, fill="#C9C0DC", font=info_font)

        if syncing:
            caption = f"{self.engine.kind} sync at {self.engine.actual_fps:.0f} updates a second"
        elif self.engine and self.engine.error:
            caption = self.engine.error
        elif self.fade_job and self.fade_job.is_alive():
            caption = f"{self.fade_label}, {int(self.fade_job.progress * 100)}% done"
        elif not self.settings["bulbs"]:
            caption = "No bulbs yet. Find them on the Bulbs page."
        else:
            caption = self.status_text or "Not syncing"
        if self.sleep_timers:
            key, dl = min(self.sleep_timers.items(), key=lambda kv: kv[1])
            mins = max(0, int((dl - time.time()) // 60) + 1)
            caption += f". {self._key_name(key)} off in {mins} min"
        c.create_text(w / 2, my0 + mh + 46 * scale, text=caption, fill=MUTED, font=info_font,
                      width=w - 80)
        if thumb is None:
            msg = "Your screen or music shows here while syncing"
            c.create_text(w / 2, my0 + mh / 2, text=msg, fill=MUTED, font=info_font, width=mw - 30,
                          justify="center")

    # ------------------------------------------------------------ body: sidebar + pages
    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(6, 20))

        nav = ctk.CTkFrame(body, fg_color="transparent", width=180)
        nav.pack(side="left", fill="y", padx=(0, 14))
        self.nav_buttons = {}
        for gi, group in enumerate(self.NAV):
            if gi:
                ctk.CTkFrame(nav, height=1, fg_color=LINE).pack(fill="x", padx=(14, 10), pady=8)
            for name in group:
                row = ctk.CTkFrame(nav, fg_color="transparent")
                row.pack(fill="x", pady=1)
                bar = ctk.CTkFrame(row, width=3, height=32, corner_radius=2, fg_color="transparent")
                bar.pack(side="left", padx=(0, 8))
                btn = ctk.CTkButton(row, text=name, anchor="w", width=160, height=38, corner_radius=10,
                                    fg_color="transparent", hover_color=SURFACE, text_color=MUTED,
                                    font=self.f_nav, command=lambda n=name: self._show_page(n))
                btn.pack(side="left", fill="x")
                self.nav_buttons[name] = (btn, bar)

        self.page_host = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=16)
        self.page_host.pack(side="left", fill="both", expand=True)
        stack = ctk.CTkFrame(self.page_host, fg_color=SURFACE, corner_radius=0)
        stack.pack(fill="both", expand=True, padx=8, pady=8)
        self.pages = {}
        builders = {"Lights": self._build_lights_page, "Rooms": self._build_rooms_page,
                    "Screen sync": self._build_screen_page, "Music sync": self._build_music_page,
                    "App": self._build_app_page}
        for group in self.NAV:
            for name in group:
                frame = ctk.CTkFrame(stack, fg_color=SURFACE, corner_radius=0)
                frame.place(relx=0, rely=0, relwidth=1, relheight=1)
                builders[name](frame)
                self.pages[name] = frame
        self.pages["Detail"] = ctk.CTkFrame(stack, fg_color=SURFACE, corner_radius=0)
        self.pages["Detail"].place(relx=0, rely=0, relwidth=1, relheight=1)
        self.detail_area = SwapArea(self.pages["Detail"], SURFACE)
        self.detail_area.pack(fill="both", expand=True)
        self._dirty = {"Lights": True, "Rooms": True}

    def _show_page(self, name, nav=None):
        if self._dirty.get(name):            # rebuild underneath first, then show
            (self._refresh_light_rows if name == "Lights" else self._refresh_room_rows)()
        self.update_idletasks()
        self.pages[name].lift()
        self.current_page = name
        lit = nav or name
        for n, (btn, bar) in self.nav_buttons.items():
            on = n == lit
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

    def _slider(self, parent, label, key, lo, hi, steps, fmt="{:.0f}", note=None, on_change=None):
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
            if on_change:
                on_change(v)

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
        seg = self._segmented(row, values, lambda v, k=key: self.set(k, v), self.settings[key])
        seg.grid(row=0, column=1, sticky="w", padx=14)
        if note:
            self._label(row, note, muted=True, anchor="w", justify="left", wraplength=480).grid(
                row=1, column=1, sticky="w", padx=14, pady=(4, 0))
        self.slider_widgets[key] = (seg, None, None)
        return seg

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

    # ------------------------------------------------------------ targets: all lights, one light, a room
    # A target key is "all", "light:<ip>" or "room:<id>". Older schedules may hold a name instead.
    def _rooms(self):
        return self.settings.setdefault("rooms", [])

    def _room(self, rid):
        return next((r for r in self._rooms() if r["id"] == rid), None)

    def _room_of(self, ip):
        return next((r for r in self._rooms() if ip in r["lights"]), None)

    def _light_name(self, ip):
        return self.settings["bulbs"].get(ip, {}).get("name", ip)

    def _norm_key(self, key):
        if key in ("all", "All bulbs", "All lights") or not key:
            return "all"
        if key.startswith(("light:", "room:")):
            return key
        for ip, b in self.settings["bulbs"].items():   # an older schedule that stored a bulb name
            if b.get("name") == key:
                return f"light:{ip}"
        return "all"

    def _ips_for_key(self, key):
        key = self._norm_key(key)
        if key == "all":
            return sorted(self.settings["bulbs"])
        if key.startswith("light:"):
            ip = key[6:]
            return [ip] if ip in self.settings["bulbs"] else []
        room = self._room(key[5:])
        return [ip for ip in (room["lights"] if room else []) if ip in self.settings["bulbs"]]

    def _key_name(self, key):
        key = self._norm_key(key)
        if key == "all":
            return "All lights"
        if key.startswith("light:"):
            return self._light_name(key[6:])
        room = self._room(key[5:])
        return room["name"] if room else "Deleted room"

    def _target_choices(self):
        out = [("All lights", "all")]
        out += [(r["name"], f"room:{r['id']}") for r in self._rooms()]
        out += [(self._light_name(ip), f"light:{ip}") for ip in sorted(self.settings["bulbs"])]
        seen, unique = set(), []
        for label, key in out:   # keep menu labels unique
            while label in seen:
                label += " "
            seen.add(label)
            unique.append((label, key))
        return unique

    def _take_over(self):
        """Direct control wins: stop any sync or fade first."""
        if self.fade_job and self.fade_job.is_alive():
            self.fade_job.cancel()
        if self._is_syncing():
            self._stop(restore=False)

    def _command(self, ips, params, look, status, throttle=False):
        """Send params to the lights and remember how they look for the room picture."""
        self._take_over()
        if not status.endswith("scene"):
            self._clear_scene()
        for ip in ips:
            self.display[ip] = look
        self.status_text = status
        if throttle:   # dragging a slider or the wheel: at most ~15 sends a second
            self._pending_cmd = (list(ips), dict(params))
            if not self._cmd_job:
                self._cmd_job = self.after(70, self._flush_cmd)
        else:
            for ip in ips:
                bulb_send(self.cmd_sock, ip, params)
        self._draw_room()
        self._paint_dots()

    def _flush_cmd(self):
        self._cmd_job = None
        if self._pending_cmd:
            ips, params = self._pending_cmd
            self._pending_cmd = None
            for ip in ips:
                bulb_send(self.cmd_sock, ip, params)

    def _poll_states(self):
        """Ask each light how it looks right now (used when nothing is syncing)."""
        ips = list(self.settings["bulbs"])

        def work():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.4)
            found = {ip: state_to_display(bulb_get_state(sock, ip)) for ip in ips}
            sock.close()
            self.ui_queue.put(lambda: (self.display.update(found), self._draw_room(), self._paint_dots()))

        threading.Thread(target=work, daemon=True).start()

    def _paint_dots(self):
        for ip, dots in list(self.light_dots.items()):
            dots[:] = [d for d in dots if d.winfo_exists()]
            color, dim, _ = self._bulb_look(ip)
            for d in dots:
                try:
                    d.configure(fg_color=hex_color(color) if color else hex_color(OFF_BULB))
                except Exception:
                    pass

    def _dot(self, parent, ip, size=18):
        d = ctk.CTkFrame(parent, width=size, height=size, corner_radius=size // 2, fg_color=hex_color(OFF_BULB))
        self.light_dots.setdefault(ip, []).append(d)
        return d

    def _name_cell(self, parent, kind, ident, font, width=None):
        """A name that can be renamed in place: a label that turns into a text box."""
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        name = self._light_name(ident) if kind == "light" else (self._room(ident) or {}).get("name", "")
        kw = {"width": width} if width else {}
        cell.lbl = ctk.CTkLabel(cell, text=name, font=font, text_color=MOON, anchor="w", **kw)
        cell.lbl.pack(side="left")
        cell.kind, cell.ident, cell.font, cell.entry, cell.hint = kind, ident, font, None, None
        cell.fixed = width
        return cell

    def _start_rename(self, cell):
        if cell.entry is not None:
            return
        cell.lbl.pack_forget()
        size = cell.font.cget("size")
        width = cell.fixed or max(200, cell.lbl.winfo_reqwidth() + 60)
        e = ctk.CTkEntry(cell, width=width, height=size + 18, font=cell.font, fg_color=RAISED,
                         border_color=FILAMENT, border_width=2, text_color=MOON, corner_radius=8)
        e.insert(0, cell.lbl.cget("text"))
        e.pack(side="left")
        if not cell.fixed:   # rows are tight on space; the page header has room for a hint
            cell.hint = self._label(cell, "Enter to save, Esc to cancel", muted=True)
            cell.hint.pack(side="left", padx=10)
        cell.entry = e
        e.bind("<Return>", lambda ev: self._finish_rename(cell, True))
        e.bind("<KP_Enter>", lambda ev: self._finish_rename(cell, True))
        e.bind("<Escape>", lambda ev: self._finish_rename(cell, False))
        e.bind("<FocusOut>", lambda ev: self._finish_rename(cell, True))
        def focus():
            if e.winfo_exists():
                e.focus_set()
                e.select_range(0, "end")

        self.after(30, focus)

    def _finish_rename(self, cell, save):
        e = cell.entry
        if e is None:
            return
        cell.entry = None
        new = e.get().strip()
        old = cell.lbl.cget("text")
        e.destroy()
        if cell.hint:
            cell.hint.destroy()
        cell.lbl.pack(side="left")
        if not save or not new or new == old:
            return
        cell.lbl.configure(text=new)
        with self.lock:
            if cell.kind == "light" and cell.ident in self.settings["bulbs"]:
                self.settings["bulbs"][cell.ident]["name"] = new
            elif cell.kind == "room" and self._room(cell.ident):
                self._room(cell.ident)["name"] = new
        self._schedule_save()
        self.after(50, self._after_change)

    def _after_change(self):
        """Redraw whatever shows names, rooms or lights."""
        for name, fn in (("Lights", self._refresh_light_rows), ("Rooms", self._refresh_room_rows)):
            if getattr(self, "current_page", None) == name:
                fn()
            else:
                self._dirty[name] = True
        if self.detail_key and getattr(self, "current_page", None) == "Detail":
            if self._ips_for_key(self.detail_key) or self.detail_key.startswith("room:") and \
                    self._room(self.detail_key[5:]):
                self._open_detail(self.detail_key, self.detail_tab)
            else:
                self._show_page("Lights")
        self._draw_room()

    # ------------------------------------------------------------ the page for one light, a room, or all
    def _open_detail(self, key, tab=None):
        key = self._norm_key(key)
        if key.startswith("room:") and not self._room(key[5:]):
            self._show_page("Rooms")      # the room was deleted
            return
        if key.startswith("light:") and key[6:] not in self.settings["bulbs"]:
            self._show_page("Lights")     # the light was removed
            return
        self.detail_key = key
        self.detail_tab = tab or ("Lights" if key.startswith("room:") else "Control")
        page = self.detail_area.new_layer()
        self._scene_btns = {}
        is_room = key.startswith("room:")
        back_to = "Rooms" if is_room else "Lights"

        head = ctk.CTkFrame(page, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 0))
        self._ghost_button(head, f"Back to {back_to.lower()}", lambda: self._show_page(back_to), color=MUTED,
                           width=120).pack(anchor="w")
        title_row = ctk.CTkFrame(head, fg_color="transparent")
        title_row.pack(fill="x", pady=(4, 0))
        dots = ctk.CTkFrame(title_row, fg_color="transparent")
        dots.pack(side="left", padx=(4, 12))
        for ip in self._ips_for_key(key)[:6]:
            self._dot(dots, ip, 16).pack(side="left", padx=2)
        if key == "all":
            ctk.CTkLabel(title_row, text="All lights", font=self.f_big, text_color=MOON).pack(side="left")
            self.header_cell = None
        else:
            kind, ident = ("room", key[5:]) if is_room else ("light", key[6:])
            self.header_cell = self._name_cell(title_row, kind, ident, self.f_big)
            self.header_cell.pack(side="left")
        ips = self._ips_for_key(key)
        if key == "all":
            sub = f"{len(ips)} lights"
        elif is_room:
            sub = f"{len(ips)} light{'s' if len(ips) != 1 else ''}" + (
                f": {', '.join(self._light_name(i) for i in ips)}" if ips else ". Add lights on the Lights tab.")
        else:
            room = self._room_of(key[6:])
            sub = key[6:] + (f", in {room['name']}" if room else "")
        self._button(title_row, "Turn off", lambda: self._power(False), width=96).pack(side="right")
        self._button(title_row, "Turn on", lambda: self._power(True), width=96, primary=True).pack(
            side="right", padx=8)
        if key != "all":
            self._ghost_button(title_row, "Rename", lambda: self._start_rename(self.header_cell), color=MOON,
                               width=80).pack(side="right", padx=(0, 6))
        self._label(head, sub, muted=True).pack(anchor="w", padx=(4, 0))

        if is_room:
            tabs = ["Lights", "Control", "Scenes", "Schedules"]
        else:
            tabs = ["Control", "Scenes", "Schedules"] + (["Settings"] if key != "all" else [])
        if self.detail_tab not in tabs:
            self.detail_tab = tabs[0]
        self.detail_frames = {}
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(12, 0))
        self.detail_seg = self._segmented(bar, tabs, self._detail_switch, self.detail_tab)
        self.detail_seg.pack(anchor="w")
        host = ctk.CTkFrame(page, fg_color="transparent")
        host.pack(fill="both", expand=True)
        for t in tabs:
            f = ctk.CTkFrame(host, fg_color="transparent")
            self.detail_frames[t] = f
        self._build_control(self.detail_frames["Control"], key)
        self._build_scenes(self.detail_frames["Scenes"], key)
        self._build_schedules(self.detail_frames["Schedules"], key)
        if is_room:
            self._build_room_lights(self.detail_frames["Lights"], key[5:])
        elif "Settings" in tabs:
            self._build_light_settings(self.detail_frames["Settings"], key[6:])
        self._detail_switch(self.detail_tab)
        self.detail_area.commit(page)
        self._show_page("Detail", nav=back_to)
        self._paint_dots()

    def _detail_switch(self, tab):
        self.detail_tab = tab
        if self.detail_seg.get() != tab:
            self.detail_seg.set(tab)
        self._recolor_segment(self.detail_seg)
        for t, f in self.detail_frames.items():
            if t == tab:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()

    def _detail_ips(self):
        return self._ips_for_key(self.detail_key)

    # ---- Control tab
    def _build_control(self, page, key):
        ips = self._ips_for_key(key)
        look = self.display.get(ips[0], (None, 0, "")) if ips else (None, 0, "")
        if look[1]:
            self.settings["control_brightness"] = int(look[1])

        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(14, 12))
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.pack(side="left", padx=(0, 28), anchor="n")
        self.wheel = None
        if PIL_OK:
            self.wheel = ColorWheel(left, size=210, command=self._pick_color)
            self.wheel.pack()
            self.wheel.set_color(tuple(look[0]) if look[0] and look[2] == "Color"
                                 else tuple(self.settings["control_color"]))
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, anchor="n")

        self._label(right, "Colors", font=self.f_head).pack(anchor="w")
        sw_row = ctk.CTkFrame(right, fg_color="transparent")
        sw_row.pack(anchor="w", pady=(6, 14))
        for rgb in QUICK_COLORS:
            ctk.CTkButton(sw_row, text="", width=34, height=34, corner_radius=17, fg_color=hex_color(rgb),
                          hover_color=hex_color(rgb), border_width=0,
                          command=lambda c=rgb: (self.wheel.set_color(c) if self.wheel else None,
                                                 self._pick_color(c, throttle=False))).pack(side="left", padx=3)

        self._label(right, "Whites", font=self.f_head).pack(anchor="w")
        wrow = ctk.CTkFrame(right, fg_color="transparent")
        wrow.pack(fill="x", pady=(6, 0))
        wrow.grid_columnconfigure(0, weight=1)
        kbar = tk.Canvas(wrow, height=8, bg=SURFACE, highlightthickness=0, bd=0)
        kbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(0, 4))

        def paint_kbar(e, c=kbar):
            c.delete("all")
            w = max(10, e.width)
            for x in range(0, w, 3):
                k = MIN_KELVIN + (MAX_KELVIN - MIN_KELVIN) * x / max(1, w - 1)
                c.create_rectangle(x, 0, x + 3, 8, fill=hex_color(kelvin_to_rgb(k)), width=0)

        kbar.bind("<Configure>", paint_kbar)
        self.k_label = ctk.CTkLabel(wrow, text=f"{self.settings['control_kelvin']}K", width=70, anchor="e",
                                    font=self.f_num, text_color=FILAMENT)
        self.k_label.grid(row=1, column=1)
        self.k_slider = self._slider_widget(wrow, MIN_KELVIN, MAX_KELVIN, 43, self._pick_white)
        self.k_slider.set(self.settings["control_kelvin"])
        self.k_slider.grid(row=1, column=0, sticky="ew")
        qrow = ctk.CTkFrame(right, fg_color="transparent")
        qrow.pack(anchor="w", pady=(6, 14))
        for k, name in zip(QUICK_WHITES, ("Candle", "Warm", "Neutral", "Daylight")):
            self._ghost_button(qrow, name, lambda k=k: (self.k_slider.set(k), self._pick_white(k, throttle=False)),
                               color=MOON, width=80).pack(side="left", padx=(0, 4))

        self._label(right, "Brightness", font=self.f_head).pack(anchor="w")
        brow = ctk.CTkFrame(right, fg_color="transparent")
        brow.pack(fill="x", pady=(6, 0))
        brow.grid_columnconfigure(0, weight=1)
        self.b_label = ctk.CTkLabel(brow, text=f"{self.settings['control_brightness']}%", width=70, anchor="e",
                                    font=self.f_num, text_color=FILAMENT)
        self.b_slider = self._slider_widget(brow, 10, 100, 90, self._pick_brightness)
        self.b_slider.set(self.settings["control_brightness"])
        self.b_slider.grid(row=0, column=0, sticky="ew")
        self.b_label.grid(row=0, column=1)
        self._label(right, "Changes here stop screen or music sync so the lights stay how you set them.",
                    muted=True, wraplength=460, justify="left").pack(anchor="w", pady=(16, 0))
        self._last_mode = "white" if look[2].startswith("White") else "color"

    def _pick_color(self, rgb, throttle=True):
        self.set("control_color", list(rgb))
        self._last_mode = "color"
        dim = self.settings["control_brightness"]
        self._command(self._detail_ips(), {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2],
                                           "c": 0, "w": 0, "dimming": dim},
                      (rgb, dim, "Color"), f"{self._key_name(self.detail_key)}: color", throttle)

    def _pick_white(self, k, throttle=True):
        k = int(round(k / 100) * 100)
        self.k_label.configure(text=f"{k}K")
        self.set("control_kelvin", k)
        self._last_mode = "white"
        dim = self.settings["control_brightness"]
        self._command(self._detail_ips(), {"state": True, "temp": k, "dimming": dim},
                      (kelvin_to_rgb(k), dim, f"White {k}K"), f"{self._key_name(self.detail_key)}: white",
                      throttle)

    def _pick_brightness(self, v):
        v = int(v)
        self.b_label.configure(text=f"{v}%")
        self.set("control_brightness", v)
        ips = self._detail_ips()
        look = self.display.get(ips[0], (None, v, "")) if ips else (None, v, "")
        self._command(ips, {"state": True, "dimming": v}, (look[0] or (255, 200, 140), v, look[2] or "On"),
                      f"{self._key_name(self.detail_key)}: brightness", throttle=True)

    def _power(self, on, key=None):
        key = key or self.detail_key
        ips = self._ips_for_key(key)
        name = self._key_name(key)
        if not on:
            self._command(ips, {"state": False}, (None, 0, "Off"), f"{name}: off")
            return
        dim = self.settings["control_brightness"]
        if getattr(self, "_last_mode", "color") == "white":
            k = self.settings["control_kelvin"]
            self._command(ips, {"state": True, "temp": k, "dimming": dim},
                          (kelvin_to_rgb(k), dim, f"White {k}K"), f"{name}: on")
        else:
            rgb = tuple(self.settings["control_color"])
            self._command(ips, {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2], "c": 0, "w": 0,
                                "dimming": dim}, (rgb, dim, "Color"), f"{name}: on")

    # ---- Scenes tab
    def _build_scenes(self, page, key):
        sf = self._scroll(page)
        ctl = ctk.CTkFrame(sf, fg_color="transparent")
        ctl.pack(fill="x", padx=18, pady=(8, 0))
        self.scene_now = self._label(ctl, "", muted=True)
        self.scene_now.pack(anchor="e")
        self._slider(ctl, "Speed", "scene_speed", 10, 200, 19, "{:.0f}%",
                     "How fast moving scenes change. Still scenes ignore it.",
                     on_change=lambda v: self._resend_scene())
        self._slider(ctl, "Brightness", "scene_brightness", 10, 100, 90, "{:.0f}%",
                     on_change=lambda v: self._resend_scene())
        self._scene_btns = {}
        for title, ids in SCENE_GROUPS:
            g = self._group(sf, title)
            grid = ctk.CTkFrame(g, fg_color="transparent")
            grid.pack(fill="x", pady=(6, 0))
            for i, sid in enumerate(ids):
                img = None
                if PIL_OK:
                    if sid not in self._scene_imgs:
                        pil = scene_chip(sid)
                        self._scene_imgs[sid] = ctk.CTkImage(light_image=pil, dark_image=pil, size=(22, 22))
                    img = self._scene_imgs[sid]
                btn = ctk.CTkButton(grid, text=SCENES[sid], image=img, compound="left", anchor="w", width=150,
                                    height=40, corner_radius=10, fg_color=NIGHT, hover_color=RAISED,
                                    text_color=MOON, font=self.f_body, border_width=2, border_color=NIGHT,
                                    command=lambda s=sid: self._play_scene(s))
                btn.grid(row=i // 4, column=i % 4, padx=4, pady=4, sticky="w")
                self._scene_btns[sid] = btn
        cur = self._current_scene
        if cur and cur[1] == key and cur[0] in self._scene_btns:
            self._scene_btns[cur[0]].configure(border_color=FILAMENT)
            self.scene_now.configure(text=f"Playing {SCENES[cur[0]]}")
        ctk.CTkFrame(sf, height=12, fg_color="transparent").pack()

    def _play_scene(self, sid):
        key = self.detail_key
        ips = self._ips_for_key(key)
        dim, speed = self.settings["scene_brightness"], self.settings["scene_speed"]
        params = {"state": True, "sceneId": sid, "dimming": dim}
        if sid not in STATIC_SCENES:
            params["speed"] = speed
        self._command(ips, params, (SCENE_LOOK[sid][0], dim, SCENES[sid]),
                      f"{self._key_name(key)}: {SCENES[sid]} scene")
        self._current_scene = (sid, key)
        self.scene_now.configure(text=f"Playing {SCENES[sid]}")
        for other, btn in self._scene_btns.items():
            btn.configure(border_color=FILAMENT if other == sid else NIGHT)

    def _clear_scene(self):
        self._current_scene = None
        for btn in getattr(self, "_scene_btns", {}).values():
            try:
                btn.configure(border_color=NIGHT)
            except Exception:
                pass
        try:
            self.scene_now.configure(text="")
        except Exception:
            pass

    def _resend_scene(self):
        if self._current_scene and not self._is_syncing():
            sid, key = self._current_scene
            dim, speed = self.settings["scene_brightness"], self.settings["scene_speed"]
            params = {"state": True, "sceneId": sid, "dimming": dim}
            if sid not in STATIC_SCENES:
                params["speed"] = speed
            self._command(self._ips_for_key(key), params, (SCENE_LOOK[sid][0], dim, SCENES[sid]),
                          f"{self._key_name(key)}: {SCENES[sid]} scene", throttle=True)
            self._current_scene = (sid, key)

    # ---- Schedules tab
    def _build_schedules(self, page, key):
        sf = self._scroll(page)
        g = self._group(sf, "Sleep timer", f"Fades {self._key_name(key).lower() if key == 'all' else self._key_name(key)}"
                                           " out over a minute and turns off.")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        for m in (15, 30, 60, 90):
            self._button(row, f"{m} min", lambda m=m: self._set_sleep(key, m), width=80).pack(side="left", padx=(0, 6))
        self._ghost_button(row, "Cancel", lambda: self._set_sleep(key, None), color=ROSE).pack(side="left", padx=6)
        self.sleep_label = self._label(row, self._sleep_text(key), muted=True)
        self.sleep_label.pack(side="left", padx=10)

        g = self._group(sf, "Schedules", "These run while the app is open, including when it is in the tray. "
                                         "Turn on Open at Windows login on the App page so they always run.")
        self._button(g, "Add schedule", lambda: ScheduleDialog(self, None, key), width=130, primary=True).pack(
            anchor="w", pady=(8, 4))
        self.sched_list = ctk.CTkFrame(g, fg_color="transparent")
        self.sched_list.pack(fill="x", pady=(6, 0))
        self._refresh_schedules()

    def _refresh_schedules(self):
        box = getattr(self, "sched_list", None)
        if not box or not box.winfo_exists():
            return
        for w in box.winfo_children():
            w.destroy()
        key = self.detail_key
        items = sorted([s for s in self.settings["schedules"] if self._norm_key(s.get("target")) == key],
                       key=lambda s: s["time"])
        if not items:
            self._label(box, f"No schedules for {self._key_name(key)} yet. Add one to wake up to light, wind "
                             "down at bedtime, or start syncing at a set time.", muted=True,
                        wraplength=600, justify="left").pack(anchor="w", pady=10)
        for sc in items:
            row = ctk.CTkFrame(box, fg_color=NIGHT, corner_radius=12)
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=time_text(sc["time"]), font=self.f_big, width=120, anchor="w",
                         text_color=MOON if sc.get("enabled", True) else MUTED).grid(
                row=0, column=0, rowspan=2, padx=(16, 8), pady=10, sticky="w")
            self._label(row, action_text(sc)).grid(row=0, column=1, sticky="w", pady=(10, 0))
            self._label(row, days_text(sc["days"]), muted=True).grid(row=1, column=1, sticky="w", pady=(0, 10))
            var = ctk.BooleanVar(value=sc.get("enabled", True))
            ctk.CTkSwitch(row, text="", variable=var, width=46, switch_width=42, switch_height=22,
                          fg_color=LINE, progress_color=FILAMENT, button_color=MOON,
                          command=lambda s=sc, v=var: self._toggle_schedule(s["id"], bool(v.get()))
                          ).grid(row=0, column=2, rowspan=2, padx=6)
            self._ghost_button(row, "Edit", lambda s=sc: ScheduleDialog(self, s, key), color=MOON, width=60).grid(
                row=0, column=3, rowspan=2)
            self._ghost_button(row, "Delete", lambda s=sc: self._delete_schedule(s["id"]), color=ROSE,
                               width=60).grid(row=0, column=4, rowspan=2, padx=(0, 10))

    def _save_schedule(self, sc):
        with self.lock:
            items = [x for x in self.settings["schedules"] if x["id"] != sc["id"]]
            items.append(sc)
            self.settings["schedules"] = items
        self._schedule_save()
        self._refresh_schedules()

    def _toggle_schedule(self, sid, on):
        with self.lock:
            for x in self.settings["schedules"]:
                if x["id"] == sid:
                    x["enabled"] = on
        self._schedule_save()
        self._refresh_schedules()

    def _delete_schedule(self, sid):
        with self.lock:
            self.settings["schedules"] = [x for x in self.settings["schedules"] if x["id"] != sid]
        self._schedule_save()
        self._refresh_schedules()

    def _check_schedules(self):
        now = time.localtime()
        hhmm, today = f"{now.tm_hour:02d}:{now.tm_min:02d}", time.strftime("%Y-%m-%d", now)
        for sc in self.settings["schedules"]:
            key = (sc["id"], today, hhmm)
            if sc.get("enabled", True) and sc["time"] == hhmm and now.tm_wday in sc["days"] \
                    and key not in self._fired:
                self._fired.add(key)
                self._run_action(sc)

    def _run_action(self, sc):
        a, v = sc["action"], sc.get("value", {})
        key = self._norm_key(sc.get("target"))
        ips, name = self._ips_for_key(key), self._key_name(key)
        dim = v.get("brightness", 80)
        if a == "Turn on":
            self._command(ips, {"state": True, "dimming": dim}, ((255, 200, 140), dim, "On"), f"Schedule: {name} on")
        elif a == "Turn off":
            self._command(ips, {"state": False}, (None, 0, "Off"), f"Schedule: {name} off")
        elif a == "Set white":
            k = v.get("kelvin", 3000)
            self._command(ips, {"state": True, "temp": k, "dimming": dim}, (kelvin_to_rgb(k), dim, f"White {k}K"),
                          f"Schedule: {name} white")
        elif a == "Set color":
            rgb = tuple(v.get("color", (255, 140, 60)))
            self._command(ips, {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2], "c": 0, "w": 0,
                                "dimming": dim}, (rgb, dim, "Color"), f"Schedule: {name} color")
        elif a == "Play scene":
            sid = v.get("scene", 1)
            params = {"state": True, "sceneId": sid, "dimming": dim}
            if sid not in STATIC_SCENES:
                params["speed"] = v.get("speed", 100)
            self._command(ips, params, (SCENE_LOOK[sid][0], dim, SCENES[sid]),
                          f"Schedule: {name}: {SCENES[sid]} scene")
        elif a == "Wake-up fade":
            self._start_fade(ips, v.get("minutes", 30), 10, 100, 2200, 5000, False, f"{name}: wake-up fade")
        elif a == "Bedtime fade":
            self._start_fade(ips, v.get("minutes", 30), 60, 10, 2700, 2200, True, f"{name}: bedtime fade")
        elif a in ("Start screen sync", "Start music sync"):
            self._pick_source("Screen" if "screen" in a else "Music")
            if not self._is_syncing():
                self._start()
        elif a == "Stop sync":
            if self._is_syncing():
                self._stop()

    def _start_fade(self, ips, minutes, d0, d1, k0, k1, off, label):
        self._take_over()
        self.fade_label = label

        def done():
            self.ui_queue.put(lambda: self._fade_done(ips, off, k1, d1))

        self.fade_job = FadeJob(ips, minutes, d0, d1, k0, k1, turn_off_at_end=off, on_done=done)
        self.fade_job.start()
        self.status_text = label

    def _fade_done(self, ips, off, k, d):
        for ip in ips:
            self.display[ip] = (None, 0, "Off") if off else (kelvin_to_rgb(k), d, f"White {k}K")
        self.status_text = "Lights off" if off else "Wake-up fade finished"
        self._draw_room()
        self._paint_dots()

    def _sleep_text(self, key):
        dl = self.sleep_timers.get(key)
        if not dl:
            return ""
        return "Off at " + time.strftime("%I:%M %p", time.localtime(dl)).lstrip("0")

    def _set_sleep(self, key, minutes):
        if minutes is None:
            self.sleep_timers.pop(key, None)
        else:
            self.sleep_timers[key] = time.time() + minutes * 60
        try:
            self.sleep_label.configure(text=self._sleep_text(key))
        except Exception:
            pass
        self._draw_room()

    def _sleep_now(self, key):
        self.sleep_timers.pop(key, None)
        if self.detail_key == key:
            try:
                self.sleep_label.configure(text="")
            except Exception:
                pass
        ips = self._ips_for_key(key)
        if self._is_syncing():
            self._stop(restore=False)
        self._start_fade(ips, 1, 50, 10, 2700, 2200, True, f"{self._key_name(key)}: sleep timer")

    # ---- Screen sync page (was Color, Brightness and Screen)
    def _build_screen_page(self, page):
        sf = self._scroll(page)
        top = ctk.CTkFrame(sf, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(top, text="Screen sync", font=self.f_head, text_color=MOON).pack(side="left")
        self.preset_menu = self._menu(top, ["Presets"] + list(PRESETS), self._apply_preset, width=140)
        self.preset_menu.set("Presets")
        self.preset_menu.pack(side="right")

        g = self._group(sf, "Color", "How the app turns your screen into one color for each bulb.")
        self._choice(g, "Color mode", "color_mode", COLOR_MODES,
                     "Vivid lets strong colors win over gray. Average uses the true average of the screen. "
                     "Dominant uses the most common strong color, good for movies and games.")
        self._slider(g, "Color intensity", "saturation", 0.5, 3.0, 25, "{:.1f}x",
                     "1.0x shows the screen's real colors. Higher makes pale colors bolder.")
        g = self._group(sf, "Motion")
        self._slider(g, "Smoothing", "smoothing", 0.0, 0.95, 19, "{:.2f}",
                     "0 reacts instantly. 0.9 fades slowly and calmly.")
        self._slider(g, "Updates per second", "fps", 1, 20, 19, "{:.0f}", "WiZ bulbs keep up with 10 to 20.")
        self._slider(g, "Ignore small changes", "change_threshold", 0, 20, 20, "{:.0f}",
                     "Skips tiny shifts to reduce flicker and Wi-Fi traffic.")
        g = self._group(sf, "Brightness")
        self._choice(g, "Mode", "brightness_mode", BRIGHTNESS_MODES)
        self._slider(g, "Lowest", "min_brightness", 10, 100, 90, "{:.0f}%", "How dim the bulbs get in dark scenes.")
        self._slider(g, "Highest", "max_brightness", 10, 100, 90, "{:.0f}%")
        self._slider(g, "Response", "brightness_gain", 0.5, 6.0, 22, "{:.1f}x",
                     "Higher reaches full brightness on dimmer scenes.")
        self._slider(g, "Fixed level", "fixed_brightness", 10, 100, 90, "{:.0f}%", "Used when Mode is Fixed.")
        g = self._group(sf, "Dark scenes", "What the bulbs do when the screen is almost black.")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._label(row, "When the screen is dark", width=180, anchor="w").pack(side="left")
        om = self._menu(row, DARK_SCENE, lambda v: self.set("dark_scene", v), width=180)
        om.set(self.settings["dark_scene"])
        om.pack(side="left", padx=14)
        self.slider_widgets["dark_scene"] = (om, None, None)
        self._slider(g, "Counts as dark below", "dark_threshold", 0.0, 0.2, 20, "{:.2f}")
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
        ctk.CTkFrame(sf, height=16, fg_color="transparent").pack()

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

    # ---- Music sync page
    def _build_music_page(self, page):
        sf = self._scroll(page)
        g = self._group(sf, "Music sync", "The bulbs react to whatever is playing on this PC: Spotify, YouTube, "
                                          "games. Pick Music next to Start sync at the top.")
        if not self._soundcard_ok():
            ctk.CTkLabel(g, text="Music sync needs one more package. Run  pip install soundcard  and restart "
                                 "the app.", wraplength=620, justify="left", text_color=ROSE,
                         font=self.f_body).pack(anchor="w", pady=(4, 0))
        self._choice(g, "Style", "music_mode", MUSIC_MODES,
                     "Beat pulse jumps to a new color on each beat. Spectrum mixes bass as red, mids as green "
                     "and highs as blue. Mellow drifts slowly and breathes with the volume.")
        self._choice(g, "Colors", "music_palette", list(MUSIC_PALETTES),
                     "The range of colors Beat pulse and Mellow move through.")
        self._switch(g, "Give each bulb a different color", "music_split_colors")
        g = self._group(sf, "Response")
        self._slider(g, "Beat sensitivity", "music_sensitivity", 0.4, 2.0, 16, "{:.1f}x",
                     "Higher catches softer beats. Lower reacts only to strong kicks.")
        self._slider(g, "Smoothing", "music_smoothing", 0.0, 0.9, 18, "{:.2f}")
        self._slider(g, "Updates per second", "music_fps", 4, 20, 16, "{:.0f}")
        g = self._group(sf, "Brightness")
        self._slider(g, "Quiet parts", "music_min_brightness", 10, 100, 90, "{:.0f}%")
        self._slider(g, "Loud parts", "music_max_brightness", 10, 100, 90, "{:.0f}%")
        ctk.CTkFrame(sf, height=16, fg_color="transparent").pack()

    def _soundcard_ok(self):
        try:
            import importlib.util
            return importlib.util.find_spec("soundcard") is not None
        except Exception:
            return False

    # ---- Lights page (home)
    def _build_lights_page(self, page):
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(bar, text="Lights", font=self.f_head, text_color=MOON).pack(side="left")
        self._button(bar, "Control all lights", lambda: self._open_detail("all"), width=150,
                     primary=True).pack(side="left", padx=16)
        self.find_btn = self._button(bar, "Find lights", self._discover, width=110)
        self.find_btn.pack(side="right")
        self._button(bar, "Add", self._add_ip, width=60).pack(side="right", padx=(6, 12))
        self.ip_entry = ctk.CTkEntry(bar, width=150, height=34, corner_radius=10, fg_color=RAISED,
                                     border_width=0, text_color=MOON, placeholder_text="Add by IP address",
                                     placeholder_text_color=MUTED, font=self.f_body)
        self.ip_entry.pack(side="right")
        self.discover_label = self._label(page, "Click a light to control it, play scenes or set schedules. "
                                                "Screen area picks the part of the screen it follows during "
                                                "screen sync.", muted=True, anchor="w", justify="left",
                                          wraplength=640)
        self.discover_label.pack(fill="x", padx=18, pady=(4, 6))
        self.light_area = SwapArea(page, SURFACE)
        self.light_area.pack(fill="both", expand=True)

    def _hover(self, row, children=()):
        """Rows light up under the mouse so it is clear they open."""
        def on(e):
            row.configure(fg_color=ROW_HOVER)

        def off(e):
            x, y = row.winfo_pointerxy()
            w = row.winfo_containing(x, y)
            while w is not None and w is not row:
                w = getattr(w, "master", None)
            if w is not row:
                row.configure(fg_color=NIGHT)

        for w in [row, *children]:
            w.bind("<Enter>", on, add="+")
            w.bind("<Leave>", off, add="+")

    def _clickable(self, widgets, key):
        for w in widgets:
            w.bind("<Button-1>", lambda e, k=key: self._open_detail(k))
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

    def _refresh_light_rows(self):
        area = getattr(self, "light_area", None)
        if not area:
            return
        self._dirty["Lights"] = False
        layer = area.new_layer()
        box = self.light_list = self._scroll(layer)
        bulbs = self.settings["bulbs"]
        if not bulbs:
            self._label(box, "No lights found yet. Click Find lights, or add one by its IP address.",
                        muted=True).pack(pady=40)
        for i, (ip, b) in enumerate(sorted(bulbs.items())):
            b.setdefault("name", f"Light {i + 1}")
            b.setdefault("enabled", True)
            b.setdefault("zone", "Full screen")
            b.setdefault("brightness_scale", 100)
            key = f"light:{ip}"
            row = ctk.CTkFrame(box, fg_color=NIGHT, corner_radius=12)
            row.pack(fill="x", padx=12, pady=5)
            row.grid_columnconfigure(3, weight=1)
            dot = self._dot(row, ip)
            dot.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=14)
            name = self._name_cell(row, "light", ip, self.f_row, width=170)
            name.grid(row=0, column=1, sticky="w", pady=(10, 0))
            room = self._room_of(ip)
            sub = self._label(row, ip + (f", {room['name']}" if room else ""), muted=True, anchor="w")
            sub.grid(row=1, column=1, sticky="w", pady=(0, 10))
            self._clickable([row, dot, name, name.lbl, sub], key)
            self._hover(row, [dot, name, name.lbl, sub])

            zone = self._menu(row, ZONES, lambda v, ip=ip: (self.set_bulb(ip, "zone", v), self._draw_room()),
                              width=140)
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
            self._label(bri, "Sync limit", muted=True).grid(row=1, column=0, sticky="w")
            en = ctk.BooleanVar(value=b["enabled"])
            ctk.CTkSwitch(row, text="Sync", variable=en, width=70, switch_width=42, switch_height=22,
                          fg_color=LINE, progress_color=FILAMENT, button_color=MOON, font=self.f_small,
                          text_color=MUTED,
                          command=lambda ip=ip, v=en: (self.set_bulb(ip, "enabled", bool(v.get())), self._draw_room())
                          ).grid(row=0, column=4, rowspan=2, padx=(10, 4))
            acts = ctk.CTkFrame(row, fg_color="transparent")
            acts.grid(row=0, column=5, rowspan=2, padx=(0, 6))
            self._ghost_button(acts, "Identify", lambda ip=ip: self._identify(ip), width=70).pack()
            self._ghost_button(acts, "Rename", lambda c=name: self._start_rename(c), width=70).pack()
            self._button(row, "Open", lambda k=key: self._open_detail(k), width=64).grid(
                row=0, column=6, rowspan=2, padx=(0, 12))
        area.commit(layer)
        self._paint_dots()

    def _discover(self):
        self.find_btn.configure(state="disabled", text="Searching")
        self.discover_label.configure(text="Searching your network for WiZ lights.")

        def work():
            found = discover_bulbs()
            self.ui_queue.put(lambda: self._discover_done(found))

        threading.Thread(target=work, daemon=True).start()

    def _discover_done(self, found):
        self.find_btn.configure(state="normal", text="Find lights")
        with self.lock:
            known = self.settings["bulbs"]
            new = [ip for ip in found if ip not in known]
            for ip in sorted(new):
                known[ip] = {"name": f"Light {len(known) + 1}", "enabled": True,
                             "zone": "Full screen", "brightness_scale": 100}
        self._schedule_save()
        for ip, st in found.items():
            self.display[ip] = state_to_display(st)
        if found:
            self.discover_label.configure(text=f"Found {len(found)} lights, {len(new)} of them new.")
        else:
            self.discover_label.configure(
                text="No lights answered. Check that this PC and the lights are on the same Wi-Fi, "
                     "and that local communication is on in the WiZ app.")
        self._after_change()

    def _add_ip(self):
        ip = self.ip_entry.get().strip()
        try:
            socket.inet_aton(ip)
        except OSError:
            self.discover_label.configure(text=f"{ip or 'That'} is not an IP address. It should look like 192.168.1.50.")
            return
        with self.lock:
            self.settings["bulbs"].setdefault(ip, {"name": f"Light {len(self.settings['bulbs']) + 1}",
                                                    "enabled": True, "zone": "Full screen",
                                                    "brightness_scale": 100})
        self._schedule_save()
        self.ip_entry.delete(0, "end")
        self._after_change()
        self._poll_states()

    def _remove_bulb(self, ip):
        with self.lock:
            self.settings["bulbs"].pop(ip, None)
            for r in self._rooms():
                if ip in r["lights"]:
                    r["lights"].remove(ip)
        self.display.pop(ip, None)
        self._schedule_save()
        self._show_page("Lights")
        self._after_change()

    def _identify(self, ip):
        """Flash the light soft yellow at 50% once. Works while syncing too."""
        self.status_text = f"{self._light_name(ip)} flashed soft yellow"
        engine = self.engine if self._is_syncing() else None
        flash_for = 0.7
        if engine:
            engine.hold_until[ip] = time.time() + flash_for + 0.1

        def work():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            state = None if engine else bulb_get_state(sock, ip)
            bulb_send(sock, ip, {"state": True, "r": 255, "g": 200, "b": 80, "c": 0, "w": 0, "dimming": 50})
            time.sleep(flash_for)
            if engine:
                engine.hold_until.pop(ip, None)
            else:
                params = restore_params(state)
                if params:
                    bulb_send(sock, ip, params)
            sock.close()

        threading.Thread(target=work, daemon=True).start()
        self._draw_room()

    # ---- Settings tab for one light
    def _build_light_settings(self, page, ip):
        b = self.settings["bulbs"].get(ip, {})
        sf = self._scroll(page)
        g = self._group(sf, "Room")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._label(row, "In room", width=180, anchor="w").pack(side="left")
        names = ["No room"] + [r["name"] for r in self._rooms()]
        menu = self._menu(row, names, lambda v, ip=ip: self._set_light_room(ip, v), width=200)
        room = self._room_of(ip)
        menu.set(room["name"] if room else "No room")
        menu.pack(side="left", padx=14)
        self._ghost_button(row, "New room", lambda: RoomDialog(self, preselect=[ip]), color=MOON,
                           width=90).pack(side="left")

        g = self._group(sf, "Screen sync", "How this light behaves while screen or music sync runs.")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._label(row, "Screen area", width=180, anchor="w").pack(side="left")
        zone = self._menu(row, ZONES, lambda v: (self.set_bulb(ip, "zone", v), self._draw_room()), width=200)
        zone.set(b.get("zone", "Full screen"))
        zone.pack(side="left", padx=14)
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure(1, weight=1)
        self._label(row, "Brightness limit", width=180, anchor="w").grid(row=0, column=0, sticky="w")
        lbl = ctk.CTkLabel(row, text=f"{b.get('brightness_scale', 100)}%", width=70, anchor="e",
                           font=self.f_num, text_color=FILAMENT)
        lbl.grid(row=0, column=2)
        sl = self._slider_widget(row, 10, 100, 18, lambda v: (lbl.configure(text=f"{int(v)}%"),
                                                                self.set_bulb(ip, "brightness_scale", int(v))))
        sl.set(b.get("brightness_scale", 100))
        sl.grid(row=0, column=1, sticky="ew", padx=14)
        var = ctk.BooleanVar(value=b.get("enabled", True))
        ctk.CTkSwitch(g, text="Include this light in screen and music sync", variable=var, font=self.f_body,
                      text_color=MOON, fg_color=LINE, progress_color=FILAMENT, button_color=MOON,
                      switch_width=42, switch_height=22,
                      command=lambda: (self.set_bulb(ip, "enabled", bool(var.get())), self._draw_room())
                      ).pack(anchor="w", pady=(12, 0))

        g = self._group(sf, "This light")
        self._label(g, f"IP address {ip}", muted=True).pack(anchor="w")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", pady=(10, 16))
        self._button(row, "Identify", lambda: self._identify(ip), width=100).pack(side="left")
        self._button(row, "Rename", lambda: self._start_rename(self.header_cell), width=100).pack(side="left",
                                                                                                 padx=8)
        self._button(row, "Remove light", lambda: self._remove_bulb(ip), width=130, color=ROSE).pack(side="left")

    def _set_light_room(self, ip, room_name):
        with self.lock:
            for r in self._rooms():
                if ip in r["lights"]:
                    r["lights"].remove(ip)
            target = next((r for r in self._rooms() if r["name"] == room_name), None)
            if target:
                target["lights"].append(ip)
        self._schedule_save()
        self._after_change()

    # ---- Rooms page
    def _build_rooms_page(self, page):
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(bar, text="Rooms", font=self.f_head, text_color=MOON).pack(side="left")
        self._button(bar, "Add room", lambda: RoomDialog(self), width=110, primary=True).pack(side="right")
        self._label(page, "Group lights into rooms and control each room as one: colors, whites, scenes and "
                          "schedules. A light can be in one room at a time.", muted=True, anchor="w",
                    justify="left", wraplength=640).pack(fill="x", padx=18, pady=(4, 6))
        self.room_area = SwapArea(page, SURFACE)
        self.room_area.pack(fill="both", expand=True)

    def _room_state(self, ips):
        on = [ip for ip in ips if self._bulb_look(ip)[0]]
        if not ips:
            return "Empty"
        if not on:
            return "All off"
        if len(on) == len(ips):
            return "All on"
        return f"{len(on)} of {len(ips)} on"

    def _refresh_room_rows(self):
        area = getattr(self, "room_area", None)
        if not area:
            return
        self._dirty["Rooms"] = False
        layer = area.new_layer()
        box = self._scroll(layer)
        rooms = self._rooms()
        if not rooms:
            self._label(box, "No rooms yet. Click Add room, give it a name and choose its lights.",
                        muted=True).pack(pady=40)
        for r in rooms:
            key = f"room:{r['id']}"
            ips = [ip for ip in r["lights"] if ip in self.settings["bulbs"]]
            row = ctk.CTkFrame(box, fg_color=NIGHT, corner_radius=12, height=68)
            row.pack(fill="x", padx=12, pady=4)
            row.grid_columnconfigure(1, weight=1)
            dots = ctk.CTkFrame(row, fg_color="transparent")
            dots.grid(row=0, column=0, rowspan=2, padx=(18, 14), pady=16)
            for ip in ips[:4]:
                self._dot(dots, ip, 14).pack(side="left", padx=1)
            if not ips:
                ctk.CTkFrame(dots, width=14, height=14, corner_radius=7, fg_color=hex_color(OFF_BULB)).pack()
            name = ctk.CTkLabel(row, text=r["name"], font=self.f_row, text_color=MOON, anchor="w")
            name.grid(row=0, column=1, sticky="w", pady=(12, 0))
            sub = self._label(row, ", ".join(self._light_name(ip) for ip in ips) if ips else "No lights yet",
                              muted=True, anchor="w")
            sub.grid(row=1, column=1, sticky="w", pady=(0, 12))
            count = ctk.CTkLabel(row, text=f"{len(ips)} light{'s' if len(ips) != 1 else ''}", font=self.f_num,
                                 text_color=MOON, anchor="e")
            count.grid(row=0, column=2, sticky="e", padx=18, pady=(12, 0))
            state = self._label(row, self._room_state(ips), muted=True, anchor="e")
            state.grid(row=1, column=2, sticky="e", padx=18, pady=(0, 12))
            parts = [row, dots, name, sub, count, state, *dots.winfo_children()]
            self._clickable(parts, key)
            self._hover(row, parts[1:])
        area.commit(layer)
        self._paint_dots()

    # ---- Lights tab for a room: its lights, adding and removing, deleting the room
    def _build_room_lights(self, page, rid):
        room = self._room(rid)
        sf = self._scroll(page)
        ips = [ip for ip in room["lights"] if ip in self.settings["bulbs"]]
        g = self._group(sf, "Lights in this room", "Click a light to open it. Everything on the Control, Scenes "
                                                   "and Schedules tabs applies to all of them together.")
        if not ips:
            self._label(g, "No lights in this room yet. Add one below.", muted=True).pack(anchor="w", pady=8)
        for ip in ips:
            b = self.settings["bulbs"][ip]
            row = ctk.CTkFrame(g, fg_color=NIGHT, corner_radius=12)
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(1, weight=1)
            dot = self._dot(row, ip)
            dot.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=12)
            name = ctk.CTkLabel(row, text=b.get("name", ip), font=self.f_row, text_color=MOON, anchor="w")
            name.grid(row=0, column=1, sticky="w", pady=(10, 0))
            words = self._bulb_look(ip)[2] or ""
            look = self._bulb_look(ip)
            detail = f"{words}, {look[1]}%" if look[0] and words not in ("Off", "") else (words or "Unknown")
            sub = self._label(row, f"{ip}, {detail}", muted=True, anchor="w")
            sub.grid(row=1, column=1, sticky="w", pady=(0, 10))
            self._ghost_button(row, "Identify", lambda ip=ip: self._identify(ip), width=70).grid(
                row=0, column=2, rowspan=2)
            self._ghost_button(row, "Remove from room", lambda ip=ip: self._toggle_room_light(rid, ip, False),
                               color=ROSE, width=130).grid(row=0, column=3, rowspan=2, padx=(0, 10))
            parts = [row, dot, name, sub]
            self._clickable(parts, f"light:{ip}")
            self._hover(row, parts[1:])

        others = [ip for ip in sorted(self.settings["bulbs"]) if ip not in ips]
        g = self._group(sf, "Add a light")
        if others:
            row = ctk.CTkFrame(g, fg_color="transparent")
            row.pack(fill="x", pady=(8, 0))
            labels = {}
            for ip in others:
                other = self._room_of(ip)
                labels[self._light_name(ip) + (f" (now in {other['name']})" if other else "")] = ip
            pick = self._menu(row, list(labels), lambda v: None, width=260)
            pick.set(list(labels)[0])
            pick.pack(side="left")
            self._button(row, "Add to room", lambda: self._toggle_room_light(rid, labels[pick.get()], True),
                         width=120, primary=True).pack(side="left", padx=10)
            self._label(g, "A light can be in one room. Adding it here moves it out of its current room.",
                        muted=True).pack(anchor="w", pady=(6, 0))
        else:
            self._label(g, "Every light is already in this room. Find more on the Lights page.",
                        muted=True).pack(anchor="w", pady=(4, 0))

        g = self._group(sf, "This room")
        self._button(g, "Delete room", lambda: self._delete_room(rid), width=130, color=ROSE).pack(
            anchor="w", pady=(8, 4))
        self._label(g, "Deleting a room keeps its lights. Schedules for the room stop running.", muted=True
                    ).pack(anchor="w", pady=(0, 16))

    def _toggle_room_light(self, rid, ip, on):
        with self.lock:
            for r in self._rooms():
                if ip in r["lights"]:
                    r["lights"].remove(ip)
            if on:
                self._room(rid)["lights"].append(ip)
        self._schedule_save()
        self._after_change()

    def _save_room(self, rid, name, ips):
        with self.lock:
            room = self._room(rid) if rid else None
            if not room:
                room = {"id": f"r{int(time.time() * 1000)}", "name": name, "lights": []}
                self._rooms().append(room)
            room["name"] = name
            for r in self._rooms():   # a light lives in one room
                r["lights"] = [ip for ip in r["lights"] if ip not in ips]
            room["lights"] = list(ips)
        self._schedule_save()
        self._after_change()
        return room

    def _delete_room(self, rid):
        with self.lock:
            self.settings["rooms"] = [r for r in self._rooms() if r["id"] != rid]
        self._schedule_save()
        self._show_page("Rooms")
        self._after_change()

    # ---- App page
    def _build_app_page(self, page):
        sf = self._scroll(page)
        g = self._group(sf, "Startup")
        self._switch(g, "Start syncing when the app opens", "start_sync_on_launch")
        self._switch(g, "Open at Windows login, hidden in the tray", "launch_at_login", command=self._toggle_login)
        self._switch(g, "Put the bulbs back the way they were when syncing stops", "restore_on_stop")
        g = self._group(sf, "Closing the window")
        sw = self._switch(g, "Keep running in the system tray when I close the window", "close_to_tray")
        if TRAY_OK:
            note = "Right-click the tray icon near the clock to open the app, start or stop syncing, or exit."
        else:
            note = "The tray needs two packages. Run  pip install pystray pillow  and restart the app."
            sw.configure(state="disabled")
        self._label(g, note, muted=True, wraplength=620, justify="left").pack(anchor="w")
        self._button(g, "Exit app", self._quit, color=ROSE).pack(anchor="w", pady=(10, 0))
        g = self._group(sf, "Settings")
        self._label(g, f"Saved automatically to {SETTINGS_FILE}", muted=True, wraplength=620,
                    justify="left").pack(anchor="w")
        self._button(g, "Reset all settings", self._reset, width=150, color=ROSE).pack(anchor="w", pady=(10, 16))

    def _toggle_login(self, enabled):
        try:
            set_launch_at_login(enabled)
        except OSError as exc:
            self.status_text = f"Could not change the login setting: {exc}"

    def _reset(self):
        with self.lock:
            bulbs, schedules, rooms = self.settings["bulbs"], self.settings["schedules"], self._rooms()
            self.settings = copy.deepcopy(DEFAULTS)
            self.settings["bulbs"] = {ip: {"name": b.get("name", ip), "enabled": True,
                                           "zone": "Full screen", "brightness_scale": 100}
                                      for ip, b in bulbs.items()}
            self.settings["schedules"] = schedules
            self.settings["rooms"] = rooms
        self._sync_widgets()
        self._after_change()
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
            self.discover_label.configure(text="Turn on Sync for at least one light first.")
            self._show_page("Lights")
            return
        if self.fade_job and self.fade_job.is_alive():
            self.fade_job.cancel()
        self._clear_scene()
        if self.settings["sync_source"] == "Music":
            self.engine = MusicEngine(self.get_settings)
        else:
            self.engine = ScreenEngine(self.get_settings)
        self.engine.start()
        self.start_btn.configure(text="Stop sync", fg_color=RAISED, hover_color=LINE, text_color=ROSE)
        self._update_tray()

    def _stop(self, restore=True):
        if self.engine:
            if not restore:
                self.engine.saved_states = {}
            self.engine.stop()
            self.engine.join(timeout=3)
        self.start_btn.configure(text="Start sync", fg_color=FILAMENT, hover_color=FILAMENT_HOVER,
                                 text_color=NIGHT)
        self.status_text = ""
        self._update_tray()
        if restore:
            self.after(400, self._poll_states)
        self._draw_room()
        self._paint_dots()

    def _tick(self):
        while True:
            try:
                self.ui_queue.get_nowait()()
            except queue.Empty:
                break
        if self._quitting:
            return
        now = time.time()
        eng = self.engine
        if eng and eng.is_alive():
            self._paint_dots()
            if now - self._last_hero > 0.18 and self.winfo_viewable():
                self._last_hero = now
                self._draw_room()
        elif eng and eng.error and self.start_btn.cget("text") == "Stop sync":
            self._stop(restore=False)
        elif self.fade_job and self.fade_job.is_alive() and now - self._last_hero > 1.0:
            self._last_hero = now
            self._draw_room()
        if now - self._last_sched_check > 2.0:
            self._last_sched_check = now
            self._check_schedules()
            for key, dl in list(self.sleep_timers.items()):
                if now >= dl:
                    self._sleep_now(key)
            if self.sleep_timers and self.winfo_viewable():
                self._draw_room()
        self.after(150, self._tick)

    # ------------------------------------------------------------ system tray
    def _setup_tray(self):
        if not TRAY_OK:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open WiZard", lambda: self.ui_queue.put(self._show), default=True),
            pystray.MenuItem(lambda item: "Stop sync" if self._is_syncing() else "Start sync",
                             lambda: self.ui_queue.put(self._toggle)),
            pystray.MenuItem("Turn all lights off",
                             lambda: self.ui_queue.put(lambda: self._power(False, "all"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda: self.ui_queue.put(self._quit)),
        )
        try:
            self.tray = pystray.Icon("wizard", tray_image(False), APP_NAME, menu)
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
                    self.tray.notify("Still running in the system tray. Right-click the icon to exit.", APP_NAME)
                except Exception:
                    pass
            return
        self._quit()

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        if self.fade_job and self.fade_job.is_alive():
            self.fade_job.cancel()
        self._stop()
        save_settings(self.get_settings())
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()


class RoomDialog(ctk.CTkToplevel):
    """Create a room: a name and its lights."""

    def __init__(self, app, preselect=None):
        super().__init__(app, fg_color=SURFACE)
        self.app = app
        self.title("Add room")
        self.resizable(False, False)
        self.transient(app)
        self.after(50, self.grab_set)
        app._dialog_icon(self)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(0, 20), side="bottom")
        app._button(btns, "Add room", self._save, width=120, primary=True).pack(side="right")
        app._button(btns, "Cancel", self.destroy, width=90).pack(side="right", padx=8)
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=24, pady=(20, 8))
        ctk.CTkLabel(wrap, text="Add room", font=app.f_head, text_color=MOON).pack(anchor="w")
        app._label(wrap, "Name", anchor="w").pack(anchor="w", pady=(14, 4))
        self.name = ctk.CTkEntry(wrap, height=36, corner_radius=10, fg_color=RAISED, border_width=0,
                                 text_color=MOON, placeholder_text="Bedroom", placeholder_text_color=MUTED,
                                 font=app.f_body)
        self.name.pack(fill="x")
        app._label(wrap, "Lights in this room", anchor="w").pack(anchor="w", pady=(16, 4))
        box = ctk.CTkScrollableFrame(wrap, fg_color=NIGHT, height=min(190, 44 * max(2, len(app.settings["bulbs"]))),
                                     width=380, corner_radius=10)
        box.pack(fill="x")
        self.vars = {}
        for ip in sorted(app.settings["bulbs"]):
            other = app._room_of(ip)
            extra = f"  (now in {other['name']})" if other else ""
            var = ctk.BooleanVar(value=ip in (preselect or []))
            ctk.CTkCheckBox(box, text=app._light_name(ip) + extra, variable=var, font=app.f_body, text_color=MOON,
                            fg_color=FILAMENT, hover_color=FILAMENT_HOVER, border_color=LINE,
                            checkmark_color=NIGHT).pack(anchor="w", pady=4, padx=8)
            self.vars[ip] = var
        self.msg = app._label(wrap, "", muted=True)
        self.msg.pack(anchor="w", pady=(8, 0))
        self.name.bind("<Return>", lambda e: self._save())
        app._center_dialog(self)
        self.after(100, self.name.focus_set)

    def _save(self):
        name = self.name.get().strip()
        if not name:
            self.msg.configure(text="Give the room a name first.", text_color=ROSE)
            return
        ips = [ip for ip, v in self.vars.items() if v.get()]
        room = self.app._save_room(None, name, ips)
        self.destroy()
        self.app._open_detail(f"room:{room['id']}", "Lights")


class ScheduleDialog(ctk.CTkToplevel):
    """Add or edit one schedule for a light, a room, or all lights."""

    def __init__(self, app, existing=None, key="all"):
        super().__init__(app, fg_color=SURFACE)
        self.app = app
        self.sc = copy.deepcopy(existing) if existing else {
            "id": f"s{int(time.time() * 1000)}", "enabled": True, "time": "07:00", "days": [0, 1, 2, 3, 4],
            "target": key, "action": "Wake-up fade", "value": {"minutes": 30}}
        self.title("Edit schedule" if existing else "Add schedule")
        self.resizable(False, False)
        self.transient(app)
        self.after(50, self.grab_set)
        f = app
        app._dialog_icon(self)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(0, 20), side="bottom")
        f._button(btns, "Save schedule", self._save, width=130, primary=True).pack(side="right")
        f._button(btns, "Cancel", self.destroy, width=90).pack(side="right", padx=8)

        wrap = ctk.CTkFrame(self, fg_color="transparent", width=520)
        wrap.pack(fill="both", expand=True, padx=24, pady=(20, 8))
        ctk.CTkLabel(wrap, text="Edit schedule" if existing else "Add schedule", font=f.f_head,
                     text_color=MOON).pack(anchor="w")

        h, m = (int(x) for x in self.sc["time"].split(":"))
        row = self._row(wrap, "Time")
        self.hour = f._menu(row, [str(i) for i in range(1, 13)], lambda v: None, width=70)
        self.hour.set(str((h % 12) or 12))
        self.hour.pack(side="left")
        self.minute = f._menu(row, [f"{i:02d}" for i in range(0, 60)], lambda v: None, width=76)
        self.minute.set(f"{m:02d}")
        self.minute.pack(side="left", padx=6)
        self.ampm = f._segmented(row, ["AM", "PM"], lambda v: None, "AM" if h < 12 else "PM")
        self.ampm.pack(side="left", padx=6)

        row = self._row(wrap, "Days")
        self.day_vars = []
        for i, d in enumerate(DAY_NAMES):
            var = ctk.BooleanVar(value=i in self.sc["days"])
            ctk.CTkCheckBox(row, text=d, variable=var, width=58, font=f.f_small, text_color=MOON,
                            fg_color=FILAMENT, hover_color=FILAMENT_HOVER, border_color=LINE,
                            checkmark_color=NIGHT, checkbox_width=18, checkbox_height=18).pack(side="left")
            self.day_vars.append(var)

        row = self._row(wrap, "Lights")
        self.choices = app._target_choices()
        labels = [c[0] for c in self.choices]
        cur = app._norm_key(self.sc.get("target"))
        self.target = f._menu(row, labels, lambda v: None, width=220)
        self.target.set(next((lbl for lbl, k in self.choices if k == cur), labels[0]))
        self.target.pack(side="left")

        row = self._row(wrap, "Action")
        self.action = f._menu(row, ACTIONS, lambda v: self._build_value(), width=220)
        self.action.set(self.sc["action"])
        self.action.pack(side="left")

        self.value_box = ctk.CTkFrame(wrap, fg_color="transparent", width=500, height=150)
        self.value_box.pack(fill="x", pady=(8, 0))
        self.value_box.pack_propagate(False)   # fixed height, so changing the action never resizes the window
        self.values = {}
        self._build_value()
        app._center_dialog(self)

    def _row(self, parent, label):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(12, 0))
        self.app._label(row, label, width=70, anchor="w").pack(side="left")
        return row

    def _value_slider(self, label, key, lo, hi, steps, default, fmt):
        row = ctk.CTkFrame(self.value_box, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure(1, weight=1)
        self.app._label(row, label, width=110, anchor="w").grid(row=0, column=0, sticky="w")
        v0 = self.sc.get("value", {}).get(key, default)
        lbl = ctk.CTkLabel(row, text=fmt.format(v0), width=70, anchor="e", font=self.app.f_num,
                           text_color=FILAMENT)
        lbl.grid(row=0, column=2)
        var = {"v": v0}

        def ch(v):
            var["v"] = int(round(v))
            lbl.configure(text=fmt.format(var["v"]))

        sl = self.app._slider_widget(row, lo, hi, steps, ch)
        sl.set(v0)
        sl.grid(row=0, column=1, sticky="ew", padx=10)
        self.values[key] = var

    def _build_value(self):
        for w in self.value_box.winfo_children():
            w.destroy()
        self.values = {}
        a = self.action.get()
        if a in ("Turn on", "Set white", "Set color", "Play scene"):
            self._value_slider("Brightness", "brightness", 10, 100, 90, 80, "{}%")
        if a == "Set white":
            self._value_slider("White", "kelvin", MIN_KELVIN, MAX_KELVIN, 43, 3000, "{}K")
        if a == "Set color":
            row = ctk.CTkFrame(self.value_box, fg_color="transparent")
            row.pack(fill="x", pady=(10, 0))
            self.app._label(row, "Color", width=110, anchor="w").pack(side="left")
            cur = tuple(self.sc.get("value", {}).get("color", QUICK_COLORS[1]))
            self.values["color"] = {"v": list(cur)}
            self._color_btns = []
            for rgb in QUICK_COLORS:
                b = ctk.CTkButton(row, text="", width=30, height=30, corner_radius=15, fg_color=hex_color(rgb),
                                  hover_color=hex_color(rgb), border_color=MOON,
                                  border_width=3 if tuple(rgb) == cur else 0,
                                  command=lambda c=rgb: self._pick_color(c))
                b.pack(side="left", padx=2)
                self._color_btns.append((rgb, b))
        if a == "Play scene":
            row = ctk.CTkFrame(self.value_box, fg_color="transparent")
            row.pack(fill="x", pady=(10, 0))
            self.app._label(row, "Scene", width=110, anchor="w").pack(side="left")
            names = [SCENES[i] for i in sorted(SCENES)]
            menu = self.app._menu(row, names, lambda v: None, width=200)
            menu.set(SCENES.get(self.sc.get("value", {}).get("scene", 1), "Ocean"))
            menu.pack(side="left", padx=10)
            self.values["scene"] = {"menu": menu}
            self._value_slider("Speed", "speed", 10, 200, 19, 100, "{}%")
        if a in ("Wake-up fade", "Bedtime fade"):
            self._value_slider("Fade length", "minutes", 5, 60, 11, 30, "{} min")
            note = ("Starts at this time with dim warm light and brightens to daylight."
                    if a == "Wake-up fade" else "Starts at this time, dims slowly and turns the lights off.")
            self.app._label(self.value_box, note, muted=True, wraplength=480, justify="left").pack(anchor="w",
                                                                                                pady=(8, 0))

    def _pick_color(self, rgb):
        self.values["color"]["v"] = list(rgb)
        for c, b in self._color_btns:
            b.configure(border_width=3 if c == rgb else 0)

    def _save(self):
        h = int(self.hour.get()) % 12 + (12 if self.ampm.get() == "PM" else 0)
        days = [i for i, v in enumerate(self.day_vars) if v.get()]
        value = {}
        for k, v in self.values.items():
            value[k] = SCENE_IDS[v["menu"].get()] if "menu" in v else v["v"]
        target = dict(self.choices).get(self.target.get(), "all")
        self.sc.update({"time": f"{h:02d}:{int(self.minute.get()):02d}", "days": days,
                        "target": target, "action": self.action.get(), "value": value})
        self.app._save_schedule(self.sc)
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


def claim_single_instance():
    """
    Returns a listening socket if this is the only WiZard running.
    If another WiZard is running, asks it to show its window and returns None.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        srv.listen(2)
        return srv
    except OSError:
        srv.close()
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=1) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None


def listen_for_show(srv, app):
    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16).startswith(b"show"):
                        app.ui_queue.put(app._show)
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()


def main():
    import signal
    srv = claim_single_instance()
    if srv is None:
        return   # the running WiZard has been brought to the front
    app = App(autostart="--autostart" in sys.argv)
    listen_for_show(srv, app)
    if app.settings.get("launch_at_login"):
        try:
            set_launch_at_login(True)   # keep the login entry pointing at this copy of the app
        except OSError:
            pass
    # Ctrl+C in the terminal: exit cleanly (stop syncing, restore bulbs) instead of breaking the app.
    signal.signal(signal.SIGINT, lambda *_: app.ui_queue.put(app._quit))
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app._quit()


if __name__ == "__main__":
    main()

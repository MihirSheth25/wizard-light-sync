"""
The app logic behind WiZard's window: lights, rooms, scenes, schedules, sleep timers,
presets and sync. It has no user interface. The window talks to it through Api methods
and gets changes back through the emit callback.

Copyright (c) 2026 Mihir Sheth. MIT License, see LICENSE.
"""

import copy
import socket
import threading
import time

import wiz_core as W


class Controller:
    LIVE_FPS = 12          # how often the window gets bulb colors while syncing

    def __init__(self, emit=lambda kind, data: None):
        self.emit = emit
        self.settings = W.load_settings()
        self.lock = threading.RLock()
        self.engine = None
        self.fade_job = None
        self.fade_label = ""
        self.sleep_timers = {}            # target key -> time its lights go off
        self.display = {}                 # ip -> (rgb or None, dim, words) when nothing is syncing
        self.current_scene = None         # (scene id, target key) while a scene plays from the app
        self.status = ""
        self.side_display = None          # "left" or "right" when Windows shows a second display
        self.visible = True               # is the window showing; work for it pauses when hidden
        self.quitting = False
        self.discovering = False
        self._fired = set()
        self._save_timer = None
        self._state_timer = None
        self._pending_cmd = None
        self._cmd_timer = None
        self._wake = threading.Event()    # wakes the clock thread early (new schedule, sleep timer)
        self._wake_live = threading.Event()
        self._last_mode = "color"
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.settimeout(0.5)
        threading.Thread(target=self._clock, daemon=True).start()
        threading.Thread(target=self._live, daemon=True).start()
        threading.Thread(target=self._watch_displays, daemon=True).start()
        if self.settings["bulbs"]:
            self.poll_states()

    # ------------------------------------------------------------ settings
    def get_settings(self):
        with self.lock:
            return copy.deepcopy(self.settings)

    def save_soon(self):
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, lambda: W.save_settings(self.get_settings()))
        self._save_timer.daemon = True
        self._save_timer.start()

    def changed(self):
        """Tell the window to refresh, at most once every 40 ms."""
        if self._state_timer and self._state_timer.is_alive():
            return
        self._state_timer = threading.Timer(0.04, lambda: self.emit("state", self.snapshot()))
        self._state_timer.daemon = True
        self._state_timer.start()

    def set_setting(self, key, value):
        if key not in W.DEFAULTS or key in ("bulbs", "rooms", "schedules", "screen_presets",
                                            "music_profiles", "music_user_defaults"):
            return False
        with self.lock:
            if key == "music_mode":
                # switching style: bring back that style's own settings
                if value not in W.MUSIC_MODES:
                    return False
                self.settings.update(copy.deepcopy(self.settings["music_profiles"].get(value, {})))
            elif key in W.MUSIC_KEYS:
                self.settings["music_profiles"].setdefault(self.settings["music_mode"], {})[key] = value
            self.settings[key] = value
        self.save_soon()
        if key == "launch_at_login":
            try:
                W.set_launch_at_login(bool(value))
            except OSError as exc:
                self.say(f"Could not change the login setting: {exc}")
        if key == "sync_source" and self.syncing() and self.engine.kind != value:
            self._switch_engine()
        if key in ("scene_speed", "scene_brightness") and self.current_scene and not self.syncing():
            self._send_scene(*self.current_scene, throttle=True)
        self.changed()
        return True

    def say(self, text):
        self.status = text
        self.emit("toast", text)

    # ------------------------------------------------------------ targets
    def rooms(self):
        return self.settings.setdefault("rooms", [])

    def room(self, rid):
        return next((r for r in self.rooms() if r["id"] == rid), None)

    def room_of(self, ip):
        return next((r for r in self.rooms() if ip in r["lights"]), None)

    def light_name(self, ip):
        return self.settings["bulbs"].get(ip, {}).get("name", ip)

    def norm_key(self, key):
        if key in ("all", "All bulbs", "All lights") or not key:
            return "all"
        if key.startswith(("light:", "room:")):
            return key
        for ip, b in self.settings["bulbs"].items():   # an older schedule that stored a bulb name
            if b.get("name") == key:
                return f"light:{ip}"
        return "all"

    def ips_for(self, key):
        key = self.norm_key(key)
        if key == "all":
            return sorted(self.settings["bulbs"])
        if key.startswith("light:"):
            ip = key[6:]
            return [ip] if ip in self.settings["bulbs"] else []
        room = self.room(key[5:])
        return [ip for ip in (room["lights"] if room else []) if ip in self.settings["bulbs"]]

    def key_name(self, key):
        key = self.norm_key(key)
        if key == "all":
            return "All lights"
        if key.startswith("light:"):
            return self.light_name(key[6:])
        room = self.room(key[5:])
        return room["name"] if room else "Deleted room"

    # ------------------------------------------------------------ what the window shows
    def look(self, ip):
        """(rgb or None, dimming, words) for a light right now."""
        if self.syncing() and self.settings["bulbs"].get(ip, {}).get("enabled", True):
            col = self.engine.current.get(ip)
            if col and col[3]:
                return tuple(col[:3]), col[3], "Syncing"
            return None, 0, "Syncing"
        return self.display.get(ip, (None, 0, ""))

    def live_colors(self):
        out = {}
        for ip in self.settings["bulbs"]:
            rgb, dim, words = self.look(ip)
            out[ip] = [list(rgb) if rgb else None, dim, words]
        return out

    def snapshot(self):
        with self.lock:
            s = copy.deepcopy(self.settings)
        lights = []
        for ip in sorted(s["bulbs"]):
            b = s["bulbs"][ip]
            rgb, dim, words = self.look(ip)
            room = self.room_of(ip)
            lights.append({"ip": ip, "name": b.get("name", ip), "zone": b.get("zone", "Full screen"),
                           "enabled": b.get("enabled", True), "scale": b.get("brightness_scale", 100),
                           "room": room["id"] if room else None,
                           "rgb": list(rgb) if rgb else None, "dim": dim, "words": words or "",
                           "side": W_side(b.get("zone", "Full screen"), len(lights))})
        eng = self.engine if self.syncing() else None
        settings = {k: v for k, v in s.items() if k not in ("bulbs", "rooms", "schedules", "music_profiles",
                                                            "music_user_defaults")}
        return {
            "settings": settings,
            "lights": lights,
            "rooms": s["rooms"],
            "schedules": [dict(sc, summary=W.action_text(sc), when=f"{W.time_text(sc['time'])}, "
                                                             f"{W.days_text(sc['days'])}",
                               target_name=self.key_name(sc.get("target")))
                          for sc in sorted(s["schedules"], key=lambda x: x["time"])],
            "sync": {"on": bool(eng), "source": s["sync_source"],
                     "kind": eng.kind if eng else None,
                     "fps": round(eng.actual_fps, 1) if eng else 0,
                     "bpm": round(getattr(eng, "bpm", 0) or 0) if eng else 0,
                     "error": self.engine.error if self.engine and self.engine.error else None},
            "fade": self.fade_label if self.fade_job and self.fade_job.is_alive() else "",
            "sleep": {k: v for k, v in self.sleep_timers.items()},
            "scene_playing": list(self.current_scene) if self.current_scene else None,
            "preset_changed": self.preset_changed(),
            "music_changed": self.music_changed(),
            "side_display": self.side_display,
            "status": self.status,
            "discovering": self.discovering,
        }

    def static_info(self):
        """Things that never change while the app runs."""
        return {
            "scenes": [{"id": sid, "name": name, "look": [list(c) for c in W.SCENE_LOOK[sid]],
                        "moving": sid not in W.STATIC_SCENES} for sid, name in W.SCENES.items()],
            "scene_groups": [[g, ids] for g, ids in W.SCENE_GROUPS],
            "zones": W.ZONES, "color_modes": W.COLOR_MODES, "brightness_modes": W.BRIGHTNESS_MODES,
            "dark_scene": W.DARK_SCENE, "music_modes": W.MUSIC_MODES, "palettes": list(W.MUSIC_PALETTES),
            "actions": W.ACTIONS, "quick_colors": [list(c) for c in W.QUICK_COLORS],
            "quick_whites": W.QUICK_WHITES, "kelvin": [W.MIN_KELVIN, W.MAX_KELVIN],
            "defaults": {k: v for k, v in W.DEFAULTS.items() if not isinstance(v, (dict, list))},
            "screen_keys": list(W.SCREEN_KEYS), "settings_file": W.SETTINGS_FILE,
            "soundcard": soundcard_ok(),
        }

    # ------------------------------------------------------------ sending commands
    def take_over(self):
        """Direct control wins: stop any sync or fade first."""
        if self.fade_job and self.fade_job.is_alive():
            self.fade_job.cancel()
        if self.syncing():
            self.stop_sync(restore=False)

    def command(self, ips, cmd, status, throttle=False, keep_scene=False):
        params, look = cmd
        self.take_over()
        if not keep_scene:
            self.current_scene = None
        for ip in ips:
            self.display[ip] = look
        self.status = status
        if throttle:     # dragging a slider or the wheel: at most about 15 sends a second
            self._pending_cmd = (list(ips), dict(params))
            if not (self._cmd_timer and self._cmd_timer.is_alive()):
                self._cmd_timer = threading.Timer(0.066, self._flush_cmd)
                self._cmd_timer.daemon = True
                self._cmd_timer.start()
        else:
            self._pending_cmd = None
            for ip in ips:
                W.bulb_send(self.cmd_sock, ip, params)
        self.changed()

    def _flush_cmd(self):
        pending, self._pending_cmd = self._pending_cmd, None
        if pending:
            ips, params = pending
            for ip in ips:
                W.bulb_send(self.cmd_sock, ip, params)

    def poll_states(self, ips=None):
        """Ask lights how they look right now, in the background."""
        ips = list(ips or self.settings["bulbs"])

        def work():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.4)
            found = {ip: W.state_to_display(W.bulb_get_state(sock, ip)) for ip in ips}
            sock.close()
            self.display.update(found)
            self.changed()

        threading.Thread(target=work, daemon=True).start()

    def power(self, key, on):
        ips, name = self.ips_for(key), self.key_name(key)
        if not on:
            self.command(ips, W.OFF_CMD, f"{name}: off")
            return
        s = self.settings
        dim = s["control_brightness"]
        mode = self._last_mode
        cmd = W.white_cmd(s["control_kelvin"], dim) if mode == "white" else W.color_cmd(s["control_color"], dim)
        self.command(ips, cmd, f"{name}: on")

    def set_color(self, key, rgb, final=True):
        with self.lock:
            self.settings["control_color"] = [int(x) for x in rgb[:3]]
        self._last_mode = "color"
        self.save_soon()
        self.command(self.ips_for(key), W.color_cmd(rgb, self.settings["control_brightness"]),
                     f"{self.key_name(key)}: color", throttle=not final)

    def set_white(self, key, kelvin, final=True):
        k = int(round(int(kelvin) / 100) * 100)
        with self.lock:
            self.settings["control_kelvin"] = k
        self._last_mode = "white"
        self.save_soon()
        self.command(self.ips_for(key), W.white_cmd(k, self.settings["control_brightness"]),
                     f"{self.key_name(key)}: white", throttle=not final)

    def set_brightness(self, key, dim, final=True):
        dim = int(max(10, min(100, dim)))
        with self.lock:
            self.settings["control_brightness"] = dim
        self.save_soon()
        ips = self.ips_for(key)
        look = self.display.get(ips[0], (None, dim, "")) if ips else (None, dim, "")
        self.command(ips, ({"state": True, "dimming": dim}, (look[0] or (255, 200, 140), dim, look[2] or "On")),
                     f"{self.key_name(key)}: brightness", throttle=not final, keep_scene=True)

    def set_light_brightness(self, ip, dim, final=True):
        self.set_brightness(f"light:{ip}", dim, final)

    def play_scene(self, key, sid):
        self._send_scene(int(sid), self.norm_key(key))

    def _send_scene(self, sid, key, throttle=False):
        s = self.settings
        cmd = W.scene_cmd(sid, s["scene_brightness"], s["scene_speed"])
        self.command(self.ips_for(key), cmd, f"{self.key_name(key)}: {W.SCENES[sid]}", throttle, keep_scene=True)
        self.current_scene = (sid, key)

    def identify(self, ip):
        """Flash the light warm white at 50% once. Works while syncing too."""
        engine = self.engine if self.syncing() else None
        flash_for = 0.7
        if engine:
            engine.hold_until[ip] = time.time() + flash_for + 0.1

        def work():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            state = None if engine else W.bulb_get_state(sock, ip)
            W.bulb_send(sock, ip, W.white_cmd(2700, 50)[0])
            time.sleep(flash_for)
            if engine:
                engine.hold_until.pop(ip, None)
            else:
                params = W.restore_params(state)
                if params:
                    W.bulb_send(sock, ip, params)
            sock.close()

        threading.Thread(target=work, daemon=True).start()
        self.say(f"{self.light_name(ip)} flashed warm white")

    # ------------------------------------------------------------ lights
    def discover(self):
        if self.discovering:
            return
        self.discovering = True
        self.changed()

        def work():
            found = W.discover_bulbs()
            with self.lock:
                known = self.settings["bulbs"]
                new = [ip for ip in found if ip not in known]
                for ip in sorted(new):
                    known[ip] = {"name": f"Light {len(known) + 1}", "enabled": True,
                                 "zone": "Full screen", "brightness_scale": 100}
            for ip, st in found.items():
                self.display[ip] = W.state_to_display(st)
            self.discovering = False
            self.save_soon()
            if found:
                self.say(f"Found {len(found)} lights, {len(new)} of them new.")
            else:
                self.say("No lights answered. Check that this PC and the lights are on the same Wi-Fi, "
                         "and that local communication is on in the WiZ app.")
            self.changed()

        threading.Thread(target=work, daemon=True).start()

    def add_ip(self, ip):
        ip = (ip or "").strip()
        try:
            socket.inet_aton(ip)
            if ip.count(".") != 3:
                raise OSError
        except OSError:
            return {"ok": False, "message": f"{ip or 'That'} is not an IP address. It should look like 192.168.1.50."}
        with self.lock:
            self.settings["bulbs"].setdefault(ip, {"name": f"Light {len(self.settings['bulbs']) + 1}",
                                                    "enabled": True, "zone": "Full screen",
                                                    "brightness_scale": 100})
        self.save_soon()
        self.poll_states([ip])
        self.changed()
        return {"ok": True, "message": f"Added {ip}."}

    def set_light(self, ip, key, value):
        if key not in ("name", "zone", "enabled", "brightness_scale"):
            return False
        if key == "name":
            value = " ".join(str(value).split())[:40] or ip
        with self.lock:
            if ip not in self.settings["bulbs"]:
                return False
            self.settings["bulbs"][ip][key] = value
        self.save_soon()
        self.changed()
        return True

    def remove_light(self, ip):
        with self.lock:
            self.settings["bulbs"].pop(ip, None)
            for r in self.rooms():
                if ip in r["lights"]:
                    r["lights"].remove(ip)
        self.display.pop(ip, None)
        self.save_soon()
        self.changed()

    def set_light_room(self, ip, rid):
        with self.lock:
            for r in self.rooms():
                if ip in r["lights"]:
                    r["lights"].remove(ip)
            room = self.room(rid) if rid else None
            if room:
                room["lights"].append(ip)
        self.save_soon()
        self.changed()

    # ------------------------------------------------------------ rooms
    def save_room(self, rid, name, ips):
        name = " ".join(str(name).split())[:40] or "Room"
        with self.lock:
            room = self.room(rid) if rid else None
            if not room:
                room = {"id": f"r{int(time.time() * 1000)}", "name": name, "lights": []}
                self.rooms().append(room)
            room["name"] = name
            for r in self.rooms():   # a light lives in one room
                r["lights"] = [ip for ip in r["lights"] if ip not in ips]
            room["lights"] = [ip for ip in ips if ip in self.settings["bulbs"]]
        self.save_soon()
        self.changed()
        return room["id"]

    def rename_room(self, rid, name):
        with self.lock:
            room = self.room(rid)
            if room:
                room["name"] = " ".join(str(name).split())[:40] or room["name"]
        self.save_soon()
        self.changed()

    def delete_room(self, rid):
        with self.lock:
            self.settings["rooms"] = [r for r in self.rooms() if r["id"] != rid]
        self.save_soon()
        self.changed()

    # ------------------------------------------------------------ schedules, fades, sleep
    def save_schedule(self, sc):
        sc = dict(sc)
        sc.setdefault("id", f"s{int(time.time() * 1000)}")
        sc["days"] = sorted({int(d) for d in sc.get("days", [])})
        sc.setdefault("enabled", True)
        with self.lock:
            items = [x for x in self.settings["schedules"] if x["id"] != sc["id"]]
            items.append(sc)
            self.settings["schedules"] = items
        self.save_soon()
        self._wake.set()
        self.changed()
        return sc["id"]

    def toggle_schedule(self, sid, on):
        with self.lock:
            for x in self.settings["schedules"]:
                if x["id"] == sid:
                    x["enabled"] = bool(on)
        self.save_soon()
        self.changed()

    def delete_schedule(self, sid):
        with self.lock:
            self.settings["schedules"] = [x for x in self.settings["schedules"] if x["id"] != sid]
        self.save_soon()
        self.changed()

    def run_action(self, sc):
        a, v = sc["action"], sc.get("value", {})
        key = self.norm_key(sc.get("target"))
        ips, name = self.ips_for(key), self.key_name(key)
        dim = v.get("brightness", 80)
        cmd = {"Turn on": lambda: W.on_cmd(dim),
               "Turn off": lambda: W.OFF_CMD,
               "Set white": lambda: W.white_cmd(v.get("kelvin", 3000), dim),
               "Set color": lambda: W.color_cmd(v.get("color", (255, 140, 60)), dim),
               "Play scene": lambda: W.scene_cmd(v.get("scene", 1), dim, v.get("speed", 100))}.get(a)
        if cmd:
            self.command(ips, cmd(), f"Schedule: {name}: {W.action_text(sc).lower()}")
        if a == "Wake-up fade":
            self.start_fade(ips, v.get("minutes", 30), 10, 100, 2200, 5000, False, f"{name}: wake-up fade")
        elif a == "Bedtime fade":
            self.start_fade(ips, v.get("minutes", 30), 60, 10, 2700, 2200, True, f"{name}: bedtime fade")
        elif a in ("Start screen sync", "Start music sync"):
            self.set_setting("sync_source", "Screen" if "screen" in a else "Music")
            self.start_sync()
        elif a == "Stop sync":
            self.stop_sync()

    def start_fade(self, ips, minutes, d0, d1, k0, k1, off, label):
        self.take_over()
        self.fade_label = label

        def done():
            for ip in ips:
                self.display[ip] = (None, 0, "Off") if off else (W.kelvin_to_rgb(k1), d1, f"White {k1}K")
            self.say("Lights off" if off else "Wake-up fade finished")
            self.changed()

        self.fade_job = W.FadeJob(ips, minutes, d0, d1, k0, k1, turn_off_at_end=off, on_done=done)
        self.fade_job.start()
        self.say(label)
        self.changed()

    def set_sleep(self, key, minutes):
        key = self.norm_key(key)
        if minutes in (None, 0, "0", ""):
            self.sleep_timers.pop(key, None)
        else:
            self.sleep_timers[key] = time.time() + float(minutes) * 60
            when = time.strftime("%I:%M %p", time.localtime(self.sleep_timers[key])).lstrip("0")
            self.say(f"{self.key_name(key)} will turn off at {when}")
        self._wake.set()
        self.changed()

    def _sleep_now(self, key):
        self.sleep_timers.pop(key, None)
        ips = self.ips_for(key)
        self.start_fade(ips, 1, 50, 10, 2700, 2200, True, f"{self.key_name(key)}: sleep timer")

    def _clock(self):
        """Runs schedules and sleep timers. Sleeps until the next minute or the next deadline."""
        while not self.quitting:
            now = time.time()
            lt = time.localtime(now)
            hhmm, today = f"{lt.tm_hour:02d}:{lt.tm_min:02d}", time.strftime("%Y-%m-%d", lt)
            for sc in list(self.settings["schedules"]):
                key = (sc["id"], today, hhmm)
                if sc.get("enabled", True) and sc["time"] == hhmm and lt.tm_wday in sc["days"] \
                        and key not in self._fired:
                    self._fired.add(key)
                    try:
                        self.run_action(sc)
                    except Exception as exc:
                        self.say(f"A schedule could not run: {exc}")
            for key, dl in list(self.sleep_timers.items()):
                if now >= dl:
                    self._sleep_now(key)
            wait = 60.5 - lt.tm_sec
            if self.sleep_timers:
                wait = min(wait, max(0.2, min(self.sleep_timers.values()) - now))
            self._wake.wait(wait)
            self._wake.clear()

    # ------------------------------------------------------------ sync
    def syncing(self):
        return bool(self.engine and self.engine.is_alive())

    def start_sync(self, source=None):
        if source in ("Screen", "Music"):
            self.set_setting("sync_source", source)
        if self.syncing():
            return {"ok": True}
        if not any(b.get("enabled", True) for b in self.settings["bulbs"].values()):
            return {"ok": False, "message": "Turn on Sync for at least one light first."}
        if self.fade_job and self.fade_job.is_alive():
            self.fade_job.cancel()
        self.current_scene = None
        cls = W.MusicEngine if self.settings["sync_source"] == "Music" else W.ScreenEngine
        self.engine = cls(self.get_settings)
        self.engine.start()
        threading.Thread(target=self._watch_engine, args=(self.engine,), daemon=True).start()
        self.status = f"{self.settings['sync_source']} sync on"
        self._wake_live.set()
        self.emit("sync", True)
        self.changed()
        return {"ok": True}

    def _switch_engine(self):
        """Switching between screen and music while syncing: hand over to the other engine at once.
        The new engine keeps the light states saved before sync began, so stopping still restores them."""
        old = self.engine
        saved = dict(old.saved_states)
        old.saved_states = {}
        old.stop()
        old.join(timeout=3)
        cls = W.MusicEngine if self.settings["sync_source"] == "Music" else W.ScreenEngine
        self.engine = cls(self.get_settings)
        self.engine.saved_states = saved
        self.engine.start()
        threading.Thread(target=self._watch_engine, args=(self.engine,), daemon=True).start()
        self.status = f"{self.settings['sync_source']} sync on"
        self.emit("sync", True)

    def stop_sync(self, restore=True):
        eng = self.engine
        if eng:
            if not restore:
                eng.saved_states = {}
            eng.stop()
            eng.join(timeout=3)
        self.status = ""
        self.emit("sync", False)
        if restore:
            threading.Timer(0.4, self.poll_states).start()
        self.changed()

    def toggle_sync(self):
        if self.syncing():
            self.stop_sync()
        else:
            return self.start_sync()

    def _watch_engine(self, eng):
        eng.join()
        if eng is self.engine and eng.error:
            self.say(eng.error)
            self.emit("sync", False)
            self.changed()

    def _live(self):
        """While something moves the lights and the window shows, send their colors about 12 times a second."""
        last = None
        while not self.quitting:
            active = self.visible and (self.syncing() or (self.fade_job and self.fade_job.is_alive()))
            if not active:
                last = None
                self._wake_live.wait(2.0)
                self._wake_live.clear()
                continue
            colors = self.live_colors()
            if colors != last:
                last = colors
                eng = self.engine if self.syncing() else None
                self.emit("live", {"colors": colors,
                                   "fps": round(eng.actual_fps, 1) if eng else 0,
                                   "bpm": round(getattr(eng, "bpm", 0) or 0) if eng else 0})
            time.sleep(1.0 / self.LIVE_FPS)

    def set_visible(self, visible):
        self.visible = bool(visible)
        if visible:
            self._wake_live.set()
            if not self.syncing():
                self.poll_states()

    def _watch_displays(self):
        """While the window shows, check every few seconds whether Windows shows a second display."""
        while not self.quitting:
            if self.visible:
                side = None
                try:
                    import mss
                    grabber = getattr(mss, "MSS", None) or mss.mss
                    with grabber() as sct:
                        mons = sct.monitors[1:]
                    idx = self.settings["monitor"]
                    cap = mons[idx - 1] if 0 < idx <= len(mons) else mons[0]
                    others = [m for m in mons if m is not cap]
                    if others:
                        o = others[0]
                        dx = (o["left"] + o["width"] / 2) - (cap["left"] + cap["width"] / 2)
                        side = "left" if dx < 0 else "right"
                except Exception:
                    side = None
                if side != self.side_display:
                    self.side_display = side
                    self.changed()
            time.sleep(4)

    def monitors(self):
        try:
            import mss
            grabber = getattr(mss, "MSS", None) or mss.mss
            with grabber() as sct:
                mons = sct.monitors[1:]
            return [{"index": i, "label": f"Monitor {i}, {m['width']} x {m['height']}"
                                          + (", main" if m["left"] == 0 and m["top"] == 0 else "")}
                    for i, m in enumerate(mons, start=1)] or [{"index": 1, "label": "Monitor 1"}]
        except Exception:
            return [{"index": 1, "label": "Monitor 1"}]

    # ------------------------------------------------------------ presets
    def screen_values(self):
        with self.lock:
            return {k: copy.deepcopy(self.settings[k]) for k in W.SCREEN_KEYS}

    def preset_changed(self):
        preset = self.settings["screen_presets"].get(self.settings["screen_preset"])
        if preset is None:
            return True
        for k, v in self.screen_values().items():
            a = preset.get(k)
            if isinstance(v, float) or isinstance(a, float):
                if a is None or abs(float(v) - float(a)) > 1e-6:
                    return True
            elif v != a:
                return True
        return False

    def pick_preset(self, name):
        values = self.settings["screen_presets"].get(name)
        if values is None:
            return False
        with self.lock:
            self.settings.update(copy.deepcopy(values))
            self.settings["screen_preset"] = name
        self.save_soon()
        self.changed()
        return True

    def save_preset(self):
        values = self.screen_values()
        with self.lock:
            self.settings["screen_presets"][self.settings["screen_preset"]] = values
        self.save_soon()
        self.changed()

    def create_preset(self, name):
        name = " ".join(str(name).split())[:30]
        if not name:
            return {"ok": False, "message": "Type a name for the preset first."}
        if name in self.settings["screen_presets"]:
            return {"ok": False, "message": f"There is already a preset called {name}."}
        values = self.screen_values()
        with self.lock:
            self.settings["screen_presets"][name] = values
            self.settings["screen_preset"] = name
        self.save_soon()
        self.changed()
        return {"ok": True}

    def delete_preset(self, name):
        if name == "Default":
            return False
        with self.lock:
            self.settings["screen_presets"].pop(name, None)
            if self.settings["screen_preset"] == name:
                self.settings["screen_preset"] = "Default"
        self.save_soon()
        self.changed()
        return True

    def music_default(self, mode=None):
        """The settings Reset to defaults brings back for a music style: yours if you set them, else the app's."""
        mode = mode or self.settings["music_mode"]
        saved = self.settings["music_user_defaults"].get(mode, {})
        return {k: copy.deepcopy(saved.get(k, W.DEFAULTS[k])) for k in W.MUSIC_KEYS}

    def music_changed(self):
        base = self.music_default()
        for k in W.MUSIC_KEYS:
            a, b = self.settings[k], base[k]
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
                if abs(float(a) - float(b)) > 1e-6:
                    return True
            elif a != b:
                return True
        return False

    def set_music_default(self):
        with self.lock:
            mode = self.settings["music_mode"]
            self.settings["music_user_defaults"][mode] = {k: copy.deepcopy(self.settings[k]) for k in W.MUSIC_KEYS}
        self.save_soon()
        self.say(f"Saved as the default for {mode}.")
        self.changed()

    def reset_keys(self, which):
        with self.lock:
            if which == "screen":
                self.settings.update({k: copy.deepcopy(W.DEFAULTS[k]) for k in W.SCREEN_KEYS})
            else:
                values = self.music_default()
                self.settings.update(values)
                self.settings["music_profiles"][self.settings["music_mode"]] = copy.deepcopy(values)
        self.save_soon()
        self.changed()

    def reset_all(self):
        with self.lock:
            keep = {k: self.settings[k] for k in ("bulbs", "schedules", "rooms", "screen_presets",
                                                  "music_user_defaults")}
            self.settings = copy.deepcopy(W.DEFAULTS)
            self.settings.update(keep)
            self.settings["music_profiles"] = {m: self.music_default(m) for m in W.MUSIC_MODES}
            self.settings.update(self.settings["music_profiles"][self.settings["music_mode"]])
            self.settings["bulbs"] = {ip: {"name": b.get("name", ip), "enabled": True,
                                           "zone": "Full screen", "brightness_scale": 100}
                                      for ip, b in keep["bulbs"].items()}
        try:
            W.set_launch_at_login(False)
        except OSError:
            pass
        self.save_soon()
        self.changed()

    # ------------------------------------------------------------ exit
    def shutdown(self):
        """Stop everything, leave every light on Cozy at full brightness, save."""
        if self.quitting:
            return
        self.quitting = True
        self._wake.set()
        self._wake_live.set()
        if self.fade_job and self.fade_job.is_alive():
            self.fade_job.cancel()
        if self.syncing():
            self.stop_sync(restore=False)
        params, _ = W.scene_cmd(W.SCENE_IDS["Cozy"], 100, self.settings["scene_speed"])
        for _ in range(2):          # UDP can drop a packet, so send it twice
            for ip in self.settings["bulbs"]:
                W.bulb_send(self.cmd_sock, ip, params)
            time.sleep(0.05)
        if self._save_timer:
            self._save_timer.cancel()
        W.save_settings(self.get_settings())


def W_side(zone, index):
    """Which side of the monitor a light sits on in the room picture."""
    if "left" in zone.lower():
        return "left"
    if "right" in zone.lower():
        return "right"
    return "left" if index % 2 == 0 else "right"


def soundcard_ok():
    try:
        import importlib.util
        return importlib.util.find_spec("soundcard") is not None
    except Exception:
        return False

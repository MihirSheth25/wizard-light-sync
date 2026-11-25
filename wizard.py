"""
WiZard
A Windows desktop app for WiZ-compatible smart bulbs (such as Philips Smart LED connected by WiZ):
screen sync, music sync, manual control, scenes, rooms, schedules and a sleep timer.
The window is a web page (ui folder) shown by Windows' built-in WebView2 through pywebview.
The bulbs are driven by wiz_core.py and wiz_controller.py.

Unofficial. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.

Run:   python wizard.py
Needs: pip install -r requirements.txt

Copyright (c) 2026 Mihir Sheth. MIT License, see LICENSE.
"""

import json
import os
import queue
import signal
import socket
import sys
import threading

import wiz_core as W
from wiz_controller import Controller

APP_NAME = "WiZard"
SINGLE_INSTANCE_PORT = 38917   # only one WiZard runs at a time; a second launch opens the first
IS_WINDOWS = sys.platform == "win32"


def resource(*parts):
    """A file shipped with the app: inside the built .exe, or next to this script."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


# ======================================================================= Windows process tuning
# From the BatteryTray notes: stay out of the way of games, and give memory back when idle.

def set_below_normal_priority():
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        except Exception:
            pass


def set_efficiency_mode(on):
    """Windows 11 efficiency mode (EcoQoS). On while idle in the tray, off while syncing."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        class PPTS(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]

        st = PPTS(1, 1, 1 if on else 0)
        k32 = ctypes.windll.kernel32
        k32.SetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st))
    except Exception:
        pass


def trim_memory():
    """Hand unused memory back to Windows. Only at quiet moments, never in a loop."""
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
        except Exception:
            pass


# ======================================================================= the bridge the page calls

class Api:
    """
    Every public method here can be called from the page as pywebview.api.<name>(...).
    Attributes start with an underscore so pywebview does not try to expose them.
    """

    def __init__(self, ctl, host):
        self._ctl = ctl
        self._host = host

    # startup and state
    def init(self):
        self._host.page_ready()
        return {"static": self._ctl.static_info(), "state": self._ctl.snapshot(),
                "monitors": self._ctl.monitors(), "tray": self._host.tray_ok}

    def state(self):
        return self._ctl.snapshot()

    def monitors(self):
        return self._ctl.monitors()

    # settings
    def set_setting(self, key, value):
        return self._ctl.set_setting(key, value)

    # lights
    def discover(self):
        self._ctl.discover()

    def add_ip(self, ip):
        return self._ctl.add_ip(ip)

    def set_light(self, ip, key, value):
        return self._ctl.set_light(ip, key, value)

    def remove_light(self, ip):
        self._ctl.remove_light(ip)

    def identify(self, ip):
        self._ctl.identify(ip)

    def set_light_room(self, ip, rid):
        self._ctl.set_light_room(ip, rid)

    # control
    def power(self, key, on):
        self._ctl.power(key, bool(on))

    def set_color(self, key, rgb, final=True):
        self._ctl.set_color(key, rgb, final)

    def set_white(self, key, kelvin, final=True):
        self._ctl.set_white(key, kelvin, final)

    def set_brightness(self, key, dim, final=True):
        self._ctl.set_brightness(key, dim, final)

    def play_scene(self, key, sid):
        self._ctl.play_scene(key, sid)

    def set_sleep(self, key, minutes):
        self._ctl.set_sleep(key, minutes)

    # rooms
    def save_room(self, rid, name, ips):
        return self._ctl.save_room(rid, name, ips)

    def rename_room(self, rid, name):
        self._ctl.rename_room(rid, name)

    def delete_room(self, rid):
        self._ctl.delete_room(rid)

    # schedules
    def save_schedule(self, sc):
        return self._ctl.save_schedule(sc)

    def toggle_schedule(self, sid, on):
        self._ctl.toggle_schedule(sid, on)

    def delete_schedule(self, sid):
        self._ctl.delete_schedule(sid)

    # sync
    def start_sync(self, source=None):
        return self._ctl.start_sync(source)

    def stop_sync(self):
        self._ctl.stop_sync()

    # presets and resets
    def pick_preset(self, name):
        return self._ctl.pick_preset(name)

    def save_preset(self):
        self._ctl.save_preset()

    def create_preset(self, name):
        return self._ctl.create_preset(name)

    def delete_preset(self, name):
        return self._ctl.delete_preset(name)

    def set_music_default(self):
        self._ctl.set_music_default()

    def reset(self, which):
        if which == "all":
            self._ctl.reset_all()
        else:
            self._ctl.reset_keys(which)

    # app
    def quit(self):
        threading.Thread(target=self._host.quit, daemon=True).start()


class TrayApi:
    """What the themed tray menu (ui/tray.html) can do."""

    def __init__(self, host):
        self._host = host

    def state(self):
        ctl = self._host.ctl
        eng = ctl.engine if ctl.syncing() else None
        colors = []
        for ip in sorted(ctl.settings["bulbs"]):
            rgb = ctl.look(ip)[0]
            colors.append(",".join(str(int(v)) for v in rgb) if rgb else None)
        return {"syncing": bool(eng), "kind": eng.kind if eng else None, "source": ctl.settings["sync_source"],
                "colors": colors[:6], "on": sum(1 for c in colors if c), "total": len(colors)}

    def open(self):
        self.hide()
        self._host.show()

    def toggle_sync(self):
        ctl = self._host.ctl
        if ctl.syncing():
            ctl.stop_sync()
        else:
            result = ctl.start_sync()
            if result and not result.get("ok"):
                self._host.show()
                ctl.say(result.get("message", ""))
        self.hide()

    def all_off(self):
        self._host.ctl.power("all", False)
        self.hide()

    def quit(self):
        self.hide()
        threading.Thread(target=self._host.quit, daemon=True).start()

    def hide(self):
        menu = self._host.menu_win
        if menu:
            threading.Thread(target=menu.hide, daemon=True).start()


# ======================================================================= host: window, tray, pushes

class Host:
    def __init__(self, autostart=False):
        self.autostart = autostart
        self.window = None
        self.menu_win = None          # the themed tray menu, created hidden at startup
        self._menu_ready = False
        self.tray = None
        self.tray_ok = False
        self.ready = False            # the page has loaded and asked for its first state
        self.shown = not autostart
        self.outbox = queue.Queue()
        self.ctl = Controller(emit=self.emit)
        self._notified = False
        self._stale = False
        threading.Thread(target=self._sender, daemon=True).start()

    # pushes to the page go through one thread, so nobody waits on the window
    def emit(self, kind, data):
        if kind == "sync":
            self._update_tray()
            self._tune_process()
        if not self.ready or not self.window:
            return
        if not self.shown and kind in ("live", "state"):
            self._stale = True
            return
        self.outbox.put((kind, data))

    def _sender(self):
        while True:
            kind, data = self.outbox.get()
            latest = {kind: data}
            order = [kind]
            while True:                       # keep only the newest live and state messages
                try:
                    k, d = self.outbox.get_nowait()
                except queue.Empty:
                    break
                if k not in latest:
                    order.append(k)
                latest[k] = d
            for k in order:
                try:
                    payload = json.dumps(latest[k], separators=(",", ":"))
                    self.window.run_js(f"window.WZ&&WZ.recv({json.dumps(k)},{payload})")
                except Exception:
                    pass

    def page_ready(self):
        self.ready = True
        self._stale = False

    def page_visible(self, shown):
        self.shown = shown
        self.ctl.set_visible(shown)
        self._tune_process()
        if shown and self._stale:
            self._stale = False
            self.outbox.put(("state", self.ctl.snapshot()))

    def _tune_process(self):
        set_efficiency_mode(not self.shown and not self.ctl.syncing())

    # ---- window
    def create_window(self):
        import webview
        self.window = webview.create_window(
            APP_NAME, url=resource("ui", "index.html"), js_api=Api(self.ctl, self),
            width=1240, height=820, min_size=(960, 640), background_color="#0D0E11",
            hidden=self.autostart, text_select=False)
        self.window.events.closing += self._on_closing
        self.window.events.shown += self._on_shown
        self.window.events.minimized += lambda: self.page_visible(False)
        self.window.events.restored += lambda: self.page_visible(True)

    def create_tray_menu(self):
        """A small frameless window styled like the app, shown when the tray icon is right-clicked."""
        import webview
        try:
            self.menu_win = webview.create_window(
                "WiZard menu", url=resource("ui", "tray.html"), js_api=TrayApi(self), width=260, height=226,
                frameless=True, easy_drag=False, on_top=True, hidden=True, resizable=False,
                background_color="#131519", text_select=False)
        except Exception:
            self.menu_win = None

    def _prepare_menu(self):
        """Once: keep the menu out of the taskbar, round its corners, hide it when it loses focus."""
        if self._menu_ready or not IS_WINDOWS:
            return
        self._menu_ready = True
        win = self.menu_win

        def work():
            try:
                win.native.ShowInTaskbar = False
            except Exception:
                pass
            try:
                win.native.Deactivate += lambda sender, args: threading.Thread(target=win.hide, daemon=True).start()
            except Exception:
                pass
        _on_ui_thread(win, work)
        try:
            import ctypes
            pref = ctypes.c_int(2)    # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(win.native.Handle.ToInt32(), 33, ctypes.byref(pref), 4)
        except Exception:
            pass

    def show_tray_menu(self):
        """Open the themed menu just above the mouse. Returns False to fall back to the plain menu."""
        win = self.menu_win
        if not (IS_WINDOWS and win and win.native):
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            self._prepare_menu()
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(info))
            work = info.rcWork
            w, h = int(win.native.Width), int(win.native.Height)
            x = max(work.left, min(pt.x, work.right) - w)
            y = max(work.top, min(pt.y, work.bottom) - h)
            hwnd = win.native.Handle.ToInt32()
            user32.SetWindowPos(hwnd, -1, x, y, 0, 0, 0x0001)      # topmost, keep size
            win.show()
            user32.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False

    def _on_shown(self):
        dark_title_bar(self.window)

    def _on_closing(self):
        if self.ctl.quitting:
            return True
        if self.tray and self.ctl.settings.get("close_to_tray", True):
            threading.Thread(target=self.hide, daemon=True).start()
            return False          # keep running in the tray
        threading.Thread(target=self.quit, daemon=True).start()
        return False

    def hide(self):
        self.window.hide()
        self.page_visible(False)
        webview_low_memory(self.window, True)
        trim_memory()
        if not self._notified and self.tray:
            self._notified = True
            try:
                self.tray.notify("Still running in the system tray. Right-click the icon to exit.", APP_NAME)
            except Exception:
                pass

    def show(self):
        webview_low_memory(self.window, False)
        self.window.show()
        self.window.restore()
        bring_to_front(self.window)
        self.page_visible(True)

    def quit(self):
        self.ctl.shutdown()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            if self.menu_win:
                self.menu_win.destroy()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            os._exit(0)

    # ---- tray
    def setup_tray(self):
        try:
            import pystray
        except Exception:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open WiZard", lambda: self.show(), default=True),
            pystray.MenuItem(lambda item: "Stop sync" if self.ctl.syncing() else "Start sync",
                             lambda: self.ctl.stop_sync() if self.ctl.syncing() else self.ctl.start_sync()),
            pystray.MenuItem("Turn all lights off", lambda: self.ctl.power("all", False)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda: threading.Thread(target=self.quit, daemon=True).start()),
        )
        host = self

        class TrayIcon(pystray.Icon):
            """Left click opens WiZard; right click opens the menu styled like the app."""

            def _on_notify(self, wparam, lparam):
                try:
                    if lparam == 0x0202:                       # WM_LBUTTONUP
                        host.show()
                        return
                    if lparam == 0x0205 and host.show_tray_menu():   # WM_RBUTTONUP
                        return
                except Exception:
                    pass
                return super()._on_notify(wparam, lparam)

        try:
            self.tray = TrayIcon("wizard", tray_image(False), APP_NAME, menu)
            self.tray.run_detached()
            self.tray_ok = True
        except Exception:
            self.tray = None

    def _update_tray(self):
        if self.tray:
            try:
                syncing = self.ctl.syncing()
                self.tray.icon = tray_image(syncing)
                self.tray.title = f"{APP_NAME}, {'syncing' if syncing else 'not syncing'}"
                self.tray.update_menu()
            except Exception:
                pass


# ======================================================================= native window touches

def _on_ui_thread(window, fn):
    try:
        from System import Action
        window.native.Invoke(Action(fn))
    except Exception:
        pass


def dark_title_bar(window):
    """Windows 10/11: dark title bar to match the page."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        hwnd = window.native.Handle.ToInt32()
        on = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE, and its older number
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
    except Exception:
        pass


def bring_to_front(window):
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(window.native.Handle.ToInt32())
    except Exception:
        pass


def webview_low_memory(window, low):
    """Ask WebView2 to use as little memory as it can while the window is hidden."""
    def work():
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
            wv = window.native.webview
            wv.CoreWebView2.MemoryUsageTargetLevel = Level.Low if low else Level.Normal
            if low:
                wv.Visible = False
                wv.CoreWebView2.TrySuspendAsync()
            else:
                if wv.CoreWebView2.IsSuspended:
                    wv.CoreWebView2.Resume()
                wv.Visible = True
        except Exception:
            pass
    if IS_WINDOWS and window and window.native:
        _on_ui_thread(window, work)


def tray_image(syncing):
    """The wizard hat: in RGB while syncing, plain gray when not."""
    from PIL import Image
    try:
        return Image.open(resource("assets", "tray_on.png" if syncing else "tray_off.png")).convert("RGBA")
    except OSError:
        return Image.new("RGBA", (64, 64), (150, 155, 165, 255))


# ======================================================================= single instance

def claim_single_instance():
    """A listening socket if this is the only WiZard running; otherwise asks the running one to show."""
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


def listen_for_show(srv, host):
    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16).startswith(b"show"):
                        host.show()
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()


def main():
    srv = claim_single_instance()
    if srv is None:
        return   # the running WiZard has been brought to the front
    try:
        import webview
    except ImportError:
        sys.exit("WiZard needs pywebview. Run:  python -m pip install -r requirements.txt")
    set_below_normal_priority()
    autostart = "--autostart" in sys.argv
    host = Host(autostart=autostart)
    host.setup_tray()
    host.create_window()
    host.create_tray_menu()
    listen_for_show(srv, host)
    if host.ctl.settings.get("launch_at_login"):
        try:
            W.set_launch_at_login(True)   # keep the login entry pointing at this copy of the app
        except OSError:
            pass
    if not host.ctl.settings["bulbs"]:
        host.ctl.discover()
    if autostart or host.ctl.settings["start_sync_on_launch"]:
        threading.Timer(1.5, host.ctl.start_sync).start()
    if autostart:
        host.page_visible(False)
    # Ctrl+C in a terminal: exit cleanly instead of leaving the app stuck
    signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=host.quit, daemon=True).start())
    webview.start(icon=resource("wiz.ico"), private_mode=True)
    host.ctl.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()

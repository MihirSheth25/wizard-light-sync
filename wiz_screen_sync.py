"""
WiZ Screen Sync
Makes Philips WiZ bulbs follow the colors on your PC screen, like a TV backlight.

Unofficial. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.

How it works:
  1. Finds WiZ bulbs on your local network (UDP port 38899).
  2. Captures your monitor several times per second.
  3. Picks a color for each bulb (vivid colors count more than gray).
  4. Sends that color to each bulb.
  5. On exit (Ctrl+C), puts every bulb back the way it was.

Before first use:
  - In the WiZ app: Settings > Security > Allow local communication > On.
  - PC and bulbs must be on the same Wi-Fi network.
  - pip install mss numpy

Examples:
  python wiz_screen_sync.py --list
  python wiz_screen_sync.py
  python wiz_screen_sync.py --zone 192.168.1.40=left --zone 192.168.1.41=right
  python wiz_screen_sync.py --monitor 2 --fps 15

Copyright (c) 2026 Mihir Sheth. MIT License, see LICENSE.
"""

import argparse
import json
import socket
import sys
import time

import numpy as np

WIZ_PORT = 38899
ZONES = ("full", "left", "right", "top", "bottom", "center")


# ---------------------------------------------------------------- bulbs

def discover_bulbs(timeout=2.0):
    """Broadcast getPilot and return {ip: reply_params} for every bulb that answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    msg = json.dumps({"method": "getPilot", "params": {}}).encode()
    found = {}
    end = time.time() + timeout
    next_send = 0.0
    while time.time() < end:
        if time.time() >= next_send:
            try:
                sock.sendto(msg, ("255.255.255.255", WIZ_PORT))
            except OSError:
                pass
            next_send = time.time() + 0.7
        try:
            data, (ip, _) = sock.recvfrom(4096)
        except socket.timeout:
            continue
        try:
            reply = json.loads(data.decode())
        except ValueError:
            continue
        if reply.get("method") == "getPilot" and "result" in reply:
            found[ip] = reply["result"]
    sock.close()
    return found


def get_state(sock, ip):
    """Read a bulb's current state so it can be restored later."""
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
    """Turn a saved getPilot result into setPilot params."""
    if not state:
        return None
    if not state.get("state", True):
        return {"state": False}
    params = {"state": True}
    if "dimming" in state:
        params["dimming"] = state["dimming"]
    scene = state.get("sceneId", 0)
    if scene:
        params["sceneId"] = scene
        if "speed" in state:
            params["speed"] = state["speed"]
    elif "temp" in state:
        params["temp"] = state["temp"]
    elif all(k in state for k in ("r", "g", "b")):
        for k in ("r", "g", "b", "c", "w"):
            if k in state:
                params[k] = state[k]
    return params


def send(sock, ip, params):
    msg = json.dumps({"method": "setPilot", "params": params}).encode()
    try:
        sock.sendto(msg, (ip, WIZ_PORT))
    except OSError:
        pass


# ---------------------------------------------------------------- color

def crop_zone(frame, zone):
    h, w = frame.shape[:2]
    if zone == "left":
        return frame[:, : w // 2]
    if zone == "right":
        return frame[:, w // 2:]
    if zone == "top":
        return frame[: h // 2]
    if zone == "bottom":
        return frame[h // 2:]
    if zone == "center":
        return frame[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    return frame


def pick_color(pixels, saturation_boost=1.3):
    """
    Return (r, g, b, brightness 0..1) for an RGB pixel array.
    Vivid pixels get more weight than gray ones, so a red explosion on a
    gray background still turns the room red instead of muddy pink.
    """
    px = pixels.reshape(-1, 3).astype(np.float32) / 255.0
    mx = px.max(axis=1)
    mn = px.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    weight = (sat ** 2) * mx + 0.02
    rgb = (px * weight[:, None]).sum(axis=0) / weight.sum()
    brightness = float(mx.mean())

    # Push the color away from gray, then scale so the brightest channel is full.
    gray = rgb.mean()
    rgb = gray + (rgb - gray) * saturation_boost
    rgb = np.clip(rgb, 0.0, None)
    peak = rgb.max()
    if peak < 1e-4:
        return 0, 0, 0, brightness
    rgb = rgb / peak
    r, g, b = (int(round(v * 255)) for v in rgb)
    return r, g, b, brightness


class Smoother:
    """Blends each new color with the last one so the bulbs do not flicker."""

    def __init__(self, amount):
        self.amount = amount  # 0 = instant, 0.9 = very slow
        self.value = None

    def update(self, new):
        new = np.array(new, dtype=np.float32)
        if self.value is None:
            self.value = new
        else:
            self.value = self.value * self.amount + new * (1 - self.amount)
        return self.value


# ---------------------------------------------------------------- main loop

def run(args):
    import mss  # imported here so --help and --list work without it

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)

    # Which bulbs, and which part of the screen each one follows.
    zones = {}
    for z in args.zone or []:
        ip, _, zone = z.partition("=")
        if zone not in ZONES:
            sys.exit(f"Unknown zone '{zone}'. Use one of: {', '.join(ZONES)}")
        zones[ip.strip()] = zone

    ips = list(args.bulbs or []) + [ip for ip in zones if ip not in (args.bulbs or [])]
    if not ips:
        print("Looking for WiZ bulbs...")
        ips = sorted(discover_bulbs())
        if not ips:
            sys.exit(
                "No bulbs found. Check that 'Allow local communication' is on in the "
                "WiZ app and that the PC is on the same Wi-Fi as the bulbs."
            )
    for ip in ips:
        zones.setdefault(ip, "full")
    print("Syncing:", ", ".join(f"{ip} ({zones[ip]})" for ip in ips))

    saved = {ip: get_state(sock, ip) for ip in ips}
    smoothers = {ip: Smoother(args.smoothing) for ip in ips}
    last_sent = {ip: None for ip in ips}
    interval = 1.0 / args.fps
    step = max(1, args.sample_step)

    print("Running. Press Ctrl+C to stop and restore your bulbs.")
    last_debug = 0.0
    try:
        with mss.mss() as sct:
            if args.monitor >= len(sct.monitors):
                sys.exit(f"Monitor {args.monitor} not found. You have {len(sct.monitors) - 1}.")
            mon = sct.monitors[args.monitor]
            for i, m in enumerate(sct.monitors[1:], start=1):
                mark = "   <- capturing this one" if i == args.monitor else ""
                print(f"  Monitor {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']}){mark}")
            if args.debug:
                import mss.tools
                first = sct.grab(mon)
                mss.tools.to_png(first.rgb, first.size, output="capture_check.png")
                print("  Saved capture_check.png in this folder. Open it to see what the app sees.")
            while True:
                t0 = time.perf_counter()
                shot = np.asarray(sct.grab(mon))          # BGRA
                frame = shot[::step, ::step, 2::-1]       # downsample, BGRA -> RGB
                if args.debug and t0 - last_debug >= 1.0:
                    last_debug = t0
                    r, g, b, bright = pick_color(frame, args.saturation)
                    note = "   [screen looks BLACK to the app]" if bright < 0.02 else ""
                    print(f"  brightness {bright:.2f} -> bulb color ({r},{g},{b}){note}")
                for ip in ips:
                    r, g, b, bright = pick_color(crop_zone(frame, zones[ip]), args.saturation)
                    sr, sg, sb, sbright = smoothers[ip].update((r, g, b, bright))
                    dim = int(args.min_brightness + (args.max_brightness - args.min_brightness)
                              * min(1.0, sbright * args.brightness_gain))
                    dim = max(10, min(100, dim))
                    color = (int(sr), int(sg), int(sb), dim)
                    prev = last_sent[ip]
                    # Skip tiny changes so the bulb is not flooded with commands.
                    if prev is None or max(abs(a - b) for a, b in zip(color, prev)) >= 3:
                        if color[:3] == (0, 0, 0):
                            send(sock, ip, {"state": True, "temp": 2700, "dimming": 10})
                        else:
                            send(sock, ip, {"state": True, "r": color[0], "g": color[1],
                                            "b": color[2], "dimming": dim})
                        last_sent[ip] = color
                time.sleep(max(0.0, interval - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        print("\nRestoring bulbs...")
        for ip in ips:
            params = restore_params(saved.get(ip))
            if params:
                for _ in range(3):
                    send(sock, ip, params)
                    time.sleep(0.05)
        sock.close()
        print("Done.")


def main():
    p = argparse.ArgumentParser(description="Sync Philips WiZ bulbs to your screen colors.")
    p.add_argument("--list", action="store_true", help="find bulbs on the network and exit")
    p.add_argument("--bulbs", nargs="+", metavar="IP", help="bulb IPs (default: find all)")
    p.add_argument("--zone", action="append", metavar="IP=ZONE",
                   help=f"screen area a bulb follows: {', '.join(ZONES)} (repeatable)")
    p.add_argument("--monitor", type=int, default=1, help="1 = primary monitor (default), 2 = second")
    p.add_argument("--fps", type=float, default=10, help="updates per second (default 10, max ~20)")
    p.add_argument("--smoothing", type=float, default=0.5, help="0 = instant, 0.9 = slow (default 0.5)")
    p.add_argument("--saturation", type=float, default=1.3, help="color boost (default 1.3)")
    p.add_argument("--min-brightness", type=int, default=15, help="bulb brightness on dark scenes (10-100)")
    p.add_argument("--max-brightness", type=int, default=100, help="bulb brightness on bright scenes (10-100)")
    p.add_argument("--brightness-gain", type=float, default=1.5, help="how fast bulbs brighten (default 1.5)")
    p.add_argument("--sample-step", type=int, default=8, help="read every Nth pixel (default 8)")
    p.add_argument("--debug", action="store_true",
                   help="print the captured color every second and save capture_check.png")
    args = p.parse_args()
    args.fps = max(1.0, min(20.0, args.fps))
    args.smoothing = max(0.0, min(0.95, args.smoothing))

    if args.list:
        print("Looking for WiZ bulbs...")
        bulbs = discover_bulbs()
        if not bulbs:
            print("None found. Turn on 'Allow local communication' in the WiZ app.")
        for ip, info in sorted(bulbs.items()):
            print(f"  {ip}  mac={info.get('mac', '?')}  on={info.get('state', '?')}")
        return

    run(args)


if __name__ == "__main__":
    main()

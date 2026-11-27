"""
Measures how long WiZard's hot paths take on this PC. See docs/PERFORMANCE.md.

Reads your settings but does not change them, and sends nothing to the bulbs.

Run:  python tools/benchmark.py
"""

import os
import sys
import timeit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

import wiz_core as W  # noqa: E402
from wiz_controller import Controller  # noqa: E402


def ms(fn, n):
    fn()
    return timeit.timeit(fn, number=n) / n * 1000


def main():
    rng = np.random.default_rng(1)
    ctl = Controller(lambda *a: None)
    s = ctl.get_settings()
    shot = rng.integers(0, 256, (1440, 2560, 4), dtype=np.uint8)     # a 2560 x 1440 capture, BGRA like mss
    frame = shot[::8, ::8, 2::-1]

    def screen_frame(zones):
        f = W.strip_black_bars(W.crop_margins(shot[::8, ::8, 2::-1], s))
        picked = {}
        for z in zones:
            if z not in picked:
                picked[z] = W.pick_color(W.crop_zone(f, z), "Vivid", 2.0)
        return [picked[z] for z in zones]

    analyzer = W.MusicAnalyzer()
    blocks = [rng.standard_normal(1024).astype(np.float32) * 0.3 for _ in range(400)]
    tick = [0]

    def analyze():
        tick[0] += 1
        analyzer.analyze(blocks[tick[0] % 400], 1.0, tick[0] / 43)

    rows = [("Settings read by the sync engines", ms(ctl.get_settings, 3000)),
            ("Interface state update", ms(ctl.snapshot, 500))]
    rows += [(f"Pick one color, {m}", ms(lambda m=m: W.pick_color(frame, m, 2.0), 100)) for m in W.COLOR_MODES]
    rows += [("Find black bars", ms(lambda: W.strip_black_bars(frame), 200)),
             ("Screen frame, 2 lights on Full screen", ms(lambda: screen_frame(["Full screen"] * 2), 100)),
             ("Screen frame, left half and right half", ms(lambda: screen_frame(["Left half", "Right half"]), 100)),
             ("Music analysis, one block", ms(analyze, 4000))]
    for name, t in rows:
        print(f"{name:<42}{t:9.3f} ms")
    os._exit(0)      # the controller's background threads do not need a clean shutdown here


if __name__ == "__main__":
    main()

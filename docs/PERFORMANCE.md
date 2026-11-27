# Performance

This page explains the optimization pass of November 2026: where WiZard spent its time, what changed, and how the new code was checked against the old code. Every change keeps the app's output exactly the same. Bulbs receive the same messages in the same order, and the interface draws the same things.

## Results

Measured on a 2560 x 1440 screen with the default sample step of 8 (one pixel in 8 in each direction), Python 3.12 and NumPy 2. Lower is better.

| Work | Before | After | Faster by |
| --- | --- | --- | --- |
| One screen sync frame, 2 lights on Full screen | 14.3 ms | 2.0 ms | 7.3x |
| One screen sync frame, 3 lights on Full screen | 20.9 ms | 2.1 ms | 10.2x |
| One screen sync frame, left half and right half | 8.4 ms | 2.0 ms | 4.2x |
| Pick one color, Vivid | 6.3 ms | 2.0 ms | 3.1x |
| Pick one color, Average | 6.0 ms | 1.5 ms | 4.0x |
| Pick one color, Dominant | 8.2 ms | 3.6 ms | 2.3x |
| Find black bars | 1.5 ms | 0.23 ms | 6.8x |
| Music analysis, one block of 1024 samples | 0.18 ms | 0.08 ms | 2.2x |
| Settings read by the sync engines | 0.107 ms | under 0.001 ms | over 300x |
| Interface state update | 0.17 ms | 0.045 ms | 3.8x |

At 10 to 20 screen frames per second, screen sync used to keep one CPU core about 15 to 40 percent busy with color math alone. It now needs about 2 to 4 percent. Music sync was already light: its analysis drops from about 8 ms to about 3 ms of CPU per second. Both engines also stop creating a full copy of the settings on every tick, which removes a steady stream of short-lived objects for Python's memory manager to clean up.

## What was slow and what changed

The changes are listed from most to least impact. All of them are low risk because they do the same arithmetic in the same order, or skip work whose result is already known.

### 1. Per-pixel maximum and minimum

`pick_color` and `strip_black_bars` need the brightest and darkest channel of every pixel. NumPy's `max(axis=...)` and `min(axis=...)` are slow when the axis has only three entries (red, green and blue), because the reduction loop runs once per pixel. That one step took about 2.2 ms per call.

The new code compares the three channels pairwise with `np.maximum` and `np.minimum`, which runs as three fast whole-array passes. It takes about 0.04 ms. The result is exactly the same, because a maximum or minimum always returns one of its inputs and never rounds anything.

Trade-off: one small helper function (`_max3` and `_min3` in `wiz_core.py`). The intent stays clear from the name.

### 2. One color per screen area per frame

Screen sync used to compute a color for every light, even when several lights followed the same screen area. With two lights on Full screen, the same color was computed twice. The engine now keeps a small dictionary for the current frame and reuses the color for lights that share an area. Everything after that step, such as smoothing, brightness and the change threshold, still runs separately for each light.

Trade-off: four extra lines in the screen sync loop. The dictionary only lives for one frame, so it cannot go out of date.

### 3. Reused settings copy

The sync engines ask the controller for the settings on every tick, about 55 times per second in total. Each request used to make a full deep copy of the settings, including every bulb, room, schedule and preset.

The controller now hands out the same read-only copy until something changes. Every change to the settings already happens inside `with ctl.lock:`, so the lock was given a small wrapper (`_WriteLock`) that marks the copy as out of date whenever such a block ends. Readers use the plain lock and never mark it.

Trade-off: this is the one change that relies on rules rather than plain arithmetic, and both rules are written down in the code:

- Every change to `ctl.settings` must happen inside `with ctl.lock:`. All existing changes already did, and this was checked line by line.
- Code that receives the settings from `get_settings()` must not change them. No code did.

### 4. Music analysis lookups built once

For every audio block, the analyzer used to rebuild the window function and the frequency masks for each band, the bass, the mids and the kick drum. These never change for a given block size, so they are now built once and reused. The analysis then works on the same numbers as before.

Trade-off: one cached table per block size, which is a few kilobytes.

### 5. Light icons repaint only when they change

Live updates reach the page several times per second, and every light icon used to be restyled each time, even when its color and brightness had not changed. Each icon now remembers what it last showed and skips identical updates.

Trade-off: two lines in `paintBulb` in `ui/app.js`.

## What was left out on purpose

Some faster options would have changed the results, so they were not used:

- **Faster screen capture**, such as downscaling in Windows or capturing with DXGI Desktop Duplication. These would sample slightly different pixels, so the colors would change. Some would also need new packages.
- **Matrix routines for the weighted color sum**, such as `np.dot` or `einsum`. These add the numbers in a different order, which can change the last digit of the result.
- **A ring buffer for the music history.** This also changes the order of addition when the average is taken.
- **Reusing one screen-capture object in the display watcher.** That object reads the list of monitors once, so a display connected later would not be noticed.

## How identical output was checked

The new code was run side by side with an untouched copy of the old code. Every comparison below required exact equality, not near equality.

| Check | Inputs | Result |
| --- | --- | --- |
| `pick_color` | 300 frames (random, black, flat and letterboxed images in the same memory layout as real captures), every color mode, every screen area, 4 color intensities | 35,952 cases identical |
| `strip_black_bars` | The same 300 frames at 3 thresholds | Identical |
| `MusicAnalyzer.analyze` | 6 audio streams, 9,000 blocks of noise, tones, bursts and silence at two block sizes | Identical |
| Screen sync engine | Full runs against a fake screen and fake bulbs: 3 light layouts, 3 color modes and 3 dark-screen settings | 7,773 bulb messages identical, in the same order |
| Controller | 26 actions: setting changes, music styles and their defaults, presets, rooms, schedules, light changes, both resets | Settings, interface state and saved file identical after each action, and the reused copy always equal to the live settings |
| Interface | Every page during a series of color and brightness changes | Light icons identical on all 7 pages, no console errors |

The existing rhythm, music-style and sync-switching tests also passed.

## Running the benchmark

`tools/benchmark.py` measures the same work on your own PC. It reads your settings but does not change them, and it does not talk to the bulbs.

```powershell
python tools\benchmark.py
```

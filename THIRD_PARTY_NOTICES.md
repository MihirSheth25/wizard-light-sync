# Third-party notices

WiZard's own code is under the MIT License (see [LICENSE](LICENSE)). It uses the Python packages below. They are installed from PyPI with `requirements.txt` and are not copied into this repository.

| Package | What WiZard uses it for | License |
| --- | --- | --- |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | Window, buttons, sliders and other widgets | MIT |
| [mss](https://github.com/BoboTiG/python-mss) | Screen capture for screen sync | MIT |
| [NumPy](https://numpy.org) | Color and audio math | BSD 3-Clause |
| [Pillow](https://python-pillow.org) | Drawing the room preview, icons and color wheel | MIT-CMU (HPND) |
| [pystray](https://github.com/moses-palmer/pystray) | System tray icon | LGPL-3.0 |
| [SoundCard](https://github.com/bastibe/SoundCard) | Loopback audio for music sync | BSD 3-Clause |

These packages also pull in their own dependencies (such as `cffi` for SoundCard and `darkdetect` for CustomTkinter), each under its own license.

## If you share a built .exe

A PyInstaller .exe contains copies of the packages above and of Python itself. If you give the .exe to other people:

- Include this file and the license text of each bundled package. Each project's license is linked from its page in the table above.
- pystray is under the LGPL-3.0. Share it unmodified, say that the .exe uses pystray under the LGPL-3.0, and tell people where to get pystray's source (https://github.com/moses-palmer/pystray). Because the app is open source, people can also rebuild it with a different pystray version, which the LGPL asks for.
- Python itself is under the Python Software Foundation License.

Sharing only the source code in this repository needs none of the steps above, because the packages are not included.

## Trademarks

WiZ, WiZ Connected and Philips are trademarks of Signify Holding or its affiliates. WiZard is not affiliated with or endorsed by them. The names appear only to describe which bulbs the app works with.

## Fonts

The app asks Windows for the Bahnschrift and Segoe UI fonts that ship with Windows. No font files are included in this repository or in a built .exe.

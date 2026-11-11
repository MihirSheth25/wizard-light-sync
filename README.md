# WiZ Screen Sync

A Windows desktop app that makes WiZ smart bulbs follow the colors on your screen. It talks to the bulbs directly over the local Wi-Fi network.

Unofficial project. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.

## Setup

1. Allow local communication for your bulbs in the WiZ phone app.
2. Put the PC and the bulbs on the same network.
3. Install the packages and start the app:

```powershell
python -m pip install -r requirements.txt
```

```powershell
python wiz_sync_app.py
```

Click Find bulbs, pick a screen area for each bulb, and click Start sync. Closing the window keeps the app in the tray.

The first version, `wiz_screen_sync.py`, still works as a command-line tool.

## License

MIT. See [LICENSE](LICENSE).

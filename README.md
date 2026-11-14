# WiZard

A Windows desktop app for WiZ smart bulbs: screen sync, music sync, manual control, scenes, rooms, schedules and a sleep timer. It talks to the bulbs directly over the local Wi-Fi network.

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

Click Find lights on the Lights page. Click a light or a room to control it. Choose Screen or Music next to Start sync to sync the lights.

Settings are saved to `%APPDATA%\WiZard\settings.json`.

## License

MIT. See [LICENSE](LICENSE).

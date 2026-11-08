# WiZ Screen Sync

A small Python script that makes WiZ smart bulbs follow the colors on a Windows PC screen. It talks to the bulbs directly over the local Wi-Fi network.

Unofficial project. Not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.

## Setup

1. Allow local communication for your bulbs in the WiZ phone app.
2. Put the PC and the bulbs on the same network.
3. Install the packages:

```powershell
python -m pip install -r requirements.txt
```

## Usage

```powershell
python wiz_screen_sync.py --list
```

```powershell
python wiz_screen_sync.py --zone 192.168.1.40=left --zone 192.168.1.41=right
```

Press Ctrl+C to stop. The bulbs go back to how they were.

## License

MIT. See [LICENSE](LICENSE).

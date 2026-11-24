"""
Development preview: serves the WiZard page in a normal browser at http://127.0.0.1:8765,
wired to the real app logic. Useful for working on the page without building the app.
Pushes from Python reach the page through Server-Sent Events.

Run:  python tools/dev_server.py
"""

import json
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wizard import Api  # noqa: E402
from wiz_controller import Controller  # noqa: E402

clients = []
lock = threading.Lock()


def emit(kind, data):
    with lock:
        for q in clients:
            q.put((kind, data))


class FakeHost:
    tray_ok = False

    def page_ready(self):
        pass

    def quit(self):
        os._exit(0)


ctl = Controller(emit=emit)
api = Api(ctl, FakeHost())
TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
         ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue()
            with lock:
                clients.append(q)
            try:
                while True:
                    kind, data = q.get()
                    self.wfile.write(f"data: {json.dumps([kind, data])}\n\n".encode())
                    self.wfile.flush()
            except OSError:
                with lock:
                    clients.remove(q)
            return
        path = self.path.split("?")[0]
        path = "/index.html" if path == "/" else path
        full = os.path.normpath(os.path.join(ROOT, "ui", path.lstrip("/")))
        if not full.startswith(os.path.join(ROOT, "ui")) or not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
        fn = getattr(api, name, None)
        if name.startswith("_") or not callable(fn):
            self.send_error(404)
            return
        result = fn(*args)
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("WIZARD_DEV_PORT", "8765"))
    print(f"WiZard preview on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

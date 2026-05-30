#!/usr/bin/env python3
"""VDJ Treta — local static server + Mixxx proxy.

Serves the visual app and proxies ``/mixxx/*`` to the local Mixxx HTTP API
(http://localhost:7778) so the browser can poll beat/VU data without tripping
CORS. The daemon WebSocket (ws://localhost:7779) is connected to directly from
the browser — cross-origin WS needs no proxy.

Run:  python3 vdj/serve.py   →  open http://localhost:8089
Then drag the window onto the TV (extended display) and hit F for fullscreen.
"""

import os
import sys
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VDJ_DIR = Path(__file__).parent
MIXXX = "http://localhost:7778"
PORT = 8089


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # required for Range / 206 to be honored

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VDJ_DIR), **kwargs)

    def do_GET(self):
        if self.path.startswith("/mixxx/"):
            return self._proxy(self.path[len("/mixxx"):])
        return self._serve_static()

    def _serve_static(self):
        """Static file serving WITH Range support so <video> can stream MP4."""
        clean = self.path.split("?", 1)[0]
        path = self.translate_path(clean)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return
        size = os.path.getsize(path)
        ctype = self.guess_type(path)
        rng = self.headers.get("Range")
        with open(path, "rb") as f:
            if rng and rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = min(int(e) if e else size - 1, size - 1)
                length = max(0, end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                f.seek(start)
                self.wfile.write(f.read(length))
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(f.read())

    def _proxy(self, tail):
        try:
            with urllib.request.urlopen(MIXXX + tail, timeout=2) as r:
                body = r.read()
            code = 200
        except Exception as e:
            body = f'{{"error":"{e}"}}'.encode()
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))  # HTTP/1.1 keep-alive needs this
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet — don't spam the console with polls
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"VDJ Treta → http://localhost:{port}  (proxying Mixxx at {MIXXX})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

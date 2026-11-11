"""Serve only the public runtime shell, never an arbitrary filesystem path."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
POLICY = "default-src 'none'; script-src 'self' blob: 'unsafe-inline' 'unsafe-eval'; style-src blob: data: 'unsafe-inline'; img-src blob: data:; font-src data: blob:; frame-src 'self' about: blob:; worker-src blob:; connect-src 'none'; form-action 'none'; object-src 'none'; base-uri 'none'"


class RuntimeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = urlsplit("http://" + self.headers.get("Host", "")).hostname or ""
        if host != "localhost" and not host.endswith(".localhost"):
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        file = {"/": "index.html", "/bridge.js": "bridge.js"}.get(path)
        if not file:
            self.send_error(404)
            return
        raw = (ROOT / file).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if file.endswith(".js") else "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", POLICY)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5195)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), RuntimeHandler).serve_forever()

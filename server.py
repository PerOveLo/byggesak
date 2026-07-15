# -*- coding: utf-8 -*-
"""
Lokal server for byggesakskartet.

Server statiske filer fra prosjektmappen og proxyer PDF-vedlegg fra
OpenGov (som ikke sender CORS-headere) slik at den innebygde PDF-viseren
i kartet kan lese dem.

Start:  py -X utf8 server.py        (http://localhost:8742)
"""

import http.server
import socketserver
import urllib.parse
import urllib.request
import sys

PORT = 8742
ALLOWED_HOSTS = {"opengov.360online.com"}
UA = {"User-Agent": "FlekkeroyByggesakskart/2.0 (privat innsynsverktoy)"}


class Handler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith("/pdfproxy?"):
            self.handle_proxy()
        else:
            super().do_GET()

    def handle_proxy(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = (qs.get("url") or [""])[0]
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            self.send_error(403, "Ugyldig mål for proxy")
            return
        try:
            req = urllib.request.Request(target, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "application/pdf")
        except Exception as e:  # noqa: BLE001
            self.send_error(502, f"Klarte ikke hente dokumentet: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # roligere logg
        if "/pdfproxy" in (args[0] if args else ""):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"Byggesakskart: http://localhost:{port}  (Ctrl+C for å stoppe)")
        httpd.serve_forever()

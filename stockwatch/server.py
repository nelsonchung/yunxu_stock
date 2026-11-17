import argparse
import json
import logging
import re
import signal
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .intraday import MAX_IDS
from .store import InputError, Store
from .worker import TAIPEI, Worker

STATIC = Path(__file__).resolve().parent.parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/indicators.js": ("indicators.js", "text/javascript; charset=utf-8"),
    "/technical.js": ("technical.js", "text/javascript; charset=utf-8"),
    "/intraday.js": ("intraday.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def send(self, code, content, mime):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)

    def json(self, code, value):
        self.send(code, json.dumps(value, ensure_ascii=False).encode(),
                  "application/json; charset=utf-8")

    def trusted_host(self):
        host = self.headers.get("Host", "")
        if host not in self.server.allowed_hosts:
            self.json(HTTPStatus.FORBIDDEN, {"error": "請使用此服務的區域網路位址"})
            return False
        return True

    def do_GET(self):
        if not self.trusted_host():
            return
        target = urlsplit(self.path)
        if target.path in ASSETS:
            name, mime = ASSETS[target.path]
            self.send(HTTPStatus.OK, (STATIC / name).read_bytes(), mime)
        elif target.path == "/api/state":
            worker = self.server.worker
            self.json(HTTPStatus.OK, self.server.store.state(
                datetime.now(TAIPEI).date(), bool(worker.us_key), worker.us_full, worker.finmind_history))
        elif target.path == "/api/search":
            try:
                fields = parse_qs(target.query, max_num_fields=8)
            except ValueError:
                self.json(HTTPStatus.BAD_REQUEST, {"error": "搜尋欄位過多"})
                return
            self.json(HTTPStatus.OK, {"results": self.server.store.search(
                fields.get("q", [""])[0], fields.get("market", [""])[0])})
        elif target.path == "/api/intraday":
            origin = self.headers.get("Origin")
            if (origin and origin != "http://" + self.headers["Host"]) or (
                    self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")):
                self.json(HTTPStatus.FORBIDDEN, {"error": "不允許跨網站操作"})
                return
            try:
                fields = parse_qs(target.query, max_num_fields=2)
                ids = fields.get("ids", [""])[0].split(",") if fields.get("ids") else []
                if len(ids) > MAX_IDS or any(not re.fullmatch(r"[0-9]{1,19}", item)
                                               or not 1 <= int(item) < 2**63 for item in ids):
                    raise ValueError("Invalid IDs")
            except ValueError:
                self.json(HTTPStatus.BAD_REQUEST, {"error": "行情查詢的股票選擇無效"})
                return
            self.json(HTTPStatus.OK, self.server.worker.intraday.snapshot(list(dict.fromkeys(map(int, ids)))))
        elif target.path == "/healthz":
            with self.server.store.connect() as db:
                db.execute("SELECT 1 FROM settings").fetchone()
            self.json(HTTPStatus.OK, {"ok": True})
        else:
            self.json(HTTPStatus.NOT_FOUND, {"error": "找不到頁面"})

    def do_POST(self):
        if not self.trusted_host():
            return
        if self.path != "/api/action":
            self.json(HTTPStatus.NOT_FOUND, {"error": "找不到操作"})
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers["Host"]:
            self.json(HTTPStatus.FORBIDDEN, {"error": "不允許跨網站操作"})
            return
        if self.headers.get_content_type() != "application/json":
            self.json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "需要 JSON 資料"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise InputError("請求大小有誤")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise InputError("欄位格式有誤")
            self.server.store.action(data)
        except InputError as exc:
            self.json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (ValueError, UnicodeError, RecursionError):
            self.json(HTTPStatus.BAD_REQUEST, {"error": "請求格式有誤"})
        else:
            self.json(HTTPStatus.OK, {"ok": True})

    def log_message(self, fmt, *args):
        if getattr(self, "command", None) != "GET" or not getattr(self, "path", "").startswith(
                ("/api/state", "/api/search", "/api/intraday")):
            super().log_message(fmt, *args)


def make_server(bind, port, store, worker):
    server = ThreadingHTTPServer((bind, port), Handler)
    server.store, server.worker = store, worker
    actual = server.server_port
    server.allowed_hosts = {f"{host}:{actual}" for host in (bind, "localhost", "127.0.0.1")}
    if actual == 80:
        server.allowed_hosts.update((bind, "localhost", "127.0.0.1"))
    return server


def main():
    parser = argparse.ArgumentParser(description="鋆旭觀測站")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--db", type=Path, default=Path("state/stocks.sqlite3"))
    parser.add_argument("--offline", action="store_true", help="serve without any background requests")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    worker = Worker(store, offline=args.offline)
    stop = threading.Event()
    with make_server(args.bind, args.port, store, worker) as server:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: threading.Thread(target=server.shutdown).start())
        thread = None
        if not args.offline:
            thread = threading.Thread(target=worker.run, args=(stop,), daemon=True)
            thread.start()
        logging.info("Serving on %s:%d", args.bind, server.server_port)
        try:
            server.serve_forever()
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=17)


if __name__ == "__main__":
    main()

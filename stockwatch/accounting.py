"""Loopback-only, read-only view of a SinoPac securities account."""

import argparse
import http.client
import json
import math
import os
import re
import signal
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
STATIC = Path(__file__).resolve().parent.parent / "static"
ASSETS = {
    "/accounting": ("accounting.html", "text/html"),
    "/accounting.js": ("accounting.js", "text/javascript"),
    "/accounting.css": ("accounting.css", "text/css"),
    "/style.css": ("style.css", "text/css"),
}
BROKER_ROUTES = {
    ("GET", "/api/v1/info"),
    ("GET", "/api/v1/auth/accounts"),
    ("POST", "/api/v1/portfolio/position_unit"),
    ("POST", "/api/v1/portfolio/account_balance"),
}
UNAVAILABLE = "目前無法連線至永豐帳務服務"
POSITIONS_ERROR = "目前無法取得證券庫存"
BALANCE_ERROR = "目前無法取得交割餘額"
MAX_RESPONSE_BYTES = 1_048_576


class BrokerError(Exception):
    pass


def required_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BrokerError
    return value


def number_or_none(value):
    return None if value is None else required_number(value)


def positions_view(rows):
    if not isinstance(rows, list):
        raise BrokerError
    result = []
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"[A-Z0-9]{1,16}", str(row.get("code", ""))):
            raise BrokerError
        direction = row.get("direction")
        quantity = required_number(row.get("quantity"))
        if direction not in ("Buy", "Sell") or quantity < 0 or int(quantity) != quantity:
            raise BrokerError
        result.append({
            "code": str(row["code"]),
            "direction": direction,
            "quantity": int(quantity),
            "avg_price": number_or_none(row.get("price")),
            "last_price": number_or_none(row.get("last_price")),
            "pnl": number_or_none(row.get("pnl")),
        })
    unrealized = (required_number(sum(row["pnl"] for row in result))
                  if all(row["pnl"] is not None for row in result) else None)
    return result, unrealized


def balance_view(row):
    if not isinstance(row, dict) or row.get("errmsg"):
        raise BrokerError
    amount = required_number(row.get("acc_balance"))
    as_of = None
    if isinstance(row.get("date"), str):
        try:
            stamp = datetime.fromisoformat(row["date"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=TAIPEI)
            as_of = stamp.isoformat(timespec="seconds")
        except ValueError:
            pass
    return {"amount": amount, "as_of": as_of}


class Accounting:
    def __init__(self, broker_port, token, *, cache_seconds=30):
        self.broker_port = broker_port
        self.token = token
        self.cache_seconds = cache_seconds
        self.cached = None
        self.expires = 0
        self.lock = threading.Lock()

    def broker(self, method, path, payload=None):
        if (method, path) not in BROKER_ROUTES:
            raise BrokerError
        connection = http.client.HTTPConnection("127.0.0.1", self.broker_port, timeout=8)
        try:
            headers = {"Accept": "application/json", "Authorization": "Bearer " + self.token}
            body = None
            if payload is not None:
                body = json.dumps(payload)
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            if response.status != HTTPStatus.OK:
                raise BrokerError
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise BrokerError
            return json.loads(body)
        except (OSError, http.client.HTTPException, ValueError, RecursionError) as error:
            raise BrokerError from error
        finally:
            connection.close()

    def stock_account(self):
        info = self.broker("GET", "/api/v1/info")
        if not isinstance(info, dict) or info.get("simulation") is not False:
            raise BrokerError
        accounts = self.broker("GET", "/api/v1/auth/accounts")
        if not isinstance(accounts, list):
            raise BrokerError
        stocks = [row for row in accounts if isinstance(row, dict) and row.get("account_type") == "S"]
        if len(stocks) != 1 or stocks[0].get("signed") is not True:
            raise BrokerError
        stock = stocks[0]
        selectors = ("broker_id", "account_id", "person_id")
        if any(not isinstance(stock.get(key), str) or not stock[key].strip() for key in selectors):
            raise BrokerError
        return {"account_type": "S", **{key: stock[key] for key in selectors}}

    def refresh(self):
        result = {"positions": None, "unrealized_pnl": None, "balance": None, "errors": {}}
        try:
            stock = self.stock_account()
        except BrokerError:
            result["errors"].update(positions=UNAVAILABLE, balance=UNAVAILABLE)
        else:
            try:
                rows = self.broker("POST", "/api/v1/portfolio/position_unit", stock | {"unit": "Share"})
                result["positions"], result["unrealized_pnl"] = positions_view(rows)
            except (BrokerError, OverflowError):
                result["errors"]["positions"] = POSITIONS_ERROR
            try:
                result["balance"] = balance_view(self.broker("POST", "/api/v1/portfolio/account_balance", stock))
            except (BrokerError, OverflowError):
                result["errors"]["balance"] = BALANCE_ERROR
        result["fetched_at"] = datetime.now(TAIPEI).isoformat(timespec="seconds")
        return result

    def snapshot(self):
        with self.lock:
            if self.cached is None or time.monotonic() >= self.expires:
                self.cached = self.refresh()
                self.expires = time.monotonic() + self.cache_seconds
            return self.cached


class AccountingHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_):
        pass

    def send(self, status, content, mime):
        self.send_response(status)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, data):
        self.send(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), "application/json")

    def trusted_api(self, host):
        return (self.headers.get("X-Yunxu-Accounting") == "1"
                and self.headers.get("Sec-Fetch-Site") in (None, "none", "same-origin")
                and self.headers.get("Origin") in (None, f"http://{host}"))

    def do_GET(self):
        host = self.headers.get("Host")
        if host not in self.server.allowed_hosts:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "此頁僅供本機連線"})
            return
        if self.path in ASSETS or self.path == "/":
            filename, mime = ASSETS["/accounting" if self.path == "/" else self.path]
            try:
                content = (STATIC / filename).read_bytes()
            except OSError:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到頁面"})
            else:
                self.send(HTTPStatus.OK, content, mime)
        elif self.path == "/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True})
        elif self.path == "/api/accounting":
            if not self.trusted_api(host):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "請從帳務頁面查詢"})
                return
            self.send_json(HTTPStatus.OK, self.server.accounting.snapshot())
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到頁面"})

    def do_POST(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "此服務只接受讀取"})


def make_server(port, accounting):
    server = ThreadingHTTPServer(("127.0.0.1", port), AccountingHandler)
    server.accounting = accounting
    server.allowed_hosts = {f"{host}:{server.server_port}" for host in ("127.0.0.1", "localhost")}
    return server


def main():
    parser = argparse.ArgumentParser(description="鋆旭觀測站私人帳務")
    parser.add_argument("--port", type=int, default=8094)
    parser.add_argument("--broker-port", type=int, default=8092)
    args = parser.parse_args()
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        parser.error("Missing accounting service credential directory")
    try:
        token = (Path(directory) / "accounting-http-token").read_text(encoding="utf-8").strip()
    except OSError:
        parser.error("Could not load accounting service credential")
    if not token or "\n" in token or "\r" in token:
        parser.error("Invalid accounting service credential")
    with make_server(args.port, Accounting(args.broker_port, token)) as server:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        server.serve_forever()


if __name__ == "__main__":
    main()

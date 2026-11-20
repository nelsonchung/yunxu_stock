"""Loopback adapter from Shioaji's native quote callbacks to the dashboard SSE feed."""
import hmac
import json
import os
import queue
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .intraday import FEEDS, MAX_IDS, SYMBOL

FIELDS = {
    "Tick": (
        "code", "date", "time", "open", "avg_price", "close", "high", "low",
        "total_amount", "volume", "total_volume", "price_chg", "pct_chg",
        "bid_side_total_vol", "ask_side_total_vol", "simtrade", "intraday_odd",
    ),
    "BidAsk": (
        "code", "date", "time", "bid_price", "bid_volume", "ask_price",
        "ask_volume", "suspend", "simtrade", "intraday_odd",
    ),
}


class QuoteFeed:
    def __init__(self, api, quote_type, token):
        self.api = api
        self.quote_type = quote_type
        self.token = token
        self.subscriptions = {}
        self.sub_lock = threading.Lock()
        self.listeners = set()
        self.listener_lock = threading.Lock()

    def update(self, subscribe, exchange, symbol, kind):
        if (exchange not in ("TSE", "OTC") or not isinstance(symbol, str)
                or not SYMBOL.fullmatch(symbol) or kind not in FEEDS):
            raise ValueError("Unsupported stock feed")
        key = exchange, symbol, kind
        with self.sub_lock:
            if subscribe and key not in self.subscriptions:
                if len(self.subscriptions) >= MAX_IDS * len(FEEDS):
                    raise ValueError("Too many stock feeds")
                contract = self.api.contracts.get(symbol)
                if contract is None or getattr(contract.exchange, "value", contract.exchange) != exchange:
                    raise ValueError("Unknown stock")
                self.api.subscribe(contract, quote_type=self.quote_type[kind], intraday_odd=False)
                self.subscriptions[key] = contract
            elif not subscribe and key in self.subscriptions:
                self.api.unsubscribe(self.subscriptions[key], quote_type=self.quote_type[kind], intraday_odd=False)
                del self.subscriptions[key]

    def publish(self, kind, quote):
        payload = {field: getattr(quote, field, None) for field in FIELDS[kind]}
        event = f"event: {FEEDS[kind]}\ndata: {json.dumps(payload, default=str, allow_nan=False)}\n\n".encode()
        with self.listener_lock:
            for listener in tuple(self.listeners):
                try:
                    listener.put_nowait(event)
                except queue.Full:
                    self.listeners.remove(listener)
                    # Disconnect this reader so a reconnect can resume from new events.
                    try:
                        listener.get_nowait()
                    except queue.Empty:
                        pass
                    listener.put_nowait(None)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def authenticated(self):
        expected = ("Bearer " + self.server.feed.token).encode()
        presented = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(expected, presented):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return False
        return True

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authenticated():
            return
        if self.path == "/api/v1/info":
            self.send_json(200, {"name": "Shioaji API Server", "simulation": False})
        elif self.path == "/api/v1/stream/data":
            self.stream()
        else:
            self.send_json(404, {"error": "Unknown quote endpoint"})

    def do_POST(self):
        if not self.authenticated():
            return
        prefix = "/api/v1/stream/"
        if self.path not in (prefix + "subscribe", prefix + "unsubscribe"):
            self.send_json(404, {"error": "Unknown quote endpoint"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError
            data = json.loads(self.rfile.read(size))
            if (not isinstance(data, dict) or data.get("security_type") != "STK"
                    or data.get("intraday_odd") is not False or not isinstance(data.get("quote_type"), str)):
                raise ValueError
            self.server.feed.update(self.path == prefix + "subscribe", data.get("exchange"),
                                    data.get("code"), data["quote_type"])
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {"success": False})
            return
        except Exception:
            self.send_json(503, {"success": False})
            return
        self.send_json(200, {"success": True})

    def stream(self):
        feed = self.server.feed
        listener = queue.Queue(maxsize=512)
        with feed.listener_lock:
            accepted = len(feed.listeners) < 4
            if accepted:
                feed.listeners.add(listener)
        if not accepted:
            self.send_json(503, {"error": "Too many readers"})
            return
        self.connection.settimeout(10)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            while listener in feed.listeners:
                try:
                    event = listener.get(timeout=15)
                except queue.Empty:
                    event = b": keep-alive\n\n"
                if event is None:
                    break
                self.wfile.write(event)
        except OSError:
            pass
        finally:
            with feed.listener_lock:
                feed.listeners.discard(listener)


def main():
    import shioaji as sj

    key, secret = os.environ["SJ_API_KEY"], os.environ["SJ_SEC_KEY"]
    api = sj.Shioaji(simulation=False)
    api.login(api_key=key, secret_key=secret, subscribe_trade=False)
    try:
        feed = QuoteFeed(api, {"Tick": sj.QuoteType.Tick, "BidAsk": sj.QuoteType.BidAsk}, f"{key}:{secret}")
        api.set_on_tick_stk_v1_callback(lambda _, quote: feed.publish("Tick", quote))
        api.set_on_bidask_stk_v1_callback(lambda _, quote: feed.publish("BidAsk", quote))
        with ThreadingHTTPServer(("127.0.0.1", 8091), Handler) as server:
            server.feed = feed
            signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
            server.serve_forever()
    finally:
        api.logout()


if __name__ == "__main__":
    main()

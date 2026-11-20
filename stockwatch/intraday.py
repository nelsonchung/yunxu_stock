"""Read-only intraday display from the account owner's local Shioaji daemon."""

import http.client
import json
import math
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
MAX_IDS = 36
SESSION_OPEN = time(9)
SESSION_CLOSE = time(13, 30)
SESSION_END = time(13, 35)
STREAM_PATH = "/api/v1/stream/data"
FEEDS = {"Tick": "tick_stk", "BidAsk": "bidask_stk"}
EXCHANGES = {"TWSE": "TSE", "TPEX": "OTC"}
SYMBOL = re.compile(r"[0-9]{4,6}[A-Z]{0,2}")


def configured_port():
    value = os.environ.get("SHIOAJI_PORT", "").strip()
    return int(value) if re.fullmatch(r"[0-9]{1,5}", value) and 1 <= int(value) <= 65535 else None


def configured_http_token():
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        try:
            return (Path(directory) / "shioaji-http-token").read_text().strip()
        except FileNotFoundError:
            pass
    return None


def trading_time(now):
    return now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_END


def number(value, minimum=None):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        return None
    return result


def price(value):
    result = number(value, minimum=0)
    return result if result else None


def event_date(payload, day):
    if payload.get("simtrade") is True or payload.get("intraday_odd") is True or payload.get("date") != day:
        return None
    try:
        parsed = datetime.fromisoformat(f"{day}T{payload['time']}").replace(tzinfo=TAIPEI)
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if SESSION_OPEN <= parsed.time() < SESSION_END else None


def levels(prices, sizes):
    result = []
    for value, quantity in zip((prices or [])[:5], (sizes or [])[:5]):
        value, quantity = number(value, minimum=0), number(quantity, minimum=0)
        if value is not None and quantity:
            result.append([value, quantity])
    return result


@dataclass
class Quote:
    at: datetime | None = None
    book_at: datetime | None = None
    values: dict = field(default_factory=dict)
    book: dict = field(default_factory=dict)
    points: dict = field(default_factory=dict)

    def apply_tick(self, payload, at, now, previous_day, previous):
        current = price(payload.get("close"))
        if current is None or (self.at is not None and self.at >= at):
            return
        self.at = at
        provider_change = number(payload.get("price_chg"))
        reference = price(round(current - provider_change, 6)) if provider_change is not None else None
        basis = previous if previous is not None else reference
        change = round(current - previous, 6) if previous is not None else provider_change
        high, low = price(payload.get("high")), price(payload.get("low"))
        self.values = {
            "date": at.date().isoformat(), "timestamp": at.isoformat(timespec="seconds"),
            "fetched_at": now, "price": current, "previous_close": previous, "change": change,
            "previous_close_date": previous_day, "reference_price": reference,
            "change_percent": round(change * 100 / basis, 4) if None not in (basis, change) else None,
            "open": price(payload.get("open")), "high": high, "low": low,
            "average": price(payload.get("avg_price")),
            "volume": number(payload.get("total_volume"), minimum=0),
            "value": number(payload.get("total_amount"), minimum=0),
            "amplitude": round((high - low) * 100 / basis, 4) if None not in (high, low, basis) else None,
            "bid_volume": number(payload.get("bid_side_total_vol"), minimum=0),
            "ask_volume": number(payload.get("ask_side_total_vol"), minimum=0),
        }
        minute = at.isoformat(timespec="minutes")
        volume = number(payload.get("volume"), minimum=0)
        if minute not in self.points:
            self.points[minute] = [current, volume]
        else:
            prior = self.points[minute][1]
            self.points[minute] = [current, prior + volume if None not in (prior, volume) else None]

    def apply_book(self, payload, at):
        if self.book_at is not None and self.book_at >= at:
            return
        self.book_at = at
        self.book = {"bids": levels(payload.get("bid_price"), payload.get("bid_volume")),
                     "asks": levels(payload.get("ask_price"), payload.get("ask_volume"))}
        if payload.get("suspend") is True:
            self.book = {"bids": [], "asks": []}

    def snapshot(self, closed):
        return {
            **self.values, "closed": closed,
            "bids": [], "asks": [], **({} if closed else self.book),
            "points": [[minute, *values] for minute, values in sorted(self.points.items())],
        }


def stream_events(response):
    name, data, size = None, [], 0
    for line in iter(lambda: response.readline(65_537), b""):
        size += len(line)
        if size > 65_536:
            raise ValueError("Shioaji SSE event exceeds size limit")
        line = line.rstrip(b"\r\n")
        if not line:
            if name in FEEDS.values() and data:
                try:
                    payload = json.loads(b"\n".join(data))
                except (ValueError, UnicodeError):
                    payload = None
                if isinstance(payload, dict):
                    yield name, payload
            name, data, size = None, [], 0
        elif line.startswith(b"event:"):
            name = line[6:].strip().decode()
        elif line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))


class Intraday:
    def __init__(self, store, now, port, http_token=None):
        self.store, self.now, self.port = store, now, port
        self.auth_headers = {"Authorization": f"Bearer {http_token}"} if http_token else {}
        self.lock = threading.Lock()
        self.tracked = {}
        self.pending_minutes = {}
        self.quotes = {}
        self.day = None
        self.subscriptions = set()
        self.receiver = None
        self.stream_cancel = None
        self.stream_socket = None
        self.generation = 0
        self.replayed_generation = 0
        self.synced = set()
        self.connected = False
        self.retry_at = 0
        self.reconnect_delay = 5
        self.stopping = False
        self.status = ""

    def query_stocks(self, ids, day):
        with self.store.connect() as db:
            enabled = self.store.settings(db)["enabled"]
            if ids == []:
                return enabled, []
            scope = f"AND s.id IN ({','.join('?' for _ in ids)})" if ids is not None else ""
            return enabled, list(db.execute(f"""
                SELECT s.id,s.market,s.symbol,p.day,p.close FROM stocks s
                LEFT JOIN prices p ON p.market=s.market AND p.symbol=s.symbol AND p.day=(
                    SELECT max(day) FROM prices WHERE market=s.market AND symbol=s.symbol AND day<?
                )
                WHERE s.market IN ('TWSE','TPEX') {scope}
                ORDER BY s.id LIMIT ?
            """, (day, *(ids or []), MAX_IDS)))

    def roll_day(self, day):
        if self.day != day:
            self.day = day
            self.tracked.clear()
            self.quotes.clear()

    def stored_quote(self, symbol, day):
        if symbol not in self.quotes:
            with self.store.connect() as db:
                points = {r["minute"]: [r["close"], r["volume"]] for r in db.execute("""
                    SELECT minute,close,volume FROM intraday_minutes
                    WHERE day=? AND symbol=? ORDER BY minute
                """, (day, symbol))}
            self.quotes[symbol] = Quote(points=points)
        return self.quotes[symbol]

    def flush_minutes(self):
        if not self.pending_minutes:
            return
        with self.store.connect() as db:
            db.executemany("INSERT OR REPLACE INTO intraday_minutes VALUES (?,?,?,?,?)",
                           ((*key, *point) for key, point in self.pending_minutes.items()))
        self.pending_minutes.clear()

    def snapshot(self, ids):
        now = self.now()
        local = datetime.fromtimestamp(now, TAIPEI)
        session = trading_time(local)
        state = {"configured": bool(self.port), "session": session, "provider": "Shioaji", "status": "", "quotes": {}}
        if not self.port:
            state["status"] = "尚未設定 Shioaji 即時行情"
            return state
        enabled, rows = self.query_stocks(ids, local.date().isoformat())
        with self.lock:
            self.roll_day(local.date().isoformat())
            for row in rows:
                if not SYMBOL.fullmatch(row["symbol"]):
                    continue
                quote = self.stored_quote(row["symbol"], self.day)
                if quote.values:
                    state["quotes"][str(row["id"])] = quote.snapshot(local.time() >= SESSION_CLOSE)
                elif quote.points:
                    minute = max(quote.points)
                    state["quotes"][str(row["id"])] = {
                        **quote.snapshot(True), "date": self.day, "timestamp": minute,
                        "price": quote.points[minute][0], "fetched_at": 0,
                        "previous_close": price(row["close"]), "history_only": True,
                    }
            if not enabled:
                state["status"] = "背景更新已暫停"
            elif not session:
                state["status"] = "目前非台股盤中時段"
            elif self.status:
                state["status"] = self.status
            elif not state["quotes"]:
                state["status"] = "等待永豐金證券盤中行情"
        return state

    def sync(self):
        if not self.port:
            return
        now = self.now()
        local = datetime.fromtimestamp(now, TAIPEI)
        day = local.date().isoformat()
        enabled, rows = self.query_stocks(None if trading_time(local) else [], day)
        with self.lock:
            self.flush_minutes()
            self.roll_day(day)
            self.tracked = {row["symbol"]: (EXCHANGES[row["market"]], row["day"], price(row["close"]))
                            for row in rows if enabled and SYMBOL.fullmatch(row["symbol"])}
            desired = {(exchange, symbol, kind) for symbol, (exchange, _, _) in self.tracked.items()
                       for kind in FEEDS}
        if not desired:
            self.release()
            return
        with self.lock:
            if self.stopping or now < self.retry_at:
                return
            running = self.receiver and self.receiver.is_alive()
            if not running:
                self.stream_cancel = threading.Event()
                self.receiver = threading.Thread(target=self.receive, args=(self.stream_cancel,),
                                                 name="shioaji-sse", daemon=True)
                self.receiver.start()
                return
            if not self.connected:
                return
            generation = self.generation
        if generation != self.replayed_generation:
            self.synced.clear()
            self.replayed_generation = generation
        try:
            for key in sorted(self.subscriptions - desired):
                self.subscription("unsubscribe", key)
                self.subscriptions.remove(key)
                self.synced.discard(key)
            for key in sorted(desired - self.synced):
                self.subscription("subscribe", key)
                self.subscriptions.add(key)
                self.synced.add(key)
        except (OSError, http.client.HTTPException, ValueError):
            with self.lock:
                self.status = "Shioaji 訂閱暫時無法更新"
                self.retry_at = self.now() + 30
            return
    def subscription(self, action, subscription):
        exchange, code, kind = subscription
        body = json.dumps({"security_type": "STK", "exchange": exchange, "code": code,
                           "quote_type": kind, "intraday_odd": False})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("POST", f"/api/v1/stream/{action}", body,
                               self.auth_headers | {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                if not 200 <= response.status < 300:
                    raise OSError("Shioaji subscription rejected")
        finally:
            connection.close()

    def server_mode(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", "/api/v1/info", headers=self.auth_headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    return None
                info = json.loads(response.read(8193))
                return info.get("simulation") if info.get("name") == "Shioaji API Server" else None
        finally:
            connection.close()

    def receive(self, cancel):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=45)
        failure = "無法連線至本機 Shioaji 行情服務，稍後重試"
        stream_socket = None
        try:
            if self.server_mode() is not False:
                failure = "Shioaji 未確認正式行情模式"
                return
            if cancel.is_set():
                return
            connection.request("GET", STREAM_PATH, headers=self.auth_headers | {"Accept": "text/event-stream"})
            stream_socket = connection.sock
            with self.lock:
                if self.stopping or cancel.is_set():
                    return
                self.stream_socket = stream_socket
            with connection.getresponse() as response:
                if response.status != 200 or not response.getheader("Content-Type", "").startswith("text/event-stream"):
                    raise OSError("Shioaji stream unavailable")
                with self.lock:
                    if cancel.is_set():
                        return
                    self.connected = True
                    self.status = ""
                    self.generation += 1
                for kind, payload in stream_events(response):
                    if cancel.is_set():
                        return
                    self.ingest(kind, payload)
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            pass
        finally:
            connection.close()
            with self.lock:
                if self.stream_socket is stream_socket:
                    self.stream_socket = None
                self.connected = False
                if not self.stopping and not cancel.is_set():
                    self.status = failure
                    self.retry_at = self.now() + self.reconnect_delay
                    self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    def ingest(self, kind, payload):
        now = self.now()
        local = datetime.fromtimestamp(now, TAIPEI)
        if not trading_time(local) or not isinstance(payload, dict):
            return
        day = local.date().isoformat()
        at = event_date(payload, day)
        if at is None or at.timestamp() > now + 5:
            return
        code = payload.get("code")
        with self.lock:
            tracked = self.tracked.get(code)
            if self.day != day or tracked is None:
                return
            quote = self.stored_quote(code, day)
            if kind == FEEDS["Tick"]:
                quote.apply_tick(payload, at, now, *tracked[1:])
                if quote.at == at:
                    minute = at.isoformat(timespec="minutes")
                    self.pending_minutes[(day, code, minute)] = quote.points[minute]
            elif kind == FEEDS["BidAsk"]:
                quote.apply_book(payload, at)
            self.status = ""
            self.reconnect_delay = 5

    def release(self):
        with self.lock:
            if self.stream_cancel:
                self.stream_cancel.set()
            stream_socket = self.stream_socket
            self.stream_socket = None
            self.connected = False
        if stream_socket:
            try:
                stream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # Keep unsuccessful removals pending and retry only at normal backoff.
        if self.subscriptions and self.now() >= self.retry_at:
            try:
                for key in sorted(self.subscriptions):
                    self.subscription("unsubscribe", key)
                    self.subscriptions.remove(key)
                    self.synced.discard(key)
            except (OSError, http.client.HTTPException, ValueError):
                with self.lock:
                    self.retry_at = self.now() + 30

    def close(self):
        with self.lock:
            self.stopping = True
            self.tracked.clear()
            self.flush_minutes()
        self.release()

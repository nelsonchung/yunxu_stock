import csv
import io
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .store import MARKETS, month_key

LOG = logging.getLogger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
CATALOG = {
    "TWSE": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
    "US": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt",
}
HISTORY = {
    "TWSE": "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
    "TPEX": "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock",
    "US": "https://www.alphavantage.co/query",
}
HOSTS = {urlsplit(url).hostname for url in (*CATALOG.values(), *HISTORY.values())}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ProviderError(Exception):
    def __init__(self, message, delay=0):
        super().__init__(message)
        self.delay = delay


def retry_after(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def download(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS:
        raise ProviderError("行情來源未授權")
    try:
        req = Request(url, headers={"User-Agent": "YunxuStock/1.0 (personal stock watchlist)"})
        with build_opener(NoRedirect).open(req, timeout=15) as response:
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise ProviderError("來源回應超出大小限制")
            return body.decode("utf-8-sig")
    except HTTPError as exc:
        if exc.code in (403, 429):
            raise ProviderError("來源已拒絕或限制請求，暫停更新",
                                max(86400, retry_after(exc.headers.get("Retry-After")))) from None
        raise ProviderError("來源暫時無法回應", retry_after(exc.headers.get("Retry-After"))) from None
    except (OSError, UnicodeError):
        raise ProviderError("暫時無法連線至行情來源") from None


def parse_catalog(market, content):
    if market == "US":
        rows = csv.DictReader(io.StringIO(content), delimiter="|")
        entries = {r["Symbol"]: r["Security Name"].strip() for r in rows
                   if r.get("Test Issue") == "N" and r.get("Symbol") and r.get("Security Name")}
    else:
        code, name = (("Code", "Name") if market == "TWSE"
                      else ("SecuritiesCompanyCode", "CompanyName"))
        entries = {r[code].strip(): r[name].strip() for r in json.loads(content)
                   if r.get(code) and r.get(name)}
    result = {code: name for code, name in entries.items()
              if re.fullmatch(r"[A-Z0-9][A-Z0-9.$^/+\-=]{0,19}", code) and name}
    if not result:
        raise ProviderError("來源沒有回傳有效股票名錄")
    return result


def roc_date(value):
    y, m, d = map(int, value.split("/"))
    return date(y + 1911, m, d).isoformat()


def parse_history(market, contents, period):
    payload = json.loads(contents)
    if market == "US":
        if "Note" in payload or "Information" in payload:
            raise ProviderError("美股來源限制請求；請確認金鑰、額度與方案", 86400)
        if "Time Series (Daily)" not in payload:
            raise ProviderError("美股來源未提供日線；請確認股票代碼與金鑰", 86400)
        raw = ((date.fromisoformat(day).isoformat(), row["4. close"])
               for day, row in payload["Time Series (Daily)"].items())
    else:
        if market == "TWSE":
            if payload.get("stat") == "OK":
                rows = payload["data"]
            elif payload.get("stat") in ("很抱歉，沒有符合條件的資料!", "很抱歉，沒有符合條件的資料！"):
                rows = []
            else:
                raise ProviderError("證交所日線格式異常或暫時無法查詢")
        else:
            if payload.get("stat") != "ok":
                raise ProviderError("櫃買中心日線格式異常或暫時無法查詢")
            rows = payload["tables"][0]["data"]
        raw = ((roc_date(row[0]), row[6]) for row in rows)
    points = []
    for day, value in raw:
        if value.strip() in ("", "--", "---", "----", "-"):
            continue
        close = float(value.replace(",", ""))
        if math.isfinite(close) and close > 0 and (not period or day[:7] == period[:7]):
            points.append((day, close))
    return points


class Worker:
    def __init__(self, store, fetch=download, now=time.time):
        self.store, self.fetch, self.now = store, fetch, now
        self.us_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
        self.us_full = os.environ.get("ALPHA_VANTAGE_FULL") == "1"

    def reserve(self, source, interval, ceiling, *, url=None):
        now = self.now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if url:
                gates = ("global", "host:" + urlsplit(url).hostname)
                if db.execute("SELECT 1 FROM sources WHERE name IN (?,?) AND next_at>?",
                              (*gates, now)).fetchone():
                    return False
            db.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))
            if db.execute("SELECT next_at FROM sources WHERE name=?", (source,)).fetchone()[0] > now:
                return False
            db.execute("DELETE FROM requests WHERE attempted<=?", (now - 86400,))
            times = db.execute("SELECT count(*), min(attempted) FROM requests WHERE source=?",
                               (source,)).fetchone()
            if times[0] >= ceiling:
                db.execute("UPDATE sources SET next_at=? WHERE name=?", (times[1] + 86400, source))
                return False
            db.execute("INSERT INTO requests VALUES (?,?)", (source, now))
            db.execute("UPDATE sources SET next_at=? WHERE name=?", (now + interval, source))
            if url:
                db.execute("INSERT OR REPLACE INTO sources(name,next_at) VALUES ('global',?)", (now + 5,))
            return True

    def result(self, source, error=None, *, success_interval=0, url=None):
        with self.store.connect() as db:
            if error is None:
                db.execute("UPDATE sources SET error='', failures=0, next_at=max(next_at,?) WHERE name=?",
                           (self.now() + success_interval, source))
            else:
                count = db.execute("SELECT failures FROM sources WHERE name=?", (source,)).fetchone()[0]
                delay = max(error.delay, min(86400, 900 * 2**min(count, 7)))
                db.execute("""UPDATE sources SET next_at=max(next_at,?), error=?, failures=failures+1
                              WHERE name=?""", (self.now() + delay, str(error), source))
                if url and error.delay >= 86400:
                    db.execute("INSERT OR REPLACE INTO sources(name,next_at) VALUES (?,?)",
                               ("host:" + urlsplit(url).hostname, self.now() + delay))
                LOG.warning("%s: %s", source, error)

    def attempt(self, source, work, *, url, success_interval=0):
        try:
            work()
        except ProviderError as exc:
            self.result(source, exc, url=url)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            self.result(source, ProviderError("行情來源回應格式異常，稍後重試"))
        else:
            self.result(source, success_interval=success_interval)

    def tick(self):
        now = self.now()
        for market, url in CATALOG.items():
            source = market + "_catalog"
            if self.reserve(source, 60, 3, url=url):
                self.attempt(source, lambda: self.store.replace_catalog(
                    market, parse_catalog(market, self.fetch(url))), url=url, success_interval=86400)
                return

        for market in MARKETS:
            if market == "US" and not self.us_key:
                continue
            zone = NEW_YORK if market == "US" else TAIPEI
            today = datetime.fromtimestamp(now, zone).date()
            with self.store.connect() as db:
                settings = self.store.settings(db)
                if not settings["enabled"]:
                    return
                candidates = list(db.execute("""
                    SELECT s.symbol FROM stocks s LEFT JOIN poll_order p
                    USING(market,symbol) WHERE s.market=? ORDER BY coalesce(p.attempted,0), s.id
                """, (market,)))
                cached = {(r["symbol"], r["period"]): r["updated"] for r in db.execute(
                    "SELECT symbol,period,updated FROM fetches WHERE market=?", (market,))}
            periods = ([""] if market == "US" else
                       [month_key(today, i) for i in range(settings["months"] + 1)])
            selected = None
            for row in candidates:
                symbol = row["symbol"]
                for i, period in enumerate(periods):
                    updated = cached.get((symbol, period), 0)
                    if not updated or (i == 0 and now - updated >= 86400) or (
                            i > 0 and datetime.fromtimestamp(updated, zone).date().isoformat()
                            < periods[i - 1]):
                        selected = symbol, period
                        break
                if selected:
                    break
            source = market + "_history"
            url = HISTORY[market]
            if selected and self.reserve(source, 60, 20 if market == "US" else 48, url=url):
                with self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO poll_order VALUES (?,?,?)",
                               (market, selected[0], now))
                self.attempt(source, lambda: self.history(market, *selected, now), url=url)
                return

    def history(self, market, symbol, period, now):
        if market == "TWSE":
            params = {"date": period.replace("-", ""), "stockNo": symbol, "response": "json"}
        elif market == "TPEX":
            params = {"date": period.replace("-", "/"), "code": symbol, "response": "json"}
        else:
            params = {"function": "TIME_SERIES_DAILY", "symbol": symbol,
                      "outputsize": "full" if self.us_full else "compact", "apikey": self.us_key}
        points = parse_history(market, self.fetch(HISTORY[market] + "?" + urlencode(params)), period)
        with self.store.connect() as db:
            db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                           ((market, symbol, day, close) for day, close in points))
            db.execute("INSERT OR REPLACE INTO fetches VALUES (?,?,?,?)",
                       (market, symbol, period, now))

    def run(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                LOG.error("Background update failed: %s", type(exc).__name__)
            stop.wait(5)

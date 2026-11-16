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

from .store import range_start

LOG = logging.getLogger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")
CATALOG = {
    "TWSE": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
    "US": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt",
}
INDUSTRIES = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
}
US_HISTORY = "https://www.alphavantage.co/query"
FINMIND_HISTORY = "https://api.finmindtrade.com/api/v4/data"
QUERY_ENDPOINTS = {US_HISTORY, FINMIND_HISTORY}
ALLOWED_ENDPOINTS = {*CATALOG.values(), *INDUSTRIES.values(), *QUERY_ENDPOINTS}


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
    endpoint = parts._replace(query="", fragment="").geturl()
    if (endpoint not in ALLOWED_ENDPOINTS or parts.fragment
            or (parts.query and endpoint not in QUERY_ENDPOINTS)):
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


def valid_symbol(symbol):
    return re.fullmatch(r"[A-Z0-9][A-Z0-9.$^/+\-=]{0,19}", symbol)


def parse_catalog(content):
    rows = csv.DictReader(io.StringIO(content), delimiter="|")
    entries = {r["Symbol"]: r["Security Name"].strip() for r in rows
               if r.get("Test Issue") == "N" and r.get("Symbol") and r.get("Security Name")}
    result = {code: name for code, name in entries.items() if valid_symbol(code) and name}
    if not result:
        raise ProviderError("來源沒有回傳有效股票名錄")
    return result


def parse_daily_snapshot(market, content):
    code_key, name_key, fields = (
        ("Code", "Name", ("OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice", "TradeVolume"))
        if market == "TWSE" else
        ("SecuritiesCompanyCode", "CompanyName", ("Open", "High", "Low", "Close", "TradingShares"))
    )
    catalog, prices = {}, []
    for row in json.loads(content):
        symbol, name = row[code_key].strip(), row[name_key].strip()
        if not valid_symbol(symbol) or not name:
            continue
        catalog[symbol] = name
        bar = daily_bar(row, fields)
        if bar is None:
            continue
        stamp = row["Date"].strip()
        if not re.fullmatch(r"[0-9]{7}", stamp):
            raise ProviderError("盤後行情日期格式異常")
        day = date(int(stamp[:3]) + 1911, int(stamp[3:5]), int(stamp[5:])).isoformat()
        prices.append((symbol, day, *bar))
    if not catalog:
        raise ProviderError("來源沒有回傳有效股票名錄")
    return catalog, prices


def parse_industries(market, content):
    symbol, industry, capital = (
        ("公司代號", "產業別", "實收資本額") if market == "TWSE" else
        ("SecuritiesCompanyCode", "SecuritiesIndustryCode", "Paidin.Capital.NTDollars")
    )
    entries = {}
    for company in json.loads(content):
        ticker = company[symbol].strip()
        code = company[industry].strip().zfill(2)
        if re.fullmatch(r"[0-9]{4}[A-Z]?", ticker) and re.fullmatch(r"[0-9]{2}", code):
            paid = (company.get(capital) or "").replace(",", "").strip()
            entries[ticker] = (ticker, code, int(paid) if re.fullmatch(r"[0-9]{1,18}", paid) else 0)
    if not entries:
        raise ProviderError("來源沒有回傳有效的產業分類")
    return entries.values()


def price_value(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(str(value).replace(",", ""))
    except (ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def daily_bar(row, fields):
    opening, high, low, close = (price_value(row.get(key)) for key in fields[:4])
    if close is None:
        return None
    raw_volume = str(row.get(fields[4], "")).replace(",", "").strip()
    volume = int(raw_volume) if re.fullmatch(r"[0-9]{1,18}", raw_volume) else None
    if None in (opening, high, low, volume) or not low <= min(opening, close) <= max(opening, close) <= high:
        return (None, None, None, close, None)
    return (opening, high, low, close, volume)


def parse_us_history(contents):
    payload = json.loads(contents)
    if "Note" in payload or "Information" in payload:
        raise ProviderError("美股來源限制請求；請確認金鑰、額度與方案", 86400)
    if "Time Series (Daily)" not in payload:
        raise ProviderError("美股來源未提供日線；請確認股票代碼與金鑰", 86400)
    points = []
    for day, row in payload["Time Series (Daily)"].items():
        bar = daily_bar(row, ("1. open", "2. high", "3. low", "4. close", "5. volume"))
        if bar is not None:
            points.append((date.fromisoformat(day).isoformat(), *bar))
    return points


def parse_finmind_history(content, symbol, start, end):
    payload = json.loads(content)
    if payload.get("status") in (402, 403, 429):
        raise ProviderError("FinMind 拒絕或限制請求，歷史 K 線暫停更新", 86400)
    if payload.get("status") != 200:
        raise ProviderError("FinMind 歷史 K 線暫時無法取得")
    points = []
    for row in payload["data"]:
        day = date.fromisoformat(row["date"]).isoformat()
        if row["stock_id"] != symbol or not start <= day <= end:
            continue
        bar = daily_bar(row, ("open", "max", "min", "close", "Trading_Volume"))
        if bar is not None:
            points.append((symbol, day, *bar))
    return points


class Worker:
    def __init__(self, store, fetch=download, now=time.time):
        self.store, self.fetch, self.now = store, fetch, now
        self.us_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
        self.us_full = os.environ.get("ALPHA_VANTAGE_FULL") == "1"
        self.finmind_history = os.environ.get("FINMIND_HISTORY") == "1"

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
            source = market + ("_catalog" if market == "US" else "_daily")
            if self.reserve(source, 60, 3, url=url):
                self.attempt(source, lambda: self.refresh_catalog(market, url),
                             url=url, success_interval=86400)
                return

        for market, url in INDUSTRIES.items():
            source = market + "_industries"
            if self.reserve(source, 60, 3, url=url):
                self.attempt(source, lambda: self.store.replace_industries(
                    market, parse_industries(market, self.fetch(url))), url=url, success_interval=7 * 86400)
                return

        if self.finmind_history and self.backfill_tw(now):
            return
        if not self.us_key:
            return
        with self.store.connect() as db:
            if not self.store.settings(db)["enabled"]:
                return
            selected = db.execute("""
                SELECT s.symbol FROM stocks AS s
                LEFT JOIN poll_order AS p USING(market,symbol)
                LEFT JOIN fetches AS f ON f.market=s.market AND f.symbol=s.symbol AND f.period=''
                WHERE s.market='US' AND (f.updated IS NULL OR f.updated<=?)
                ORDER BY coalesce(p.attempted,0), s.id LIMIT 1
            """, (now - 86400,)).fetchone()
        source = "US_history"
        if selected and self.reserve(source, 60, 20, url=US_HISTORY):
            symbol = selected["symbol"]
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO poll_order VALUES ('US',?,?)", (symbol, now))
            self.attempt(source, lambda: self.us_history(symbol, now), url=US_HISTORY)

    def refresh_catalog(self, market, url):
        content = self.fetch(url)
        if market == "US":
            self.store.replace_catalog(market, parse_catalog(content))
        else:
            catalog, prices = parse_daily_snapshot(market, content)
            self.store.replace_catalog(market, catalog, prices)

    def us_history(self, symbol, now):
        params = {"function": "TIME_SERIES_DAILY", "symbol": symbol,
                  "outputsize": "full" if self.us_full else "compact", "apikey": self.us_key}
        points = parse_us_history(self.fetch(US_HISTORY + "?" + urlencode(params)))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.store.settings(db)["enabled"]:
                return
            self.store.save_prices(db, "US", ((symbol, *point) for point in points), "Alpha Vantage")
            db.execute("INSERT OR REPLACE INTO fetches VALUES ('US',?,'',?)", (symbol, now))

    def backfill_tw(self, now):
        with self.store.connect() as db:
            settings = self.store.settings(db)
            if not settings["enabled"]:
                return False
            stock = db.execute("""
                SELECT s.market,s.symbol FROM stocks AS s
                WHERE s.market IN ('TWSE','TPEX') AND NOT EXISTS (
                    SELECT 1 FROM fetches AS f WHERE f.market=s.market AND f.symbol=s.symbol
                    AND f.period LIKE 'finmind:%' AND CAST(substr(f.period,9) AS INTEGER)>=?
                    AND f.updated>?
                )
                ORDER BY coalesce((SELECT max(f.updated) FROM fetches AS f
                    WHERE f.market=s.market AND f.symbol=s.symbol AND f.period LIKE 'finmind:%'),0),s.id
                LIMIT 1
            """, (settings["months"], now - 7 * 86400)).fetchone()
        source = "FinMind_history"
        if not stock or not self.reserve(source, 60, 12, url=FINMIND_HISTORY):
            return False
        self.attempt(source, lambda: self.refresh_tw_history(
            stock["market"], stock["symbol"], settings["months"], now), url=FINMIND_HISTORY)
        return True

    def refresh_tw_history(self, market, symbol, months, now):
        today = datetime.fromtimestamp(now, TAIPEI).date()
        start, end = range_start(today, months).isoformat(), today.isoformat()
        params = {"dataset": "TaiwanStockPrice", "data_id": symbol, "start_date": start, "end_date": end}
        points = parse_finmind_history(self.fetch(FINMIND_HISTORY + "?" + urlencode(params)),
                                       symbol, start, end)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.store.settings(db)["enabled"]:
                return
            self.store.save_prices(db, market, points, "FinMind")
            db.execute("INSERT OR REPLACE INTO fetches VALUES (?,?,?,?)",
                       (market, symbol, f"finmind:{months}", now))

    def run(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                LOG.error("Background update failed: %s", type(exc).__name__)
            stop.wait(5)

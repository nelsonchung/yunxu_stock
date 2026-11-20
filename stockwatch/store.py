from calendar import monthrange
import sqlite3
from contextlib import contextmanager
from datetime import date
import unicodedata

MARKETS = ("TWSE", "TPEX", "US")
MONTHS = (0, 1, 3, 6, 12, 24)
FOUNDRIES = (("TWSE", "2330"), ("TWSE", "2303"), ("TWSE", "6770"), ("TPEX", "5347"))


def month_key(day, offset=0):
    year, month = divmod(day.year * 12 + day.month - 1 - offset, 12)
    return date(year, month + 1, 1).isoformat()


def range_start(day, months):
    start = date.fromisoformat(month_key(day, months))
    return start.replace(day=min(day.day, monthrange(start.year, start.month)[1]))


def normalize(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


class InputError(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), months INTEGER,
                    enabled INTEGER, rows INTEGER, columns INTEGER);
                INSERT OR IGNORE INTO settings VALUES (1, 3, 1, 2, 2);
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, key TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS catalog (
                    market TEXT, symbol TEXT, name TEXT, search_name TEXT,
                    PRIMARY KEY(market,symbol));
                CREATE TABLE IF NOT EXISTS industries (
                    market TEXT, symbol TEXT, code TEXT NOT NULL, paid_capital INTEGER NOT NULL,
                    PRIMARY KEY(market,symbol));
                CREATE INDEX IF NOT EXISTS industry_peers ON industries(code, paid_capital DESC);
                CREATE TABLE IF NOT EXISTS stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL,
                    symbol TEXT NOT NULL, name TEXT NOT NULL,
                    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                    UNIQUE(market,symbol));
                CREATE TABLE IF NOT EXISTS prices (
                    market TEXT, symbol TEXT, day TEXT, close REAL,
                    PRIMARY KEY(market,symbol,day));
                CREATE TABLE IF NOT EXISTS intraday_minutes (
                    day TEXT, symbol TEXT, minute TEXT, close REAL, volume REAL,
                    PRIMARY KEY(day,symbol,minute));
                CREATE TABLE IF NOT EXISTS fetches (
                    market TEXT, symbol TEXT, period TEXT, updated REAL,
                    PRIMARY KEY(market,symbol,period));
                CREATE TABLE IF NOT EXISTS poll_order (
                    market TEXT, symbol TEXT, attempted REAL,
                    PRIMARY KEY(market,symbol));
                CREATE TABLE IF NOT EXISTS sources (
                    name TEXT PRIMARY KEY, next_at REAL DEFAULT 0,
                    error TEXT DEFAULT '', failures INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS requests (
                    source TEXT, attempted REAL);
                CREATE INDEX IF NOT EXISTS request_window ON requests(source,attempted);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(prices)")}
            for name, kind in (("open", "REAL"), ("high", "REAL"), ("low", "REAL"),
                               ("volume", "INTEGER"), ("source", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE prices ADD COLUMN {name} {kind}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def settings(db):
        row = dict(db.execute("SELECT months, enabled, rows, columns FROM settings").fetchone())
        row["enabled"] = bool(row["enabled"])
        return row

    @staticmethod
    def finmind_coverage(db, months):
        rows = db.execute("""
            SELECT market,symbol,max(updated) AS updated FROM fetches
            WHERE period='finmind:all' OR (
                ?>0 AND period LIKE 'finmind:%' AND CAST(substr(period,9) AS INTEGER)>=?
            )
            GROUP BY market,symbol
        """, (months, months))
        return {(row["market"], row["symbol"]): row["updated"] for row in rows}

    def search(self, q, market):
        q = normalize(q)
        if not q or len(q) > 80 or market not in ("", *MARKETS):
            return []
        with self.connect() as db:
            return [dict(r) for r in db.execute("""
                SELECT market,symbol,name FROM catalog
                WHERE (?='' OR market=?) AND
                      (instr(lower(symbol),?)>0 OR instr(search_name,?)>0)
                ORDER BY CASE WHEN lower(symbol)=? THEN 0 WHEN instr(lower(symbol),?)=1 THEN 1
                    WHEN instr(search_name,?)=1 THEN 2 ELSE 3 END, length(symbol),symbol,market
                LIMIT 12
            """, (market, market, q, q, q, q, q))]

    @staticmethod
    def save_prices(db, market, points, source):
        db.executemany("""
            INSERT INTO prices(market,symbol,day,open,high,low,close,volume,source)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market,symbol,day) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, source=excluded.source
            WHERE
                (excluded.open IS NOT NULL OR prices.open IS NULL) AND
                (excluded.source<>'FinMind' OR prices.source IS NULL OR prices.source='FinMind'
                 OR prices.open IS NULL)
        """, ((market, *point, source) for point in points))

    def replace_catalog(self, market, entries, prices=()):
        with self.connect() as db:
            db.execute("DELETE FROM catalog WHERE market=?", (market,))
            db.executemany("INSERT INTO catalog VALUES (?,?,?,?)",
                           ((market, code, name, normalize(name)) for code, name in entries.items()))
            if prices and self.settings(db)["enabled"]:
                self.save_prices(db, market, prices, market)

    def replace_industries(self, market, entries):
        with self.connect() as db:
            db.execute("DELETE FROM industries WHERE market=?", (market,))
            db.executemany("INSERT INTO industries VALUES (?,?,?,?)",
                           ((market, *entry) for entry in entries))

    @staticmethod
    def related(db, key):
        related = []
        if key in FOUNDRIES:
            for peer in FOUNDRIES:
                if peer == key:
                    continue
                item = db.execute("SELECT market,symbol,name FROM catalog WHERE market=? AND symbol=?",
                                  peer).fetchone()
                if item:
                    related.append(dict(item, relationship="晶圓代工同行"))

        sector_peers = db.execute("""
            SELECT c.market,c.symbol,c.name
            FROM industries AS target
            JOIN industries AS peer ON peer.code=target.code
            JOIN catalog AS c ON c.market=peer.market AND c.symbol=peer.symbol
            WHERE target.market=? AND target.symbol=?
                AND target.code NOT IN ('00', '20', '80')
                AND (peer.market<>target.market OR peer.symbol<>target.symbol)
            ORDER BY peer.paid_capital DESC, c.market, c.symbol
            LIMIT 8
        """, key)
        seen = {(item["market"], item["symbol"]) for item in related}
        related.extend(dict(item, relationship="同產業") for item in sector_peers
                       if (item["market"], item["symbol"]) not in seen)
        return related[:8]

    def state(self, today, us_configured, us_full, finmind_configured=False):
        with self.connect() as db:
            settings = self.settings(db)
            categories = [dict(row) for row in db.execute("SELECT id,name FROM categories ORDER BY id")]
            errors = {r["name"]: r["error"] for r in db.execute("SELECT name,error FROM sources")}
            stocks = []
            months = settings["months"]
            since = (range_start(today, months) if months else date.min).isoformat()
            coverage = self.finmind_coverage(db, months) if finmind_configured else {}
            for row in db.execute("SELECT * FROM stocks ORDER BY id"):
                s = dict(row)
                key = (s["market"], s["symbol"])
                s["related"] = self.related(db, key)
                prices = list(db.execute("""
                    SELECT day,open,high,low,close,volume FROM prices
                    WHERE market=? AND symbol=? AND day BETWEEN ? AND ? ORDER BY day
                """, (*key, since, today.isoformat())))
                s["history"] = [[p["day"], p["close"]] for p in prices]
                s["candles"] = [list(p) for p in prices if all(value is not None for value in list(p)[1:])]
                status = "等待背景更新"
                source = s["market"] + "_daily"
                if s["market"] == "US":
                    source = "US_history"
                    if s["history"]:
                        status = "日線已載入"
                    if not us_configured:
                        status = "美股行情需要 API 金鑰"
                    elif not us_full and (months == 0 or months > 3):
                        status = "目前方案最多最近 100 個交易日"
                elif s["history"]:
                    status = "每日累積盤後日線"
                if s["market"] != "US" and finmind_configured and settings["enabled"]:
                    if errors.get("FinMind_history"):
                        status = errors["FinMind_history"]
                    elif key not in coverage:
                        status = "等待補齊歷史 K 線"
                if errors.get(source) and settings["enabled"]:
                    status = errors[source]
                if not settings["enabled"] and (s["market"] != "US" or us_configured):
                    status = "已暫停背景更新"
                s["status"] = status
                stocks.append(s)
            return {"settings": settings, "categories": categories, "stocks": stocks,
                    "us_configured": us_configured}

    def action(self, data):
        def integer(key, *, nullable=False):
            value = data.get(key)
            if nullable and value is None:
                return None
            if type(value) is not int or not 1 <= value <= 2**63-1:
                raise InputError("欄位格式有誤")
            return value

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            action = data.get("action")
            if action == "settings":
                months = data.get("months")
                rows, columns = (integer(key) for key in ("rows", "columns"))
                enabled = data.get("enabled")
                if (type(months) is not int or months not in MONTHS
                        or rows > 6 or columns > 6 or type(enabled) is not bool):
                    raise InputError("請選擇有效的歷史區間與看板格數")
                db.execute("UPDATE settings SET months=?, enabled=?, rows=?, columns=?",
                           (months, enabled, rows, columns))
            elif action == "add_category":
                raw = data.get("name")
                name = raw.strip() if isinstance(raw, str) else ""
                if not 1 <= len(name) <= 40:
                    raise InputError("分類名稱須為 1 到 40 個字")
                try:
                    db.execute("INSERT INTO categories(name,key) VALUES (?,?)", (name, normalize(name)))
                except sqlite3.IntegrityError as exc:
                    raise InputError("這個分類已存在") from exc
            elif action == "remove_category":
                db.execute("DELETE FROM categories WHERE id=?", (integer("id"),))
            elif action == "remove_stock":
                db.execute("DELETE FROM stocks WHERE id=?", (integer("id"),))
            elif action in ("move_stock", "add_stock"):
                category = integer("category_id", nullable=True)
                if category is not None and not db.execute(
                        "SELECT 1 FROM categories WHERE id=?", (category,)).fetchone():
                    raise InputError("找不到這個分類")
                if action == "move_stock":
                    db.execute("UPDATE stocks SET category_id=? WHERE id=?", (category, integer("id")))
                else:
                    market, symbol = data.get("market"), data.get("symbol")
                    if market not in MARKETS or not isinstance(symbol, str) or len(symbol) > 20:
                        raise InputError("請從搜尋結果選擇一檔股票")
                    match = db.execute("SELECT name FROM catalog WHERE market=? AND symbol=?",
                                       (market, symbol)).fetchone()
                    if not match:
                        raise InputError("這檔股票不在目前的名錄中")
                    present = db.execute("SELECT 1 FROM stocks WHERE market=? AND symbol=?",
                                         (market, symbol)).fetchone()
                    if present:
                        raise InputError("已追蹤這檔股票")
                    db.execute("INSERT INTO stocks(market,symbol,name,category_id) VALUES (?,?,?,?)",
                               (market, symbol, match["name"], category))
            else:
                raise InputError("未知的操作")

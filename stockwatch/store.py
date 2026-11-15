from calendar import monthrange
import sqlite3
from contextlib import contextmanager
from datetime import date
import unicodedata

MARKETS = ("TWSE", "TPEX", "US")
MONTHS = (1, 3, 6, 12, 24)


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
                CREATE TABLE IF NOT EXISTS stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL,
                    symbol TEXT NOT NULL, name TEXT NOT NULL,
                    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                    UNIQUE(market,symbol));
                CREATE TABLE IF NOT EXISTS prices (
                    market TEXT, symbol TEXT, day TEXT, close REAL,
                    PRIMARY KEY(market,symbol,day));
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

    def replace_catalog(self, market, entries):
        with self.connect() as db:
            db.execute("DELETE FROM catalog WHERE market=?", (market,))
            db.executemany("INSERT INTO catalog VALUES (?,?,?,?)",
                           ((market, code, name, normalize(name)) for code, name in entries.items()))

    def state(self, today, us_configured, us_full):
        with self.connect() as db:
            settings = self.settings(db)
            categories = [dict(row) for row in db.execute("SELECT id,name FROM categories ORDER BY id")]
            errors = {r["name"]: r["error"] for r in db.execute("SELECT name,error FROM sources")}
            stocks = []
            start = range_start(today, settings["months"])
            since = start.isoformat()
            first_period = month_key(start)
            for row in db.execute("SELECT * FROM stocks ORDER BY id"):
                s = dict(row)
                key = (s["market"], s["symbol"])
                s["history"] = [list(p) for p in db.execute(
                    "SELECT day,close FROM prices WHERE market=? AND symbol=? AND day BETWEEN ? AND ? ORDER BY day",
                    (*key, since, today.isoformat()))]
                status = "等待背景更新"
                if s["history"]:
                    status = "日線已載入"
                if settings["enabled"] and s["market"] != "US":
                    count = db.execute("""SELECT count(*) FROM fetches
                        WHERE market=? AND symbol=? AND period BETWEEN ? AND ?""",
                        (*key, first_period, month_key(today))).fetchone()[0]
                    expected = settings["months"] + 1
                    if count < expected and s["history"]:
                        status = f"歷史補齊中（{count}/{expected} 個月份）"
                if s["market"] == "US":
                    if not us_configured:
                        status = "美股行情需要 API 金鑰"
                    elif not us_full and settings["months"] > 3:
                        status = "目前方案最多最近 100 個交易日"
                if errors.get(s["market"] + "_history") and settings["enabled"]:
                    status = errors[s["market"] + "_history"]
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
                months, rows, columns = (integer(key) for key in ("months", "rows", "columns"))
                enabled = data.get("enabled")
                if months not in MONTHS or rows > 6 or columns > 6 or type(enabled) is not bool:
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

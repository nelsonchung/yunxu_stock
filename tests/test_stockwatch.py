import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from stockwatch.server import make_server
from stockwatch.store import Store, range_start
from stockwatch.worker import CATALOG, FINMIND_HISTORY, INDUSTRIES, US_HISTORY, ProviderError, TAIPEI, Worker, download, parse_daily_snapshot, parse_us_history


class StockWatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "stocks.db"
        self.store = Store(self.path)
        self.store.replace_catalog("TWSE", {"2330": "台積電", "2337": "旺宏"})
        self.store.replace_catalog("TPEX", {"6488": "環球晶"})
        self.store.replace_catalog("US", {"AAPL": "Apple Inc."})
        self.now = datetime(2026, 11, 15, 15, tzinfo=TAIPEI).timestamp()

    def settings(self, enabled):
        self.store.action({"action": "settings", "months": 1, "enabled": enabled, "rows": 3, "columns": 2})

    def test_browser_api_and_persistence(self):
        worker = Worker(self.store)
        with make_server("127.0.0.1", 0, self.store, worker) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def api(path, data=None, **headers):
                req = Request(base + path, headers=headers | {"Content-Type": "application/json"},
                              data=json.dumps(data).encode() if data is not None else None)
                with urlopen(req, timeout=3) as response:
                    return json.load(response)

            def action(kind, **params):
                return api("/api/action", {"action": kind, **params})

            try:
                with urlopen(base, timeout=3) as response:
                    self.assertIn(b"/app.js", response.read())
                for query, market, expected in [("２３３", "TWSE", "2330"),
                                                  ("環球", "TPEX", "6488"), ("appl", "US", "AAPL")]:
                    value = api("/api/search?" + urlencode({"q": query, "market": market}))
                    self.assertEqual(value["results"][0]["symbol"], expected)
                action("add_category", name="觀察清單")
                category = api("/api/state")["categories"][0]["id"]
                action("add_stock", market="TWSE", symbol="2330", category_id=category)
                with self.assertRaises(HTTPError) as duplicate:
                    action("add_stock", market="TWSE", symbol="2330")
                self.assertEqual(duplicate.exception.code, 400)
                duplicate.exception.close()
                action("add_stock", market="US", symbol="AAPL")
                state = api("/api/state")
                tw, us = state["stocks"]
                self.assertEqual(tw["category_id"], category)
                action("move_stock", id=us["id"], category_id=category)
                action("remove_category", id=category)
                action("remove_stock", id=tw["id"])
                self.settings(False)
                with self.assertRaises(HTTPError) as cross:
                    api("/api/action", {"action": "add_category", "name": "blocked"},
                        Origin="http://elsewhere.test")
                self.assertEqual(cross.exception.code, 403)
                cross.exception.close()
                state = Store(self.path).state(date(2026, 11, 15), False, False)
                self.assertEqual(state["settings"], {"months": 1, "enabled": False, "rows": 3, "columns": 2})
                self.assertEqual([(s["symbol"], s["category_id"]) for s in state["stocks"]], [("AAPL", None)])
                self.assertEqual(api("/healthz"), {"ok": True})
            finally:
                server.shutdown()
                thread.join()

    def test_daily_openapi_reuses_catalog_and_preserves_old_history(self):
        calls = []
        def fetch(url):
            calls.append(url)
            self.assertEqual(url, CATALOG["TWSE"])
            return json.dumps([{"Date": "1151113", "Code": "2330", "Name": "台積電",
                                "ClosingPrice": "1,234.50"}])

        worker = Worker(self.store, fetch, lambda: self.now)
        for source in ("TPEX_daily", "US_catalog", "TWSE_industries", "TPEX_industries"):
            worker.reserve(source, 3 * 86400, 3)
        self.store.action({"action": "add_stock", "market": "TWSE", "symbol": "2330"})
        self.store.replace_catalog("TWSE", {"2330": "台積電"}, [("2330", "2026-10-20", None, None, None, 1000, None)])
        self.settings(False)
        worker.tick()
        points = self.store.state(date(2026, 11, 15), False, False)["stocks"][0]["history"]
        self.assertEqual(points, [["2026-10-20", 1000]])
        self.settings(True)
        worker.tick()
        self.assertEqual(calls, [CATALOG["TWSE"]])
        self.now += 86400
        worker.tick()
        points = self.store.state(date(2026, 11, 15), False, False)["stocks"][0]["history"]
        self.assertEqual(points, [["2026-10-20", 1000], ["2026-11-13", 1234.5]])
        self.assertEqual(range_start(date(2026, 3, 31), 1), date(2026, 2, 28))

    def test_rolling_budget_and_backoff_persist_across_restarts(self):
        worker = Worker(self.store, now=lambda: self.now)
        self.assertTrue(worker.reserve("US_history", 60, 2))
        self.assertFalse(worker.reserve("US_history", 60, 2))
        self.now += 61
        self.assertTrue(worker.reserve("US_history", 60, 2))
        self.now += 61
        restarted = Worker(Store(self.path), now=lambda: self.now)
        self.assertFalse(restarted.reserve("US_history", 60, 2))
        self.now += 86400
        self.assertTrue(restarted.reserve("US_history", 60, 2))
        restarted.result("US_history", ProviderError("限流", 86400))
        self.now += 3600
        self.assertFalse(worker.reserve("US_history", 60, 2))

    def test_outbound_gap_and_shared_host_cooldown(self):
        worker = Worker(self.store, now=lambda: self.now)
        tpex = CATALOG["TPEX"]
        self.assertTrue(worker.reserve("TPEX_history", 60, 48, url=tpex))
        worker.result("TPEX_history", ProviderError("限流", 86400), url=tpex)
        restarted = Worker(Store(self.path), now=lambda: self.now)
        self.assertFalse(restarted.reserve("TWSE_history", 60, 48, url=CATALOG["TWSE"]))
        self.now += 5
        self.assertTrue(restarted.reserve("TWSE_history", 60, 48, url=CATALOG["TWSE"]))
        self.now += 60
        self.assertFalse(restarted.reserve("TPEX_catalog", 60, 3, url=CATALOG["TPEX"]))

    def test_related_companies_use_cached_industry_and_prioritize_foundries(self):
        self.store.replace_catalog("TWSE", {"2330": "台積電", "6770": "力積電", "2303": "聯電",
                                           "2454": "聯發科", "2317": "鴻海"})
        self.store.replace_catalog("TPEX", {"5347": "世界", "6488": "環球晶"})
        calls = []
        def fetch(url):
            calls.append(url)
            if url == INDUSTRIES["TWSE"]:
                rows = [("2330", "24", "100"), ("6770", "24", "10"), ("2303", "24", "90"),
                        ("2454", "24", "80"), ("2317", "31", "800")]
                return json.dumps([{"公司代號": symbol, "產業別": code, "實收資本額": capital}
                                   for symbol, code, capital in rows])
            self.assertEqual(url, INDUSTRIES["TPEX"])
            return json.dumps([{"SecuritiesCompanyCode": "5347", "SecuritiesIndustryCode": "24"},
                               {"SecuritiesCompanyCode": "6488", "SecuritiesIndustryCode": "24",
                                "Paidin.Capital.NTDollars": "70"}])

        worker = Worker(self.store, fetch, lambda: self.now)
        for market in CATALOG:
            worker.reserve(market + ("_catalog" if market == "US" else "_daily"), 86400, 3)
        self.store.action({"action": "add_stock", "market": "TWSE", "symbol": "2330"})
        self.store.action({"action": "add_stock", "market": "US", "symbol": "AAPL"})
        self.settings(False)
        worker.tick()
        worker.tick()
        self.assertEqual(calls, [INDUSTRIES["TWSE"]])
        self.now += 5
        worker.tick()
        self.now += 60
        worker.tick()
        self.assertEqual(calls, list(INDUSTRIES.values()))

        stock, us = Store(self.path).state(date(2026, 11, 15), False, False)["stocks"]
        self.assertEqual([(peer["symbol"], peer["relationship"]) for peer in stock["related"]],
                         [("2303", "晶圓代工同行"), ("6770", "晶圓代工同行"), ("5347", "晶圓代工同行"),
                          ("2454", "同產業"), ("6488", "同產業")])
        self.assertEqual(us["related"], [])
        self.store.action({"action": "add_stock", "market": "TPEX", "symbol": "5347"})
        self.assertEqual(Store(self.path).state(date(2026, 11, 15), False, False)["stocks"][-1]["symbol"],
                         "5347")

    def test_finmind_is_opt_in_and_fills_ohlcv_without_replacing_official_bars(self):
        calls = []
        def fetch(url):
            calls.append(url)
            self.assertEqual(urlsplit(url)._replace(query="").geturl(), FINMIND_HISTORY)
            self.assertIn("dataset=TaiwanStockPrice&data_id=2330", url)
            rows = [
                {"date": day, "stock_id": symbol, "open": 200, "max": 220, "min": 190,
                 "close": 210, "Trading_Volume": 12345}
                for day, symbol in [("2026-10-28", "2330"), ("2026-11-13", "2330"), ("2026-11-13", "2337")]
            ]
            return json.dumps({"status": 200, "data": rows})

        self.store.action({"action": "add_stock", "market": "TWSE", "symbol": "2330"})
        self.settings(True)
        self.store.replace_catalog("TWSE", {"2330": "台積電"}, [
            ("2330", "2026-10-28", None, None, None, 205, None),
            ("2330", "2026-11-13", 201, 221, 191, 211, 6789),
        ])
        with patch.dict(os.environ, {"FINMIND_HISTORY": "", "ALPHA_VANTAGE_API_KEY": ""}):
            worker = Worker(self.store, fetch, lambda: self.now)
        for source in ("TWSE_daily", "TPEX_daily", "US_catalog", "TWSE_industries", "TPEX_industries"):
            worker.reserve(source, 86400, 3)
        worker.tick()
        self.assertEqual(calls, [])
        with patch.dict(os.environ, {"FINMIND_HISTORY": "1", "ALPHA_VANTAGE_API_KEY": ""}):
            worker = Worker(self.store, fetch, lambda: self.now)
        self.settings(False)
        worker.tick()
        self.assertEqual(calls, [])
        self.settings(True)
        worker.tick()
        worker.tick()
        self.assertEqual(len(calls), 1)
        after = Store(self.path).state(date(2026, 11, 15), False, False, True)["stocks"][0]
        self.assertEqual(after["candles"], [
            ["2026-10-28", 200, 220, 190, 210, 12345], ["2026-11-13", 201, 221, 191, 211, 6789]])
        self.assertEqual(after["history"], [["2026-10-28", 210], ["2026-11-13", 211]])
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM prices WHERE symbol='2337'").fetchone())

    def test_provider_formats_and_outbound_allowlist(self):
        local = [{"Date": "1151113", "SecuritiesCompanyCode": "5347",
                  "CompanyName": "世界", "Close": "98.4"}]
        self.assertEqual(parse_daily_snapshot("TPEX", json.dumps(local)),
                         ({"5347": "世界"}, [("5347", "2026-11-13", None, None, None, 98.4, None)]))
        us = {"Time Series (Daily)": {"2026-11-13": {"4. close": "225.5"}}}
        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-only"}):
            def fetch(url):
                self.assertTrue(url.startswith(US_HISTORY + "?"))
                self.assertIn("outputsize=compact", url)
                return json.dumps(us)
            worker = Worker(self.store, fetch)
            self.store.action({"action": "add_stock", "market": "US", "symbol": "AAPL"})
            worker.us_history("AAPL", self.now)
            self.assertEqual(self.store.state(date(2026, 11, 15), True, False)["stocks"][0]["history"],
                             [["2026-11-13", 225.5]])
        blocked = ("https://mops.twse.com.tw/t57sb01",
                   "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                   "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock",
                   CATALOG["TWSE"] + "?date=20261113")
        with patch("stockwatch.worker.build_opener") as opener:
            for url in blocked:
                with self.assertRaisesRegex(ProviderError, "未授權"):
                    download(url)
            opener.assert_not_called()
        with self.assertRaises(ProviderError):
            parse_us_history('{"Information":"rate limit"}')

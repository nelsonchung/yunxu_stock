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
from stockwatch.worker import CATALOG, HISTORY, ProviderError, TAIPEI, Worker, download, parse_history


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

    def test_worker_backfill_and_pause(self):
        seen = []
        def fetch(url):
            seen.append(url)
            self.assertEqual(urlsplit(url).hostname, "www.twse.com.tw")
            day = "115/10/20" if "20261001" in url else "115/11/14"
            return json.dumps({"stat": "OK", "data": [[day, "", "", "", "", "", "1,234.50"]]})

        worker = Worker(self.store, fetch, lambda: self.now)
        for market in CATALOG:
            worker.reserve(market + "_catalog", 86400, 3)
        self.store.action({"action": "add_stock", "market": "TWSE", "symbol": "2330"})
        self.settings(False)
        worker.tick()
        self.assertEqual(seen, [])
        self.settings(True)
        worker.tick()
        worker.tick()
        self.assertEqual(len(seen), 1)
        self.now += 61
        worker.tick()
        points = self.store.state(date(2026, 11, 15), False, False)["stocks"][0]["history"]
        self.assertEqual(points, [["2026-10-20", 1234.5], ["2026-11-14", 1234.5]])
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
        tpex = HISTORY["TPEX"]
        self.assertTrue(worker.reserve("TPEX_history", 60, 48, url=tpex))
        worker.result("TPEX_history", ProviderError("限流", 86400), url=tpex)
        restarted = Worker(Store(self.path), now=lambda: self.now)
        self.assertFalse(restarted.reserve("TWSE_history", 60, 48, url=HISTORY["TWSE"]))
        self.now += 5
        self.assertTrue(restarted.reserve("TWSE_history", 60, 48, url=HISTORY["TWSE"]))
        self.now += 60
        self.assertFalse(restarted.reserve("TPEX_catalog", 60, 3, url=CATALOG["TPEX"]))

    def test_provider_formats_and_outbound_allowlist(self):
        local = {"stat": "ok", "tables": [{"data": [["115/11/14", "", "", "", "", "", "98.4"]]}]}
        self.assertEqual(parse_history("TPEX", json.dumps(local), "2026-11-01"), [("2026-11-14", 98.4)])
        us = {"Time Series (Daily)": {"2026-11-13": {"4. close": "225.5"}}}
        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-only"}):
            def fetch(url):
                self.assertTrue(url.startswith(HISTORY["US"] + "?"))
                self.assertIn("outputsize=compact", url)
                return json.dumps(us)
            worker = Worker(self.store, fetch)
            self.store.action({"action": "add_stock", "market": "US", "symbol": "AAPL"})
            worker.history("US", "AAPL", "", self.now)
            self.assertEqual(self.store.state(date(2026, 11, 15), True, False)["stocks"][0]["history"],
                             [["2026-11-13", 225.5]])
        with self.assertRaises(ProviderError):
            download("https://mops.twse.com.tw/t57sb01")
        with self.assertRaises(ProviderError):
            parse_history("US", '{"Information":"rate limit"}', "")

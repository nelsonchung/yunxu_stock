import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from stockwatch.intraday import Intraday, TAIPEI
from stockwatch.store import Store

DAY = "2026-11-16"
TICK = {
    "code": "2330", "date": DAY, "time": "09:45:00.123456",
    "open": "680", "high": "700", "low": "670", "close": "698",
    "avg_price": "684.23", "price_chg": "-6", "pct_chg": -85,
    "volume": 7, "total_volume": 789, "total_amount": "540000000",
    "bid_side_total_vol": 281, "ask_side_total_vol": 342,
}
BOOK = {
    "code": "2330", "date": DAY, "time": "09:45:00.234567",
    "bid_price": ["697", "696"], "bid_volume": [2, 10],
    "ask_price": ["698", "699"], "ask_volume": [1, 5],
}


class IntradayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "test.db")
        self.store.replace_catalog("TWSE", {"2330": "測試公司"})
        self.store.action({"action": "add_stock", "market": "TWSE", "symbol": "2330"})
        with self.store.connect() as db:
            self.store.save_prices(db, "TWSE", [("2330", "2026-11-13", None, None, None, 695, None)], "TWSE")
        self.now = datetime(2026, 11, 16, 9, 46, tzinfo=TAIPEI).timestamp()

    def test_quote_uses_stored_close_and_ignores_nonregular_and_stale_ticks(self):
        intraday = Intraday(self.store, lambda: self.now, 8085)
        state = intraday.snapshot([1, 444])
        self.assertTrue(state["session"])
        self.assertEqual(state["quotes"], {})
        intraday.ingest("tick_stk", {**TICK, "simtrade": True})
        intraday.ingest("tick_stk", {**TICK, "intraday_odd": True})
        intraday.ingest("tick_stk", {**TICK, "date": "2026-11-13"})
        self.assertEqual(intraday.snapshot([1])["quotes"], {})
        intraday.ingest("tick_stk", TICK)
        intraday.ingest("tick_stk", {**TICK, "time": "09:45:22.234567", "close": "699", "volume": 3})
        intraday.ingest("bidask_stk", BOOK)
        quote = intraday.snapshot([1, 444])["quotes"]["1"]
        self.assertEqual(quote["previous_close"], 695)
        self.assertEqual(quote["previous_close_date"], "2026-11-13")
        self.assertEqual(quote["change"], 4)
        self.assertEqual(quote["points"], [[DAY + "T09:45+08:00", 699, 10]])
        self.assertEqual(quote["bids"], [[697, 2], [696, 10]])
        self.assertEqual(quote["volume"], 789)
        self.now += 86400
        self.assertEqual(intraday.snapshot([1])["quotes"], {})

    def test_missing_config_and_no_visible_demand_do_not_connect(self):
        intraday = Intraday(self.store, lambda: self.now, None)
        self.assertFalse(intraday.snapshot([1])["configured"])
        intraday.sync()
        self.assertIsNone(intraday.receiver)
        intraday = Intraday(self.store, lambda: self.now, 8085)
        self.now = datetime(2026, 11, 16, 8, 59, tzinfo=TAIPEI).timestamp()
        self.assertFalse(intraday.snapshot([1])["session"])
        intraday.sync()
        self.assertIsNone(intraday.receiver)

    def run_daemon_fixture(self, simulation):
        entered_stream = threading.Event()
        finish_stream = threading.Event()
        actions = []

        class FakeDaemon(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                actions.append(("GET", self.path))
                if self.path == "/api/v1/info":
                    payload = json.dumps({"name": "Shioaji API Server", "simulation": simulation}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif self.path == "/api/v1/stream/data":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(f": heartbeat\n\nevent:tick_stk\ndata:{json.dumps(TICK)}\n\n".encode())
                    self.wfile.flush()
                    entered_stream.set()
                    finish_stream.wait(3)
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                actions.append(("POST", self.path, body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        with ThreadingHTTPServer(("127.0.0.1", 0), FakeDaemon) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            intraday = Intraday(self.store, lambda: self.now, server.server_port)
            try:
                intraday.snapshot([1])
                intraday.sync()
                if simulation:
                    intraday.receiver.join(2)
                    self.assertEqual(actions, [("GET", "/api/v1/info")])
                    self.assertIn("未確認正式行情", intraday.snapshot([1])["status"])
                else:
                    self.assertTrue(entered_stream.wait(2))
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and not intraday.snapshot([1])["quotes"]:
                        time.sleep(.01)
                    self.assertEqual(intraday.snapshot([1])["quotes"]["1"]["price"], 698)
                    intraday.sync()
                    self.assertEqual([(path, body["quote_type"]) for method, path, *rest in actions
                                      if method == "POST" for body in rest],
                                     [("/api/v1/stream/subscribe", "BidAsk"), ("/api/v1/stream/subscribe", "Tick")])
                    self.now += 61
                    intraday.sync()
                    self.assertEqual([entry[1] for entry in actions[-2:]], ["/api/v1/stream/unsubscribe"] * 2)
            finally:
                intraday.close()
                finish_stream.set()
                server.shutdown()
                thread.join()

    def test_shioaji_http_subscriptions_and_sse(self):
        self.run_daemon_fixture(simulation=False)

    def test_simulation_daemon_is_rejected(self):
        self.run_daemon_fixture(simulation=True)

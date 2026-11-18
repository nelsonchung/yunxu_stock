import http.client
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from stockwatch.accounting import Accounting, make_server


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


def broker_fixture(positions, balance, *, simulation=False, signed=True):
    calls = []
    stock = {"account_type": "S", "broker_id": "test-broker", "account_id": "test-stock",
             "person_id": "test-person", "signed": signed, "username": "test-name"}
    routes = {
        ("GET", "/api/v1/info"): {"simulation": simulation},
        ("GET", "/api/v1/auth/accounts"): [stock, {"account_type": "F", "signed": True}],
        ("POST", "/api/v1/portfolio/position_unit"): positions,
        ("POST", "/api/v1/portfolio/account_balance"): balance,
    }

    class BrokerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self):
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n)) if n else None
            calls.append((self.headers.get("Authorization"), self.command, self.path, data))
            payload = routes.get((self.command, self.path), {"error": "unexpected route"})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        do_GET = do_POST = reply

    return ThreadingHTTPServer(("127.0.0.1", 0), BrokerHandler), calls


class AccountingTests(unittest.TestCase):
    allowed_headers = {"X-Yunxu-Accounting": "1"}
    sample_positions = [{"code": "1234", "direction": "Buy", "quantity": 17,
                         "price": 21.5, "last_price": 22, "pnl": 8.5, "id": "hidden"},
                        {"code": "5678", "direction": "Sell", "quantity": 9,
                         "price": 18, "last_price": 18.5, "pnl": -4.5}]
    sample_balance = {"acc_balance": 500, "date": "2026-11-17 09:21:10.085711", "errmsg": ""}

    def test_private_route_checks_before_contacting_broker(self):
        broker, calls = broker_fixture(self.sample_positions, self.sample_balance)
        with serving(broker) as broker_port, serving(make_server(0, Accounting(broker_port, "fixture"))) as port:
            self.assertEqual(request(port, "GET", "/healthz")[0], 200)
            for headers in ({}, self.allowed_headers | {"Host": f"untrusted.example:{port}"},
                            self.allowed_headers | {"Origin": "https://untrusted.example"},
                            self.allowed_headers | {"Sec-Fetch-Site": "cross-site"},
                            self.allowed_headers | {"Sec-Fetch-Site": "same-site"}):
                self.assertEqual(request(port, "GET", "/api/accounting", headers)[0], 403)
            self.assertEqual(request(port, "POST", "/api/accounting", self.allowed_headers)[0], 405)
            self.assertEqual(request(port, "GET", "/api/accounting?account=F", self.allowed_headers)[0], 404)
            self.assertEqual(calls, [])

    def test_curates_securities_data_and_coalesces_queries(self):
        broker, calls = broker_fixture(self.sample_positions, self.sample_balance)
        with serving(broker) as broker_port, serving(make_server(0, Accounting(broker_port, "fixture"))) as port:
            headers = self.allowed_headers | {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
            status, response_headers, result = request(port, "GET", "/api/accounting", headers)
            self.assertEqual(status, 200)
            self.assertEqual(response_headers["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", response_headers)
            self.assertEqual(result["balance"], {"amount": 500, "as_of": "2026-11-17T09:21:10+08:00"})
            self.assertEqual(result["unrealized_pnl"], 4)
            self.assertEqual(result["positions"][0], {"code": "1234", "direction": "Buy", "quantity": 17,
                                                       "avg_price": 21.5, "last_price": 22, "pnl": 8.5})
            self.assertEqual(result["errors"], {})
            self.assertEqual(request(port, "GET", "/api/accounting", headers)[2], result)
            self.assertEqual([row[1:3] for row in calls], [
                ("GET", "/api/v1/info"), ("GET", "/api/v1/auth/accounts"),
                ("POST", "/api/v1/portfolio/position_unit"), ("POST", "/api/v1/portfolio/account_balance"),
            ])
            self.assertTrue(all(row[0] == "Bearer fixture" for row in calls))
            self.assertEqual(calls[2][3], {"account_type": "S", "unit": "Share", "broker_id": "test-broker",
                                            "account_id": "test-stock", "person_id": "test-person"})
            self.assertNotIn("test-", json.dumps(result))

    def test_partial_errors_and_incomplete_pnl_do_not_invent_totals(self):
        positions = [self.sample_positions[0], self.sample_positions[1] | {"pnl": None}]
        broker, _ = broker_fixture(positions, {"acc_balance": 0, "errmsg": "private error detail"})
        with serving(broker) as port:
            result = Accounting(port, "fixture").snapshot()
        self.assertEqual(len(result["positions"]), 2)
        self.assertIsNone(result["unrealized_pnl"])
        self.assertIsNone(result["balance"])
        self.assertEqual(set(result["errors"]), {"balance"})
        self.assertNotIn("private error", json.dumps(result))

    def test_rejects_simulation_and_unsigned_account_before_private_queries(self):
        for simulation, signed, expected_calls in ((True, True, 1), (False, False, 2)):
            with self.subTest(simulation=simulation, signed=signed):
                broker, calls = broker_fixture([], self.sample_balance, simulation=simulation, signed=signed)
                with serving(broker) as port:
                    result = Accounting(port, "fixture").snapshot()
                self.assertEqual(set(result["errors"]), {"positions", "balance"})
                self.assertEqual(len(calls), expected_calls)
                self.assertTrue(all(row[1] == "GET" for row in calls))

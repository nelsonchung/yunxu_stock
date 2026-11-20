import http.client
import json
import threading
import unittest
from datetime import date, time
from decimal import Decimal
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

from stockwatch.intraday import stream_events
from stockwatch.quote_gateway import Handler, QuoteFeed


class QuoteGatewayTests(unittest.TestCase):
    def test_loopback_http_forwards_stock_events_only(self):
        api = Mock()
        api.contracts.get.return_value = SimpleNamespace(exchange="TSE", code="2330")
        feed = QuoteFeed(api, {"Tick": "tick", "BidAsk": "bid_ask"}, "fixture-token")
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.feed = feed
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.check_gateway(server.server_port, feed)
            finally:
                server.shutdown()
                thread.join(timeout=2)
        api.subscribe.assert_called_once_with(api.contracts.get.return_value, quote_type="tick", intraday_odd=False)
        api.unsubscribe.assert_called_once_with(api.contracts.get.return_value, quote_type="tick", intraday_odd=False)

    def check_gateway(self, port, feed):
        headers = {"Authorization": "Bearer fixture-token"}
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        def request(method, path, auth, payload=None):
            client.request(method, path, body=payload, headers=auth)
            reply = client.getresponse()
            return reply.status, json.loads(reply.read())

        try:
            self.assertEqual(request("GET", "/api/v1/info", {})[0], 401)
            self.assertIs(request("GET", "/api/v1/info", headers)[1]["simulation"], False)
            self.assertEqual(request("POST", "/api/v1/order/place_order", headers)[0], 404)

            payload = json.dumps({"security_type": "STK", "exchange": "TSE", "code": "2330",
                                  "quote_type": "Tick", "intraday_odd": False})
            stream.request("GET", "/api/v1/stream/data", headers=headers)
            with stream.getresponse() as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(request("POST", "/api/v1/stream/subscribe", headers, payload)[0], 200)
                self.assertEqual(request("POST", "/api/v1/stream/subscribe", headers, payload)[0], 200)
                feed.publish("Tick", SimpleNamespace(code="2330", date=date(2026, 11, 19),
                                                     time=time(9, 31), close=Decimal("2150.5"),
                                                     intraday_odd=False))
                name, quote = next(stream_events(response))
                self.assertEqual((name, quote["code"], quote["date"], quote["time"], quote["close"]),
                                 ("tick_stk", "2330", "2026-11-19", "09:31:00", "2150.5"))
                self.assertEqual(request("POST", "/api/v1/stream/unsubscribe", headers, payload)[0], 200)
        finally:
            stream.close()
            client.close()

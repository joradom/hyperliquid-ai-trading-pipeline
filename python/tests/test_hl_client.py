import io
import json
import unittest
import urllib.error
from unittest.mock import patch, Mock

from src.hl_client import HyperliquidAPIError, HyperliquidInfoClient
from src.rest_policy import RESTCoordinator
from test_rest_policy import Clock


class Response:
    def __init__(self, body): self.body = body.encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, *args): return self.body


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.coordinator = RESTCoordinator(timer=self.clock, wait=self.clock.wait)
        self.client = HyperliquidInfoClient(timeout=0.01, coordinator=self.coordinator)

    @patch("urllib.request.urlopen", return_value=Response("not-json"))
    def test_malformed_json(self, _):
        with self.assertRaises(HyperliquidAPIError): self.client._post({"type":"x"})

    @patch("urllib.request.urlopen", side_effect=TimeoutError("timed out"))
    def test_timeout(self, _):
        with self.assertRaisesRegex(HyperliquidAPIError, "timed out"): self.client._post({"type":"x"})

    def test_http_error(self):
        error = urllib.error.HTTPError(self.client.api_url, 500, "bad", {}, io.BytesIO(b"server error"))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(HyperliquidAPIError, "HTTP 500"): self.client._post({"type":"x"})

    def test_structured_http_error_and_injected_transport(self):
        error = urllib.error.HTTPError("https://example.test/info", 429, "limited", {}, io.BytesIO(b"synthetic"))
        def transport(*args, **kwargs): raise error
        client = HyperliquidInfoClient(transport=transport, clock_ms=lambda: 123, coordinator=self.coordinator, rng=lambda: 0)
        with self.assertRaises(HyperliquidAPIError) as caught:
            client._post({"type": "l2Book"})
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.request_type, "l2Book")
        self.assertIs(caught.exception.__cause__, error)
        client.transport = lambda *args, **kwargs: Response("[]")
        self.assertEqual(client._post({"type": "x"}).captured_at_ms, 123)

    def http429(self, headers=None):
        return urllib.error.HTTPError("https://example.test/info", 429, "limited", headers or {}, io.BytesIO(b"synthetic"))

    def test_retry_after_and_every_attempt_is_charged(self):
        transport = Mock(side_effect=[self.http429({"Retry-After":"3"}), Response("[]")])
        client = HyperliquidInfoClient(coordinator=self.coordinator, transport=transport, rng=lambda: 0)
        self.assertEqual(client._post({"type":"l2Book"}).value, [])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(self.clock.now, 3)
        self.assertEqual(sum(x[1] for x in self.coordinator._entries), 4)

    def test_exponential_backoff_jitter_and_attempt_limit(self):
        transport = Mock(side_effect=[self.http429(),self.http429(),self.http429()])
        client = HyperliquidInfoClient(coordinator=self.coordinator, transport=transport, rng=lambda: 1)
        with self.assertRaises(HyperliquidAPIError) as caught: client._post({"type":"l2Book"})
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(self.clock.waits, [1.25,2.25])
        self.assertEqual(self.coordinator._inflight, 0)

    def test_retry_after_longer_than_deadline_is_never_shortened(self):
        transport=Mock(side_effect=self.http429({"Retry-After":"60"}))
        client=HyperliquidInfoClient(coordinator=self.coordinator, transport=transport, rng=lambda:0)
        with client.request_scope(deadline_seconds=3), self.assertRaises(HyperliquidAPIError) as caught:
            client._post({"type":"l2Book"})
        self.assertEqual(caught.exception.code,"retry_wait_exceeded")
        self.assertEqual(transport.call_count,1)
        self.assertEqual(self.clock.waits,[])
        second=HyperliquidInfoClient(coordinator=self.coordinator, transport=transport)
        with self.assertRaises(HyperliquidAPIError) as blocked: second._post({"type":"l2Book"})
        self.assertEqual(blocked.exception.attempts,0)
        self.assertEqual(transport.call_count,1)

    def test_retry_after_date_and_invalid_values(self):
        from email.utils import formatdate
        client=HyperliquidInfoClient(coordinator=self.coordinator,clock_ms=lambda:1000000)
        self.assertEqual(client._retry_after(formatdate(1005,usegmt=True)),5)
        for value in (None,"invalid","nan","inf"):
            self.assertIsNone(client._retry_after(value))
        self.assertEqual(client._retry_after("-1"),0)

    def test_scope_prevents_retries_and_late_response(self):
        transport=Mock(side_effect=self.http429())
        client=HyperliquidInfoClient(coordinator=self.coordinator,transport=transport,rng=lambda:0)
        with client.request_scope(deadline_seconds=3,max_attempts=1), self.assertRaises(HyperliquidAPIError):
            client._post({"type":"l2Book"})
        self.assertEqual(transport.call_count,1)
        self.clock.now=10
        def late(*args, **kwargs):
            self.clock.now += 4
            self.assertLessEqual(kwargs["timeout"],3)
            return Response("[]")
        client.transport=late
        with client.request_scope(deadline_seconds=3), self.assertRaises(HyperliquidAPIError) as caught:
            client._post({"type":"l2Book"})
        self.assertEqual(caught.exception.code,"deadline_exceeded")

    def test_injected_clock_without_explicit_coordinator(self):
        clock=Clock(); wait=clock.wait
        client=HyperliquidInfoClient('https://clock-injection.invalid/info',timer=clock,wait=wait,
                                    transport=lambda *a,**k:Response("[]"))
        second=HyperliquidInfoClient('https://clock-injection.invalid:443/info')
        self.assertIs(client.coordinator,second.coordinator)
        client._post({"type":"l2Book"}); client._post({"type":"l2Book"})
        self.assertEqual(clock.now,0)
        self.assertEqual(sum(entry[1] for entry in client.coordinator._entries),4)

    def test_network_failure_is_not_retried_and_preserves_cause(self):
        error=urllib.error.URLError("synthetic unavailable")
        transport=Mock(side_effect=error)
        client=HyperliquidInfoClient(coordinator=self.coordinator,transport=transport)
        with self.assertRaises(HyperliquidAPIError) as caught: client._post({"type":"l2Book"})
        self.assertEqual(transport.call_count,1)
        self.assertEqual(caught.exception.code,"network_error")
        self.assertIs(caught.exception.__cause__,error)
        self.assertEqual(self.coordinator._inflight,0)

    def test_nested_scope_does_not_extend_deadline(self):
        transport=Mock(return_value=Response("[]"))
        client=HyperliquidInfoClient(coordinator=self.coordinator,transport=transport)
        with client.request_scope(deadline_seconds=1):
            with client.request_scope(deadline_seconds=100):
                self.clock.now=2
                with self.assertRaises(HyperliquidAPIError): client._post({"type":"l2Book"})
        transport.assert_not_called()
        client._post({"type":"l2Book"})
        transport.assert_called_once()

    @patch("urllib.request.urlopen", return_value=Response(json.dumps({"wrong":"shape"})))
    def test_api_shape_error(self, _):
        with self.assertRaises(HyperliquidAPIError): self.client.meta_and_asset_contexts()


if __name__ == "__main__": unittest.main()

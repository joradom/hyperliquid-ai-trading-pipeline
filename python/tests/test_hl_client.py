import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from src.hl_client import HyperliquidAPIError, HyperliquidInfoClient


class Response:
    def __init__(self, body): self.body = body.encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, *args): return self.body


class ClientTests(unittest.TestCase):
    def setUp(self): self.client = HyperliquidInfoClient(timeout=0.01)

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

    @patch("urllib.request.urlopen", return_value=Response(json.dumps({"wrong":"shape"})))
    def test_api_shape_error(self, _):
        with self.assertRaises(HyperliquidAPIError): self.client.meta_and_asset_contexts()


if __name__ == "__main__": unittest.main()

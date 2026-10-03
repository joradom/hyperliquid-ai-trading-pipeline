import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hl_api
from src import account_config, trade_state

USER = "0x" + "1" * 40
OTHER_USER = "0x" + "2" * 40


class AccountConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.local.json"
        self.config_patch = patch.object(account_config, "CONFIG_PATH", self.path)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.env_patch = patch.dict("os.environ", {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_precedence_and_local_fallback(self):
        self.path.write_text(json.dumps({"hyperliquid_account_address": USER}))
        self.assertEqual(account_config.resolve_account_address(), USER)
        with patch.dict("os.environ", {account_config.ACCOUNT_ENV: OTHER_USER}):
            self.assertEqual(account_config.resolve_account_address(), OTHER_USER)
            self.assertEqual(account_config.resolve_account_address(USER), USER)

    def test_missing_account_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "account required"):
            account_config.resolve_account_address()

    def test_invalid_config_and_address_do_not_disclose_values(self):
        self.path.write_text("not-json")
        with self.assertRaisesRegex(ValueError, "unable to read"):
            account_config.resolve_account_address()
        self.path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            account_config.resolve_account_address()
        with self.assertRaises(ValueError) as error:
            account_config.resolve_account_address("<PUBLIC_ACCOUNT_ADDRESS>")
        self.assertNotIn("<PUBLIC_ACCOUNT_ADDRESS>", str(error.exception))
        with patch.dict("os.environ", {account_config.ACCOUNT_ENV: ""}):
            with self.assertRaises(ValueError):
                account_config.resolve_account_address()

    def test_market_does_not_require_or_read_account_config(self):
        self.path.write_text("not-json")
        with patch("sys.argv", ["hl_api.py", "market"]), patch.object(hl_api, "market") as market:
            self.assertEqual(hl_api.main(), 0)
        market.assert_called_once()

    def test_missing_cli_account_never_queries_network(self):
        with patch("sys.argv", ["hl_api.py", "account"]), patch.object(hl_api, "post_info") as post:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(hl_api.main(), 2)
        post.assert_not_called()
        with patch("sys.argv", ["trade_state"]), patch.object(trade_state, "HyperliquidInfoClient") as client:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(trade_state.main(), 1)
        client.assert_not_called()

    def test_trade_verify_does_not_require_account(self):
        self.path.write_text("not-json")
        with patch("sys.argv", ["trade_state", "--verify", "snapshot.json"]):
            with patch.object(trade_state, "load_and_verify", return_value=True) as verify:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(trade_state.main(), 0)
        verify.assert_called_once_with(Path("snapshot.json"))

    def test_explicit_cli_account_overrides_bad_config(self):
        self.path.write_text("not-json")
        with patch("sys.argv", ["hl_api.py", "--user", USER, "account"]):
            with patch.object(hl_api, "account") as account:
                self.assertEqual(hl_api.main(), 0)
        account.assert_called_once_with(USER, False)


if __name__ == "__main__":
    unittest.main()

"""Profile parsing and validation, without services or credentials."""
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from src.research_config import DEFAULT_CONFIG_PATH, ResearchConfig, ScreenerConfig, load_config, parse_yaml


class ResearchConfigTests(unittest.TestCase):
    def load_document(self, document, profile=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.yaml"
            path.write_text(document)
            return load_config(path, profile)

    def test_defaults_reproduce_original_scanner_and_single_codex(self):
        settings = load_config()
        self.assertEqual(settings, ResearchConfig())
        self.assertEqual(settings.scanner, {"min_day_notional_volume_usd": 1_000_000, "max_spread_bps": 10})
        self.assertTrue(settings.legacy_output)
        self.assertFalse(settings.consensus)
        self.assertEqual(settings.providers, ("codex",))

    def test_experimental_profile_preserves_pending_strict_consensus(self):
        settings = load_config(profile="experimental")
        self.assertEqual(settings.scanner, {"min_day_notional_volume_usd": 10_000_000, "max_spread_bps": 5})
        self.assertEqual(settings.providers, ("codex", "agy"))
        self.assertTrue(settings.consensus)
        self.assertFalse(settings.legacy_output)

    def test_custom_file_default_profile_and_explicit_override(self):
        text = DEFAULT_CONFIG_PATH.read_text().replace("default_profile: default", "default_profile: experimental")
        self.assertEqual(self.load_document(text).profile, "experimental")
        self.assertEqual(self.load_document(text, "default"), ResearchConfig())

    def test_custom_profile_and_single_agy(self):
        text = DEFAULT_CONFIG_PATH.read_text().replace("[codex]", "[agy]").replace("1000000\n", "2000000\n")
        settings = self.load_document(text)
        self.assertEqual(settings.providers, ("agy",))
        self.assertEqual(settings.min_day_notional_volume_usd, 2_000_000)

    def test_missing_file_and_unknown_profile_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "cannot load research configuration"):
                load_config(Path(directory) / "missing.yaml")
        with self.assertRaisesRegex(ValueError, "unknown research profile"):
            load_config(profile="unknown")

    def test_invalid_yaml_duplicate_keys_and_unsupported_features(self):
        invalid = ("a: 1\na: 2", "a:\n b: 2", "a: 1\n  b: 2", "a:\n\tb: 2",
                   "a: &anchor", "a: *alias", "a: !tag", "a: 1.5", "a: null", "- a: 1",
                   "a:\n    b: 2", "a: {b: 2}")
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_yaml(text)

    def test_unknown_fields_and_invalid_unused_profiles_are_rejected(self):
        text = DEFAULT_CONFIG_PATH.read_text()
        for document in (text + "unknown: true\n", text.replace("consensus: true", "unknown: true"),
                         text.replace("max_spread_bps: 5", "max_spread_bps: false"),
                         text.replace("default_profile: default", "default_profile: absent"),
                         text.replace("providers: [codex]", "providers: codex")):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.load_document(document)

    def test_threshold_types_and_provider_combinations_are_validated(self):
        invalid = ({"min_day_notional_volume_usd": 0}, {"max_spread_bps": -1}, {"max_spread_bps": 1.5},
                   {"max_spread_bps": True}, {"providers": ()}, {"providers": ("unknown",)},
                   {"providers": ("codex", "codex")}, {"providers": ("codex", "agy")},
                   {"providers": ("agy",), "consensus": True}, {"consensus": "false"})
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ResearchConfig(**fields)

    def test_comments_quotes_and_provider_order(self):
        text = DEFAULT_CONFIG_PATH.read_text().replace("default_profile: default", 'default_profile: "default" # chosen')
        text = text.replace("[codex, agy]", "['agy', codex]")
        self.assertEqual(self.load_document(text), ResearchConfig())
        self.assertEqual(self.load_document(text, "experimental").providers, ("agy", "codex"))

    def test_optional_rest_profile_and_old_yaml_defaults(self):
        from src.rest_policy import RESTConfig
        text=DEFAULT_CONFIG_PATH.read_text().replace("    screener:","    rest:\n      budget: 600\n      max_inflight: 2\n      context_ttl_seconds: 5\n    screener:")
        settings=self.load_document(text)
        self.assertEqual(settings.rest, RESTConfig(budget=600,max_inflight=2,context_ttl_seconds=5))
        self.assertEqual(load_config().rest,RESTConfig())
        for bad in ("budget: 1201", "budget: false", "unknown: 1"):
            with self.assertRaises(ValueError): self.load_document(text.replace("budget: 600",bad))

    def test_configuration_is_immutable_and_scanner_values_are_detached(self):
        settings = load_config()
        with self.assertRaises(FrozenInstanceError):
            settings.consensus = True
        settings.scanner["max_spread_bps"] = 100
        self.assertEqual(load_config(), settings)

    def test_old_yaml_without_screener_and_partial_settings_use_profile_defaults(self):
        text = """default_profile: strict
profiles:
  strict:
    scanner:
      min_day_notional_volume_usd: 10000000
      max_spread_bps: 5
    ai:
      providers: [codex]
      consensus: false
"""
        settings = self.load_document(text)
        self.assertEqual(settings.screener.parameters, {"top_n": 50, "limit": 10, "sort_by": "vol_delta",
                                                       "min_volume": 10_000_000, "max_workers": 8})
        text += "    screener:\n      limit: 3\n      sort_by: volume\n"
        changed = self.load_document(text)
        self.assertEqual(changed.screener.limit, 3)
        self.assertEqual(changed.screener.sort_by, "volume")
        self.assertEqual(changed.screener.min_volume, 10_000_000)

    def test_screener_settings_do_not_change_existing_output_contract(self):
        settings = ResearchConfig(screener=ScreenerConfig(limit=2, sort_by="oi"))
        self.assertTrue(settings.legacy_output)
        self.assertEqual(settings.screener.min_volume, settings.min_day_notional_volume_usd)
        parameters = settings.screener.parameters
        parameters["limit"] = 100
        self.assertEqual(settings.screener.limit, 2)
        with self.assertRaises(FrozenInstanceError):
            settings.screener.limit = 10

    def test_screener_invalid_fields_types_and_nonfinite_volume(self):
        invalid = ({"top_n": 0}, {"limit": True}, {"max_workers": -1}, {"limit": 2.5},
                   {"sort_by": "unknown"}, {"min_volume": -1}, {"min_volume": True},
                   {"min_volume": float("nan")}, {"min_volume": float("inf")})
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ScreenerConfig(**fields)
        text = DEFAULT_CONFIG_PATH.read_text().replace("top_n: 50", "unsupported: 50")
        with self.assertRaisesRegex(ValueError, "invalid screener fields"):
            self.load_document(text)

    def test_research_profiles_align_discovery_volume_and_preserve_standalone_defaults(self):
        from src.market_screener import DEFAULT_MIN_VOLUME
        for profile in ("default", "experimental"):
            settings = load_config(profile=profile)
            self.assertEqual(settings.screener.min_volume, settings.min_day_notional_volume_usd)
        self.assertEqual(DEFAULT_MIN_VOLUME, 10_000_000)


if __name__ == "__main__":
    unittest.main()

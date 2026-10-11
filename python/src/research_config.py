"""Small, strict research profiles; standard library only, no import-time I/O.

The YAML subset is mappings indented by two spaces, plain/quoted scalar tokens,
positive integers, true/false and inline provider lists. Reject unsupported YAML
rather than silently interpreting anchors, tags, duplicate keys or expressions.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .rest_policy import RESTConfig

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "research.yaml"
SCANNER_KEYS = frozenset(("min_day_notional_volume_usd", "max_spread_bps"))
SORT_MODES = ("vol_delta", "volume", "oi", "px_change", "momentum")


@dataclass(frozen=True)
class ScreenerConfig:
    top_n: int = 50
    limit: int = 10
    sort_by: str = "vol_delta"
    min_volume: int | float | None = None
    max_workers: int = 8

    def __post_init__(self):
        for name in ("top_n", "limit", "max_workers"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"screener.{name} must be a positive integer")
        if self.sort_by not in SORT_MODES:
            raise ValueError(f"screener.sort_by must be one of {SORT_MODES}")
        if self.min_volume is not None:
            try:
                valid = type(self.min_volume) in (int, float) and math.isfinite(self.min_volume) and self.min_volume >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("screener.min_volume must be finite and nonnegative")

    @property
    def parameters(self):
        return {name: getattr(self, name) for name in ("top_n", "limit", "sort_by", "min_volume", "max_workers")}


def validate_thresholds(values):
    if not isinstance(values, dict) or set(values) != SCANNER_KEYS:
        raise ValueError("scanner requires min_day_notional_volume_usd and max_spread_bps")
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise ValueError("scanner thresholds must be positive integers")


@dataclass(frozen=True)
class ResearchConfig:
    profile: str = "default"
    min_day_notional_volume_usd: int = 1_000_000
    max_spread_bps: int = 10
    providers: tuple[str, ...] = ("codex",)
    consensus: bool = False
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    rest: RESTConfig = field(default_factory=RESTConfig)

    def __post_init__(self):
        if not isinstance(self.profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.profile):
            raise ValueError("invalid profile name")
        if not isinstance(self.rest, RESTConfig):
            raise ValueError("rest must be RESTConfig")
        validate_thresholds(self.scanner)
        if not isinstance(self.screener, ScreenerConfig):
            raise ValueError("screener must be ScreenerConfig")
        if self.screener.min_volume is None:
            object.__setattr__(self, "screener", replace(self.screener, min_volume=self.min_day_notional_volume_usd))
        if (type(self.providers) is not tuple or not self.providers
                or any(provider not in ("codex", "agy") for provider in self.providers)
                or len(set(self.providers)) != len(self.providers)):
            raise ValueError("providers must be a nonempty, unique list of codex/agy")
        if type(self.consensus) is not bool:
            raise ValueError("consensus must be boolean")
        if self.consensus and set(self.providers) != {"codex", "agy"}:
            raise ValueError("consensus requires codex and agy")
        if not self.consensus and len(self.providers) != 1:
            raise ValueError("without consensus select exactly one provider")

    @property
    def scanner(self):
        return {"min_day_notional_volume_usd": self.min_day_notional_volume_usd,
                "max_spread_bps": self.max_spread_bps}

    @property
    def legacy_output(self):
        return (self.scanner == ResearchConfig().scanner
                and self.providers == ("codex",) and not self.consensus)


def _scalar(value):
    if value in ("null", "Null", "NULL", "~"):
        raise ValueError("null is not supported in research YAML")
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",") if value[1:-1].strip() else []
        return [_scalar(item.strip()) for item in items]
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        value = value[1:-1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        raise ValueError("unsupported YAML scalar")
    return value


def parse_yaml(document):
    root = {}
    stack = [(-2, root)]
    previous_indent = -2
    for number, raw in enumerate(document.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = re.fullmatch(r"( *)([A-Za-z][A-Za-z0-9_-]*):(?: +(.*))?", line)
        if not match or "\t" in raw:
            raise ValueError(f"unsupported YAML syntax at line {number}")
        indent, key, value = len(match[1]), match[2], match[3]
        if indent % 2 or indent > previous_indent + 2:
            raise ValueError(f"invalid YAML indentation at line {number}")
        while stack[-1][0] >= indent:
            stack.pop()
        if indent != stack[-1][0] + 2:
            raise ValueError(f"invalid YAML nesting at line {number}")
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"duplicate YAML key at line {number}")
        parent[key] = {} if value is None else _scalar(value)
        if value is None:
            stack.append((indent, parent[key]))
        previous_indent = indent
    return root


def load_config(path=None, profile=None):
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        document = parse_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot load research configuration {path}: {exc}") from exc
    if set(document) != {"default_profile", "profiles"} or not isinstance(document["profiles"], dict):
        raise ValueError("configuration requires only default_profile and profiles")
    profiles = {}
    for name, values in document["profiles"].items():
        if (not isinstance(values, dict) or not {"scanner", "ai"} <= set(values)
                or set(values) - {"scanner", "ai", "screener", "rest"}):
            raise ValueError(f"profile {name} requires scanner and ai, with optional screener and rest")
        validate_thresholds(values["scanner"])
        ai = values["ai"]
        if not isinstance(ai, dict) or set(ai) != {"providers", "consensus"} or not isinstance(ai["providers"], list):
            raise ValueError(f"profile {name} requires ai.providers list and ai.consensus boolean")
        screener = values.get("screener", {})
        if not isinstance(screener, dict) or set(screener) - set(ScreenerConfig().parameters):
            raise ValueError(f"profile {name} has invalid screener fields")
        rest = values.get("rest", {})
        if not isinstance(rest, dict) or set(rest) - set(RESTConfig.__dataclass_fields__):
            raise ValueError(f"profile {name} has invalid rest fields")
        profiles[name] = ResearchConfig(profile=name, **values["scanner"],
                                       providers=tuple(ai["providers"]), consensus=ai["consensus"],
                                       screener=ScreenerConfig(**screener), rest=RESTConfig(**rest))
    default = document["default_profile"]
    if not isinstance(default, str) or default not in profiles:
        raise ValueError("default_profile must name an existing profile")
    selected = default if profile is None else profile
    if not isinstance(selected, str) or selected not in profiles:
        raise ValueError(f"unknown research profile: {selected}")
    return profiles[selected]

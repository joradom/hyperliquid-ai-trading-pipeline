"""Shared deterministic SHA-256 support for R102 snapshots."""
from __future__ import annotations
import copy, hashlib, json
from typing import Any


def canonical_checksum_payload(snapshot: dict[str, Any]) -> bytes:
    relevant = copy.deepcopy(snapshot)
    relevant.pop("integrity", None)
    return json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def attach_checksum(snapshot: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_checksum_payload(snapshot)).hexdigest()
    snapshot["integrity"] = {"algorithm": "sha256", "canonicalization": "UTF-8 JSON; sorted keys; compact separators; integrity excluded",
                             "scope": "entire top-level document except the integrity field", "checksum": digest}
    return digest


def verify_checksum(snapshot: dict[str, Any]) -> bool:
    integrity = snapshot.get("integrity")
    return bool(isinstance(integrity, dict) and integrity.get("algorithm") == "sha256" and
                isinstance(integrity.get("checksum"), str) and
                hashlib.sha256(canonical_checksum_payload(snapshot)).hexdigest() == integrity["checksum"])

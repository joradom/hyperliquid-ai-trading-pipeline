"""Read-only, standard-library privacy check of changed Git index blobs only."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

PERSONAL_HOME = re.compile(
    r"(?i)(?:/(?:home|users)/|/root/|[a-z]:[\\/]+(?:users|documents and settings)[\\/]+)"
    r"[\w.-]+(?:[/\\][^\s\"'`<>]*)?"
)
PATH_TOKEN = re.compile(r"(?<![\w:/])(?:/[\w.-]+(?:/[\w.-]+)+|~[/\\][^\s\"'`<>]+)")
SECRET_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-)?[a-zA-Z0-9_-]{20,}|gh[pousr]_[a-zA-Z0-9]{25,}"
    r"|github_pat_[a-zA-Z0-9_]{30,}|xox[baprs]-[a-zA-Z0-9-]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[a-zA-Z0-9_-]{35}|eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{15,}\.[a-zA-Z0-9_-]{20,})\b"
    r"|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])(?:[\"']?)(?P<key>(?:[a-z][\w-]*[_-])?"
    r"(?:api[_-]?key|secret|secret[_-]?key|access[_-]?token|auth[_-]?token|token|password"
    r"|private[_-]?key|mnemonic|seed[_-]?phrase))[\"']?\s*[:=]\s*"
    r"(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s,;\]}]+))"
)
WALLET = re.compile(
    r"(?<![a-zA-Z0-9])(?:0x[0-9a-fA-F]{40}(?![0-9a-fA-F])"
    r"|(?:bc1|tb1)[ac-hj-np-z02-9]{25,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})(?![a-zA-Z0-9])"
)
LABELED_WALLET = re.compile(
    r"(?i)[\"']?(?:hyperliquid[_-]?account[_-]?address|wallet[_-]?address|account[_-]?address)"
    r"[\"']?\s*[:=]\s*[\"']([a-zA-Z0-9]{26,90})[\"']"
)
PLACEHOLDER = re.compile(
    r"(?i)^(?:<[^<>]+>|\$\{?[a-z_][a-z0-9_]*\}?|YOUR_[A-Z0-9_]+|DUMMY_[A-Z0-9_]+"
    r"|REDACTED|PLACEHOLDER|NONE|NULL|TRUE|FALSE|\.\.\.)$"
)
BEARER = re.compile(r"(?i)\bBearer\s+([a-zA-Z0-9._-]{16,})")
URL_AUTH = re.compile(r"(?i)\bhttps?://([^/\s@:]+):([^/\s@]+)@")
STANDARD_ROOTS = {"bin", "dev", "etc", "lib", "lib64", "proc", "run", "sbin", "sys", "tmp", "usr", "var", "missing"}
PRIVATE_DIRS = {".aws", ".codex", ".gemini", ".claude", ".ssh", "accounts", "sessions", "logs"}
PRIVATE_DATA_DIRS = {"snapshots", "order_flow", "night_guard", "captures"}
BENCHMARK_OUTPUT_DIRS = {"results", "output", "outputs", "artifacts", "data", "snapshots", "captures", "logs"}
PRIVATE_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".log", ".sqlite-wal", ".sqlite-shm",
                    ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm")


class GitReadError(Exception):
    """No command, stderr or payload is attached to the public error."""


class UsageError(Exception):
    pass


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError() from None


def _git(cwd, *args):
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(["git", "-C", str(cwd), "-c", "core.fsmonitor=false", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment, check=False)
    except OSError:
        raise GitReadError() from None
    if result.returncode:
        raise GitReadError() from None
    return result.stdout


def _placeholder(value):
    return not value or bool(PLACEHOLDER.fullmatch(value.strip())) or value.startswith(("os.environ", "os.getenv", "getenv("))


def _synthetic_address(value, filename):
    if not value.lower().startswith("0x"):
        return False
    digits = value[2:].lower()
    if set(digits) == {"0"}:
        return True
    path = Path(filename)
    synthetic_file = "fixtures" in path.parts or path.name.startswith("test_")
    return synthetic_file and len(set(digits)) == 1


def _private_file(filename):
    parts = tuple(part.lower() for part in Path(filename).parts)
    name = parts[-1]
    if name.startswith(".env") and name not in (".env.example", ".env.sample"):
        return True
    if (name.startswith(("config.local", "secrets.", "credentials."))
            or name in ("secrets", "credentials", "wallet.dat", "keystore.json", "id_rsa", "id_ed25519")
            or name.endswith(PRIVATE_SUFFIXES) or set(parts[:-1]) & PRIVATE_DIRS):
        return True
    if parts[0] == "data" and len(parts) > 1 and parts[1] in PRIVATE_DATA_DIRS:
        return True
    if parts[0] == "benchmarks" and len(parts) > 1:
        if parts[1] in BENCHMARK_OUTPUT_DIRS:
            return True
        if "fixtures" not in parts and "docs" not in parts and name.endswith((".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".webp")):
            return True
    return ("fixtures" not in parts and name.endswith(".json")
            and (name in ("account.json", "account_state.json", "wallet.json") or name.endswith("_trade_state.json")))


def _personal_path(line, markers):
    # Escaped JSON slashes are still paths; URLs and placeholders are not local paths.
    text = line.replace("\\/", "/")
    text = re.sub(r"https?://[^\s\"'`<>]+", "", text, flags=re.IGNORECASE)
    if PERSONAL_HOME.search(text):
        return True
    for match in re.finditer(r"\$(?:HOME|\{HOME\})[/\\]([^\s\"'`<>]+)", text):
        if any(marker in match[1].lower() for marker in markers):
            return True
    for match in PATH_TOKEN.finditer(text):
        value = match[0]
        if value.startswith("~"):
            relative = value[2:].split("/", 1)[0].lower()
            if relative not in ("repo", "project", "repository", ".config", ".local", ".cache", ".ssh"):
                return True
        elif any(marker in value.lower() for marker in markers):
            return True
    return False


def _safe_filename(filename, markers):
    # A filename can itself contain a secret/address. Never reproduce that value.
    result = SECRET_TOKEN.sub("[redacted]", filename)
    result = WALLET.sub("[redacted]", result)
    if PERSONAL_HOME.search(filename) or ASSIGNMENT.search(filename) or BEARER.search(filename) or URL_AUTH.search(filename):
        return "<redacted-filename>"
    for marker in markers:
        result = re.sub(re.escape(marker), "[private-name]", result, flags=re.IGNORECASE)
    return result


def _secret_assignment(match, filename):
    value = next((part for part in match.groups()[1:] if part is not None), "")
    if _placeholder(value):
        return False
    # Unquoted Python identifiers/expressions are references, not embedded values.
    if filename.endswith(".py") and match.group(4) is not None and not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{16,}", value):
        return False
    weak_key = match.group("key").lower() in ("token", "secret")
    return bool(value) and (not weak_key or len(value) >= 8)


def _secret_text(text):
    return (bool(SECRET_TOKEN.search(text))
            or any(not _placeholder(match[1]) for match in BEARER.finditer(text))
            or any(not (_placeholder(match[1]) and _placeholder(match[2])) for match in URL_AUTH.finditer(text)))


def scan_blob(filename, data, *, markers=()):
    """Return only file/line/category; bytes come from the index, never disk files."""
    shown = _safe_filename(filename, markers)
    found = set()
    if _private_file(filename) or data.startswith(b"SQLite format 3\x00"):
        return [{"file": shown, "line": 0, "category": "private_file"}]
    if _secret_text(filename): found.add((0, "possible_secret"))
    if WALLET.search(filename): found.add((0, "wallet_address"))
    if any(_secret_assignment(match, "<filename>") for match in ASSIGNMENT.finditer(filename)):
        found.add((0, "possible_secret"))
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if _personal_path(line, markers): found.add((number, "personal_path"))
        if _secret_text(line): found.add((number, "possible_secret"))
        for match in ASSIGNMENT.finditer(line):
            if _secret_assignment(match, filename): found.add((number, "possible_secret"))
        for match in WALLET.finditer(line):
            if not _synthetic_address(match[0], filename): found.add((number, "wallet_address"))
        for match in LABELED_WALLET.finditer(line):
            if not _placeholder(match[1]) and not _synthetic_address(match[1], filename):
                found.add((number, "wallet_address"))
    return [{"file": shown, "line": number, "category": category} for number, category in sorted(found)]


def check_staged(cwd=None):
    cwd = Path.cwd() if cwd is None else Path(cwd)
    root = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel")).strip())
    markers = tuple(name.lower() for name in (root.name, root.parent.name)
                    if len(name) >= 8 and name.lower() not in STANDARD_ROOTS | {"workspace", "workspaces", "projects", "repository"}
                    and not re.fullmatch(r"tmp[a-z0-9_]{6,}", name.lower()))
    names = _git(root, "diff", "--cached", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv",
                 "--diff-filter=ACMTU", "-z").split(b"\x00")
    conflicts = set()
    for record in _git(root, "ls-files", "--unmerged", "-z").split(b"\x00"):
        if record: conflicts.add(os.fsdecode(record.split(b"\t", 1)[1]))
    findings = []
    for raw in names:
        if not raw: continue
        name = os.fsdecode(raw)
        if name in conflicts:
            findings.append({"file": _safe_filename(name, markers), "line": 0, "category": "unmerged_index"})
        else:
            data = b"" if _private_file(name) else _git(root, "cat-file", "blob", ":" + name)
            findings.extend(scan_blob(name, data, markers=markers))
    return findings


def main(argv=None, *, cwd=None):
    parser = QuietParser(description=__doc__)
    try:
        parser.parse_args(argv)
        findings = check_staged(cwd)
    except UsageError:
        print(json.dumps({"file": "<arguments>", "line": 0, "category": "argument_error"}))
        return 2
    except (GitReadError, OSError):
        print(json.dumps({"file": "<git-index>", "line": 0, "category": "git_error"}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"file": "<git-index>", "line": 0, "category": "interrupted"}))
        return 2
    for finding in findings:
        print(json.dumps(finding, ensure_ascii=True, sort_keys=True))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic index tests in disposable repositories; no project staging changes."""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/check_privacy.py"
STDLIB_RUN = subprocess.run
spec = importlib.util.spec_from_file_location("r102_privacy_check", SCRIPT)
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


def home_path():
    return "/" + "home" + "/" + "synthetic_user" + "/private_project/file.txt"


def token():
    return "sk-" + "proj-" + "0123456789Ab" * 4


def address():
    return "0x" + "0123456789abcdef" * 2 + "01234567"


class PrivacyDetectionTests(unittest.TestCase):
    def categories(self, text, name="notes.md", **kwargs):
        return privacy.scan_blob(name, text.encode(), **kwargs)

    def test_personal_paths_linux_windows_escaped_json_and_abbreviated(self):
        windows = "C:" + "\\" + "Users" + "\\synthetic_user\\private_project"
        for text in (home_path(), home_path().upper(), windows, windows.replace("\\", "\\\\"),
                     home_path().replace("/", "\\/"), "~" + "/private_project/run"):
            with self.subTest(case=text[:1]):
                self.assertEqual(self.categories(text), [{"file": "notes.md", "line": 1, "category": "personal_path"}])

    def test_absolute_path_with_private_checkout_marker(self):
        text = "/mnt/" + "synthetic_checkout" + "/report"
        self.assertEqual(self.categories(text, markers=("synthetic_checkout",))[0]["category"], "personal_path")
        for prefix in ("/tmp/", "$HOME/", "${HOME}/"):
            self.assertEqual(self.categories(prefix + "synthetic_checkout/file", markers=("synthetic_checkout",))[0]["category"], "personal_path")

    def test_standard_paths_urls_relative_names_and_explicit_placeholders(self):
        text = '\n'.join(("/tmp /proc/self/status /usr/bin/env /etc/hosts /missing/research.yaml",
                          "https://example.invalid/home/synthetic_user https://example.invalid/info",
                          "$HOME/repo ${HOME}/repo <repo>/python ../../AGENTS.md R102",
                          "/home/<user>/repo C:\\Users\\<user>\\repo ~/repo ~/.local/bin ~/.config/app",
                          'api_key = "<API_KEY>"', 'password: REDACTED', 'token: ${TOKEN}',
                          'wallet_address: "<PUBLIC_ACCOUNT_ADDRESS>"'))
        self.assertEqual(self.categories(text), [])

    def test_provider_tokens_private_key_header_and_short_password(self):
        header = "-----BEGIN " + "PRIVATE KEY-----"
        payload = "intro\n" + token() + "\n" + header + "\npassword='" + "tiny" + "'"
        findings = self.categories(payload)
        self.assertEqual([row["line"] for row in findings], [2, 3, 4])
        self.assertTrue(all(row["category"] == "possible_secret" for row in findings))
        self.assertNotIn(token(), json.dumps(findings))
        google = "AI" + "za" + "0123456789Ab" * 2 + "0123456789A"
        self.assertEqual(self.categories(google)[0]["category"], "possible_secret")

    def test_bearer_and_url_credentials_without_disclosing_values(self):
        opaque = "opaque_" + "123456789abcdef" * 2
        payload = "Authorization: " + "Bearer " + opaque
        self.assertEqual(self.categories(payload)[0]["category"], "possible_secret")
        url = "https://" + "synthetic_login:" + opaque + "@example.invalid"
        findings = self.categories(url)
        self.assertEqual(findings[0]["category"], "possible_secret")
        self.assertNotIn(opaque, json.dumps(findings))
        self.assertEqual(self.categories("https://<USER>:<PASSWORD>@example.invalid\nBearer YOUR_ACCESS_TOKEN"), [])

    def test_python_runtime_key_references_are_not_embedded_values(self):
        payload = 'api_key = os.environ["OPENAI_API_KEY"]\napi_key = runtime_value\npassword = get_password()'
        self.assertEqual(self.categories(payload, "example.py"), [])
        embedded = "api_key" + " = " + json.dumps(token())
        self.assertEqual(self.categories(embedded, "example.py")[0]["category"], "possible_secret")

    def test_wallets_are_detected_even_inside_test_or_fixture_files(self):
        for name in ("notes.md", "python/tests/test_example.py", "python/tests/fixtures/synthetic.json"):
            self.assertEqual(self.categories(address(), name), [{"file": name, "line": 1, "category": "wallet_address"}])
        bitcoin = "bc" + "1" + "q" * 40
        self.assertEqual(self.categories(bitcoin)[0]["category"], "wallet_address")
        labeled = 'wallet_address = "' + "AbCdefghijkLMNPqrstUVwxyz12345678" + '"'
        self.assertEqual(self.categories(labeled)[0]["category"], "wallet_address")

    def test_obvious_synthetic_evm_constants_and_checksums(self):
        repeated = "0x" + "a" * 40
        zero = "0x" + "0" * 40
        self.assertEqual(self.categories(repeated + "\n" + zero, "python/tests/test_example.py"), [])
        self.assertEqual(self.categories(repeated)[0]["category"], "wallet_address")
        self.assertEqual(self.categories('checksum = "' + "abcdef0123456789" * 4 + '"'), [])

    def test_private_file_locations_and_database_header(self):
        names = (".env", "config.local.json", "accounts/state.json", "data/snapshots/state.json",
                 "benchmarks/results/run.json", "run.sqlite3", "run.sqlite3-wal", "run.log",
                 ".codex/auth.json", "account_state.json", "python/tests/fixtures/secrets.json",
                 "secrets", "credentials", "wallet.dat", "keystore.json")
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.categories("", name), [{"file": name, "line": 0, "category": "private_file"}])
        self.assertEqual(privacy.scan_blob("report.bin", b"SQLite format 3\x00payload")[0]["category"], "private_file")
        self.assertEqual(self.categories('API_KEY=<API_KEY>', '.env.example'), [])
        self.assertEqual(self.categories('{}', 'python/tests/fixtures/orderbook_legacy.json'), [])

    def test_output_has_only_metadata_and_redacts_sensitive_filenames(self):
        for value in (token(), address()):
            findings = self.categories(value, "notes/" + value + ".md")
            self.assertNotIn(value, json.dumps(findings))
            self.assertTrue(all(set(row) == {"file", "line", "category"} for row in findings))
            self.assertIn("[redacted]", findings[0]["file"])
        filename = "notes/" + "password" + "=" + "synthetic_value" + ".md"
        findings = self.categories("", filename)
        self.assertEqual(findings[0]["file"], "<redacted-filename>")


class PrivacyIndexTests(unittest.TestCase):
    def setUp(self):
        # Other suites mock the shared subprocess module. These isolated tests
        # need real local Git and the checker CLI, never model/wallet commands.
        runner_patch = patch.object(subprocess, "run", STDLIB_RUN)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.environment.pop(name, None)
        self.env_patch = patch.dict(os.environ, self.environment, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "core.autocrlf=false", *args],
                                       env=self.environment, stderr=subprocess.PIPE)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def stage(self, name, text):
        self.write(name, text)
        self.git("add", "--", name)

    def run_check(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = privacy.main([], cwd=self.repo)
        self.assertEqual(err.getvalue(), "")
        return code, out.getvalue()

    def test_empty_index_and_untracked_or_ignored_files_are_not_examined(self):
        self.write("private.md", home_path() + "\n" + token())
        self.write(".env", "API_KEY=" + token())
        self.write(".gitignore", ".env\n")
        self.assertEqual(self.run_check(), (0, ""))

    def test_removed_index_entry_is_not_read_from_disk(self):
        self.stage("notes.md", token())
        self.git("rm", "--cached", "--", "notes.md")
        self.assertEqual(self.run_check(), (0, ""))
        self.assertTrue((self.repo / "notes.md").is_file())

    def test_renamed_staged_destination_is_checked(self):
        self.stage("old.md", address())
        self.git("mv", "--", "old.md", "new.md")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["file"], "new.md")

    def test_unmerged_index_returns_a_metadata_only_finding(self):
        blobs = []
        for data in (b"base", b"ours", b"theirs"):
            blobs.append(subprocess.check_output(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                                                  input=data, env=self.environment).decode().strip())
        records = ''.join(f"100644 {sha} {stage}\tconflict.md\n" for stage, sha in enumerate(blobs, 1))
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--index-info"],
                       input=records.encode(), env=self.environment, check=True, capture_output=True)
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {"file": "conflict.md", "line": 0, "category": "unmerged_index"})

    def test_staged_clean_unstaged_secret_and_no_index_or_file_mutation(self):
        self.stage("notes.md", "portable documentation")
        self.write("notes.md", token())
        before_index = (self.repo / ".git/index").read_bytes()
        before_file = (self.repo / "notes.md").read_bytes()
        self.assertEqual(self.run_check(), (0, ""))
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual((self.repo / "notes.md").read_bytes(), before_file)

    def test_staged_secret_is_detected_even_when_worktree_is_clean(self):
        self.stage("notes.md", "first\n" + token())
        self.write("notes.md", "clean local copy")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {"file": "notes.md", "line": 2, "category": "possible_secret"})
        self.assertNotIn(token(), output)

    def test_private_file_is_blocked_without_opening_its_worktree_copy(self):
        self.stage("account_state.json", '{}')
        (self.repo / "account_state.json").unlink()
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["category"], "private_file")

    def test_changed_whole_blob_not_only_added_lines_is_checked(self):
        self.stage("notes.md", token() + "\nunchanged confidential line\nnew harmless line")
        self.assertEqual(self.run_check()[0], 1)

    def test_symlink_target_is_not_followed(self):
        target = Path(self.temp.name) / "outside.txt"
        target.write_text(token())
        (self.repo / "link.txt").symlink_to(target)
        self.git("add", "--", "link.txt")
        self.assertEqual(self.run_check(), (0, ""))

    def test_privacy_rules_checker_source_and_test_source_have_no_false_positives(self):
        for name in ("AGENTS.md", ".agents/rules/privacy.md", "tools/check_privacy.py", "python/tests/test_privacy_check.py"):
            self.stage(name, (ROOT / name).read_text())
        self.assertEqual(self.run_check(), (0, ""))

    def test_cli_process_exit_code_and_json_lines(self):
        self.stage("notes.md", home_path() + "\n" + address())
        result = subprocess.run(["python3", "-B", str(SCRIPT)], cwd=self.repo, env=self.environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({r["category"] for r in records}, {"personal_path", "wallet_address"})
        self.assertNotIn(home_path(), result.stdout)
        self.assertNotIn(address(), result.stdout)

    def test_git_errors_and_invalid_arguments_never_echo_details(self):
        for error in (privacy.GitReadError(), OSError(token())):
            out = io.StringIO()
            with patch.object(privacy, "_git", side_effect=error), redirect_stdout(out):
                self.assertEqual(privacy.main([], cwd=self.repo), 2)
            self.assertEqual(json.loads(out.getvalue()), {"file": "<git-index>", "line": 0, "category": "git_error"})
            self.assertNotIn(token(), out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(privacy.main(["--unknown", token()], cwd=self.repo), 2)
        self.assertNotIn(token(), out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["category"], "argument_error")


if __name__ == "__main__":
    unittest.main()

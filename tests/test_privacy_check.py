"""tools/privacy_check.py: emails, ids, secrets, allowlist and local denylist.

Sensitive-looking samples are assembled from parts so this file does not
trip the scanner it tests."""
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "privacy_check", ROOT / "tools" / "privacy_check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

REAL_EMAIL = "jane.roe" + "@" + "acme-insurance.io"
SLACK_ID = "U" + "03AB4CD5EF"
CHANNEL_ID = "C" + "07XY12ZZ9"
AWS = "AKIA" + "IOSFODNN7ABCDEFG"
GH = "ghp_" + "Zk3Qm9Lw2Xv8Rt5Yh1Bn7Cj4Dp6Fs0AgHeKu"


def scan(text, allow=(), deny=(), path="f.txt"):
    return pc.scan_text(path, text, allow, pc.compile_denylist(deny))


class ScanTest(unittest.TestCase):
    def kinds(self, text, **kw):
        return [f.kind.split(" ")[0] for f in scan(text, **kw)]

    def test_real_looking_email_is_flagged(self):
        (f,) = scan(f"contact {REAL_EMAIL} please")
        self.assertEqual(f.kind, "email")
        self.assertEqual(f.line, 1)

    def test_reserved_domains_pass(self):
        for dom in ("example.com", "example.org", "mail.example.com",
                    "corp.test", "host.invalid", "x.example", "a.localhost"):
            self.assertEqual(scan(f"a@{dom}"), [], dom)

    def test_lookalike_domains_are_flagged(self):
        for dom in ("example.com.evil.net", "notexample.com", "example.co"):
            self.assertEqual(self.kinds(f"a@{dom}"), ["email"], dom)

    def test_slack_ids(self):
        self.assertEqual(self.kinds(f"user {SLACK_ID} in {CHANNEL_ID}"),
                         ["slack-style", "slack-style"])
        for prefix in "UCWG":
            self.assertEqual(len(scan(prefix + "4Q8Z1M7K2")), 1, prefix)  # 9 chars
        self.assertEqual(len(scan("U" + "4Q8Z1M7K2P6")), 1)              # 12 chars
        self.assertEqual(scan("U" + "4Q8Z1M7K2P6X"), [])                 # 13: too long
        self.assertEqual(scan("U" + "4Q8Z1M7"), [])                       # 7 after the prefix: short
        self.assertEqual(scan("Z" + "4Q8Z1M7K2"), [])                     # other prefix
        self.assertEqual(scan("u" + "4q8z1m7k2"), [])                     # lower case

    def test_secrets(self):
        for text in (AWS, GH, "xox" + "b-4827193056-Qw8Er5Ty2Ui9Op3As",
                     "-----BEGIN RSA " + "PRIVATE KEY-----",
                     'api_key = "' + "Zk3Qm9Lw2Xv8Rt5Yh1Bn7Cj4" + '"'):
            kinds = self.kinds(text)
            self.assertIn("secret", kinds, text)

    def test_placeholders_pass(self):
        for text in ('"user_token": "xoxp-replace-me"',
                     'token = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"',
                     'secret = "00000000-0000-0000-0000-000000000000"',
                     'api_key: "your-api-key-goes-here-123"'):
            self.assertEqual(scan(text), [], text)

    def test_secret_values_are_never_printed(self):
        (f,) = scan(GH)
        self.assertNotIn(GH, str(f))
        self.assertIn("<hidden>", str(f))

    def test_allowlist_exact_regex_and_scoped(self):
        text = f"{SLACK_ID} {REAL_EMAIL}"
        self.assertEqual(len(scan(text)), 2)
        self.assertEqual(len(scan(text, allow=[SLACK_ID])), 1)
        self.assertEqual(len(scan(text, allow=["re:U03[A-Z0-9]+"])), 1)
        scoped = [f"docs/*.md :: {SLACK_ID}"]
        self.assertEqual(len(scan(text, allow=scoped, path="docs/a.md")), 1)
        self.assertEqual(len(scan(text, allow=scoped, path="src/a.py")), 2)

    def test_allowlist_regex_must_match_the_whole_token(self):
        self.assertEqual(len(scan(SLACK_ID, allow=["re:U03"])), 1)

    def test_denylist_substring_and_regex_case_insensitive(self):
        self.assertEqual(len(scan("see Project Zebra today", deny=["project zebra"])), 1)
        self.assertEqual(len(scan("codename-77", deny=["re:codename-\\d+"])), 1)
        self.assertEqual(scan("nothing here", deny=["project zebra"]), [])

    def test_denylist_hits_hide_the_entry_and_cannot_be_allowlisted(self):
        (f,) = scan("Project Zebra", deny=["project zebra"], allow=["Project Zebra"])
        self.assertNotIn("zebra", str(f).lower())
        self.assertEqual(f.kind, "denylist entry 1")

    def test_bad_denylist_regex_is_an_error(self):
        with self.assertRaises(ValueError):
            pc.compile_denylist(["re:("])

    def test_entry_file_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.txt"
            path.write_text("# comment\n\n  one \ntwo\n")
            self.assertEqual(pc.load_entries(path), ["one", "two"])
            self.assertEqual(pc.load_entries(Path(tmp) / "absent.txt"), [])


class CliTest(unittest.TestCase):
    """Run main() against a throwaway git repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        patcher = mock.patch.object(pc, "ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t",
                        "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)

    def main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = pc.main(["--allowlist", str(self.repo / "none.txt"), *args])
        return code, out.getvalue()

    def test_clean_repo_exits_zero(self):
        (self.repo / "a.txt").write_text("hello a@example.com\n")
        self.git("add", ".")
        code, out = self.main("--denylist", str(self.repo / "no-deny"))
        self.assertEqual(code, 0)
        self.assertIn("0 finding", out)

    def test_findings_exit_one_and_name_file_and_line(self):
        (self.repo / "a.txt").write_text(f"ok\n{REAL_EMAIL}\n")
        self.git("add", ".")
        code, out = self.main("--denylist", str(self.repo / "no-deny"))
        self.assertEqual(code, 1)
        self.assertIn("a.txt:2: email", out)

    def test_untracked_files_are_not_scanned(self):
        (self.repo / "tracked.txt").write_text("fine\n")
        self.git("add", ".")
        (self.repo / "loose.txt").write_text(REAL_EMAIL)
        self.assertEqual(self.main("--denylist", str(self.repo / "x"))[0], 0)

    def test_local_denylist_fails_the_run(self):
        (self.repo / "a.txt").write_text("the Zebra project\n")
        self.git("add", ".")
        deny = self.repo.parent / (self.repo.name + "-deny.txt")
        self.addCleanup(lambda: deny.exists() and deny.unlink())
        deny.write_text("# private terms\nzebra\n")
        code, out = self.main("--denylist", str(deny))
        self.assertEqual(code, 1)
        self.assertIn("denylist entry 1", out)
        self.assertNotIn("zebra", out.lower().replace("denylist", ""))
        self.assertIn("denylist active", out)

    def test_denylist_from_environment(self):
        (self.repo / "a.txt").write_text("the Zebra project\n")
        self.git("add", ".")
        deny = self.repo.parent / (self.repo.name + "-env.txt")
        self.addCleanup(lambda: deny.exists() and deny.unlink())
        deny.write_text("zebra\n")
        with mock.patch.dict(os.environ, {"MEMORY_DAEMON_DENYLIST": str(deny)}):
            self.assertEqual(self.main()[0], 1)

    def test_staged_mode_scans_the_index_not_the_work_tree(self):
        (self.repo / "a.txt").write_text("clean\n")
        self.git("add", ".")
        (self.repo / "a.txt").write_text(REAL_EMAIL + "\n")  # unstaged edit
        code, _ = self.main("--staged", "--denylist", str(self.repo / "x"))
        self.assertEqual(code, 0)
        self.git("add", ".")
        code, _ = self.main("--staged", "--denylist", str(self.repo / "x"))
        self.assertEqual(code, 1)

    def test_binary_files_are_skipped(self):
        (self.repo / "b.bin").write_bytes(b"\0\1" + REAL_EMAIL.encode())
        self.git("add", ".")
        self.assertEqual(self.main("--denylist", str(self.repo / "x"))[0], 0)

    def test_bad_denylist_regex_exits_two(self):
        (self.repo / "a.txt").write_text("x\n")
        self.git("add", ".")
        deny = self.repo.parent / (self.repo.name + "-bad.txt")
        self.addCleanup(lambda: deny.exists() and deny.unlink())
        deny.write_text("re:(\n")
        self.assertEqual(self.main("--denylist", str(deny))[0], 2)


class RepoAllowlistTest(unittest.TestCase):
    def test_shipped_allowlist_entries_are_valid_regexes(self):
        import re
        for entry in pc.load_entries(pc.ALLOWLIST):
            token = entry.partition(" :: ")[2] if " :: " in entry else entry
            if token.strip().startswith("re:"):
                re.compile(token.strip()[3:])


if __name__ == "__main__":
    unittest.main()

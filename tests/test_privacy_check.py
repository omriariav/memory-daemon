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

REAL_EMAIL = "jane.roe" + "@" + "acme-widgets.io"
SLACK_ID = "U" + "03AB4CD5EF"
CHANNEL_ID = "C" + "07XY12ZZ9"
AWS = "AKIA" + "IOSFODNN7ABCDEFG"
GH = "ghp_" + "Zk3Qm9Lw2Xv8Rt5Yh1Bn7Cj4Dp6Fs0AgHeKu"
HOME = "/Users/" + "jdoe"
WORK_ID = "acme" + "-widgets"


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

    def test_slack_dm_and_team_ids_need_a_digit(self):
        self.assertEqual(self.kinds("D" + "04AB7CD9E"), ["slack-style"])
        self.assertEqual(self.kinds("T" + "01ABC2DEF3"), ["slack-style"])
        self.assertEqual(scan("TRANSCRIPT DIRECTORY DESCRIPTION"), [])

    def test_home_paths(self):
        for text in (f"cd {HOME}/code", "/home/" + "jdoe/x", f'"{HOME}"'):
            self.assertEqual(self.kinds(text), ["home"], text)
        for text in ("/Users/test/x", "/home/test", "/Users/Shared/x",
                     "/home/runner/work", "~/Users/x", "see /Users/<name>/"):
            self.assertEqual(scan(text), [], text)

    def test_slack_workspace_hosts(self):
        host = WORK_ID + ".slack" + ".com"
        self.assertEqual(self.kinds(f"open https://{host}/archives/x"), ["slack"])
        for text in ("https://api.slack.com/methods", "files.slack.com",
                     "example.slack.com", "example-co.slack.com", "app.slack.com"):
            self.assertEqual(scan(text), [], text)

    def test_slack_webhook_urls_are_secrets(self):
        url = "https://hooks.slack" + ".com/services/" + "T" + "04AB7CD9E/B05XY8ZW1Q/Kd83jFh2LpQ9"
        kinds = self.kinds(url)
        self.assertIn("secret", kinds)

    def test_word_entries_match_whole_words_only(self):
        self.assertEqual(len(scan("Ada Lovelace joined", deny=["word:ada lovelace"])), 1)
        self.assertEqual(scan("Canada Lovelaces", deny=["word:ada lovelace"]), [])
        self.assertEqual(len(scan("x (Ada Lovelace)", deny=["word:Ada Lovelace"])), 1)
        self.assertEqual(len(scan("Canada Lovelace", deny=["ada lovelace"])), 1)  # plain = substring

    def test_word_entries_allow_separators_and_last_first_order(self):
        deny = ["word:Ada Lovelace", "word:Grace Brewster Hopper", "word:Cher"]
        for text in ("Ada Lovelace", "ada.lovelace", "ADA_LOVELACE", "ada-lovelace",
                     "Ada   Lovelace", "Lovelace, Ada", "lovelace,ada", "Lovelace ,Ada",
                     "x ada.lovelace@host", "Brewster Hopper, Grace",
                     "Grace.Brewster-Hopper", "hello, cher!"):
            self.assertEqual(len(scan(text, deny=deny)), 1, text)
        for text in ("Adam Lovelace", "Ada Lovelaces", "Canada Lovelace",
                     "Lovelace Ada", "Ada, Lovelace", "Grace Hopper",
                     "Cherry", "Ada the Lovelace"):
            self.assertEqual(scan(text, deny=deny), [], text)

    def test_word_entry_with_apostrophe_and_non_latin_names(self):
        deny = ["word:Pat O'Neil", "word:\u05e0\u05d5\u05e2\u05dd \u05db\u05d4\u05df"]
        for text in ("Pat O'Neil", "o\u2019neil, pat", "pat.o.neil",
                     "\u05e0\u05d5\u05e2\u05dd \u05db\u05d4\u05df", "\u05db\u05d4\u05df, \u05e0\u05d5\u05e2\u05dd"):
            self.assertEqual(len(scan(text, deny=deny)), 1, text)

    def test_entry_numbers_are_reported_for_the_right_entries(self):
        deny = ["plain term", "word:Ada Lovelace", "re:zz+", "word:Bo Peep"]
        found = scan("zzz and Peep, Bo and ada lovelace and PLAIN TERM", deny=deny)
        self.assertEqual(sorted(f.kind for f in found),
                         ["denylist entry 1", "denylist entry 2",
                          "denylist entry 3", "denylist entry 4"])

    def test_a_long_denylist_scans_fast(self):
        import time
        names = [f"word:First{i} Last{i}" for i in range(2000)]
        text = "\n".join(f"line {i} with some ordinary words and First{i % 7} only"
                         for i in range(20000))
        started = time.time()
        self.assertEqual(scan(text, deny=names), [])
        self.assertLess(time.time() - started, 5)
        self.assertEqual(len(scan("x First5 Last5 y", deny=names)), 1)

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


class AllowlistFileTest(unittest.TestCase):
    def test_entry_lines_only_face_the_denylist(self):
        deny = pc.compile_denylist(["word:zebra"])
        text = f"# a comment about {REAL_EMAIL}\n{SLACK_ID}\nzebra.txt :: ok\nrepo :: Zebra\n"
        found = pc.scan_allowlist("tools/privacy_allowlist.txt", text, deny)
        got = sorted((f.line, f.kind.split(" ")[0]) for f in found)
        self.assertEqual(got, [(1, "email"), (3, "denylist"), (4, "denylist")])

    def test_comments_are_checked_for_ids_too(self):
        found = pc.scan_allowlist("a", f"# {SLACK_ID}\n", [])
        self.assertEqual([f.line for f in found], [1])

    def test_the_shipped_allowlist_is_scanned_not_skipped(self):
        # Through run(): the file is read and scanned, so a denylist hit in it
        # is reported.
        deny = pc.compile_denylist(["re:::\\s*(?:gemini-notes)"])
        found = pc.run([pc.os.path.relpath(pc.ALLOWLIST, pc.ROOT)], False, [], deny)
        self.assertTrue(found)
        self.assertTrue(all(f.kind.startswith("denylist") for f in found))

    def test_shipped_allowlist_is_clean_and_scoped(self):
        entries = pc.load_entries(pc.ALLOWLIST)
        self.assertEqual(pc.run([pc.os.path.relpath(pc.ALLOWLIST, pc.ROOT)],
                                False, [], []), [])
        unscoped = [e for e in entries if " :: " not in e]
        self.assertEqual(unscoped, ["re:[CUGWDT]0EXAMPLE[A-Z0-9]*"])


class CommitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        patcher = mock.patch.object(pc, "ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_git("init", "-q")
        self.commit("base", "Base commit")
        self.base = self.run_git("rev-parse", "HEAD").strip()

    def run_git(self, *args, env=None):
        full = dict(os.environ, **(env or {}))
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True, env=full).stdout

    def commit(self, name, message, author="Dev One", email="dev@example.com"):
        (self.repo / f"{name}.txt").write_text(name)
        self.run_git("add", ".")
        self.run_git("commit", "-q", "-m", message, env={
            "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email})

    def scan(self, deny=()):
        return pc.scan_commits(f"{self.base}..HEAD", [], pc.compile_denylist(deny))

    def test_clean_range(self):
        self.commit("a", "Fix a thing\n\nCo-Authored-By: Tool <noreply@" "anthropic.com>")
        count, found = self.scan()
        self.assertEqual((count, found), (1, []))

    def test_only_the_range_is_scanned(self):
        self.commit("a", f"mention {REAL_EMAIL}")
        self.assertEqual(pc.scan_commits("HEAD..HEAD", [], [])[0], 0)

    def test_message_findings_name_the_commit(self):
        self.commit("a", f"line one\nwrite to {REAL_EMAIL} and {SLACK_ID}")
        _, found = self.scan()
        self.assertEqual(sorted(f.kind for f in found), ["email", "slack-style id"])
        self.assertTrue(found[0].path.startswith("commit:"))
        self.assertEqual({f.line for f in found}, {2})

    def test_work_looking_author_email_is_flagged(self):
        self.commit("a", "ok", email="jane" + "@" + WORK_ID + ".io")
        _, found = self.scan()
        self.assertEqual([f.kind for f in found], ["email", "email"])  # author, committer
        self.assertTrue(str(found[0].line).startswith("identity"))

    def test_personal_and_noreply_identities_pass(self):
        for email in ("me" + "@gmail.com", "12345+me@" "users.noreply.github.com",
                      "dev@example.org"):
            self.commit(email[:5], "ok", email=email)
        self.assertEqual(self.scan()[1], [])

    def test_denylist_covers_message_name_and_email(self):
        self.commit("a", "about the zebra launch", author="Zed Quux",
                    email="zq@" "gmail.com")
        for term in ("word:zebra", "word:zed quux", "zq@gmail"):
            _, found = self.scan([term])
            self.assertGreaterEqual(len(found), 1, term)
            self.assertNotIn(term.split(":")[-1], str(found[0]).lower())

    def test_cli_commits_option_and_bad_range(self):
        self.commit("a", f"mention {REAL_EMAIL}")
        out = io.StringIO()
        with redirect_stdout(out):
            code = pc.main(["--commits", f"{self.base}..HEAD",
                            "--allowlist", str(self.repo / "none"),
                            "--denylist", str(self.repo / "none")])
        self.assertEqual(code, 1)
        self.assertIn("1 commit(s), 1 finding(s)", out.getvalue())
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(pc.main(["--commits", "nope..alsonope"]), 2)


class RepoAllowlistTest(unittest.TestCase):
    def test_shipped_allowlist_entries_are_valid_regexes(self):
        import re
        for entry in pc.load_entries(pc.ALLOWLIST):
            token = entry.partition(" :: ")[2] if " :: " in entry else entry
            if token.strip().startswith("re:"):
                re.compile(token.strip()[3:])


if __name__ == "__main__":
    unittest.main()

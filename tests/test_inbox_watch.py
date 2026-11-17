"""Change-driven refresh, the stdlib half: the Watcher, the units built from
settings, and the new settings (no Textual)."""
import os
import tempfile
import unittest
from pathlib import Path

from workspace_daemon.inbox import settings, sources, watch


class FakeUnit:
    def __init__(self, name, paths):
        self.name, self._paths = name, paths

    def watch(self):
        return list(self._paths() if callable(self._paths) else self._paths)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def touch(self, name, text="x", mtime=None):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return str(path)


class WatcherTest(TmpBase):
    def make(self, units, **kw):
        self.clock = Clock()
        w = watch.Watcher(units, clock=self.clock, **kw)
        return w

    def test_no_change_means_nothing_is_due(self):
        a = self.touch("a.txt")
        w = self.make([FakeUnit("a", [a])])
        w.commit()
        for _ in range(3):
            self.clock.t += 10
            self.assertEqual(w.due(), [])

    def test_only_the_unit_with_a_changed_file_is_due(self):
        a, b = self.touch("a.txt"), self.touch("b.txt")
        w = self.make([FakeUnit("a", [a]), FakeUnit("b", [b])])
        w.commit()
        self.touch("a.txt", "longer text")
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])

    def test_an_atomic_replace_with_the_same_mtime_and_size_counts(self):
        a = self.touch("a.txt", "AAAA", mtime=1_000_000)
        w = self.make([FakeUnit("a", [a])])
        w.commit()
        tmp = self.touch("a.tmp", "BBBB", mtime=1_000_000)   # same size, mtime
        before = os.stat(a)
        os.replace(tmp, a)
        after = os.stat(a)
        self.assertEqual((before.st_mtime_ns, before.st_size),
                         (after.st_mtime_ns, after.st_size))
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])

    def test_unchanged_lists_what_was_checked_and_found_current(self):
        a, b, c = (self.touch(n) for n in ("a.txt", "b.txt", "c.txt"))
        w = self.make([FakeUnit("a", [a]), FakeUnit("b", [b]),
                       FakeUnit("c", [c]), FakeUnit("none", [])],
                      debounce_s=2.0)
        w.commit()
        self.touch("b.txt", "changed")        # b is changed
        self.clock.t += 0.5                   # ... but held by the debounce
        self.assertEqual(w.due(), [])
        self.assertEqual(sorted(w.unchanged), ["a", "c"])
        self.clock.t += 5
        self.assertEqual(w.due(), ["b"])
        self.assertEqual(sorted(w.unchanged), ["a", "c"])

    def test_same_size_new_mtime_counts(self):
        a = self.touch("a.txt", "x", mtime=1_000_000)
        w = self.make([FakeUnit("a", [a])])
        w.commit()
        self.touch("a.txt", "y", mtime=1_000_050)   # same size
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])

    def test_due_does_not_commit_so_a_busy_app_loses_nothing(self):
        a = self.touch("a.txt")
        w = self.make([FakeUnit("a", [a])])
        w.commit()
        self.touch("a.txt", "changed")
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])
        self.assertEqual(w.due(), ["a"])      # still due: nothing committed
        w.commit(["a"])
        self.assertEqual(w.due(), [])

    def test_a_change_during_a_refresh_is_due_again(self):
        a = self.touch("a.txt")
        w = self.make([FakeUnit("a", [a])])
        w.commit()
        self.touch("a.txt", "one")
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])
        w.commit(["a"])                        # the refresh starts here
        self.touch("a.txt", "two, longer")     # lands while it runs
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])

    def test_debounce_is_once_per_interval_per_source(self):
        a, b = self.touch("a.txt"), self.touch("b.txt")
        w = self.make([FakeUnit("a", [a]), FakeUnit("b", [b])], debounce_s=2.0)
        w.commit()
        runs = []
        for step in range(1, 21):               # a changes every 0.5 s for 10 s
            self.clock.t += 0.5
            self.touch("a.txt", "x" * (step + 1))
            due = w.due()
            if due:
                self.assertEqual(due, ["a"])
                runs.append(self.clock.t)
                w.commit(due)
        self.assertGreaterEqual(len(runs), 3)
        self.assertTrue(all(b - a >= 2.0 for a, b in zip(runs, runs[1:])), runs)
        # b is debounced on its own clock: its first change is not held back
        self.touch("b.txt", "changed")
        self.assertIn("b", w.due())

    def test_a_change_held_by_the_debounce_fires_once_it_is_over(self):
        a = self.touch("a.txt")
        w = self.make([FakeUnit("a", [a])], debounce_s=2.0)
        w.commit()
        self.touch("a.txt", "changed")
        self.clock.t += 0.5
        self.assertEqual(w.due(), [])
        self.clock.t += 2
        self.assertEqual(w.due(), ["a"])

    def test_a_missing_file_is_a_baseline_and_appearing_is_a_change(self):
        later = str(self.base / "later.jsonl")
        w = self.make([FakeUnit("a", [later])])
        w.commit()
        self.clock.t += 10
        self.assertEqual(w.due(), [])           # still missing: no change
        self.touch("later.jsonl", "now here")
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])
        w.commit(["a"])
        os.unlink(later)                        # vanishing is a change too
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])

    def test_stat_errors_never_raise_and_are_a_stable_signature(self):
        a = self.touch("a.txt")
        for exc in (PermissionError("no"), OSError("io"), ValueError("nul")):
            def boom(path, exc=exc):
                raise exc
            w = self.make([FakeUnit("a", [a])], stat=boom)
            w.commit()
            for _ in range(3):
                self.clock.t += 10
                self.assertEqual(w.due(), [], repr(exc))
        self.assertEqual(watch.file_signature(a, stat=boom), watch.ERROR)
        self.assertIsNone(watch.file_signature(str(self.base / "nope")))
        self.assertIsNone(watch.file_signature(a + "/child"))  # not a directory

    def test_recovering_from_a_stat_error_is_a_change(self):
        a = self.touch("a.txt")
        mode = {"fail": True}

        def flaky(path):
            if mode["fail"]:
                raise PermissionError("no")
            return os.stat(path)

        w = self.make([FakeUnit("a", [a])], stat=flaky)
        w.commit()
        mode["fail"] = False
        self.clock.t += 10
        self.assertEqual(w.due(), ["a"])

    def test_a_unit_with_no_files_is_never_due_and_a_broken_one_is_harmless(self):
        def broken():
            raise RuntimeError("boom")

        w = self.make([FakeUnit("none", []), FakeUnit("broken", broken)])
        w.commit()
        self.clock.t += 10
        self.assertEqual(w.due(), [])

    def test_a_glob_style_watch_sees_new_files(self):
        import glob
        w = self.make([FakeUnit("g", lambda: sorted(
            glob.glob(str(self.base / "d" / "*.yaml"))))])
        self.touch("d/one.yaml")
        w.commit()
        self.touch("d/two.yaml")
        self.clock.t += 10
        self.assertEqual(w.due(), ["g"])

    def test_inactive_units_are_never_due_and_activating_rebaselines(self):
        a = self.touch("a.txt")
        w = self.make([FakeUnit("a", [a])])
        w.commit()
        w.set_active("a", False)
        self.touch("a.txt", "changed")
        self.clock.t += 10
        self.assertEqual(w.due(), [])
        w.set_active("a", True)                 # the change is the new baseline
        self.clock.t += 10
        self.assertEqual(w.due(), [])
        w.set_active("nope", True)              # unknown names are ignored


class UnitsTest(TmpBase):
    def units(self, raw):
        cfg = settings.validate(raw)
        return {u.name: u for u in sources.make_units(cfg, self.base)}

    def test_builtin_units_know_their_files(self):
        units = self.units({})
        self.assertEqual(list(units), ["events", "events.invites"])
        state = self.base / "state"
        self.assertEqual(units["events"].watch(), [
            str(state / "events.jsonl"), str(state / "events.jsonl.1"),
            str(state / "alerts.json")])
        self.assertEqual(units["events.invites"].watch(),
                         [str(state / "calendar-snapshot.json")])

    def test_invites_off_means_no_invites_unit(self):
        units = self.units({"sources": [{"type": "events",
                                         "invites_days": 0}]})
        self.assertEqual(list(units), ["events"])

    def test_the_label_filter_also_watches_the_routine_files(self):
        self.touch("routines/mail.yaml", "id: mail\n")
        self.touch("routines/_template.yaml", "id: t\n")
        units = self.units({"sources": [{
            "type": "events", "email_require_label": "MY_LABEL"}]})
        self.assertIn(str(self.base / "routines" / "mail.yaml"),
                      units["events"].watch())
        plain = self.units({})
        self.assertFalse(any("routines" in p for p in plain["events"].watch()))

    def test_command_watch_paths_expand(self):
        units = self.units({"sources": [
            {"name": "mail", "type": "command", "argv": ["x"],
             "watch": ["state/comms-inbox.jsonl", "/abs/file", "~/f.json"]},
            {"name": "bare", "type": "command", "argv": ["x"]}]})
        self.assertEqual(units["mail"].watch(), [
            str(self.base / "state" / "comms-inbox.jsonl"), "/abs/file",
            os.path.expanduser("~/f.json")])
        self.assertEqual(units["bare"].watch(), [])   # fallback timer only

    def test_collect_still_merges_and_dedups_through_units(self):
        res = sources.collect(settings.default_settings(), self.base, now=1.0)
        self.assertEqual([r.name for r in res], ["events"])


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        cfg = settings.default_settings()
        self.assertEqual(cfg["poll_interval_s"], 600.0)
        self.assertEqual(cfg["watch_interval_s"], 3.0)

    def test_ranges(self):
        for ok in (1, 3, 30, 2.5):
            self.assertEqual(settings.validate(
                {"watch_interval_s": ok})["watch_interval_s"], float(ok))
        for bad in (0, 0.5, 31, -1, "3", True, None):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"watch_interval_s": bad})
        for ok in (30, 600, 3600):
            self.assertEqual(settings.validate(
                {"poll_interval_s": ok})["poll_interval_s"], float(ok))
        for bad in (29, 5, 3601, "600", True):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"poll_interval_s": bad})

    def test_watch_key(self):
        cfg = settings.validate({"sources": [
            {"name": "m", "type": "command", "argv": ["x"],
             "watch": [" state/a.jsonl ", "~/b"]}]})
        self.assertEqual(cfg["sources"][0]["watch"], ["state/a.jsonl", "~/b"])
        cfg = settings.validate({"sources": [
            {"name": "m", "type": "command", "argv": ["x"]}]})
        self.assertEqual(cfg["sources"][0]["watch"], [])
        for bad in ("state/a", [""], [" "], [1], [None], ["a\nb"],
                    ["x"] * 51, {"a": 1}):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"sources": [
                    {"name": "m", "type": "command", "argv": ["x"],
                     "watch": bad}]})
        with self.assertRaises(settings.SettingsError):   # events has none
            settings.validate({"sources": [
                {"type": "events", "watch": ["x"]}]})


if __name__ == "__main__":
    unittest.main()

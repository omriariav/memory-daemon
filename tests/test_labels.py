"""Label-cache tests.

The cache exists to stop every hourly run refetching ~940 names that change
maybe monthly. The risk it introduces is staleness turning a real label into a
false "does not exist in Gmail" — the same shape as a bug this project already
shipped — so most of these pin the self-healing behaviour.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace_daemon import labels  # noqa: E402


class FakeGmail:
    def __init__(self, names):
        self.names = list(names)
        self.calls = 0

    def user_labels(self):
        self.calls += 1
        return list(self.names)


class LabelCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()
        self.fake = FakeGmail(["EMEA", "CHANNELS", "ECN"])
        self.saved = labels.gmail
        labels.gmail = self.fake

    def tearDown(self):
        labels.gmail = self.saved
        self.tmp.cleanup()

    def cache(self, **kw):
        return labels.Catalog(self.base, **kw)

    def test_first_use_fetches_and_persists(self):
        self.assertEqual(self.cache().names(), ["EMEA", "CHANNELS", "ECN"])
        self.assertEqual(self.fake.calls, 1)
        stored = json.loads(labels.cache_file(self.base).read_text())
        self.assertEqual(stored["labels"], ["EMEA", "CHANNELS", "ECN"])
        self.assertGreater(stored["fetched_at"], 0)

    def test_a_second_run_uses_the_cache(self):
        self.cache().names()
        self.cache().names()
        self.assertEqual(self.fake.calls, 1, "a warm cache must not refetch")

    def test_an_expired_cache_refetches(self):
        self.cache().names()
        self.cache(ttl=-1).names()
        self.assertEqual(self.fake.calls, 2)

    def test_force_refresh_ignores_a_warm_cache(self):
        self.cache().names()
        self.cache(force_refresh=True).names()
        self.assertEqual(self.fake.calls, 2)

    def test_resolve_is_case_insensitive_without_refetching(self):
        cache = self.cache()
        self.assertEqual(cache.resolve("emea"), "EMEA")
        self.assertEqual(self.fake.calls, 1)

    def test_a_label_created_after_caching_is_still_found(self):
        """The staleness trap: a new label must not read as a false error."""
        cache = self.cache()
        cache.names()
        self.fake.names.append("BRAND NEW")
        self.assertEqual(cache.resolve("BRAND NEW"), "BRAND NEW")
        self.assertEqual(self.fake.calls, 2, "a miss must refetch exactly once")

    def test_a_genuinely_unknown_label_returns_none_after_one_refetch(self):
        cache = self.cache()
        cache.names()
        self.assertIsNone(cache.resolve("NEVER EXISTED"))
        self.assertEqual(self.fake.calls, 2, "must not refetch on every miss")

    def test_a_cold_cache_miss_fetches_exactly_once(self):
        """resolve() as the very first call, with nothing on disk.

        The refetch-on-miss guard has to notice that names() already fetched.
        An earlier version compared _fetched_at to 0, which names() had already
        overwritten by then, so a cold miss fetched twice. Every other miss test
        warms the cache first and so never covered this.
        """
        self.assertIsNone(self.cache().resolve("NEVER EXISTED"))
        self.assertEqual(self.fake.calls, 1,
                         "a cold cache already holds fresh data; no second fetch")

    def test_a_cold_cache_hit_fetches_exactly_once(self):
        self.assertEqual(self.cache().resolve("emea"), "EMEA")
        self.assertEqual(self.fake.calls, 1)

    def test_an_expired_cache_miss_fetches_exactly_once(self):
        self.cache().names()
        self.fake.calls = 0
        self.assertIsNone(self.cache(ttl=-1).resolve("NEVER EXISTED"))
        self.assertEqual(self.fake.calls, 1, "the expiry refetch is already current")

    def test_a_clock_jump_backwards_does_not_freeze_the_cache(self):
        """A negative age would otherwise read as fresh forever."""
        cache = self.cache()
        cache.names()
        cache._fetched_at = time.time() + 10 * self.cache().ttl  # clock jumped back
        self.fake.names.append("LATER")
        self.assertIn("LATER", cache.names(), "an implausible timestamp must expire")

    def test_read_only_never_writes_the_cache(self):
        self.cache(read_only=True).names()
        self.assertFalse(labels.cache_file(self.base).exists())

    def test_a_corrupt_cache_file_is_ignored_not_fatal(self):
        for bad in ("not json", "[]", '{"labels": "nope"}', '{"fetched_at": 1}'):
            labels.cache_file(self.base).write_text(bad)
            self.assertEqual(self.cache().names(), ["EMEA", "CHANNELS", "ECN"], bad)

    def test_a_stale_cache_is_used_when_still_inside_the_ttl(self):
        cache = self.cache()
        cache.names()
        cache._fetched_at = time.time() - 60
        self.fake.names.append("LATER")
        self.assertNotIn("LATER", cache.names(), "inside the ttl the cache stands")
        self.assertEqual(self.fake.calls, 1)


if __name__ == "__main__":
    unittest.main()

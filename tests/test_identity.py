"""Pluggable identity providers: gws default and slack_mcp (masked emails)."""
import datetime
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import claude_bridge, identity, memory_sink

SEARCH = "mcp__claude_ai_Example_Slack__slack_search_users"
PROFILE = "mcp__claude_ai_Example_Slack__slack_read_user_profile"


def search_text(*users):
    blocks = "".join(
        f"### Result {i} of {len(users)}\nName: {name}\nUser ID: {uid}\n"
        f"Title: Engineer\nEmail: [EMAIL:*****.*****@*******.***]\n"
        f"Timezone: Europe/Example\n\n---\n\n"
        for i, (uid, name) in enumerate(users, 1)
    )
    body = f"# Search Results for: \n\n## Users ({len(users)} results)\n{blocks}"
    return json.dumps({"results": body, "pagination_info": "End of results\n"})


def profile_text(user_id, username, real="Alice Smith", display="Alice",
                 email="[EMAIL:*****.*****@*******.***]", bot="No"):
    body = (
        f"User ID: {user_id}\nUsername: {username}\nDisplay Name: {display}\n"
        f"Real Name: {real}\nPronouns: \nTitle: Engineer\nEmail: {email}\n"
        f"Organization Name: Example\nAdmin: No\nOwner: No\nBot: {bot}\n"
        f"Restricted: No\n"
    )
    return json.dumps({"result": body})


def search_result(query, text):
    return {"name": SEARCH, "input": {"keywords": [query]}, "text": text,
            "is_error": False}


def profile_result(user_id, text):
    return {"name": PROFILE, "input": {"user_id": user_id}, "text": text,
            "is_error": False}


def zero_results(*queries):
    empty = json.dumps({"results": "## Users (0 results)\n"})
    return [search_result(query, empty) for query in queries]


def alice_results(query="alice.smith", uid="U0EXAMPLE01"):
    return [
        search_result(query, search_text((uid, "Alice Smith"))),
        profile_result(uid, profile_text(uid, query)),
    ]


class ParserTest(unittest.TestCase):
    def test_search_multiple_hits(self):
        text = search_text(("U0EXAMPLE01", "A"), ("U0EXAMPLE02", "B"))
        self.assertEqual(
            identity.parse_search_users(text), ["U0EXAMPLE01", "U0EXAMPLE02"]
        )

    def test_search_zero_hits_and_no_users_section(self):
        zero = json.dumps({"results": "# Search\n\n## Users (0 results)\n"})
        self.assertEqual(identity.parse_search_users(zero), [])
        self.assertEqual(identity.parse_search_users("nothing here"), [])
        self.assertEqual(identity.parse_search_users(None), [])
        self.assertEqual(identity.parse_search_users("[1, 2]"), [])

    def test_search_non_json_raw_text(self):
        raw = json.loads(search_text(("U0EXAMPLE01", "A")))["results"]
        self.assertEqual(identity.parse_search_users(raw), ["U0EXAMPLE01"])

    def test_search_ignores_other_sections(self):
        raw = (
            "## Users (1 results)\n### Result 1 of 1\nUser ID: U0EXAMPLE01\n\n"
            "## Channels (1 results)\n### Result 1 of 1\nUser ID: CNOTUSER\n"
        )
        self.assertEqual(identity.parse_search_users(raw), ["U0EXAMPLE01"])

    def test_profile_parse_and_unknown_labels_ignored(self):
        fields = identity.parse_profile(profile_text("U0EXAMPLE01", "alice.smith"))
        self.assertEqual(fields["Username"], "alice.smith")
        self.assertEqual(fields["Real Name"], "Alice Smith")
        self.assertEqual(fields["Bot"], "No")

    def test_profile_garbage_is_empty(self):
        self.assertEqual(identity.parse_profile("{not json at all"), {})
        self.assertEqual(identity.parse_profile(""), {})

    def test_mask_matching(self):
        match = identity._mask_matches
        self.assertTrue(match("[EMAIL:*****.*****@*******.***]", "alice.smith@example.com"))
        self.assertFalse(match("[EMAIL:*****.*****@*******.***]", "alicesmith@example.com"))
        self.assertFalse(match("[EMAIL:****.*****@*******.***]", "alice.smith@example.com"))
        self.assertFalse(match("alice.smith@example.com", "alice.smith@example.com"))
        self.assertFalse(match("", "a@b.co"))


class AcceptTest(unittest.TestCase):
    EMAIL = "alice.smith@example.com"
    MASK = "[EMAIL:*****.*****@*******.***]"

    def fields(self, **over):
        base = {"Username": "alice.smith", "Real Name": "Alice Smith",
                "Display Name": "Alice", "Email": self.MASK, "Bot": "No"}
        base.update(over)
        return base

    def accept(self, *profiles):
        return identity._accept(
            self.EMAIL, {f"U0EXAMPLE0{i}": p for i, p in enumerate(profiles)}
        )

    def test_accepts_single_verified_candidate(self):
        self.assertEqual(self.accept(self.fields()), {
            "email": self.EMAIL, "name": "Alice Smith", "slug": "alice-smith",
            "resource_name": "slack:U0EXAMPLE00",
        })

    def test_name_falls_back_to_display_then_derived(self):
        person = self.accept(self.fields(**{"Real Name": ""}))
        self.assertEqual(person["name"], "Alice")
        person = self.accept(self.fields(**{"Real Name": "", "Display Name": ""}))
        self.assertEqual(person["name"], "Alice Smith")

    def test_slug_comes_from_email_not_editable_name(self):
        person = self.accept(self.fields(**{"Real Name": "Zoë Müller"}))
        self.assertEqual(person["slug"], "alice-smith")
        self.assertEqual(person["name"], "Zoë Müller")
        person = self.accept(self.fields(**{"Real Name": "CEO Bob"}))
        self.assertEqual(person["slug"], "alice-smith")

    def test_rejects_bot_username_mismatch_mask_mismatch(self):
        self.assertIsNone(self.accept(self.fields(Bot="Yes")))
        self.assertIsNone(self.accept(self.fields(Username="alice.smyth")))
        self.assertIsNone(
            self.accept(self.fields(Email="[EMAIL:****.*****@*******.***]"))
        )
        self.assertIsNone(self.accept({}))

    def test_bot_is_an_allowlist(self):
        for bad in ("Yes", "", "maybe", "true"):
            self.assertIsNone(self.accept(self.fields(Bot=bad)), bad)
        fields = self.fields()
        del fields["Bot"]
        self.assertIsNone(self.accept(fields))
        self.assertIsNotNone(self.accept(self.fields(Bot=" NO ")))

    def test_rejects_deleted_or_deactivated_profiles(self):
        self.assertIsNone(self.accept(self.fields(Deleted="Yes")))
        self.assertIsNone(self.accept(self.fields(Deactivated="true")))
        self.assertIsNotNone(self.accept(self.fields(Deleted="No")))

    def test_username_compare_is_casefolded(self):
        self.assertIsNotNone(self.accept(self.fields(Username="Alice.Smith")))

    def test_rejects_ambiguous_candidates(self):
        self.assertIsNone(self.accept(self.fields(), self.fields()))

    def test_one_valid_among_invalid_is_accepted(self):
        person = self.accept(self.fields(Bot="Yes"), self.fields())
        self.assertEqual(person["resource_name"], "slack:U0EXAMPLE01")


class ProviderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cfg = {
            "provider": "slack_mcp", "owner_email": "Me@Example.com",
            "owner_slug": "me-example", "search_tool": SEARCH,
            "profile_tool": PROFILE, "internal_domains": ["example.com"],
        }
        self.logs = []
        patcher = mock.patch.object(
            identity, "log", side_effect=self.logs.append
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def provider(self, **over):
        return identity.SlackMcpProvider({**self.cfg, **over}, self.base)

    @property
    def cache_file(self):
        return self.base / "state" / "identity-cache.json"


class SlackProviderTest(ProviderBase):
    def test_owner_shortcut_makes_no_lookup(self):
        provider = self.provider()
        with mock.patch.object(claude_bridge, "call_tools") as call:
            self.assertEqual(provider.current_user_email(), "me@example.com")
            person = provider.resolve_email("ME@example.com")
            provider.prefetch(["me@example.com"])
        call.assert_not_called()
        self.assertEqual(person, {
            "email": "me@example.com", "name": "Me",
            "slug": "me-example", "resource_name": "owner",
        })
        named = self.provider(owner_name="Me Example")
        self.assertEqual(named.resolve_email("me@example.com")["name"], "Me Example")

    def test_resolves_via_bridge_with_expected_call_shape(self):
        provider = self.provider(model="sonnet")
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ) as call:
            person = provider.resolve_email("Alice.Smith@example.com")
        self.assertEqual(person["slug"], "alice-smith")
        self.assertEqual(person["resource_name"], "slack:U0EXAMPLE01")
        args, kwargs = call.call_args
        self.assertEqual(args[1], {SEARCH: {}, PROFILE: {}})
        self.assertEqual(kwargs["required"], [SEARCH])
        self.assertEqual(kwargs["max_turns"], 2 * 1 + 6)
        self.assertEqual(kwargs["model"], "sonnet")
        self.assertEqual(kwargs["base_dir"], self.base)
        self.assertIn("alice.smith", args[0])

    def test_unattributable_and_malformed_results_are_ignored(self):
        results = [
            search_result("someone.else", search_text(("U0EXAMPLE09", "X"))),
            {"name": SEARCH, "input": {}, "text": search_text(("U0EXAMPLE01", "A")),
             "is_error": False},
            {"name": PROFILE, "input": {"user_id": 5}, "text": "x", "is_error": False},
            profile_result("U0EXAMPLE01", "garbage"),
            search_result("alice.smith", "not even json"),
        ]
        with mock.patch.object(claude_bridge, "call_tools", return_value=results):
            with self.assertRaises(RuntimeError):  # undecidable, not a miss
                self.provider().resolve_email("alice.smith@example.com")
        self.assertFalse(self.cache_file.exists())

    def test_keywords_as_string_accepted(self):
        results = [
            {"name": SEARCH, "input": {"keywords": "alice.smith"},
             "text": search_text(("U0EXAMPLE01", "A")), "is_error": False},
            profile_result("U0EXAMPLE01", profile_text("U0EXAMPLE01", "alice.smith")),
        ]
        with mock.patch.object(claude_bridge, "call_tools", return_value=results):
            self.assertIsNotNone(self.provider().resolve_email("alice.smith@example.com"))

    def test_zero_hits_is_unresolved_not_error(self):
        results = zero_results("alice.smith")
        with mock.patch.object(claude_bridge, "call_tools", return_value=results):
            self.assertIsNone(self.provider().resolve_email("alice.smith@example.com"))

    def test_prefetch_batches_and_serves_resolves(self):
        emails = [f"user{i}@example.com" for i in range(5)]
        calls = []

        def fake(prompt, tools, **kwargs):
            calls.append(kwargs["max_turns"])
            return zero_results(*(
                e.split("@")[0] for e in emails
                if f"- {e.split('@')[0]}" in prompt.splitlines()
            ))

        provider = self.provider(batch_size=2)
        with mock.patch.object(claude_bridge, "call_tools", side_effect=fake):
            provider.prefetch(emails + ["ME@example.com", "not-an-email"])
            self.assertEqual(calls, [10, 10, 8])
            for email in emails:
                self.assertIsNone(provider.resolve_email(email))
        self.assertEqual(len(calls), 3)

    def test_external_domain_rejected_before_any_lookup(self):
        provider = self.provider(internal_domains=["example.com"])
        with mock.patch.object(claude_bridge, "call_tools") as call:
            # example.org has the same mask shape as example.com
            self.assertIsNone(provider.resolve_email("alice.smith@example.org"))
            self.assertIsNone(provider.resolve_email("alice.smith@sub.example.com"))
            self.assertIsNone(provider.resolve_email("alice.smith@example.com.org"))
            provider.prefetch(["alice.smith@example.org"])
        call.assert_not_called()
        self.assertFalse(self.cache_file.exists())

    def test_external_twin_does_not_resolve_to_internal_user(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ) as call:
            provider.prefetch(["alice.smith@example.com", "alice.smith@example.org"])
            self.assertIsNotNone(provider.resolve_email("alice.smith@example.com"))
            self.assertIsNone(provider.resolve_email("alice.smith@example.org"))
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("alice.smith@example.org", json.loads(
            self.cache_file.read_text())["entries"])

    def test_internal_domain_match_is_case_insensitive_and_multi(self):
        provider = self.provider(internal_domains=["example.com", "example.net"])
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ):
            self.assertIsNotNone(provider.resolve_email("Alice.Smith@EXAMPLE.com"))

    def test_logs_counts_only(self):
        with mock.patch.object(claude_bridge, "call_tools", return_value=alice_results()):
            self.provider().resolve_email("alice.smith@example.com")
        text = " ".join(self.logs)
        self.assertNotIn("alice", text.casefold())
        self.assertNotIn("example.com", text)
        self.assertIn("1 email(s), 1 resolved", text)


class CacheTest(ProviderBase):
    def test_cache_written_private_and_reused_across_instances(self):
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ) as call:
            self.provider().resolve_email("alice.smith@example.com")
            self.assertEqual(call.call_count, 1)
            fresh = self.provider()
            person = fresh.resolve_email("alice.smith@example.com")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(person["slug"], "alice-smith")
        self.assertEqual(stat.S_IMODE(self.cache_file.stat().st_mode), 0o600)
        data = json.loads(self.cache_file.read_text())
        self.assertEqual(data["version"], 1)
        entry = data["entries"][self.key("alice.smith@example.com")]
        self.assertEqual(entry["person"]["slug"], "alice-smith")
        self.assertIn("checked_at", entry)

    def key(self, email, **over):
        return f"{self.provider(**over).namespace}:{email}"

    def _age(self, email, days):
        data = json.loads(self.cache_file.read_text())
        stamp = (identity._now() - datetime.timedelta(days=days)).isoformat()
        data["entries"][self.key(email)]["checked_at"] = stamp
        self.cache_file.write_text(json.dumps(data))

    def test_hit_ttl_expiry(self):
        email = "alice.smith@example.com"
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ) as call:
            self.provider().resolve_email(email)
            self._age(email, 29)
            self.provider().resolve_email(email)
            self.assertEqual(call.call_count, 1)
            self._age(email, 31)
            self.provider().resolve_email(email)
            self.assertEqual(call.call_count, 2)

    def test_miss_ttl_expiry_is_shorter(self):
        email = "nobody@example.com"
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("nobody")
        ) as call:
            self.provider().resolve_email(email)
            self._age(email, 0.5)
            self.assertIsNone(self.provider().resolve_email(email))
            self.assertEqual(call.call_count, 1)
            self._age(email, 2)
            self.provider().resolve_email(email)
            self.assertEqual(call.call_count, 2)

    def test_custom_ttls_and_relative_cache_path(self):
        email = "nobody@example.com"
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("nobody")
        ) as call:
            provider = self.provider(cache="other/ids.json", miss_ttl_days=10)
            provider.resolve_email(email)
            self.assertTrue((self.base / "other" / "ids.json").exists())
            data = json.loads((self.base / "other" / "ids.json").read_text())
            data["entries"][self.key(email, cache="other/ids.json")]["checked_at"] = (
                identity._now() - datetime.timedelta(days=5)
            ).isoformat()
            (self.base / "other" / "ids.json").write_text(json.dumps(data))
            self.provider(cache="other/ids.json", miss_ttl_days=10).resolve_email(email)
        self.assertEqual(call.call_count, 1)

    def test_corrupt_cache_treated_as_empty_and_logged_once(self):
        self.cache_file.parent.mkdir(parents=True)
        self.cache_file.write_text("{ corrupt")
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("a.b", "c.d")
        ) as call:
            provider.resolve_email("a.b@example.com")
            provider.resolve_email("c.d@example.com")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(
            len([m for m in self.logs if "cache unreadable" in m]), 1
        )
        self.assertEqual(
            json.loads(self.cache_file.read_text())["version"], 1
        )

    def test_wrong_shape_and_bad_entries_ignored(self):
        self.cache_file.parent.mkdir(parents=True)
        self.cache_file.write_text(json.dumps({"version": 9, "entries": {}}))
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("a.b")
        ) as call:
            self.provider().resolve_email("a.b@example.com")
        self.assertEqual(call.call_count, 1)
        self.cache_file.write_text(json.dumps({"version": 1, "entries": {
            self.key("a.b@example.com"): {"person": "oops", "checked_at": "never"},
        }}))
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("a.b")
        ) as call:
            self.provider().resolve_email("a.b@example.com")
        self.assertEqual(call.call_count, 1)

    def test_entries_are_namespaced_by_config(self):
        base = self.provider()
        self.assertNotEqual(base.namespace, self.provider(
            internal_domains=["example.com", "example.net"]).namespace)
        self.assertNotEqual(base.namespace, self.provider(
            search_tool="mcp__other__search").namespace)
        self.assertNotEqual(base.namespace, self.provider(
            profile_tool="mcp__other__profile").namespace)
        self.assertEqual(base.namespace, self.provider(model="sonnet").namespace)
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ) as call:
            base.resolve_email("alice.smith@example.com")
            other = self.provider(internal_domains=["example.com", "example.net"])
            other.resolve_email("alice.smith@example.com")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(json.loads(self.cache_file.read_text())["entries"]), 2)

    def test_save_merges_entries_written_by_another_routine(self):
        first, second = self.provider(), self.provider(
            internal_domains=["example.com", "example.net"])
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ):
            first.resolve_email("alice.smith@example.com")
            second.resolve_email("alice.smith@example.com")
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("nobody")
        ):
            first.resolve_email("nobody@example.com")  # stale _disk snapshot
        entries = json.loads(self.cache_file.read_text())["entries"]
        self.assertEqual(len(entries), 3)
        self.assertIn(f"{second.namespace}:alice.smith@example.com", entries)
        self.assertEqual(stat.S_IMODE(self.cache_file.stat().st_mode), 0o600)

    def test_clear_cache_drops_run_state_but_keeps_disk(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ) as call:
            provider.resolve_email("alice.smith@example.com")
            provider.clear_cache()
            provider.resolve_email("alice.smith@example.com")
        self.assertEqual(call.call_count, 1)


class IncompleteLookupTest(ProviderBase):
    EMAIL = "alice.smith@example.com"

    def check_undecided(self, results):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=results
        ) as call:
            with self.assertRaises(RuntimeError):
                provider.resolve_email(self.EMAIL)
            with self.assertRaises(RuntimeError):  # no refetch within the run
                provider.resolve_email(self.EMAIL)
            provider.prefetch([self.EMAIL])
        self.assertEqual(call.call_count, 1)
        self.assertFalse(self.cache_file.exists())
        # A fresh run retries rather than serving a cached miss.
        provider.clear_cache()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ):
            self.assertIsNotNone(provider.resolve_email(self.EMAIL))

    def test_skipped_query_is_not_a_miss(self):
        self.check_undecided([])

    def test_search_error_result_is_not_a_miss(self):
        errored = dict(alice_results()[0], is_error=True)
        self.check_undecided([errored])

    def test_search_error_json_without_results_is_not_a_miss(self):
        self.check_undecided([search_result(
            "alice.smith", json.dumps({"error": "rate_limited"})
        )])

    def test_missing_profile_read_is_not_a_miss(self):
        self.check_undecided(alice_results()[:1])

    def test_errored_profile_read_is_not_a_miss(self):
        search, profile = alice_results()
        self.check_undecided([search, dict(profile, is_error=True)])

    def test_unprofiled_extra_candidate_blocks_a_decision(self):
        # Alice verifies, but a second unprofiled candidate could be a twin.
        self.check_undecided([
            search_result("alice.smith", search_text(
                ("U0EXAMPLE01", "Alice Smith"), ("U0EXAMPLE02", "Alice Twin"))),
            profile_result("U0EXAMPLE01", profile_text("U0EXAMPLE01", "alice.smith")),
        ])

    def test_complete_miss_is_cached(self):
        provider = self.provider()
        results = [
            search_result("alice.smith", search_text(("U0EXAMPLE01", "A"))),
            profile_result("U0EXAMPLE01", profile_text(
                "U0EXAMPLE01", "someone.else")),
        ]
        with mock.patch.object(claude_bridge, "call_tools", return_value=results):
            self.assertIsNone(provider.resolve_email(self.EMAIL))
        entries = json.loads(self.cache_file.read_text())["entries"]
        self.assertIsNone(entries[f"{provider.namespace}:{self.EMAIL}"]["person"])

    def test_one_undecided_email_does_not_block_the_rest(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ):
            provider.prefetch([self.EMAIL, "bob.jones@example.com"])
        self.assertIsNotNone(provider.resolve_email(self.EMAIL))
        with self.assertRaises(RuntimeError):
            provider.resolve_email("bob.jones@example.com")
        self.assertEqual(
            list(json.loads(self.cache_file.read_text())["entries"]),
            [f"{provider.namespace}:{self.EMAIL}"],
        )


class OutageTest(ProviderBase):
    def test_bridge_error_raises_runtime_error_and_latches(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools",
            side_effect=claude_bridge.BridgeError("boom"),
        ) as call:
            with self.assertRaises(RuntimeError):
                provider.resolve_email("a.b@example.com")
            with self.assertRaises(RuntimeError):
                provider.resolve_email("c.d@example.com")
            with self.assertRaises(RuntimeError):
                provider.prefetch(["e.f@example.com"])
        self.assertEqual(call.call_count, 1)
        self.assertFalse(self.cache_file.exists())

    def test_guard_stop_is_not_latched_for_other_routines(self):
        provider = self.provider()  # shared by every routine using this config
        addr = "alice.smith@" + self.cfg["internal_domains"][0]
        with mock.patch.object(
            claude_bridge, "call_tools",
            side_effect=[
                claude_bridge.BudgetExhausted("routine cap $1 reached"),
                alice_results(),
            ],
        ) as call:
            with self.assertRaises(claude_bridge.BudgetExhausted):
                provider.resolve_email(addr)  # routine A
            person = provider.resolve_email(addr)  # routine B
        self.assertEqual(person["email"], addr)
        self.assertEqual(call.call_count, 2)
        self.assertIsNone(provider._failure)

    def test_clear_cache_unlatches(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools",
            side_effect=claude_bridge.BridgeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                provider.resolve_email("a.b@example.com")
        provider.clear_cache()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=zero_results("a.b")
        ):
            self.assertIsNone(provider.resolve_email("a.b@example.com"))

    def test_dry_run_is_an_outage_without_logging(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools",
            side_effect=claude_bridge.BridgeDryRun("dry"),
        ):
            with self.assertRaises(RuntimeError):
                provider.resolve_email("a.b@example.com")
        self.assertEqual(self.logs, [])

    def test_cached_people_still_resolve_during_outage(self):
        provider = self.provider()
        with mock.patch.object(
            claude_bridge, "call_tools", return_value=alice_results()
        ):
            provider.resolve_email("alice.smith@example.com")
        with mock.patch.object(
            claude_bridge, "call_tools",
            side_effect=claude_bridge.BridgeError("boom"),
        ):
            self.assertIsNotNone(provider.resolve_email("alice.smith@example.com"))
            with self.assertRaises(RuntimeError):
                provider.resolve_email("zed.zed@example.com")


class GwsDefaultTest(unittest.TestCase):
    def setUp(self):
        identity.clear_cache()

    def test_default_and_explicit_gws_use_directory(self):
        for routine in (
            {}, {"memory": {"store": "/s"}},
            {"memory": {"identity": {"provider": "gws"}}},
        ):
            provider = identity.provider_for(routine)
            self.assertIsInstance(provider, identity.GwsProvider)
        with mock.patch.object(
            identity.contacts, "resolve_email", return_value={"slug": "x"}
        ) as resolve, mock.patch.object(
            identity.drive, "current_user_email", return_value="me@example.com"
        ):
            self.assertEqual(provider.current_user_email(), "me@example.com")
            self.assertEqual(provider.resolve_email("a@example.com"), {"slug": "x"})
            self.assertIsNone(provider.prefetch(["a@example.com"]))
        resolve.assert_called_once_with("a@example.com")

    def test_clear_cache_clears_gws_state(self):
        with mock.patch.object(identity.contacts, "clear_cache") as c, \
             mock.patch.object(identity.drive, "clear_identity_cache") as d:
            identity.clear_cache()
        c.assert_called_once()
        d.assert_called_once()

    def test_factory_builds_slack_provider_and_requires_configure(self):
        routine = {"memory": {"identity": {
            "provider": "slack_mcp", "owner_email": "me@example.com",
            "owner_slug": "me-example", "search_tool": SEARCH,
            "profile_tool": PROFILE, "internal_domains": ["example.com"],
        }}}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(identity, "_base_dir", None):
                with self.assertRaises(RuntimeError):
                    identity.provider_for(routine)
            with mock.patch.object(identity, "_base_dir", Path(tmp)):
                first = identity.provider_for(routine)
                self.assertIsInstance(first, identity.SlackMcpProvider)
                self.assertIs(identity.provider_for(routine), first)
            identity._providers.clear()


class FakeProvider:
    """Deterministic provider: knows one participant, owner is fixed."""

    def __init__(self):
        self.prefetched = []

    def current_user_email(self):
        return "me@example.com"

    def prefetch(self, emails):
        self.prefetched.append(list(emails))

    def resolve_email(self, email):
        if email == "me@example.com":
            return {"email": email, "name": "Me", "slug": "me-example",
                    "resource_name": "owner"}
        if email == "alice.smith@example.com":
            return {"email": email, "name": "Alice Smith",
                    "slug": "alice-smith", "resource_name": "slack:U0EXAMPLE01"}
        return None

    def clear_cache(self):
        pass


class FakeResult:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


class SinkIntegrationTest(unittest.TestCase):
    def setUp(self):
        memory_sink._slug_cache.clear()
        memory_sink._slug_cache["/store"] = {"jane-doe", "me-example"}
        self.routine = {"id": "r", "memory": {
            "store": "/store", "type": "note", "extract": True,
        }}
        self.item = {"id": "gmail:t1", "title": "t", "date": "2026-07-27",
                     "frontmatter": {"source_people": [
                         {"email": "Alice.Smith@example.com", "name": "A"},
                         {"email": "unknown.person@example.com", "name": "U"},
                         {"email": "me@example.com", "name": "Me"},
                     ]}}

    def capture(self, provider, people):
        calls = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            calls["args"] = args
            return FakeResult("✓ created 2026-07-27-t\n")

        extraction = {"worthy": True, "type": "note", "title": "T",
                      "people": people, "tags": [], "body": "b"}
        with mock.patch.object(memory_sink, "_cli", side_effect=fake_cli), \
             mock.patch.object(memory_sink.subprocess, "run",
                               return_value=FakeResult()), \
             mock.patch.object(memory_sink, "_extract", return_value=extraction), \
             mock.patch.object(
                 memory_sink.identity_providers, "provider_for",
                 return_value=provider), \
             mock.patch.object(memory_sink, "log") as sink_log:
            out = memory_sink.capture(self.routine, self.item, "summary")
        self.sink_logs = " ".join(c.args[0] for c in sink_log.call_args_list)
        return out, calls

    def test_verified_participant_allowed_and_unresolved_dropped(self):
        provider = FakeProvider()
        out, calls = self.capture(
            provider, ["alice-smith", "unknown-person", "me-example"]
        )
        args = calls["args"]
        self.assertEqual(args[args.index("--people") + 1], "alice-smith")
        self.assertIn("people-unmapped", args[args.index("--tags") + 1])
        self.assertEqual(out["memory"], "created")
        self.assertEqual(len(provider.prefetched), 1)
        self.assertEqual(
            provider.prefetched[0],
            ["alice.smith@example.com", "unknown.person@example.com",
             "me@example.com"],
        )

    def test_sink_logs_carry_no_emails_or_names(self):
        self.capture(FakeProvider(), ["alice-smith", "unknown-person"])
        logs = self.sink_logs.casefold()
        self.assertIn("verified source participant", logs)
        for leaked in ("alice", "smith", "example.com", "unknown-person", "unknown.person", "@"):
            self.assertNotIn(leaked, logs)

    def test_provider_outage_fails_closed(self):
        class Down(FakeProvider):
            def prefetch(self, emails):
                raise RuntimeError("outage")

            def resolve_email(self, email):
                if email == "me@example.com":
                    return super().resolve_email(email)
                raise RuntimeError("outage")

        out, calls = self.capture(Down(), ["alice-smith", "jane-doe"])
        args = calls["args"]
        # Participants unresolved: only the store-known person survives.
        self.assertEqual(args[args.index("--people") + 1], "jane-doe")
        self.assertIn("people-unmapped", args[args.index("--tags") + 1])
        self.assertEqual(out["memory"], "created")

    def test_end_to_end_with_real_slack_provider_and_mocked_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = identity.SlackMcpProvider({
                "provider": "slack_mcp", "owner_email": "me@example.com",
                "owner_slug": "me-example", "search_tool": SEARCH,
                "profile_tool": PROFILE, "internal_domains": ["example.com"],
            }, Path(tmp))
            with mock.patch.object(
                claude_bridge, "call_tools", return_value=alice_results()
            ) as call, mock.patch.object(identity, "log"):
                out, calls = self.capture(provider, ["alice-smith"])
        self.assertEqual(call.call_count, 1)
        args = calls["args"]
        self.assertEqual(args[args.index("--people") + 1], "alice-smith")
        self.assertIn("people-unmapped", args[args.index("--tags") + 1])


class ValidateTest(unittest.TestCase):
    def good(self, **over):
        cfg = {"store": "/store", "identity": {
            "provider": "slack_mcp", "owner_email": "me@example.com",
            "owner_slug": "me-example", "search_tool": SEARCH,
            "profile_tool": PROFILE, "internal_domains": ["example.com"],
            **over,
        }}
        return cfg

    def problems(self, **over):
        return identity.validate(self.good(**over), "r")

    def test_valid_configs(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(
            owner_name="Me Example", model="haiku",
            cache="state/identity-cache.json", hit_ttl_days=30,
            miss_ttl_days=1, batch_size=15,
        ), [])
        self.assertEqual(identity.validate({"store": "/s"}, "r"), [])
        self.assertEqual(
            identity.validate({"identity": {"provider": "gws"}}, "r"), []
        )

    def test_bad_provider_and_shape(self):
        self.assertTrue(identity.validate({"identity": {"provider": "x"}}, "r"))
        self.assertTrue(identity.validate({"identity": "gws"}, "r"))
        self.assertTrue(self.problems(bogus=1))

    def test_required_keys(self):
        for key in ("owner_email", "owner_slug", "search_tool", "profile_tool",
                    "internal_domains"):
            cfg = self.good()
            del cfg["identity"][key]
            self.assertTrue(identity.validate(cfg, "r"), key)

    def test_tool_names_must_be_mcp(self):
        self.assertTrue(self.problems(search_tool="slack_search_users"))
        self.assertTrue(self.problems(profile_tool="Bash"))

    def test_numbers_must_be_positive_ints(self):
        for key in ("hit_ttl_days", "miss_ttl_days", "batch_size"):
            for bad in (0, -1, "3", 1.5, True):
                self.assertTrue(self.problems(**{key: bad}), (key, bad))

    def test_owner_and_cache_shape(self):
        self.assertTrue(self.problems(owner_email="not-an-email"))
        self.assertTrue(self.problems(owner_slug="Me Example"))
        self.assertTrue(self.problems(cache="/abs/cache.json"))
        self.assertTrue(self.problems(cache="../up.json"))
        self.assertTrue(self.problems(model=""))

    def test_internal_domains_shape(self):
        self.assertEqual(
            self.problems(internal_domains=["a.example.com", "example.org"]), [])
        for bad in ([], "example.com", ["Example.com"], ["example"], ["@example.com"],
                    ["example.com", 5], [""], ["a b.com"], None):
            self.assertTrue(self.problems(internal_domains=bad), bad)

    def test_memory_sink_validate_accepts_identity_block(self):
        routine = {"id": "r", "memory": self.good()}
        self.assertEqual(memory_sink.validate(routine), [])
        routine["memory"]["identity"]["provider"] = "nope"
        self.assertTrue(memory_sink.validate(routine))


class BridgeRequiredTest(unittest.TestCase):
    def test_required_must_be_subset(self):
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.call_tools(
                "p", {"mcp__a__b": {}}, base_dir="/nonexistent",
                required=["mcp__a__c"],
            )


if __name__ == "__main__":
    unittest.main()

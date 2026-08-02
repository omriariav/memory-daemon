"""Read-only Slack activity census behavior."""
import json
import tempfile
import unittest
from pathlib import Path

from workspace_daemon import slack_census


class APIError(RuntimeError):
    def __init__(self, error, retry_after=None):
        super().__init__(error)
        self.error = error
        self.retry_after = retry_after


class CensusTest(unittest.TestCase):
    CONVERSATIONS = [
        {"id": "C1", "name": "project", "is_private": False},
        {"id": "D2", "user": "U2", "is_im": True, "is_private": True},
        {"id": "G3", "name": "mpdm-a--b", "is_mpim": True, "is_private": True},
    ]

    def test_only_stale_conversation_errors_are_nonfatal(self):
        errors = [
            {"id": "D1", "error": "channel_not_found"},
            {"id": "C2", "error": "is_archived"},
            {"id": "G3", "error": "not_in_channel"},
        ]
        self.assertEqual(slack_census.fatal_errors(errors), [])
        self.assertEqual(
            slack_census.fatal_errors([
                *errors,
                {"id": "C4", "error": "missing_scope"},
            ]),
            [{"id": "C4", "error": "missing_scope"}],
        )

    def test_rejects_checkpoint_with_out_of_range_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            checkpoint.write_text(json.dumps({
                "version": 1,
                "cutoff_epoch": 10,
                "cutoff_at": "1970-01-01T00:00:10Z",
                "until_epoch": 20,
                "until_at": "1970-01-01T00:00:20Z",
                "inventory": [{"id": "C1"}],
                "next_index": 2,
                "active": [],
                "errors": [],
            }))

            with self.assertRaisesRegex(RuntimeError, "next_index"):
                slack_census.load_checkpoint(checkpoint)

    def test_rejects_checkpoint_result_outside_completed_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            checkpoint.write_text(json.dumps({
                "version": 1,
                "cutoff_epoch": 10,
                "cutoff_at": "1970-01-01T00:00:10Z",
                "inventory": [{"id": "C1"}, {"id": "C2"}],
                "next_index": 1,
                "active": [{"id": "C2"}],
                "errors": [],
            }))

            with self.assertRaisesRegex(RuntimeError, "completed prefix"):
                slack_census.load_checkpoint(checkpoint)

    def test_rejects_unpaired_fixed_window_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            checkpoint.write_text(json.dumps({
                "version": 1,
                "cutoff_epoch": 10,
                "cutoff_at": "1970-01-01T00:00:10Z",
                "until_epoch": 20,
                "inventory": [],
                "next_index": 0,
                "active": [],
                "errors": [],
            }))

            with self.assertRaisesRegex(RuntimeError, "must either both"):
                slack_census.load_checkpoint(checkpoint)

    def test_reports_only_recent_activity_without_message_text(self):
        responses = {
            "C1": {"ok": True, "messages": [{"ts": "20.0", "text": "secret"}]},
            "D2": {"ok": True, "messages": []},
            "G3": {
                "ok": True,
                "messages": [{"ts": "30.0", "text": "sensitive-body"}],
            },
        }
        calls = []

        def api(method, params):
            calls.append((method, params))
            return responses[params["channel"]]

        result = slack_census.run(
            self.CONVERSATIONS,
            api,
            cutoff_epoch=10.123456,
            until_epoch=20.654321,
            requests_per_minute=50,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [(row["id"], row["type"]) for row in result["active"]],
            [("C1", "public_channel")],
        )
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("sensitive-body", json.dumps(result))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1]["limit"], 200)
        self.assertLess(float(calls[0][1]["oldest"]), 10.123456)
        self.assertEqual(calls[0][1]["latest"], "20.654321")
        self.assertEqual(calls[0][1]["inclusive"], "true")
        self.assertEqual(
            result["cutoff_at"], "1970-01-01T00:00:10.123456Z"
        )
        self.assertEqual(
            result["until_at"], "1970-01-01T00:00:20.654321Z"
        )

    def test_reply_to_old_root_makes_conversation_active(self):
        def api(_method, _params):
            return {
                "ok": True,
                "messages": [{
                    "ts": "100.0",
                    "latest_reply": "950.0",
                    "text": "must not be stored",
                }],
            }

        result = slack_census.run(
            [self.CONVERSATIONS[0]],
            api,
            cutoff_epoch=900,
            until_epoch=1000,
            thread_root_days=30,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result["active"][0]["latest_ts"], "950.0")
        self.assertNotIn("must not be stored", json.dumps(result))

    def test_checkpoint_resumes_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            calls = []

            def interrupted(_method, params):
                calls.append(params["channel"])
                if params["channel"] == "D2":
                    raise KeyboardInterrupt()
                return {"ok": True, "messages": []}

            with self.assertRaises(KeyboardInterrupt):
                slack_census.run(
                    self.CONVERSATIONS,
                    interrupted,
                    cutoff_epoch=10,
                    until_epoch=40,
                    requests_per_minute=50,
                    checkpoint=checkpoint,
                    sleep=lambda _seconds: None,
                )

            # The first item is not at a ten-item flush boundary, so resume may
            # safely repeat it; no completed active result is lost or duplicated.
            resumed_calls = []

            def resumed(_method, params):
                resumed_calls.append(params)
                return {"ok": True, "messages": []}

            result = slack_census.run(
                self.CONVERSATIONS,
                resumed,
                cutoff_epoch=10,
                until_epoch=99,
                requests_per_minute=50,
                checkpoint=checkpoint,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result["next_index"], 3)
            self.assertEqual(
                {call["latest"] for call in resumed_calls},
                {"40.000000"},
            )

    def test_completed_checkpoint_starts_a_fresh_census(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            first_calls = []

            def first_api(_method, params):
                first_calls.append(params["channel"])
                return {"ok": True, "messages": [{"ts": "20.0"}]}

            slack_census.run(
                [self.CONVERSATIONS[0]],
                first_api,
                cutoff_epoch=10,
                until_epoch=20,
                requests_per_minute=50,
                checkpoint=checkpoint,
                sleep=lambda _seconds: None,
            )

            second_calls = []

            def second_api(_method, params):
                second_calls.append(params["channel"])
                return {"ok": True, "messages": []}

            result = slack_census.run(
                [self.CONVERSATIONS[1]],
                second_api,
                cutoff_epoch=30,
                until_epoch=40,
                requests_per_minute=50,
                checkpoint=checkpoint,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(first_calls, ["C1"])
            self.assertEqual(second_calls, ["D2"])
            self.assertEqual(result["cutoff_epoch"], 30)
            self.assertEqual(result["until_epoch"], 40)
            self.assertEqual([row["id"] for row in result["inventory"]], ["D2"])
            self.assertEqual(result["active"], [])
            self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)

    def test_rate_limit_retries_same_conversation_after_slack_delay(self):
        for retry_after in (7, 120):
            with self.subTest(retry_after=retry_after):
                attempts = []
                sleeps = []

                def api(_method, params):
                    attempts.append(params["channel"])
                    if len(attempts) == 1:
                        raise APIError("ratelimited", retry_after=retry_after)
                    return {"ok": True, "messages": []}

                slack_census.run(
                    [self.CONVERSATIONS[0]],
                    api,
                    cutoff_epoch=10,
                    requests_per_minute=50,
                    sleep=sleeps.append,
                )

                self.assertEqual(attempts, ["C1", "C1"])
                self.assertEqual(sleeps, [retry_after])


if __name__ == "__main__":
    unittest.main()

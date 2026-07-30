"""Read-only Slack activity census behavior."""
import json
import tempfile
import unittest
from pathlib import Path

from workspace_daemon import slack_census


class APIError(RuntimeError):
    def __init__(self, error):
        super().__init__(error)
        self.error = error


class CensusTest(unittest.TestCase):
    CONVERSATIONS = [
        {"id": "C1", "name": "project", "is_private": False},
        {"id": "D2", "user": "U2", "is_im": True, "is_private": True},
        {"id": "G3", "name": "mpdm-a--b", "is_mpim": True, "is_private": True},
    ]

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
            cutoff_epoch=10,
            requests_per_minute=50,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [(row["id"], row["type"]) for row in result["active"]],
            [("C1", "public_channel"), ("G3", "mpim")],
        )
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("sensitive-body", json.dumps(result))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1]["limit"], 1)

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
                    requests_per_minute=50,
                    checkpoint=checkpoint,
                    sleep=lambda _seconds: None,
                )

            # The first item is not at a ten-item flush boundary, so resume may
            # safely repeat it; no completed active result is lost or duplicated.
            result = slack_census.run(
                self.CONVERSATIONS,
                lambda _method, _params: {"ok": True, "messages": []},
                cutoff_epoch=10,
                requests_per_minute=50,
                checkpoint=checkpoint,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result["next_index"], 3)

    def test_rate_limit_retries_same_conversation(self):
        attempts = []
        sleeps = []

        def api(_method, params):
            attempts.append(params["channel"])
            if len(attempts) == 1:
                raise APIError("ratelimited")
            return {"ok": True, "messages": []}

        slack_census.run(
            [self.CONVERSATIONS[0]],
            api,
            cutoff_epoch=10,
            requests_per_minute=50,
            sleep=sleeps.append,
        )

        self.assertEqual(attempts, ["C1", "C1"])
        self.assertEqual(sleeps, [60])


if __name__ == "__main__":
    unittest.main()

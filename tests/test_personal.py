"""Personal-content flagging and ID redaction (synthetic numbers only)."""
import unittest
from unittest import mock

from workspace_daemon import llm, memory_sink, personal


def make_il_id(body8="12345678"):
    """Append the check digit that makes a valid Israeli ID."""
    for check in range(10):
        candidate = body8 + str(check)
        if personal.israeli_id_valid(candidate):
            return candidate
    raise AssertionError("no check digit")


def make_invalid_il_id(body8="12345678"):
    valid = make_il_id(body8)
    return body8 + str((int(valid[-1]) + 1) % 10)


def make_luhn(prefix, length):
    body = prefix + "0" * (length - len(prefix) - 1)
    for check in range(10):
        if personal.luhn_valid(body + str(check)):
            return body + str(check)
    raise AssertionError


def make_iban(country="DE", bban="370400440532013000"):
    rearranged = bban + country
    num = "".join(str(int(c, 36)) for c in rearranged) + "00"
    check = 98 - int(num) % 97
    return f"{country}{check:02d}{bban}"


class RedactTest(unittest.TestCase):
    def test_israeli_id_checksum(self):
        valid = make_il_id()
        self.assertTrue(personal.israeli_id_valid(valid))
        self.assertFalse(personal.israeli_id_valid(make_invalid_il_id()))
        self.assertFalse(personal.israeli_id_valid("000000000"))

    def test_valid_bare_id_redacted_invalid_kept(self):
        valid, invalid = make_il_id(), make_invalid_il_id()
        out, n = personal.redact(f"ת\"ז של דנה: {valid} and number {invalid}.")
        self.assertIn("[ID]", out)
        self.assertNotIn(valid, out)
        self.assertIn(invalid, out)
        self.assertEqual(n, 1)

    def test_bare_valid_id_without_context_kept(self):
        valid = make_il_id()
        self.assertEqual(personal.redact(f"order {valid} shipped"),
                         (f"order {valid} shipped", 0))

    def test_labelled_valid_id_redacted_invalid_kept(self):
        valid, invalid = make_il_id(), make_invalid_il_id()
        for label in ('ת"ז', "ת.ז.", "תעודת זהות", "מספר זהות",
                      "identity number", "Israeli ID number", "national ID no."):
            out, n = personal.redact(f"{label}: {valid}")
            self.assertEqual(n, 1, label)
            self.assertNotIn(valid, out)
            self.assertIn("[ID]", out)
            self.assertEqual(personal.redact(f"{label}: {invalid}")[1], 0, label)

    def test_labelled_id_with_inner_spaces_and_dashes(self):
        valid = make_il_id()
        spaced = f"{valid[:3]} {valid[3:6]} {valid[6:]}"
        dashed = f"{valid[:3]}-{valid[3:6]}-{valid[6:]}"
        for v in (spaced, dashed):
            out, n = personal.redact(f"ת.ז. {v}")
            self.assertEqual((out, n), ("ת.ז. [ID]", 1))

    def test_passport_and_ssn(self):
        out, n = personal.redact("passport no. 12345678 and דרכון: X1234567")
        self.assertEqual(n, 2)
        self.assertNotIn("1234567", out)
        out, n = personal.redact("SSN 123-45-6789 on file")
        self.assertEqual((out, n), ("SSN [ID] on file", 1))

    def test_work_identifiers_unchanged(self):
        for text in (
            "Claim ID: 4821937", "Policy ID: LP1234567", "Order id 98765432",
            "Zoom Meeting ID: 91234567890", "Jira ticket ID 100234",
            "user id: 123456", "ID: 123456782", "id 987654321",
            "landline 031234567 and 021234567", "order number 123456782",
        ):
            self.assertEqual(personal.redact(text), (text, 0), text)

    def test_bare_valid_id_near_context_word_redacted(self):
        valid = make_il_id()
        out, n = personal.redact(f"identity doc, see {valid} attached")
        self.assertEqual(n, 1)
        self.assertNotIn(valid, out)

    def test_labelled_ssn_with_dashes(self):
        out, n = personal.redact("SSN 123-45-6789 on file")
        self.assertEqual(out, "SSN [ID] on file")
        self.assertEqual(n, 1)

    def test_card_numbers(self):
        card = make_luhn("4", 16)
        spaced = " ".join(card[i:i + 4] for i in range(0, 16, 4))
        out, n = personal.redact(f"card {card} and {spaced}")
        self.assertEqual(n, 2)
        self.assertNotIn(card, out)
        bad = card[:-1] + str((int(card[-1]) + 1) % 10)
        self.assertEqual(personal.redact(f"card {bad}"), (f"card {bad}", 0))

    def test_iban(self):
        iban = make_iban()
        out, n = personal.redact(f"pay to {iban} today")
        self.assertEqual(out, "pay to [ID] today")
        self.assertEqual(n, 1)
        spaced = " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))
        self.assertEqual(personal.redact(spaced)[1], 1)
        broken = iban[:-1] + str((int(iban[-1]) + 1) % 10)
        self.assertEqual(personal.redact(broken)[1], 0)

    def test_ordinary_numbers_untouched(self):
        # Digit strings that would pass a checksum must still survive when
        # they are shaped like dates, amounts, phones, Slack ts, ticket ids.
        valid = make_il_id()
        samples = [
            "Meeting on 2026-07-28 at 10:00",
            "Total is 1,234,567.89 USD and 12,345,678",
            "call +972-50-123-4567 or 050-123-4567 or (555) 010-9999",
            "slack:C0123ABCD:1700000000.000100",
            "p1700000000000100",
            "ticket PROJ-123456789 and #4821",
            f"v{valid} and {valid}-x and {valid}.5",
            "version 2026.07.28.1 build 987654",
        ]
        for text in samples:
            self.assertEqual(personal.redact(text), (text, 0), text)

    def test_amount_beside_id_label_word_not_overredacted(self):
        text = "valid: the id was approved for 5000 units"
        self.assertEqual(personal.redact(text), (text, 0))


class KeywordTest(unittest.TestCase):
    def test_english_and_hebrew(self):
        kws = personal.DEFAULT_PERSONAL_KEYWORDS
        for text in (
            "Please resend my payslip", "my salary review", "taking sick leave",
            "going through a divorce", "renew my passport",
            "תלוש שכר חודשי", "פרטי הפנסיה שלי", 'מספר ת"ז', "המשכורת שלי",
            "ימי מחלה", "קרן השתלמות", "גירושין", "הדרכון שלי",
        ):
            self.assertTrue(personal.keyword_hit(text, kws), text)

    def test_work_vocabulary_not_flagged(self):
        kws = personal.DEFAULT_PERSONAL_KEYWORDS
        for text in (
            "Roadmap review for Q3", "insurance policy renewal flow",
            "medical claims triage", "our lawyer reviewed the filing",
            "פוליסת ביטוח חדשה", "תביעת ביטוח רפואי", "ביטוח דירה ללקוחות",
        ):
            self.assertFalse(personal.keyword_hit(text, kws), text)

    def test_hebrew_whole_word_with_one_prefix(self):
        self.assertTrue(personal.keyword_hit("השכר עלה", ["שכר"]))
        self.assertTrue(personal.keyword_hit("שכר", ["שכר"]))
        for text in ("השכרה של משרד", "הוא נשכר אתמול", "שכרתי רכב"):
            self.assertFalse(personal.keyword_hit(text, ["שכר"]), text)

    def test_custom_keywords(self):
        cfg = {"personal_keywords": ["custody"]}
        self.assertTrue(personal.keywords_detected(cfg, "custody hearing"))
        self.assertFalse(personal.keywords_detected(cfg, "payslip"))
        self.assertFalse(personal.keywords_detected(
            {"personal_keywords": []}, "payslip"))


class ConfigTest(unittest.TestCase):
    def routine(self, **extra):
        return {"id": "r", "memory": {"store": "/s", **extra}}

    def test_defaults_and_disable(self):
        self.assertEqual(personal.personal_tag({}), "personal")
        self.assertIsNone(personal.personal_tag({"personal_tag": None}))
        self.assertIsNone(personal.personal_tag({"personal_tag": False}))
        self.assertEqual(personal.personal_tag({"personal_tag": "private"}), "private")
        self.assertTrue(personal.redact_enabled({}))
        self.assertFalse(personal.redact_enabled({"redact_ids": False}))

    def test_validation(self):
        ok = memory_sink.validate(self.routine(
            personal_tag="private", redact_ids=False, personal_keywords=["x"]))
        self.assertEqual(ok, [])
        self.assertEqual(memory_sink.validate(self.routine(personal_tag=None)), [])
        self.assertEqual(memory_sink.validate(self.routine(personal_tag=False)), [])
        for bad in (dict(personal_tag="Not A Slug"), dict(personal_tag=True),
                    dict(redact_ids="yes"), dict(personal_keywords="payroll"),
                    dict(personal_keywords=[""])):
            self.assertTrue(memory_sink.validate(self.routine(**bad)), bad)


class FakeResult:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


class CaptureTest(unittest.TestCase):
    def setUp(self):
        memory_sink._slug_cache.clear()
        memory_sink._slug_cache["/store"] = set()

    def capture(self, extraction=None, summary="Quarterly roadmap sync.",
                body="Roadmap discussion.", **cfg):
        routine = {"id": "r", "memory": {"store": "/store", "extract": False, **cfg}}
        item = {"id": "slack:C1:1.0", "title": "t", "date": "2026-07-27",
                "body": body, "frontmatter": {}}
        calls, logs = {}, []

        def fake_cli(store, args, stdin_text=None, timeout=120):
            calls["args"], calls["stdin"] = args, stdin_text
            return FakeResult("✓ created e1\n")

        patches = [
            mock.patch.object(memory_sink, "_cli", side_effect=fake_cli),
            mock.patch.object(memory_sink, "_authenticated_identity",
                              return_value={"email": None, "person": None,
                                            "safe": False}),
            mock.patch.object(memory_sink, "_commit_store"),
            mock.patch.object(memory_sink, "log", side_effect=logs.append),
        ]
        if extraction is not None:
            routine["memory"]["extract"] = True
            patches.append(mock.patch.object(
                memory_sink, "_extract", return_value=extraction))
        for p in patches:
            p.start()
        try:
            memory_sink.capture(routine, item, summary)
        finally:
            for p in patches:
                p.stop()
        args = calls["args"]
        tags = args[args.index("--tags") + 1].split(",")
        return tags, calls["stdin"], logs

    def extraction(self, **over):
        data = {"worthy": True, "owner_attention": False, "type": "note",
                "title": "T", "people": [], "tags": ["x"], "follows": [],
                "body": "b"}
        data.update(over)
        return data

    def test_model_flag_adds_tag(self):
        tags, _, logs = self.capture(self.extraction(
            personal=True, personal_reason="health"))
        self.assertIn("personal", tags)
        self.assertTrue(any("reason=health" in l for l in logs))

    def test_model_false_no_tag(self):
        tags, _, _ = self.capture(self.extraction(personal=False))
        self.assertNotIn("personal", tags)

    def test_missing_field_no_tag(self):
        tags, _, _ = self.capture(self.extraction())
        self.assertNotIn("personal", tags)

    def test_non_bool_values_follow_deterministic_rule(self):
        for bad in ("true", 1, None, "yes"):
            tags, _, _ = self.capture(self.extraction(personal=bad))
            self.assertNotIn("personal", tags, bad)
            tags, _, _ = self.capture(
                self.extraction(personal=bad), summary="Question about my payslip")
            self.assertIn("personal", tags, bad)

    def test_invalid_reason_not_logged(self):
        secret = "secret-diagnosis-text"
        _, _, logs = self.capture(self.extraction(
            personal=True, personal_reason=secret))
        self.assertFalse(any(secret in l for l in logs))

    def test_valid_model_false_beats_keywords(self):
        for summary, body in (
            ("insurance policy renewal and medical claims", "x"),
            ("תביעת ביטוח רפואי של לקוח", "פוליסת ביטוח"),
            ("my payslip", "תלוש שכר"),
        ):
            tags, _, _ = self.capture(
                self.extraction(personal=False), summary=summary, body=body)
            self.assertNotIn("personal", tags, summary)

    def test_rental_and_hiring_words_not_tagged(self):
        tags, _, _ = self.capture(summary="השכרה של משרד, נשכר עובד חדש")
        self.assertNotIn("personal", tags)

    def test_keywords_backstop_missing_verdict(self):
        tags, _, _ = self.capture(
            self.extraction(), summary="Updated my salary bands")
        self.assertIn("personal", tags)
        tags, _, _ = self.capture(body="שלחתי את תלוש השכר")
        self.assertIn("personal", tags)

    def test_backstop_without_extraction(self):
        tags, _, _ = self.capture(summary="please resend my payslip")
        self.assertIn("personal", tags)

    def test_id_number_forces_personal_and_redacts_body(self):
        ident = make_il_id()
        tags, stdin, logs = self.capture(
            self.extraction(personal=False, body=f'ת"ז {ident} filed.'),
            body=f'applicant ת"ז {ident}')
        self.assertIn("personal", tags)
        self.assertTrue(any("reason=id_number" in l for l in logs))
        self.assertNotIn(ident, stdin)
        self.assertIn("[ID]", stdin)
        self.assertTrue(any("redacted" in l for l in logs))
        self.assertFalse(any(ident in l for l in logs))

    def test_summary_redacted_when_extract_disabled(self):
        ident = make_il_id()
        _, stdin, _ = self.capture(summary=f"passport {ident} noted")
        self.assertNotIn(ident, stdin)

    def test_custom_tag(self):
        tags, _, _ = self.capture(
            self.extraction(personal=True), personal_tag="private")
        self.assertIn("private", tags)
        self.assertNotIn("personal", tags)

    def test_disabled_tag(self):
        tags, _, _ = self.capture(
            self.extraction(personal=True), personal_tag=None,
            summary="my payslip")
        self.assertNotIn("personal", tags)

    def test_redaction_disabled_keeps_text(self):
        ident = make_il_id()
        _, stdin, _ = self.capture(summary=f"passport {ident} noted", redact_ids=False)
        self.assertIn(ident, stdin)

    def test_nothing_sensitive_logged(self):
        ident = make_il_id()
        _, _, logs = self.capture(
            self.extraction(personal=True, personal_reason="payroll"),
            body=f"my salary and passport {ident}")
        joined = "\n".join(logs)
        self.assertNotIn(ident, joined)
        self.assertNotIn("my salary", joined)


class ExtractSchemaTest(unittest.TestCase):
    def run_extract(self, memory, response):
        routine = {"id": "r", "analyze": {"provider": "gemini", "model": "m"},
                   "memory": memory}
        with mock.patch("workspace_daemon.llm.analyze", return_value=response) as a:
            data = memory_sink._extract(
                routine, {"title": "T", "date": "d"}, "summary", "/store")
        return data, a.call_args.args[1]

    BASE = ('{"worthy":true,"owner_attention":false,"type":"note","title":"T",'
            '"people":[],"tags":["x"],"follows":[],"body":"b"%s}')

    def test_prompt_asks_for_personal(self):
        _, prompt = self.run_extract({"store": "/s"}, self.BASE % "")
        self.assertIn('"personal": boolean', prompt)
        self.assertIn("personal_reason", prompt)

    def test_prompt_omits_personal_when_disabled(self):
        _, prompt = self.run_extract(
            {"store": "/s", "personal_tag": None}, self.BASE % "")
        self.assertNotIn('"personal"', prompt)

    def test_accepts_personal_keys(self):
        data, _ = self.run_extract(
            {"store": "/s"},
            self.BASE % ',"personal":true,"personal_reason":"legal"')
        self.assertIs(data["personal"], True)

    def test_unknown_key_still_rejected(self):
        with self.assertRaises(ValueError):
            self.run_extract({"store": "/s"}, self.BASE % ',"bogus":1')


class AnalyzeRedactionTest(unittest.TestCase):
    def setUp(self):
        # CI has no yoetz on PATH; the command is never run (run_json is mocked).
        patcher = mock.patch.object(llm, "yoetz_bin", return_value="yoetz")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prompt_redacted_before_model_call(self):
        ident = make_il_id()
        routine = {"id": "r", "analyze": {"provider": "gemini", "model": "m"},
                   "memory": {"store": "/s"}}
        with mock.patch.object(llm, "run_json",
                               return_value={"content": "ok"}) as run, \
                mock.patch.object(llm, "log") as log:
            llm.analyze(routine, f"body with passport {ident} inside")
        sent = run.call_args.args[0][run.call_args.args[0].index("-p") + 1]
        self.assertNotIn(ident, sent)
        self.assertIn("[ID]", sent)
        self.assertNotIn(ident, str(log.call_args_list))

    def test_not_redacted_when_disabled_or_no_memory(self):
        ident = make_il_id()
        for mem in ({"store": "/s", "redact_ids": False}, None):
            routine = {"id": "r", "analyze": {"provider": "gemini", "model": "m"}}
            if mem:
                routine["memory"] = mem
            with mock.patch.object(llm, "run_json",
                                   return_value={"content": "ok"}) as run:
                llm.analyze(routine, f"passport {ident}")
            sent = run.call_args.args[0][run.call_args.args[0].index("-p") + 1]
            self.assertIn(ident, sent)


if __name__ == "__main__":
    unittest.main()

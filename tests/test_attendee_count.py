"""Calendar attendee count carried from a matched event onto the item."""
import unittest

from workspace_daemon import transcripts

# Google resource/group calendar addresses are recognised by this domain.
GOOGLE_CAL = "calendar.google.com"


def person(email, **extra):
    return dict({"email": email, "display_name": "x"}, **extra)


def owner(email="owner@example.com"):
    return person(email, self=True)


def accept(event):
    item = {"title": "t", "date": "2026-01-01", "frontmatter": {}}
    transcripts.accept_event(item, event, "high", "why")
    return item["frontmatter"]


class AttendeeCountTest(unittest.TestCase):
    def test_counts_humans_including_owner_and_skips_rooms(self):
        event = {"id": "e1", "attendees": [
            owner(), person("a@example.com"),
            person("b@example.com"),
            person("room-1@resource." + GOOGLE_CAL),
            person("c_abc@group." + GOOGLE_CAL),
            person("A@example.com"),
        ]}
        meta = accept(event)
        self.assertEqual(meta["calendar_attendee_count"], 3)
        self.assertEqual(meta["calendar_event_id"], "e1")

    def test_one_on_one_is_two(self):
        event = {"id": "e1", "attendees": [
            owner(), person("a@example.com")]}
        meta = accept(event)
        self.assertEqual(meta["calendar_attendee_count"], 2)
        self.assertEqual(meta["calendar_other_attendee"], "a@example.com")

    def test_declined_guest_is_not_counted_in_either_shape(self):
        for key, value in (("response_status", "declined"),
                           ("rsvp", "declined")):
            event = {"id": "e1", "attendees": [
                owner(), person("Dan@Example.com"),
                person("lee@example.com", **{key: value})]}
            meta = accept(event)
            self.assertEqual(meta["calendar_attendee_count"], 2)
            self.assertEqual(meta["calendar_other_attendee"], "dan@example.com")

    def test_declined_owner_still_counts(self):
        event = {"id": "e1", "attendees": [
            person("owner@example.com", self=True, rsvp="declined"),
            person("a@example.com")]}
        self.assertEqual(accept(event)["calendar_attendee_count"], 2)

    def test_owner_missing_gives_none(self):
        event = {"id": "e1", "attendees": [
            person("a@example.com"), person("b@example.com")]}
        meta = accept(event)
        self.assertNotIn("calendar_attendee_count", meta)
        self.assertNotIn("calendar_other_attendee", meta)

    def test_other_attendee_only_when_count_is_two(self):
        event = {"id": "e1", "attendees": [
            owner(), person("a@example.com"), person("b@example.com")]}
        meta = accept(event)
        self.assertEqual(meta["calendar_attendee_count"], 3)
        self.assertNotIn("calendar_other_attendee", meta)
        solo = accept({"id": "e1", "attendees": [owner()]})
        self.assertEqual(solo["calendar_attendee_count"], 1)
        self.assertNotIn("calendar_other_attendee", solo)

    def test_unknown_list_has_no_count(self):
        for event in ({"id": "e1"}, {"id": "e1", "attendees": []},
                      {"id": "e1", "attendees": None},
                      {"id": "e1", "attendees": [
                          person("r@resource." + GOOGLE_CAL)]}):
            self.assertNotIn("calendar_attendee_count", accept(event))


if __name__ == "__main__":
    unittest.main()

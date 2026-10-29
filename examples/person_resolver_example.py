#!/usr/bin/env python3
"""Example memory.person_resolver: names on stdin, slugs and facts on stdout."""
import json
import sys

PEOPLE = {
    "ada-lovelace": {"names": {"ada lovelace", "ada", "ada.lovelace"},
                     "first_names": ["Ada", "Augusta"],
                     "facts": {"title": "Staff Engineer", "team": "Engines"}},
    "sam-lead": {"names": {"sam lead", "sam"}, "first_names": ["Sam"],
                 "facts": {}},
}

names = json.load(sys.stdin)["names"]
resolved = {}
for name in names:
    hits = [slug for slug, p in PEOPLE.items() if name.strip().lower() in p["names"]]
    resolved[name] = hits[0] if len(hits) == 1 else None  # ambiguous -> null
facts = {s: PEOPLE[s]["facts"] for s in resolved.values() if s and PEOPLE[s]["facts"]}
# Names each person goes by, for matching bare first names to meeting attendees.
first_names = {s: PEOPLE[s]["first_names"] for s in resolved.values() if s}
print(json.dumps({"resolved": resolved, "facts": facts,
                  "first_names": first_names}))

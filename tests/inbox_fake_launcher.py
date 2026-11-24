"""A fake inbox task launcher for the start-a-task tests (synthetic, no network).

    python tests/inbox_fake_launcher.py <state-dir>        # request on stdin

`<state-dir>/config.json` says how each action answers; it is re-read on every
call so a test can change it between calls:

    {"draft":  <reply object> | "badjson" | "exit" | "notobject",
     "launch": <reply object> | "badjson" | "exit" | "notobject",
     "sleep_draft": 0.0, "sleep_launch": 0.0}      # seconds before answering

A reply object is printed as the one JSON line; a missing entry answers
`{"v": 1, "ok": false, "message": "no config"}`. Every request is appended, as
received, to `<state-dir>/requests.jsonl`, and its argv to `argv.json`.
"""
import json
import os
import sys
import time


def main():
    root = sys.argv[1]
    with open(os.path.join(root, "argv.json"), "w") as handle:
        json.dump(sys.argv, handle)
    raw = sys.stdin.read()
    with open(os.path.join(root, "requests.jsonl"), "a") as handle:
        handle.write(raw.strip() + "\n")
    try:
        request = json.loads(raw)
        action = request["action"]
        with open(os.path.join(root, "config.json")) as handle:
            config = json.load(handle)
    except (OSError, ValueError, KeyError):
        print(json.dumps({"v": 1, "ok": False, "message": "no config"}))
        return 0
    time.sleep(float(config.get(f"sleep_{action}", 0)))
    reply = config.get(action)
    if reply == "badjson":
        print("this is not json")
    elif reply == "notobject":
        print("[1, 2]")
    elif reply == "exit":
        sys.stderr.write("launcher exploded\nsecond line\n")
        return 3
    elif isinstance(reply, dict):
        print(json.dumps(reply))
    else:
        print(json.dumps({"v": 1, "ok": False, "message": "no config"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

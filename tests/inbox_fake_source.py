"""A fake inbox command source for the acts tests (synthetic, no network).

    python tests/inbox_fake_source.py <state-dir> act      # request on stdin
    python tests/inbox_fake_source.py <state-dir> list

`<state-dir>/config.json` says how `act` answers; it is re-read on every call
so a test can change it between calls:

    {"mode": "ok" | "shadow" | "conflict" | "fail" | "badjson" | "exit",
     "modes": {"<action>": "<mode>"},     # per action, wins over "mode"
     "sleep": 0.0}                        # seconds to wait before answering

Every request is appended, as received, to `<state-dir>/requests.jsonl`.
Like a real source it is idempotent on `op_id`: the first ok or conflict
answer for an op_id is stored in `<state-dir>/results.json` and replayed for
a repeat, without applying anything twice (`applied.jsonl` lists what was
applied). A failure is never stored, so a retry of the same op_id can succeed.

Task items (0.12.0): when `<state-dir>/items.json` holds the item with the
request's id, a fresh ok answer also changes that list the way a task source
would: `close` and `drop` take the item out (kept in `closed.json`), `reopen`
puts it back, `due` sets `meta.due` to `args.date`, so `list` shows the effect.
"""
import json
import os
import sys
import time


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _apply_task(root, request):
    """Change `items.json` for a close, drop, reopen or due (only for an id
    that is in it, or in `closed.json` for a reopen)."""
    items_path = os.path.join(root, "items.json")
    closed_path = os.path.join(root, "closed.json")
    items, closed = _load(items_path, []), _load(closed_path, [])
    item_id, action = request.get("id"), request.get("action")
    if action in ("close", "drop"):
        gone = [i for i in items if i["id"] == item_id]
        items = [i for i in items if i["id"] != item_id]
        closed += gone
    elif action == "reopen":
        back = [i for i in closed if i["id"] == item_id]
        closed = [i for i in closed if i["id"] != item_id]
        items += back
    elif action == "due":
        for i in items:
            if i["id"] == item_id:
                i["meta"]["due"] = (request.get("args") or {}).get("date")
    else:
        return
    with open(items_path, "w") as handle:
        json.dump(items, handle)
    with open(closed_path, "w") as handle:
        json.dump(closed, handle)


def main(argv):
    root, command = argv[1], argv[2]
    if command == "list":
        print(json.dumps({"v": 1, "items": _load(
            os.path.join(root, "items.json"), []), "stale": False,
            "age_min": None, "error": None}))
        return 0
    request = json.load(sys.stdin)
    with open(os.path.join(root, "requests.jsonl"), "a") as handle:
        handle.write(json.dumps(request) + "\n")
    config = _load(os.path.join(root, "config.json"), {})
    if config.get("sleep"):
        time.sleep(float(config["sleep"]))
    results_path = os.path.join(root, "results.json")
    results = _load(results_path, {})
    op_id = request.get("op_id")
    if op_id in results:
        print(json.dumps(results[op_id]))
        return 0
    mode = config.get("modes", {}).get(request.get("action"),
                                       config.get("mode", "ok"))
    if mode == "fail":
        print(json.dumps({"ok": False, "message": "source is down",
                          "shadow": False, "conflict": False}))
        return 0
    if mode == "badjson":
        print("this is not json")
        return 0
    if mode == "exit":
        sys.stderr.write("boom")
        return 3
    answer = {"ok": True, "message": f"did {request.get('action')}",
              "shadow": mode == "shadow", "conflict": False}
    if mode == "conflict":
        answer = {"ok": False, "message": "newer message", "shadow": False,
                  "conflict": True}
    if mode != "shadow" and mode != "conflict":
        with open(os.path.join(root, "applied.jsonl"), "a") as handle:
            handle.write(json.dumps({"op_id": op_id,
                                     "action": request.get("action")}) + "\n")
        _apply_task(root, request)
    results[op_id] = answer
    with open(results_path, "w") as handle:
        json.dump(results, handle)
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Gmail adapter over the Google Workspace CLI (`gws`)."""
from .shell import gws_bin, run, run_json


def user_labels():
    """All user-created label names (system labels like INBOX/STARRED excluded)."""
    result = run_json([gws_bin(), "gmail", "labels", "--format", "json"])
    return sorted(l["name"] for l in result["labels"] if l.get("type") == "user")


def user_label_ids():
    """{label id: name} of the user-created labels (`Label_<n>` ids)."""
    result = run_json([gws_bin(), "gmail", "labels", "--format", "json"])
    return {
        l["id"]: l["name"] for l in result["labels"]
        if l.get("type") == "user" and l.get("id") and l.get("name")
    }


LIST_TIMEOUT_SECONDS = 120
# `gws gmail list --all` pages through the whole result inside one process, so
# an unbounded listing (a large first catch-up) can legitimately run for minutes.
LIST_ALL_TIMEOUT_SECONDS = 600


def search(query, max_results=20, timeout=None):
    """List threads. `timeout` (seconds) bounds the one gws call; the default is
    120 for a capped listing and 600 for `max_results: 0` (all pages). A listing
    that does not finish in time raises, exactly as before."""
    limit = int(max_results)
    if timeout is None:
        timeout = LIST_ALL_TIMEOUT_SECONDS if limit == 0 else LIST_TIMEOUT_SECONDS
    args = [gws_bin(), "gmail", "list", "--query", query]
    if limit == 0:
        args.append("--all")
    else:
        args.extend(["--max", str(limit)])
    args.extend(["--format", "json"])
    result = run_json(args, timeout=timeout)
    return result.get("threads", [])


def read_message(message_id):
    return run_json([gws_bin(), "gmail", "read", message_id, "--format", "json"])


def read_thread(thread_id):
    """Return every message in a Gmail thread, in the API's chronological order."""
    return run_json([gws_bin(), "gmail", "thread", thread_id, "--format", "json"])


def links(message_id):
    """HTML links from one message, including parsed Google Docs metadata."""
    result = run_json(
        [gws_bin(), "gmail", "links", message_id, "--format", "json"]
    )
    if result.get("error"):
        raise RuntimeError(f"gws gmail links failed: {result['error']}")
    return result.get("links", [])


def _modify(message_id, add=None, remove=None):
    cmd = [gws_bin(), "gmail", "label", message_id]
    if add:
        cmd += ["--add", add]
    if remove:
        cmd += ["--remove", remove]
    run(cmd, timeout=60)


def apply_label(message_id, label):
    _modify(message_id, add=label)


def mark_read(message_id):
    _modify(message_id, remove="UNREAD")


def mark_unread(message_id):
    _modify(message_id, add="UNREAD")


def star(message_id):
    _modify(message_id, add="STARRED")


def unstar(message_id):
    _modify(message_id, remove="STARRED")


def archive(message_id):
    run([gws_bin(), "gmail", "archive", message_id], timeout=60)

"""Gmail adapter over the Google Workspace CLI (`gws`)."""
from .shell import gws_bin, run, run_json


def user_labels():
    """All user-created label names (system labels like INBOX/STARRED excluded)."""
    result = run_json([gws_bin(), "gmail", "labels", "--format", "json"])
    return sorted(l["name"] for l in result["labels"] if l.get("type") == "user")


def search(query, max_results=20):
    result = run_json(
        [gws_bin(), "gmail", "list", "--query", query,
         "--max", str(max_results), "--format", "json"]
    )
    return result.get("threads", [])


def read_message(message_id):
    return run_json([gws_bin(), "gmail", "read", message_id, "--format", "json"])


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

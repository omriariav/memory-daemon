"""Minimal on-device LLM client (Ollama HTTP API) for privacy-sensitive steps.

Transcript matching must not send raw text to a cloud model, so this module
only ever talks to a locally running Ollama server over plain HTTP using the
standard library.  It never logs prompts or model output.
"""
import json
import socket
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 120.0


class LocalLLMError(RuntimeError):
    """The local model answered, but not with usable structured JSON."""


class LocalLLMUnavailable(LocalLLMError):
    """The local model server could not be reached or timed out."""


def chat_json(base_url, model, messages, schema, timeout=DEFAULT_TIMEOUT,
              options=None, keep_alive=None):
    """Run one deterministic chat turn constrained to ``schema``; return a dict.

    ``schema`` is passed in Ollama's ``format`` field (structured outputs) and
    temperature is pinned to 0.  Raises ``LocalLLMUnavailable`` when the server
    is down or too slow (callers retry later) and ``LocalLLMError`` for any
    malformed reply.  Error text never includes message content.
    """
    url = str(base_url or DEFAULT_BASE_URL).rstrip("/") + "/api/chat"
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": schema,
        "options": {**(options or {}), "temperature": 0},
    }
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    # Never route a loopback call through an HTTP(S)_PROXY from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise LocalLLMError(f"local model returned HTTP {exc.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError):
        raise LocalLLMUnavailable("local model server unavailable") from None
    try:
        content = json.loads(raw.decode("utf-8"))["message"]["content"]
        value = json.loads(content)
    except (ValueError, KeyError, TypeError):
        raise LocalLLMError("local model returned invalid JSON") from None
    if not isinstance(value, dict):
        raise LocalLLMError("local model must return one JSON object")
    return value

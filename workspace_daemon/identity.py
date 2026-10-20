"""Pluggable identity resolution for the memory sink.

The sink needs two answers: who is the authenticated memory owner, and which
verified person does an exact participant email belong to. ``gws`` (default)
answers both from the Workspace directory. ``slack_mcp`` answers through a
claude.ai Slack connector via the headless bridge. That connector masks emails
(DLP), so an email search cannot work: the email's local part is searched
instead and every hit is verified (see ``SlackMcpProvider``).

Providers share one interface: ``current_user_email()``, ``resolve_email()``
(person dict or None; RuntimeError on an outage, latched per run),
``prefetch()`` and ``clear_cache()``.
"""
import datetime
import json
import os
import re
from pathlib import Path

from . import claude_bridge, contacts, drive
from .shell import log

PROVIDERS = ("gws", "slack_mcp")
DEFAULT_CACHE = "state/identity-cache.json"
DEFAULT_MODEL = "haiku"
DEFAULT_HIT_TTL_DAYS = 30
DEFAULT_MISS_TTL_DAYS = 1
DEFAULT_BATCH_SIZE = 15
CACHE_VERSION = 1

_IDENTITY_KEYS = {
    "provider", "owner_email", "owner_slug", "owner_name", "search_tool",
    "profile_tool", "model", "cache", "hit_ttl_days", "miss_ttl_days",
    "batch_size",
}

_base_dir = None
_providers = {}


def configure(base_dir):
    """Remember the daemon base dir (cache and bridge scratch live under it)."""
    global _base_dir
    _base_dir = base_dir


def _email(value):
    return str(value or "").strip().casefold()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def validate(cfg, rid):
    """Config problems for ``memory.identity``; empty list means valid."""
    raw = cfg.get("identity")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{rid}: memory.identity must be a mapping"]
    problems = []
    unknown = set(raw) - _IDENTITY_KEYS
    if unknown:
        problems.append(
            f"{rid}: memory.identity has unknown key(s) "
            f"{', '.join(sorted(unknown))}"
        )
    provider = raw.get("provider", "gws")
    if provider not in PROVIDERS:
        problems.append(
            f"{rid}: memory.identity.provider {provider!r} must be one of "
            f"{', '.join(PROVIDERS)}"
        )
        return problems
    if provider == "gws":
        return problems
    for key in ("owner_email", "owner_slug", "search_tool", "profile_tool"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(
                f"{rid}: memory.identity.{key} is required for slack_mcp"
            )
    owner_email = raw.get("owner_email")
    if isinstance(owner_email, str) and owner_email.strip() and (
        owner_email.count("@") != 1 or owner_email.strip().startswith("@")
        or owner_email.strip().endswith("@")
    ):
        problems.append(f"{rid}: memory.identity.owner_email must be an email")
    owner_slug = raw.get("owner_slug")
    if isinstance(owner_slug, str) and owner_slug.strip() and (
        contacts.slugify(owner_slug) != owner_slug
    ):
        problems.append(
            f"{rid}: memory.identity.owner_slug must be a kebab-case slug"
        )
    for key in ("search_tool", "profile_tool"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip() and not value.startswith("mcp__"):
            problems.append(f"{rid}: memory.identity.{key} must start with 'mcp__'")
    for key in ("owner_name", "model", "cache"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            problems.append(f"{rid}: memory.identity.{key} must be a non-empty string")
    cache = raw.get("cache")
    if isinstance(cache, str) and cache.strip() and (
        os.path.isabs(cache) or ".." in Path(cache).parts
    ):
        problems.append(
            f"{rid}: memory.identity.cache must be relative to the base dir"
        )
    for key in ("hit_ttl_days", "miss_ttl_days", "batch_size"):
        if key in raw and (
            isinstance(raw[key], bool)
            or not isinstance(raw[key], int)
            or raw[key] <= 0
        ):
            problems.append(f"{rid}: memory.identity.{key} must be a positive integer")
    return problems


class GwsProvider:
    """Default provider: Workspace directory via ``gws`` (existing behaviour)."""

    name = "gws"

    def current_user_email(self):
        return drive.current_user_email()

    def resolve_email(self, email):
        return contacts.resolve_email(email)

    def prefetch(self, emails):
        return None

    def clear_cache(self):
        contacts.clear_cache()
        drive.clear_identity_cache()


# ---- Slack connector parsing ------------------------------------------------

_MASK_RE = re.compile(r"\[EMAIL:([^\]]*)\]")
_RESULT_RE = re.compile(r"^### Result \d+ of \d+\s*$", re.MULTILINE)
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z ]*?):[ \t]*(.*)$")


def _unwrap(text):
    """Tool text is usually JSON-wrapped; return the inner string or raw text."""
    text = text if isinstance(text, str) else ""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        for key in ("results", "result"):
            if isinstance(data.get(key), str):
                return data[key]
        return ""
    return text


def _fields(block):
    fields = {}
    for line in block.splitlines():
        match = _LABEL_RE.match(line)
        if match:
            fields.setdefault(match.group(1).strip(), match.group(2).strip())
    return fields


def parse_search_users(text):
    """User ids found in a search_users result (empty on zero hits/garbage)."""
    body = _unwrap(text)
    start = body.find("## Users")
    if start < 0:
        return []
    section = body[start + len("## Users"):]
    end = re.search(r"^## ", section, re.MULTILINE)
    if end:
        section = section[:end.start()]
    ids = []
    for block in _RESULT_RE.split(section)[1:]:
        user_id = _fields(block).get("User ID", "")
        if re.fullmatch(r"[A-Za-z0-9]+", user_id) and user_id not in ids:
            ids.append(user_id)
    return ids


def parse_profile(text):
    """Profile fields from a read_user_profile result ({} if unparseable)."""
    return _fields(_unwrap(text))


def _mask_matches(mask_field, email):
    """True when ``[EMAIL:****.*****@***.***]`` has the real email's shape."""
    match = _MASK_RE.fullmatch(mask_field.strip())
    if not match or match.group(1).count("@") != 1:
        return False
    mask_local, mask_domain = match.group(1).split("@")
    local, domain = email.split("@")

    def shape(value):
        return [len(part) for part in value.split(".")]

    if not re.fullmatch(r"[*.]+", mask_local + mask_domain):
        return False
    return shape(mask_local) == shape(local) and shape(mask_domain) == shape(domain)


def _accept(email, profiles):
    """Person for ``email`` iff exactly one profile passes every check."""
    local = email.split("@")[0]
    passing = []
    for user_id, fields in profiles.items():
        if fields.get("Bot", "").strip().casefold() == "yes":
            continue
        if fields.get("Username", "").strip().casefold() != local:
            continue
        if not _mask_matches(fields.get("Email", ""), email):
            continue
        name = " ".join(
            (fields.get("Real Name") or fields.get("Display Name") or "").split()
        )
        slug = contacts.slugify(name)
        if not name or not slug:
            continue
        passing.append({
            "email": email,
            "name": name,
            "slug": slug,
            "resource_name": f"slack:{user_id}",
        })
    return passing[0] if len(passing) == 1 else None


def _keywords(tool_input):
    raw = (tool_input or {}).get("keywords")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [_email(value) for value in raw if isinstance(value, str)]


class SlackMcpProvider:
    """Resolve people through a claude.ai Slack connector (masked emails)."""

    name = "slack_mcp"

    def __init__(self, cfg, base_dir):
        self.owner_email = _email(cfg["owner_email"])
        self.owner_slug = cfg["owner_slug"]
        self.owner_name = cfg.get("owner_name")
        self.search_tool = cfg["search_tool"]
        self.profile_tool = cfg["profile_tool"]
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.hit_ttl = datetime.timedelta(
            days=cfg.get("hit_ttl_days", DEFAULT_HIT_TTL_DAYS)
        )
        self.miss_ttl = datetime.timedelta(
            days=cfg.get("miss_ttl_days", DEFAULT_MISS_TTL_DAYS)
        )
        self.batch_size = cfg.get("batch_size", DEFAULT_BATCH_SIZE)
        self.base_dir = base_dir
        self.cache_path = (
            Path(base_dir) / cfg.get("cache", DEFAULT_CACHE)
            if base_dir is not None else None
        )
        self._failure = None
        self._run = {}  # email -> person | None, resolved this run
        self._disk = None  # lazily loaded cache entries
        self._warned_corrupt = False

    # -- interface

    def current_user_email(self):
        return self.owner_email

    def clear_cache(self):
        self._failure = None
        self._run = {}
        self._disk = None
        self._warned_corrupt = False

    def prefetch(self, emails):
        wanted = []
        for value in emails:
            email = _email(value)
            if not self._valid(email) or email == self.owner_email:
                continue
            if email not in wanted and self._lookup(email) is _UNKNOWN:
                wanted.append(email)
        if not wanted:
            return
        if self._failure is not None:
            raise RuntimeError(self._failure)
        for i in range(0, len(wanted), self.batch_size):
            self._fetch(wanted[i:i + self.batch_size])

    def resolve_email(self, email):
        email = _email(email)
        if not self._valid(email):
            return None
        if email == self.owner_email:
            return {
                "email": email,
                "name": self.owner_name or self._derived_name(email),
                "slug": self.owner_slug,
                "resource_name": "owner",
            }
        found = self._lookup(email)
        if found is not _UNKNOWN:
            return found
        if self._failure is not None:
            raise RuntimeError(self._failure)
        self._fetch([email])
        return self._run[email]

    # -- internals

    @staticmethod
    def _valid(email):
        return bool(email) and email.count("@") == 1 and all(email.split("@"))

    @staticmethod
    def _derived_name(email):
        words = re.split(r"[._-]+", email.split("@")[0])
        return " ".join(word.capitalize() for word in words if word)

    def _lookup(self, email):
        if email in self._run:
            return self._run[email]
        entry = self._load_disk().get(email)
        if isinstance(entry, dict):
            try:
                checked = datetime.datetime.fromisoformat(entry["checked_at"])
            except (KeyError, TypeError, ValueError):
                checked = None
            person = entry.get("person")
            if checked is not None and (person is None or isinstance(person, dict)):
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=datetime.timezone.utc)
                ttl = self.miss_ttl if person is None else self.hit_ttl
                if _now() - checked < ttl:
                    self._run[email] = person
                    return person
        return _UNKNOWN

    def _load_disk(self):
        if self._disk is not None:
            return self._disk
        entries = {}
        if self.cache_path is not None and self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text())
                if (
                    not isinstance(data, dict)
                    or data.get("version") != CACHE_VERSION
                    or not isinstance(data.get("entries"), dict)
                ):
                    raise ValueError("unexpected cache shape")
                entries = data["entries"]
            except (OSError, ValueError):
                if not self._warned_corrupt:
                    self._warned_corrupt = True
                    log("identity WARN cache unreadable; treating as empty")
        self._disk = entries
        return entries

    def _save_disk(self):
        if self.cache_path is None:
            return
        from . import state  # late import: avoid cycles
        try:
            state.write_atomic(
                self.cache_path,
                json.dumps({"version": CACHE_VERSION, "entries": self._disk},
                           indent=2, sort_keys=True) + "\n",
                mode=0o600,
            )
        except OSError as exc:
            log(f"identity WARN cache write failed: {type(exc).__name__}")

    def _fail(self, message):
        self._failure = message
        raise RuntimeError(message)

    def _prompt(self, locals_):
        queries = "\n".join(f"- {local}" for local in locals_)
        return (
            "Look up Slack users by username. For EACH query below, call the "
            f"{self.search_tool} tool exactly once with keywords set to a "
            "list containing only that query (and a short "
            "natural_language_query). Then, for EVERY user id returned by any "
            f"search, call the {self.profile_tool} tool once with that user_id. "
            "Make no other tool calls and do not write any summary.\n\n"
            f"Queries:\n{queries}"
        )

    def _fetch(self, batch):
        locals_ = list(dict.fromkeys(email.split("@")[0] for email in batch))
        try:
            results = claude_bridge.call_tools(
                self._prompt(locals_),
                {self.search_tool: {}, self.profile_tool: {}},
                model=self.model,
                base_dir=self.base_dir,
                max_turns=2 * len(batch) + 6,
                required=[self.search_tool],
            )
        except claude_bridge.BridgeDryRun:
            self._fail("identity lookup skipped: dry run")
        except claude_bridge.BridgeError as exc:
            self._fail(f"slack identity lookup failed: {exc}")

        ids_by_query = {}
        profiles = {}
        for result in results:
            name, tool_input = result.get("name"), result.get("input")
            if name == self.search_tool:
                ids = parse_search_users(result.get("text"))
                for query in _keywords(tool_input):
                    if query in locals_:
                        known = ids_by_query.setdefault(query, [])
                        known.extend(i for i in ids if i not in known)
            elif name == self.profile_tool:
                user_id = (tool_input or {}).get("user_id")
                if isinstance(user_id, str):
                    profiles[user_id] = parse_profile(result.get("text"))

        stamp = _now().isoformat()
        disk = self._load_disk()
        hits = 0
        for email in batch:
            ids = ids_by_query.get(email.split("@")[0], [])
            person = _accept(
                email, {i: profiles[i] for i in ids if i in profiles}
            )
            hits += person is not None
            self._run[email] = person
            disk[email] = {"person": person, "checked_at": stamp}
        self._save_disk()
        log(f"identity slack_mcp lookup: {len(batch)} email(s), {hits} resolved")


_UNKNOWN = object()


def provider_for(routine):
    """Provider for a routine's ``memory.identity`` block (cached per config)."""
    memory = routine.get("memory") if isinstance(routine, dict) else None
    cfg = memory.get("identity") if isinstance(memory, dict) else None
    if not isinstance(cfg, dict) or cfg.get("provider", "gws") == "gws":
        key = ("gws",)
        if key not in _providers:
            _providers[key] = GwsProvider()
        return _providers[key]
    key = (json.dumps(cfg, sort_keys=True, default=str), str(_base_dir))
    if key not in _providers:
        if _base_dir is None:
            raise RuntimeError("identity provider slack_mcp needs identity.configure(base_dir)")
        _providers[key] = SlackMcpProvider(cfg, _base_dir)
    return _providers[key]


def clear_cache():
    """Forget per-run outcomes and latched failures (runner calls per run)."""
    GwsProvider().clear_cache()
    for provider in _providers.values():
        provider.clear_cache()

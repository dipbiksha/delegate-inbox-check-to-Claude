#!/usr/bin/env python3
"""inbox-check: report new lead replies per client workspace in Email Bison and HeyReach.

Standard library only. See README.md for behaviour, commands and variable names.

Layout: config/env loading -> HTTP client -> BisonClient -> HeyReachClient -> commands -> output formatting.
"""

import sys

if sys.version_info < (3, 9):
    sys.exit("inbox-check needs Python 3.9 or newer.")

import argparse
import concurrent.futures
import datetime as dt
import getpass
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import zoneinfo
except ImportError:  # pragma: no cover - 3.9+ always has it, but tzdata may be missing on Windows
    zoneinfo = None

VERSION = "1.0"

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
CLIENTS_PATH = ROOT / "clients.json"

# Each Email Bison customer has their own instance, so its URL comes from IC_BISON_URL (see configure_bison).
BISON_URL = None
HEYREACH_BASE = "https://api.heyreach.io/api/public"

ADMIN_KEYS = ("BISON_MASTER_KEY", "HEYREACH_ORG_KEY")
ADMIN_COMMANDS = ("setup", "add-client", "issue-keys")
MEMBER_COMMANDS = ("check", "doctor")
COMMANDS = MEMBER_COMMANDS + ADMIN_COMMANDS

PLATFORMS = ("bison", "heyreach")
PLATFORM_LABELS = {"bison": "Bison", "heyreach": "HeyReach"}

SLUG_RE = re.compile(r"^[a-z0-9_]+$")
MEMBER_RE = re.compile(r"^[a-z0-9_-]+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MAX_WORKERS = 4
DEFAULT_WINDOW_HOURS = 24
MONDAY_WINDOW_HOURS = 72  # Monday's check reaches back to Friday, same time
# With IC_SCHEDULE, a run counts from the previous scheduled slot. A run may start up to this many
# minutes late and still count as its own slot, so slots must be at least this far apart.
SCHEDULE_GRACE_MINUTES = 60
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_WINDOW_HOURS = 24 * 31
TIMEOUT = 30
# HeyReach's inbox endpoints took 11-41s per call when measured on 2026-10-05 (even limit=1, one call
# at a time), so they get a longer timeout. Everything else uses TIMEOUT.
HEYREACH_INBOX_TIMEOUT = 120
MAX_TRIES = 3


class ConfigError(Exception):
    """A configuration problem. Exit code 2."""


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

_SECRETS = set()
_SECRETS_LOCK = threading.Lock()

_SECRET_PATTERNS = [
    # Header values, in case a header ever ends up in a message.
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"), r"\1[redacted]"),
    # Bison (Laravel Sanctum) tokens look like "<id>|<secret>".
    (re.compile(r"\b\d+\|[A-Za-z0-9]{6,}"), "[redacted]"),
    # Any long opaque token-like run.
    (re.compile(r"[A-Za-z0-9_\-+/=|]{32,}"), "[redacted]"),
]


def register_secret(value):
    """Remember a secret so scrub() removes it from any text we print."""
    if value and len(value) >= 6:
        with _SECRETS_LOCK:
            _SECRETS.add(value)


def scrub(text):
    """Remove every known secret, and anything shaped like a key, from text."""
    s = str(text)
    with _SECRETS_LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        s = s.replace(secret, "[redacted]")
    for pattern, repl in _SECRET_PATTERNS:
        s = pattern.sub(repl, s)
    return s


# ---------------------------------------------------------------------------
# Config / env loading
# ---------------------------------------------------------------------------

def parse_env_text(text):
    """Parse .env content: KEY=value lines, full-line # comments, optional matching quotes.

    An optional leading "export " is ignored. Inline comments are not supported, so a value
    may contain "#".
    """
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_NAME_RE.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


class Config:
    def __init__(self, values, env_found, env_path):
        self._values = values
        self.env_found = env_found
        self.env_path = env_path

    def get(self, name):
        return self._values.get(name) or None

    def names(self):
        return sorted(self._values)


def load_config(command, environ=None, env_path=None):
    """Load IC_* variables from the real environment (wins) and .env.

    The admin keys (BISON_MASTER_KEY, HEYREACH_ORG_KEY) are only loaded for the admin
    commands. For every other command they are never read into memory.
    """
    environ = os.environ if environ is None else environ
    env_path = ENV_PATH if env_path is None else Path(env_path)
    admin = command in ADMIN_COMMANDS

    def allowed(name):
        return name.startswith("IC_") or (admin and name in ADMIN_KEYS)

    env_found = env_path.is_file()
    file_values = {}
    if env_found:
        try:
            file_values = parse_env_text(env_path.read_text(encoding="utf-8-sig"))
        except OSError as e:
            raise ConfigError(f"Could not read {env_path.name}: {e.strerror}") from None

    values = {k: v for k, v in file_values.items() if allowed(k)}
    values.update({k: v for k, v in environ.items() if allowed(k)})
    values = {k: v.strip() for k, v in values.items() if v and v.strip()}
    for name, value in values.items():
        if name.endswith("_KEY"):
            register_secret(value)
    return Config(values, env_found, env_path)


def guard_cloud(environ=None):
    """check and doctor refuse to run in a Claude cloud session that has the master key."""
    environ = os.environ if environ is None else environ
    if environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() == "true" and environ.get("BISON_MASTER_KEY", "").strip():
        raise ConfigError("Master key must not be in a cloud environment.")


def normalize_bison_url(value):
    """'https://mail.example.com/anything' -> 'https://mail.example.com'. HTTPS only: keys travel in headers."""
    raw = (value or "").strip().strip("'\"")
    if raw and "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or "@" in parsed.netloc:
        raise ConfigError(f"IC_BISON_URL must be your Email Bison address, like https://mail.example.com (got: {raw or 'nothing'}).")
    return f"https://{parsed.netloc.lower()}"


def configure_bison(cfg, required=True):
    """Set the Bison instance URL from IC_BISON_URL. Returns it, or None when not required and unset."""
    global BISON_URL
    value = cfg.get("IC_BISON_URL")
    if not value:
        if required:
            raise ConfigError("IC_BISON_URL is not set. Add your Email Bison address to .env, e.g. IC_BISON_URL=https://mail.example.com")
        return None
    BISON_URL = normalize_bison_url(value)
    return BISON_URL


def bison_url():
    if not BISON_URL:
        raise ConfigError("IC_BISON_URL is not set.")
    return BISON_URL


def key_var(slug, platform):
    """Client slug + platform -> env variable name, e.g. ("acme", "bison") -> IC_ACME_BISON_KEY."""
    return f"IC_{slug.upper()}_{platform.upper()}_KEY"


def _quote_env_value(value):
    if re.search(r"[\s#'\"]", value) and '"' not in value:
        return f'"{value}"'
    return value


def write_env_var(name, value, env_path=None):
    """Set NAME=value in .env, replacing any existing line. File mode 0600 on POSIX."""
    env_path = ENV_PATH if env_path is None else Path(env_path)
    lines = env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.is_file() else []
    new_line = f"{name}={_quote_env_value(value)}"
    out, replaced = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" in stripped and stripped.split("=", 1)[0].strip() == name and not stripped.startswith("#"):
            if not replaced:
                out.append(new_line)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    if os.name == "posix":
        os.chmod(str(env_path), 0o600)


# --- clients.json ----------------------------------------------------------

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_clients(data):
    """Validate parsed clients.json content and return the clients list."""
    if not isinstance(data, dict) or not isinstance(data.get("clients"), list):
        raise ConfigError('clients.json must be an object with a "clients" list.')
    slugs, workspace_ids = set(), {}
    for i, client in enumerate(data["clients"], 1):
        where = f"clients.json: client #{i}"
        if not isinstance(client, dict):
            raise ConfigError(f"{where} must be an object.")
        slug = client.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise ConfigError(f'{where}: "slug" must use only lowercase letters, digits and _.')
        where = f'clients.json: client "{slug}"'
        if slug in slugs:
            raise ConfigError(f"{where} appears more than once.")
        slugs.add(slug)
        name = client.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f'{where}: "name" must be a non-empty string.')
        bison = client.get("bison")
        if bison is not None:
            if not isinstance(bison, dict) or not _is_int(bison.get("workspace_id")) or bison["workspace_id"] <= 0:
                raise ConfigError(f'{where}: "bison" must be null or {{"workspace_id": <positive integer>}}.')
            ws = bison["workspace_id"]
            if ws in workspace_ids:
                raise ConfigError(f'{where}: Bison workspace {ws} is already used by "{workspace_ids[ws]}".')
            workspace_ids[ws] = slug
        heyreach = client.get("heyreach")
        if heyreach is not None:
            if not isinstance(heyreach, dict) or not isinstance(heyreach.get("enabled"), bool):
                raise ConfigError(f'{where}: "heyreach" must be null or {{"enabled": true|false}}.')
            hr_ws = heyreach.get("workspace_id")
            if hr_ws is not None and (not _is_int(hr_ws) or hr_ws <= 0):
                raise ConfigError(f'{where}: "heyreach.workspace_id" must be a positive integer if present.')
    return data["clients"]


def load_clients(path=None, missing_ok=False):
    path = CLIENTS_PATH if path is None else Path(path)
    if not path.is_file():
        if missing_ok:
            return []
        raise ConfigError(f"{path.name} not found. The admin creates it with `setup`.")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON (line {e.lineno}: {e.msg}).") from None
    except OSError as e:
        raise ConfigError(f"Could not read {path.name}: {e.strerror}") from None
    return validate_clients(data)


_KEY_VAR_RE = re.compile(r"^IC_([A-Z0-9_]+?)_(BISON|HEYREACH)_KEY$")


def clients_from_env(cfg):
    """Build the client list from IC_<SLUG>_<PLATFORM>_KEY variables, for when there is no clients.json
    (e.g. a cloud routine, where only environment variables exist). The display name comes from
    IC_<SLUG>_NAME if set. Bison workspace IDs are unknown here, so doctor can't cross-check them.
    """
    found = {}
    for name in cfg.names():
        m = _KEY_VAR_RE.match(name)
        if m:
            found.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
    clients = []
    for slug in sorted(found):
        if not SLUG_RE.match(slug):
            continue
        display = (cfg.get(f"IC_{slug.upper()}_NAME") or "").strip().strip("'\"") or slug
        clients.append({
            "slug": slug,
            "name": display,
            "bison": {} if "bison" in found[slug] else None,
            "heyreach": {"enabled": True} if "heyreach" in found[slug] else None,
        })
    return clients


def member_clients(cfg):
    """Clients for check/doctor: clients.json if present, else derived from IC_ keys. Returns (clients, source)."""
    if CLIENTS_PATH.is_file():
        return load_clients(), CLIENTS_PATH.name
    return clients_from_env(cfg), "IC_ keys (no clients.json)"


def save_clients(clients, path=None):
    path = CLIENTS_PATH if path is None else Path(path)
    validate_clients({"clients": clients})
    path.write_text(json.dumps({"clients": clients}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def uses(client, platform):
    """Does this client use the platform according to clients.json?"""
    block = client.get(platform)
    if platform == "heyreach":
        return isinstance(block, dict) and block.get("enabled") is True
    return isinstance(block, dict)


def select_clients(clients, targets):
    """Pick clients by slug or Bison workspace ID, in the order given."""
    selected = []
    for target in targets:
        t = target.strip().lower()
        match = None
        for client in clients:
            ws = (client.get("bison") or {}).get("workspace_id")
            if client["slug"] == t or (ws is not None and str(ws) == t):
                match = client
                break
        if match is None:
            raise ConfigError(f"Unknown client: {target} (not a slug or Bison workspace ID in clients.json).")
        if match not in selected:
            selected.append(match)
    return selected


def suggest_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "client"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

STATUS_REASONS = {
    400: "bad request",
    401: "key rejected",
    403: "access denied",
    404: "not found",
    405: "method not allowed",
    422: "request rejected",
    429: "rate limited",
}


class HttpError(Exception):
    def __init__(self, reason, status=None):
        self.reason = scrub(reason)
        self.status = status
        super().__init__(self.label())

    def label(self):
        """Short form used in JSON: 'key rejected (401)'."""
        return f"{self.reason} ({self.status})" if self.status else self.reason

    def text_label(self):
        """Form used in the text report: 'key rejected — 401'."""
        return f"{self.reason} — {self.status}" if self.status else self.reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send our auth headers to the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _server_message(body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        for field in ("message", "errorMessage", "detail", "title"):
            if isinstance(inner.get(field), str) and inner[field].strip():
                return inner[field].strip()[:160]
    return ""


class Http:
    """Small JSON-over-HTTPS client: 30s timeout, retries 429/5xx, keys only in headers."""

    def __init__(self, base_url, headers, timeout=TIMEOUT, max_tries=MAX_TRIES, urlopen=None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        for value in self.headers.values():
            register_secret(value.split(" ", 1)[-1])
        self.timeout = timeout
        self.max_tries = max_tries
        self._urlopen = urlopen or _OPENER.open
        self._sleep = sleep

    @staticmethod
    def _delay(attempt, retry_after):
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        return float(2 ** (attempt - 1))

    def request(self, method, path, params=None, body=None, timeout=None):
        timeout = timeout or self.timeout
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": f"inbox-check/{VERSION}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(self.headers)

        for attempt in range(1, self.max_tries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as e:
                status = e.code
                retry_after = e.headers.get("Retry-After") if e.headers is not None else None
                try:
                    err_body = e.read()
                except Exception:
                    err_body = b""
                finally:
                    e.close()
                if (status == 429 or 500 <= status < 600) and attempt < self.max_tries:
                    self._sleep(self._delay(attempt, retry_after))
                    continue
                reason = STATUS_REASONS.get(status) or ("server error" if status >= 500 else "HTTP error")
                if 300 <= status < 400:
                    reason = "unexpected redirect"
                if status in (400, 422):
                    detail = _server_message(err_body)
                    if detail:
                        reason = f"{reason}: {detail}"
                raise HttpError(reason, status) from None
            except (socket.timeout, TimeoutError):
                raise HttpError(f"timed out after {timeout}s") from None
            except urllib.error.URLError as e:
                if isinstance(e.reason, (socket.timeout, TimeoutError)):
                    raise HttpError(f"timed out after {timeout}s") from None
                raise HttpError(f"network error: {e.reason}") from None
            except OSError as e:
                raise HttpError(f"network error: {e.__class__.__name__}") from None

            if not raw or not raw.strip():
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise HttpError("unexpected response (not JSON)") from None
        raise HttpError("retries exhausted")  # pragma: no cover - loop always returns or raises


def probe_host(url, timeout=10, urlopen=None):
    """Is the host reachable at all? Any HTTP response counts. Returns (ok, detail)."""
    urlopen = urlopen or _OPENER.open
    req = urllib.request.Request(url, headers={"User-Agent": f"inbox-check/{VERSION}"}, method="GET")
    try:
        with urlopen(req, timeout=timeout):
            return True, "reachable"
    except urllib.error.HTTPError:
        return True, "reachable"
    except (socket.timeout, TimeoutError):
        return False, f"timed out after {timeout}s"
    except urllib.error.URLError as e:
        return False, scrub(f"unreachable: {e.reason}")
    except OSError as e:
        return False, f"unreachable: {e.__class__.__name__}"


def _dict(value):
    return value if isinstance(value, dict) else {}


def _shape(value, depth=0):
    """Describe a JSON value by its field names only (never values), for error messages."""
    if isinstance(value, dict):
        if depth >= 2:
            return "{…}"
        return "{" + ", ".join(f"{k}{_shape(v, depth + 1) if isinstance(v, (dict, list)) else ''}" for k in list(value)[:15] for v in [value[k]]) + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return type(value).__name__


def _list(value):
    return value if isinstance(value, list) else []


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$")


def _at_or_after(when, since):
    return when is not None and when >= since


def parse_ts(value):
    """Parse an API timestamp into an aware datetime (naive = UTC). None if missing or unparseable.

    Handles 'Z', any number of fractional digits (Bison sends 6, .NET can send 7) and +hh:mm, which
    datetime.fromisoformat doesn't on Python 3.9/3.10.
    """
    if not isinstance(value, str):
        return None
    m = _TS_RE.match(value.strip())
    if not m:
        return None
    date, hh, mm, ss, frac, zone = m.groups()
    try:
        y, mo, d = (int(x) for x in date.split("-"))
        micro = int((frac or "0")[:6].ljust(6, "0"))
        tz = dt.timezone.utc
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            zh, zm = int(zone[1:3]), int(zone[-2:])
            tz = dt.timezone(sign * dt.timedelta(hours=zh, minutes=zm))
        return dt.datetime(y, mo, d, int(hh), int(mm), int(ss or 0), micro, tzinfo=tz)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Email Bison
# ---------------------------------------------------------------------------

class BisonClient:
    """Email Bison API client.

    Verified 2026-10-05 against the instance's own OpenAPI spec
    (<IC_BISON_URL>/api/reference.openapi) and https://docs.emailbison.com:

    1. `read` filter on GET /api/replies: documented on this route (boolean), next to folder/status.
       Laravel's boolean validation accepts 1/0 but not the string "false", so we send read=0.
       Guard: if page 1 still contains a reply with read=true (or outside the inbox, or automated),
       the filter is treated as ignored and we count client-side by walking cursor pages.
       Confirmed live 2026-10-05 on three workspaces: read=0 total + read=1 total == unfiltered total.
    2. Pagination: length-aware by default, 15 per page, no documented per_page parameter. The response
       carries meta.total, so one request gives the count. If meta.total is missing, we walk cursor pages
       (pagination_type=cursor, meta.next_cursor) and count.
    3. Token creation: POST /api/workspaces/v1.1/{id}/api-tokens with {"name": ...} returns the plaintext
       token in data.plain_text_token ("<id>|<secret>"). Bison only keeps token_hash, so the plaintext
       is returned once, on creation.
    4. Workspace list: GET /api/workspaces/v1.1 (BUILD.md's /api/v1.1/workspaces returns 404 on this
       instance). Response is {"data": [{"id", "name", ...}]}, not paginated in the spec; links.next is
       followed defensively. Entries also hold webhooks_secret_key, which we never read or print.
    5. Key validation: GET /api/users (read-only, cheap). The spec documents data.team.{id, name}, but on
       2026-10-05 a fresh api-user key on a live instance answered 200 without it. So a 200 counts as a valid
       key; the workspace is cross-checked only when the response includes one.

    Never call any switch-workspace endpoint from here.
    """

    UNREAD_PARAMS = {"folder": "inbox", "status": "not_automated_reply", "read": "0"}
    MAX_PAGES = 400

    def __init__(self, key, http=None):
        register_secret(key)
        self.http = http or Http(bison_url(), {"Authorization": f"Bearer {key}"})

    def list_workspaces(self):
        workspaces, page = [], 1
        while True:
            resp = _dict(self.http.request("GET", "/api/workspaces/v1.1", params={"page": page} if page > 1 else None))
            data = resp.get("data")
            if isinstance(data, dict):  # tolerate {"data": {"0": {...}}} shapes
                data = list(data.values())
            for ws in _list(data):
                if isinstance(ws, dict) and _is_int(ws.get("id")):
                    workspaces.append({"id": ws["id"], "name": str(ws.get("name") or f"Workspace {ws['id']}")})
            if not _dict(resp.get("links")).get("next") or page >= 50:
                return workspaces
            page += 1

    def create_api_token(self, workspace_id, name):
        resp = _dict(self.http.request("POST", f"/api/workspaces/v1.1/{int(workspace_id)}/api-tokens", body={"name": name}))
        token = _dict(resp.get("data")).get("plain_text_token")
        if not isinstance(token, str) or not token:
            raise HttpError("token created but no plain_text_token in the response")
        register_secret(token)
        return token

    def current_workspace(self):
        """Confirm the key works. Returns {"id", "name"}; id is None if Bison doesn't say which workspace.

        A 200 here already proves the key is valid. The spec documents data.team, but api-user keys
        may answer without it, so a missing workspace is not treated as a failure.
        """
        resp = self.http.request("GET", "/api/users")
        data = _dict(_dict(resp).get("data"))
        if not data:
            raise HttpError(f"unexpected response from /api/users (fields: {_shape(resp)})")
        team = _dict(data.get("team")) or _dict(data.get("current_team")) or _dict(data.get("workspace"))
        ws_id = team.get("id")
        if not _is_int(ws_id):
            ws_id = next((data[f] for f in ("current_team_id", "team_id", "workspace_id") if _is_int(data.get(f))), None)
        return {"id": ws_id, "name": str(team.get("name") or "")}

    @staticmethod
    def _is_unread(reply):
        if not isinstance(reply, dict):
            return False
        folder = str(reply.get("folder") or "inbox").lower()
        return reply.get("read") is not True and reply.get("automated_reply") is not True and folder == "inbox"

    def count_unread(self):
        resp = _dict(self.http.request("GET", "/api/replies", params=self.UNREAD_PARAMS))
        replies = _list(resp.get("data"))
        total = _dict(resp.get("meta")).get("total")
        if _is_int(total) and all(self._is_unread(r) for r in replies):
            return total
        return self._count_client_side()

    NEW_PARAMS = {"folder": "inbox", "status": "not_automated_reply", "pagination_type": "cursor"}

    def count_new(self, since):
        """Human inbox replies received at or after `since`, read or not.

        /api/replies has no date filter, but cursor pages come newest first (checked live 2026-10-05:
        sorted by date_received and id, descending). We stop after the first page with nothing in the
        window, which tolerates a slightly out-of-order reply at a page boundary.
        """
        params = dict(self.NEW_PARAMS)
        count = 0
        for _ in range(self.MAX_PAGES):
            resp = _dict(self.http.request("GET", "/api/replies", params=params))
            in_window = 0
            for reply in _list(resp.get("data")):
                received = parse_ts(_dict(reply).get("date_received") or _dict(reply).get("created_at"))
                if received is None or received < since:
                    continue
                in_window += 1
                if reply.get("automated_reply") is not True and str(reply.get("folder") or "inbox").lower() == "inbox":
                    count += 1
            cursor = _dict(resp.get("meta")).get("next_cursor")
            if not in_window or not cursor:
                return count
            params["cursor"] = cursor
        raise HttpError("too many replies to count")

    def _count_client_side(self):
        params = dict(self.UNREAD_PARAMS, pagination_type="cursor")
        count = 0
        for _ in range(self.MAX_PAGES):
            resp = _dict(self.http.request("GET", "/api/replies", params=params))
            count += sum(1 for r in _list(resp.get("data")) if self._is_unread(r))
            cursor = _dict(resp.get("meta")).get("next_cursor")
            if not cursor:
                return count
            params["cursor"] = cursor
        raise HttpError("too many replies to count client-side")


# ---------------------------------------------------------------------------
# HeyReach
# ---------------------------------------------------------------------------

class HeyReachClient:
    """HeyReach public API client (per-workspace API key, X-API-KEY header).

    Verified 2026-10-05 against https://documenter.getpostman.com/view/23808049/2sA2xb5F75:

    6. Conversations: POST /inbox/GetConversationsV2 is offset-paged; its documented successor
       POST /inbox/GetConversationsV3 is cursor-paged, and its docs state totalCount is "Total matching
       conversations across all pages". Both take filters.seen (false = unread, null = both). We call V3
       with limit=1 and read totalCount. Guard: if totalCount is missing or the returned conversation has
       read=true, we walk pages and count read != true. If V3 returns 404, we fall back to V2
       (offset/limit body, same filter, same totalCount).
       Confirmed live 2026-10-05: seen=false is honoured and totalCount respects it (2 of 967 for one
       workspace); V2 and V3 return identical counts. These calls are slow (11-41s), hence
       HEYREACH_INBOX_TIMEOUT.
    7. Key check: GET /auth/CheckApiKey returns 200 for a valid key and 401 otherwise. It does not say
       which workspace the key belongs to.
    """

    MAX_PAGES = 100

    def __init__(self, key, http=None):
        register_secret(key)
        self.http = http or Http(HEYREACH_BASE, {"X-API-KEY": key})

    def check_key(self):
        self.http.request("GET", "auth/CheckApiKey")
        return True

    @staticmethod
    def _filters(seen=False):
        return {"linkedInAccountIds": [], "campaignIds": [], "tags": [], "seen": seen}

    def _inbox(self, path, body):
        return self.http.request("POST", path, body=body, timeout=HEYREACH_INBOX_TIMEOUT)

    def count_unseen(self):
        try:
            resp = _dict(self._inbox("inbox/GetConversationsV3", {"limit": 1, "cursor": None, "filters": self._filters()}))
        except HttpError as e:
            if e.status == 404:
                return self._count_v2()
            raise
        total = resp.get("totalCount")
        if _is_int(total) and all(_dict(c).get("read") is not True for c in _list(resp.get("items"))):
            return total
        return self._walk_v3()

    def count_new(self, since):
        """Conversations where the lead sent a message at or after `since`.

        Returns (new, awaiting_reply); awaiting_reply counts the new conversations whose last message is
        still the lead's. V3's `from` bounds lastMessageAt, which is usually our own outreach (checked
        live 2026-10-05), so each conversation's messages are checked for one from the lead
        (sender != "ME") inside the window.
        """
        since_utc = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"limit": 100, "cursor": None, "from": since_utc, "filters": self._filters(seen=None)}
        new = awaiting = 0
        for _ in range(self.MAX_PAGES):
            resp = _dict(self._inbox("inbox/GetConversationsV3", body))
            for conv in _list(resp.get("items")):
                conv = _dict(conv)
                last_from_lead = str(conv.get("lastMessageSender") or "ME").upper() != "ME"
                messages = _list(conv.get("messages"))
                if messages:
                    from_lead = any(
                        str(_dict(m).get("sender") or "ME").upper() != "ME"
                        and _at_or_after(parse_ts(_dict(m).get("createdAt")), since)
                        for m in messages
                    )
                else:
                    from_lead = last_from_lead and _at_or_after(parse_ts(conv.get("lastMessageAt")), since)
                if from_lead:
                    new += 1
                    if last_from_lead:
                        awaiting += 1
            body["cursor"] = resp.get("nextCursor")
            if not body["cursor"] or resp.get("hasNextPage") is False:
                return new, awaiting
        raise HttpError("too many conversations to count")

    def _walk_v3(self):
        count, cursor = 0, None
        for _ in range(self.MAX_PAGES):
            resp = _dict(self._inbox("inbox/GetConversationsV3", {"limit": 100, "cursor": cursor, "filters": self._filters()}))
            count += sum(1 for c in _list(resp.get("items")) if _dict(c).get("read") is not True)
            cursor = resp.get("nextCursor")
            if not cursor or resp.get("hasNextPage") is False:
                return count
        raise HttpError("too many conversations to count")

    def _count_v2(self):
        resp = _dict(self._inbox("inbox/GetConversationsV2", {"offset": 0, "limit": 1, "filters": self._filters()}))
        total = resp.get("totalCount")
        if not _is_int(total):
            raise HttpError("unexpected response from GetConversationsV2")
        return total


class HeyReachOrgClient:
    """HeyReach Organization API (Organization API key, same X-API-KEY header, own 300/min limit).

    8. Verified 2026-10-05 in the same Postman collection:
       - GET  /management/organizations/workspaces?Offset=&Limit= -> {totalCount, items: [{workspaceId, workspaceName, ...}]}
       - GET  /management/organizations/api-keys/workspaces/{id}  -> {apiKeys: {publicApi, n8N, make, zapier, mcp}}
       - POST /management/organizations/api-keys/workspaces/{id}  {"apiKeyType": "PUBLIC"} -> {key, previousKeyStatus}
         Creating a key DEACTIVATES the workspace's existing key of that type ("previousKeyStatus":
         "Deactivated"), so issue-keys only creates one when the workspace has none. HeyReach public keys
       are per workspace, not per person.
    """

    def __init__(self, key, http=None):
        register_secret(key)
        self.http = http or Http(HEYREACH_BASE, {"X-API-KEY": key})

    def list_workspaces(self):
        workspaces, offset = [], 0
        while True:
            resp = _dict(self.http.request("GET", "management/organizations/workspaces", params={"Offset": offset, "Limit": 100}))
            items = _list(resp.get("items"))
            for ws in items:
                if isinstance(ws, dict) and _is_int(ws.get("workspaceId")):
                    workspaces.append({"id": ws["workspaceId"], "name": str(ws.get("workspaceName") or f"Workspace {ws['workspaceId']}")})
            offset += len(items)
            total = resp.get("totalCount")
            if not items or not _is_int(total) or offset >= total or offset >= 5000:
                return workspaces

    def get_public_key(self, workspace_id):
        resp = _dict(self.http.request("GET", f"management/organizations/api-keys/workspaces/{int(workspace_id)}"))
        key = _dict(resp.get("apiKeys")).get("publicApi")
        if isinstance(key, str) and key.strip():
            register_secret(key.strip())
            return key.strip()
        return None

    def create_public_key(self, workspace_id):
        resp = _dict(self.http.request("POST", f"management/organizations/api-keys/workspaces/{int(workspace_id)}", body={"apiKeyType": "PUBLIC"}))
        key = resp.get("key")
        if not isinstance(key, str) or not key.strip():
            raise HttpError("key created but missing from the response")
        register_secret(key.strip())
        return key.strip()


# ---------------------------------------------------------------------------
# Interactive helpers (admin commands). Prompts go to stderr so stdout stays clean.
# ---------------------------------------------------------------------------

def info(msg=""):
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def ask(msg, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    sys.stderr.write(f"{msg}{suffix}: ")
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise ConfigError("No input received (this command is interactive).")
    line = line.strip()
    return line if line else ("" if default is None else str(default))


def confirm(msg, default=False):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{msg} ({hint})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_secret(msg):
    value = getpass.getpass(f"{msg}: ").strip()
    register_secret(value)
    return value


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _master_key(cfg):
    key = cfg.get("BISON_MASTER_KEY")
    prompted = False
    if not key:
        key = ask_secret("Bison master (super-admin) key")
        prompted = True
        if not key:
            raise ConfigError("A Bison master key is required for this command.")
    return key, prompted


def _offer_to_save_master_key(cfg, key):
    if confirm(f"Save the master key to {cfg.env_path.name} (file mode 600) for next time?", default=False):
        write_env_var("BISON_MASTER_KEY", key, cfg.env_path)
        info(f"Saved to {cfg.env_path.name}. It is git-ignored; never copy it anywhere else.")


def _heyreach_org(cfg):
    """Return (org_client, workspaces) if HEYREACH_ORG_KEY is set and works, else (None, [])."""
    key = cfg.get("HEYREACH_ORG_KEY")
    if not key:
        return None, []
    org = HeyReachOrgClient(key)
    try:
        return org, org.list_workspaces()
    except HttpError as e:
        info(f"HeyReach Organization API unavailable ({e.label()}); you will paste HeyReach keys instead.")
        return None, []


def _pick_heyreach_workspace(hr_workspaces, client_name):
    if not hr_workspaces:
        return None
    suggestion = ""
    for i, ws in enumerate(hr_workspaces, 1):
        if ws["name"].casefold() == client_name.casefold():
            suggestion = str(i)
    if not suggestion:
        for i, ws in enumerate(hr_workspaces, 1):
            if client_name.casefold() in ws["name"].casefold() or ws["name"].casefold() in client_name.casefold():
                suggestion = str(i)
                break
    info("  HeyReach workspaces:")
    for i, ws in enumerate(hr_workspaces, 1):
        info(f"    {i:>3}. {ws['name']} (ID {ws['id']})")
    while True:
        choice = ask("  Which HeyReach workspace is this client? Number, or blank to skip", suggestion)
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(hr_workspaces):
            return hr_workspaces[int(choice) - 1]["id"]
        info("  Please enter a number from the list.")


def _ask_client_details(workspace, existing, hr_workspaces):
    name = ask("  Client display name", workspace["name"]).strip()
    taken = {c["slug"] for c in existing}
    while True:
        slug = ask("  Slug", suggest_slug(name)).strip().lower()
        if not SLUG_RE.match(slug):
            info("  A slug may only use lowercase letters, digits and _.")
        elif slug in taken:
            info(f'  The slug "{slug}" is already used.')
        else:
            break
    heyreach = None
    if confirm("  Does this client use HeyReach?", default=False):
        heyreach = {"enabled": True}
        hr_ws = _pick_heyreach_workspace(hr_workspaces, name)
        if hr_ws:
            heyreach["workspace_id"] = hr_ws
    return {"slug": slug, "name": name, "bison": {"workspace_id": workspace["id"]}, "heyreach": heyreach}


def _admin_bison_url(cfg):
    """IC_BISON_URL from env/.env, or ask for it and offer to save it."""
    if cfg.get("IC_BISON_URL"):
        return configure_bison(cfg)
    global BISON_URL
    BISON_URL = normalize_bison_url(ask("Your Email Bison address (e.g. https://mail.example.com)"))
    if confirm(f"Save it to {cfg.env_path.name} as IC_BISON_URL?", default=True):
        write_env_var("IC_BISON_URL", BISON_URL, cfg.env_path)
    return BISON_URL


def cmd_setup(args):
    cfg = load_config("setup")
    _admin_bison_url(cfg)
    key, prompted = _master_key(cfg)
    workspaces = BisonClient(key).list_workspaces()
    if prompted:
        _offer_to_save_master_key(cfg, key)
    existing = load_clients(missing_ok=True)
    if existing and not confirm(f"{CLIENTS_PATH.name} already lists {len(existing)} client(s). Start over and replace it?", default=False):
        info("Nothing changed. Use `add-client` to add one workspace.")
        return 0
    if not workspaces:
        info("The master key can't see any Bison workspaces.")
        return 1
    _, hr_workspaces = _heyreach_org(cfg)
    info(f"\nFound {len(workspaces)} Bison workspace(s).")
    clients = []
    for ws in workspaces:
        info(f"\nWorkspace {ws['id']}: {ws['name']}")
        if confirm("  Include it?", default=False):
            clients.append(_ask_client_details(ws, clients, hr_workspaces))
    save_clients(clients)
    info(f"\nWrote {CLIENTS_PATH.name} with {len(clients)} client(s). It stays on this computer (git-ignored).")
    return 0


def cmd_add_client(args):
    cfg = load_config("add-client")
    _admin_bison_url(cfg)
    key, prompted = _master_key(cfg)
    clients = load_clients(missing_ok=True)
    workspaces = BisonClient(key).list_workspaces()
    if prompted:
        _offer_to_save_master_key(cfg, key)
    registered = {c["bison"]["workspace_id"]: c["slug"] for c in clients if c.get("bison")}
    available = [ws for ws in workspaces if ws["id"] not in registered]
    if not available:
        info("Every Bison workspace is already in clients.json.")
        return 0
    info("Bison workspaces not yet in clients.json:")
    for i, ws in enumerate(available, 1):
        info(f"  {i:>3}. {ws['name']} (ID {ws['id']})")
    while True:
        choice = ask("Pick a workspace by number, or type its workspace ID")
        picked = None
        if choice.isdigit():
            n = int(choice)
            if n in registered:
                raise ConfigError(f'Workspace {n} is already in clients.json as "{registered[n]}".')
            picked = next((ws for ws in available if ws["id"] == n), None)
            if picked is None and 1 <= n <= len(available):
                picked = available[n - 1]
        if picked:
            break
        info("Not in the list; try again.")
    _, hr_workspaces = _heyreach_org(cfg)
    info(f"\nWorkspace {picked['id']}: {picked['name']}")
    client = _ask_client_details(picked, clients, hr_workspaces)
    clients.append(client)
    save_clients(clients)
    info(f'\nAdded "{client["slug"]}" to {CLIENTS_PATH.name}. Next: `issue-keys {client["slug"]} --for <member>`.')
    return 0


def _paste_heyreach_key(client):
    for _ in range(3):
        key = ask_secret(f"  Paste {client['name']}'s HeyReach API key (blank to skip)")
        if not key:
            return None
        try:
            HeyReachClient(key).check_key()
            info("  HeyReach: key verified.")
            return key
        except HttpError as e:
            info(f"  That key didn't work ({e.label()}).")
    return None


def _heyreach_key_for(client, org, hr_workspaces, clients_state):
    """Get a validated HeyReach key for a client: from the Organization API if possible, else pasted."""
    if org is not None:
        block = client["heyreach"]
        ws_id = block.get("workspace_id")
        if not ws_id:
            ws_id = _pick_heyreach_workspace(hr_workspaces, client["name"])
            if ws_id:
                block["workspace_id"] = ws_id
                clients_state["changed"] = True
        if ws_id:
            try:
                key = org.get_public_key(ws_id)
                if key:
                    HeyReachClient(key).check_key()
                    info("  HeyReach: using the workspace's existing API key (HeyReach keys are shared per workspace).")
                    return key
                if confirm("  This HeyReach workspace has no public API key. Create one?", default=True):
                    key = org.create_public_key(ws_id)
                    HeyReachClient(key).check_key()
                    info("  HeyReach: new workspace API key created and verified.")
                    return key
            except HttpError as e:
                info(f"  HeyReach Organization API failed ({e.label()}); paste the key instead.")
    return _paste_heyreach_key(client)


def cmd_issue_keys(args):
    member = args.member.strip()
    if not MEMBER_RE.match(member):
        raise ConfigError("--for must use only lowercase letters, digits, _ and -.")
    cfg = load_config("issue-keys")
    clients = load_clients()
    selected = select_clients(clients, args.slugs)

    bison_master = None
    if any(uses(c, "bison") for c in selected):
        _admin_bison_url(cfg)
        key, _ = _master_key(cfg)
        bison_master = BisonClient(key)
    org, hr_workspaces = (None, [])
    if any(uses(c, "heyreach") for c in selected):
        org, hr_workspaces = _heyreach_org(cfg)

    issued_on = dt.date.today().isoformat()
    token_name = f"inbox-check-{member}-{issued_on}"
    lines, failures = [], []
    clients_state = {"changed": False}

    for client in selected:
        info(f"\n{client['name']} ({client['slug']})")
        if uses(client, "bison"):
            ws_id = client["bison"]["workspace_id"]
            token = None
            try:
                token = bison_master.create_api_token(ws_id, token_name)
                scoped = BisonClient(token).current_workspace()
                if scoped["id"] is not None and scoped["id"] != ws_id:
                    raise HttpError(f"new key is scoped to workspace {scoped['id']}, not {ws_id}; delete \"{token_name}\" in Bison")
                lines.append(f"{key_var(client['slug'], 'bison')}={token}")
                if scoped["id"] is None:
                    info(f'  Bison: key "{token_name}" created and works (Bison did not report its workspace).')
                else:
                    info(f'  Bison: key "{token_name}" created and verified.')
            except HttpError as e:
                failures.append(f"{client['slug']} Bison: {e.label()}")
                info(f"  Bison: FAILED ({e.label()})")
                if token and "delete" not in e.reason:
                    info(f'  The key "{token_name}" was created but not issued. Delete it in Bison (Settings → Developer API).')
        if uses(client, "heyreach"):
            key = _heyreach_key_for(client, org, hr_workspaces, clients_state)
            if key:
                lines.append(f"{key_var(client['slug'], 'heyreach')}={key}")
            else:
                failures.append(f"{client['slug']} HeyReach: no working key")
                info("  HeyReach: skipped (no working key).")

    if clients_state["changed"]:
        save_clients(clients)
        info(f"\nSaved HeyReach workspace IDs to {CLIENTS_PATH.name}.")

    if lines:
        info("\nSend this block to the team member through the password manager. It is not saved anywhere.\n")
        print(f"# inbox-check keys for {member}, issued {issued_on}")
        if bison_master is not None:
            print(f"IC_BISON_URL={bison_url()}")
        issued = {line.split("=", 1)[0] for line in lines}
        for client in selected:
            if any(key_var(client["slug"], p) in issued for p in PLATFORMS):
                print(f"IC_{client['slug'].upper()}_NAME={client['name']}")
        for line in lines:
            print(line)
        sys.stdout.flush()
    if failures:
        info("\nNot issued:")
        for failure in failures:
            info(f"  {failure}")
        return 1
    return 0


# --- check -----------------------------------------------------------------

def _check_platform(client, platform, cfg, mode="new", since=None):
    """Returns {"state": "ok", "label": "new"|"unread", "count", "awaiting"} or a not_set_up/no_key/error state."""
    if not uses(client, platform):
        return {"state": "not_set_up"}
    key = cfg.get(key_var(client["slug"], platform))
    if not key:
        return {"state": "no_key"}
    try:
        awaiting = None
        if mode == "unread":
            count = BisonClient(key).count_unread() if platform == "bison" else HeyReachClient(key).count_unseen()
        elif platform == "bison":
            count = BisonClient(key).count_new(since)
        else:
            count, awaiting = HeyReachClient(key).count_new(since)
        return {"state": "ok", "label": mode, "count": int(count), "awaiting": awaiting}
    except HttpError as e:
        return {"state": "error", "error": e}
    except Exception as e:  # keep going; one client's surprise must not stop the report
        return {"state": "error", "error": HttpError(f"unexpected error: {e.__class__.__name__}")}


def run_checks(clients, cfg, mode="new", since=None):
    results = {c["slug"]: {} for c in clients}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_check_platform, c, p, cfg, mode, since): (c["slug"], p) for c in clients for p in PLATFORMS}
        for future in concurrent.futures.as_completed(futures):
            slug, platform = futures[future]
            results[slug][platform] = future.result()
    return results


def has_any_key(client, cfg):
    return any(uses(client, p) and cfg.get(key_var(client["slug"], p)) for p in PLATFORMS)


def resolve_timezone(name):
    """Returns (tzinfo, label). Falls back to UTC if the zone is unknown."""
    if name and zoneinfo is not None:
        try:
            return zoneinfo.ZoneInfo(name), name
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            info(f"IC_TIMEZONE={name} is not a known time zone; using UTC.")
    elif name:
        info("Time zones are unavailable on this Python; using UTC.")
    return dt.timezone.utc, "UTC"


def window_start(now, hours=None):
    """Start of the "new" window: 24h back, or back to Friday (72h) when it's Monday in `now`'s zone."""
    if hours is None:
        hours = MONDAY_WINDOW_HOURS if now.weekday() == 0 else DEFAULT_WINDOW_HOURS
    return now - dt.timedelta(hours=hours), hours


def parse_schedule(times, days=None):
    """IC_SCHEDULE="12:00,15:00,19:00" and IC_SCHEDULE_DAYS="mon-fri" -> (sorted [(h, m)], {weekday ints}).

    Days are comma-separated names or ranges (mon..sun; a range may wrap, e.g. sun-thu). Default mon-fri.
    """
    slots = set()
    for part in (times or "").split(","):
        part = part.strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError(f"IC_SCHEDULE must be 24-hour times separated by commas, like 12:00,15:00,19:00 (got: {part or 'nothing'}).")
        slots.add((int(m.group(1)), int(m.group(2))))
    slots = sorted(slots)
    minutes = [h * 60 + mm for h, mm in slots]
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    if gaps and min(gaps) < SCHEDULE_GRACE_MINUTES:
        raise ConfigError(f"IC_SCHEDULE times must be at least {SCHEDULE_GRACE_MINUTES} minutes apart.")

    weekdays = set()
    for part in (days or "mon-fri").lower().split(","):
        ends = [x.strip()[:3] for x in part.split("-")]
        if not 1 <= len(ends) <= 2 or any(e not in _DAY_NAMES for e in ends):
            raise ConfigError(f"IC_SCHEDULE_DAYS must be day names or ranges like mon-fri or sun-thu (got: {part.strip() or 'nothing'}).")
        start, end = _DAY_NAMES.index(ends[0]), _DAY_NAMES.index(ends[-1])
        d = start
        while True:
            weekdays.add(d)
            if d == end:
                break
            d = (d + 1) % 7
    return slots, weekdays


def previous_slot(now, slots, weekdays):
    """The latest scheduled slot at least SCHEDULE_GRACE_MINUTES before `now` (in now's time zone).

    So a run that starts a little late still counts from the slot before its own, and a manual run
    counts from the last scheduled one.
    """
    cutoff = now - dt.timedelta(minutes=SCHEDULE_GRACE_MINUTES)
    for back in range(15):
        day = cutoff.date() - dt.timedelta(days=back)
        if day.weekday() not in weekdays:
            continue
        for h, m in reversed(slots):
            slot = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
            if slot <= cutoff:
                return slot
    raise ConfigError("IC_SCHEDULE_DAYS leaves no scheduled day in the last two weeks.")


def cmd_check(args):
    guard_cloud()
    cfg = load_config("check")
    clients, _ = member_clients(cfg)
    if args.targets:
        selected = select_clients(clients, args.targets)
    else:
        selected = [c for c in clients if has_any_key(c, cfg)]
        if not selected:
            raise ConfigError("No IC_… keys found for any client. Run `doctor`.")
    if any(uses(c, "bison") and cfg.get(key_var(c["slug"], "bison")) for c in selected):
        configure_bison(cfg)
    tz, tz_label = resolve_timezone(cfg.get("IC_TIMEZONE"))
    now = dt.datetime.now(tz).replace(microsecond=0)
    mode = "unread" if args.unread else "new"
    since = hours = basis = None
    if mode == "new":
        if args.hours is None and cfg.get("IC_SCHEDULE"):
            since = previous_slot(now, *parse_schedule(cfg.get("IC_SCHEDULE"), cfg.get("IC_SCHEDULE_DAYS")))
            hours = round((now - since).total_seconds() / 3600, 2)
            basis = "schedule"
        else:
            since, hours = window_start(now, args.hours)
            basis = "hours"
    results = run_checks(selected, cfg, mode, since)
    if args.json:
        print(json.dumps(build_json(selected, results, now, mode, since, hours, basis), indent=2, ensure_ascii=False))
    else:
        print(format_text(selected, results, now, tz_label, mode, since, hours, basis))
    failed = any(r["state"] == "error" for res in results.values() for r in res.values())
    return 1 if failed else 0


# --- doctor ----------------------------------------------------------------

def _doctor_validate(client, platform, cfg):
    key = cfg.get(key_var(client["slug"], platform))
    try:
        if platform == "bison":
            ws = BisonClient(key).current_workspace()
            expected = client["bison"].get("workspace_id")
            if ws["id"] is not None and expected is None:
                return True, f'OK (workspace {ws["id"]}{": " + ws["name"] if ws["name"] else ""})'
            if ws["id"] is None:
                return True, "OK (key works; Bison did not report its workspace)"
            if ws["id"] != expected:
                return False, f"key belongs to workspace {ws['id']}, expected {expected}"
            return True, f'OK (workspace {ws["id"]}{": " + ws["name"] if ws["name"] else ""})'
        HeyReachClient(key).check_key()
        return True, "OK"
    except ConfigError as e:
        return False, f"FAIL ({e})"
    except HttpError as e:
        return False, f"FAIL ({e.label()})"
    except Exception as e:
        return False, f"FAIL (unexpected error: {e.__class__.__name__})"


def cmd_doctor(args):
    guard_cloud()
    cfg = load_config("doctor")
    problems = False
    config_problem = False
    out = ["inbox-check doctor", ""]

    py = ".".join(str(n) for n in sys.version_info[:3])
    out.append(f"Python        {py} (OK, 3.9+ required)")
    out.append(f".env          {'found' if cfg.env_found else 'not found (using environment variables only)'}")

    try:
        url = configure_bison(cfg, required=False)
        out.append(f"Bison URL     {url or 'not set (IC_BISON_URL; needed for Bison clients)'}")
    except ConfigError as e:
        out.append(f"Bison URL     PROBLEM: {e}")
        config_problem = True

    clients = []
    from_file = CLIENTS_PATH.is_file()
    try:
        clients, _ = member_clients(cfg)
        if from_file:
            out.append(f"clients.json  {len(clients)} client(s)")
        else:
            out.append(f"Clients       {len(clients)}, taken from your IC_ keys (no clients.json)")
    except ConfigError as e:
        out.append(f"clients.json  PROBLEM: {e}")
        config_problem = True

    if clients:
        slug_w = max(len(c["slug"]) for c in clients) + 2
        name_w = max(len(c["name"]) for c in clients) + 2
        out += ["", "Keys present (values are never shown)"]
        for c in clients:
            cells = []
            for p in PLATFORMS:
                if not uses(c, p):
                    state = "not used"
                else:
                    state = "yes" if cfg.get(key_var(c["slug"], p)) else "no"
                cells.append(f"{PLATFORM_LABELS[p]}: {state:<9}")
            out.append(f"  {c['slug']:<{slug_w}}{c['name']:<{name_w}}{'  '.join(cells).rstrip()}")

        known = {key_var(c["slug"], p) for c in clients for p in PLATFORMS}
        unknown = [n for n in cfg.names() if re.match(r"^IC_.+_(BISON|HEYREACH)_KEY$", n) and n not in known]
        if unknown and from_file:
            out.append(f"  Keys for clients not in clients.json: {', '.join(unknown)}")

        tasks = [(c, p) for c in clients for p in PLATFORMS if uses(c, p) and cfg.get(key_var(c["slug"], p))]
        out += ["", "Live key checks"]
        if not tasks:
            out.append("  No keys to check.")
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = [pool.submit(_doctor_validate, c, p, cfg) for c, p in tasks]
                for (c, p), future in zip(tasks, futures):
                    ok, detail = future.result()
                    problems = problems or not ok
                    out.append(f"  {c['slug']:<{slug_w}}{PLATFORM_LABELS[p] + ':':<10}{detail}")

    tz_name = cfg.get("IC_TIMEZONE")
    if tz_name:
        _, label = resolve_timezone(tz_name)
        out += ["", f"Time zone     {label}"]
    if cfg.get("IC_SCHEDULE"):
        try:
            slots, weekdays = parse_schedule(cfg.get("IC_SCHEDULE"), cfg.get("IC_SCHEDULE_DAYS"))
            day_list = ",".join(d for i, d in enumerate(_DAY_NAMES) if i in weekdays)
            out.append(f"Schedule      {', '.join(f'{h:02d}:{m:02d}' for h, m in slots)} on {day_list}")
        except ConfigError as e:
            out.append(f"Schedule      PROBLEM: {e}")
            config_problem = True

    out += ["", "Hosts"]
    hosts = [("api.heyreach.io", "https://api.heyreach.io/")]
    if BISON_URL:
        hosts.insert(0, (urllib.parse.urlparse(BISON_URL).netloc, BISON_URL + "/"))
    host_w = max(len(h) for h, _ in hosts) + 3
    for host, url in hosts:
        ok, detail = probe_host(url)
        problems = problems or not ok
        out.append(f"  {host:<{host_w}}{detail}")

    print("\n".join(out))
    if config_problem:
        return 2
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_timestamp(now, tz_label):
    return f"{_WEEKDAYS[now.weekday()]} {now.day:02d} {_MONTHS[now.month - 1]} {now.year}, {now:%H:%M} ({tz_label})"


def format_short(when):
    return f"{_WEEKDAYS[when.weekday()]} {when.day:02d} {_MONTHS[when.month - 1]}, {when:%H:%M}"


def _cell_text(result):
    state = result["state"]
    if state == "ok":
        if not result["count"]:
            return "0"
        text = f"{result['count']} {result['label']}"
        if result.get("awaiting"):
            text += f" ({result['awaiting']} awaiting reply)"
        return text
    if state == "not_set_up":
        return "not set up"
    if state == "no_key":
        return "no key"
    return f"ERROR ({result['error'].text_label()})"


def _cell_json(result):
    state = result["state"]
    if state == "ok":
        cell = {result["label"]: result["count"]}
        if result.get("awaiting") is not None:
            cell["awaiting_reply"] = result["awaiting"]
        return cell
    if state == "not_set_up":
        return None
    if state == "no_key":
        return {"skipped": "no key"}
    return {"error": result["error"].label()}


def _sorted(clients):
    return sorted(clients, key=lambda c: (c["name"].casefold(), c["slug"]))


def _plural_clients(n):
    return f"{n} client" if n == 1 else f"{n} clients"


def format_text(clients, results, now, tz_label, mode="unread", since=None, hours=None, basis="hours"):
    rows = []
    for c in _sorted(clients):
        res = results[c["slug"]]
        rows.append((c["name"], _cell_text(res["bison"]), res["bison"]["state"] == "error", _cell_text(res["heyreach"])))
    name_w = max(len(r[0]) for r in rows) + 6
    bison_w = max((len("Bison: " + r[1]) for r in rows if not r[2]), default=0) + 4
    lines = [f"Inbox check — {format_timestamp(now, tz_label)}"]
    if mode == "new":
        when = "previous scheduled run" if basis == "schedule" else f"last {hours}h"
        lines.append(f"New replies since {format_short(since)} ({when})")
    lines.append("")
    for name, bison, _, heyreach in rows:
        field = "Bison: " + bison
        lines.append(f"{name:<{name_w}}{field:<{max(bison_w, len(field) + 4)}}HeyReach: {heyreach}".rstrip())

    total = len(clients)
    with_any = sum(1 for c in clients if any(r["state"] == "ok" and r["count"] > 0 for r in results[c["slug"]].values()))
    failed = sum(1 for c in clients if any(r["state"] == "error" for r in results[c["slug"]].values()))
    verb = "has" if total == 1 else "have"
    lines += ["", f"{with_any} of {_plural_clients(total)} {verb} {mode} replies."]
    if failed:
        lines.append(f"{_plural_clients(failed)} couldn't be fully checked (see ERROR above).")
    return "\n".join(lines)


def build_json(clients, results, now, mode="unread", since=None, hours=None, basis="hours"):
    out = {"checked_at": now.isoformat(timespec="seconds"), "mode": mode}
    if mode == "new":
        out["since"] = since.isoformat(timespec="seconds")
        out["window_hours"] = hours
        out["window_basis"] = basis
    return {
        **out,
        "clients": [
            {
                "slug": c["slug"],
                "name": c["name"],
                "bison": _cell_json(results[c["slug"]]["bison"]),
                "heyreach": _cell_json(results[c["slug"]]["heyreach"]),
            }
            for c in _sorted(clients)
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _window_hours(value):
    try:
        hours = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of hours") from None
    if not 1 <= hours <= MAX_WINDOW_HOURS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_WINDOW_HOURS}")
    return hours


def build_parser():
    parser = argparse.ArgumentParser(
        prog="inboxcheck",
        description="Report new lead replies per client in Email Bison and HeyReach. Runs `check` when no command is given.",
    )
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("check", help="count new replies (default command)")
    p.add_argument("targets", nargs="*", metavar="slug-or-workspace-id", help="only these clients")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--hours", type=_window_hours, metavar="N", help="count replies from the last N hours (default: since the previous IC_SCHEDULE slot, else 24; 72 on Mondays)")
    p.add_argument("--unread", action="store_true", help="count unread replies instead of new ones")
    sub.add_parser("doctor", help="check your setup and keys (never shows key values)")
    sub.add_parser("setup", help="admin: build clients.json from the Bison workspace list")
    sub.add_parser("add-client", help="admin: add one Bison workspace to clients.json")
    p = sub.add_parser("issue-keys", help="admin: create keys for a team member and print the IC_ block")
    p.add_argument("slugs", nargs="+", metavar="slug")
    p.add_argument("--for", dest="member", required=True, metavar="member", help="team member name ([a-z0-9_-]+)")
    return parser


HANDLERS = {
    "check": cmd_check,
    "doctor": cmd_doctor,
    "setup": cmd_setup,
    "add-client": cmd_add_client,
    "issue-keys": cmd_issue_keys,
}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "check")
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except ConfigError as e:
        info(scrub(e))
        return 2
    except HttpError as e:
        info(f"Error: {e.label()}")
        return 1
    except KeyboardInterrupt:
        info("Interrupted.")
        return 130
    except Exception as e:
        info(f"Unexpected error ({e.__class__.__name__}): {scrub(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

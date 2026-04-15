"""PKCE OAuth flow against OpenAI's auth server, with token persistence.

Mirrors what the official `@openai/codex` CLI does: pop a browser to
`auth.openai.com/oauth/authorize`, listen on a localhost port for the
redirect, exchange the code for `access_token` + `refresh_token`,
cache to `~/.silicon-pantheon/credentials/codex-oauth.json`, refresh
on expiry under a file lock.

No `codex` CLI required at runtime — we just speak the same OAuth
flow against the same OpenAI auth endpoints with the same public
client_id that the codex CLI ships with.

Public surface:

  - `login_interactive() -> CodexCredentials`
        run the full browser-based PKCE flow; blocks until the
        callback fires or times out.
  - `load_credentials() -> CodexCredentials | None`
        read the cached credentials; None if not yet logged in.
  - `ensure_fresh_access_token(creds) -> str`
        return a valid access_token; refresh first if expired.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import httpx

log = logging.getLogger("silicon.providers.codex.oauth")


# ---- public OpenAI Codex CLI client identity ---------------------------

# Sourced from the open-source codex CLI (apache-2.0, github.com/openai/codex).
# This is the public OAuth client OpenAI ships for codex; using it from a
# third-party app is the same pattern OpenClaw and similar tools follow.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
# Scopes requested at sign-in. `offline_access` is what gets us a
# refresh_token in the response.
SCOPES = "openid profile email offline_access"
# Redirect target the codex CLI registers. Must match what's
# whitelisted server-side; localhost ports vary across runs.
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT_DEFAULT = 1455  # codex CLI's default; matches what auth.openai.com expects
REDIRECT_PATH = "/auth/callback"

# Path where we persist tokens. Separate from any codex CLI auth.json
# the user may also have — silicon-pantheon owns this file.
CREDENTIALS_PATH = (
    Path.home() / ".silicon-pantheon" / "credentials" / "codex-oauth.json"
)


# ---- data shape ---------------------------------------------------------


@dataclasses.dataclass
class CodexCredentials:
    """Persisted OAuth state for a single signed-in user.

    Stored on disk as JSON with the same field names. Refresh writes
    the file in-place; expires_at is an absolute unix timestamp so we
    don't have to track when the file was written.
    """

    access_token: str
    refresh_token: str
    # Absolute unix timestamp (seconds). Server returns `expires_in`
    # (relative seconds); we convert at write time so reload-after-
    # restart still computes "is expired" correctly.
    expires_at: float
    # Optional metadata returned by /token — useful for diagnostics
    # ("which account is this?") but never required for API auth.
    account_id: str | None = None
    id_token: str | None = None
    # Pinned at the moment of issue; if a future codex protocol version
    # requires a different one we can detect via this field.
    issued_at: float = dataclasses.field(default_factory=time.time)

    def is_expired(self, now: float | None = None, slack_s: float = 60.0) -> bool:
        """True when the access_token is within `slack_s` of expiry.

        60s slack mirrors what the codex CLI does — gives us a buffer
        to refresh before a long-running tool call gets a 401 mid-
        flight.
        """
        return (now or time.time()) >= (self.expires_at - slack_s)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CodexCredentials":
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=float(d["expires_at"]),
            account_id=d.get("account_id"),
            id_token=d.get("id_token"),
            issued_at=float(d.get("issued_at", time.time())),
        )


# ---- persistence --------------------------------------------------------


def _ensure_credentials_dir() -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Tighten perms on the parent dir too — these are bearer tokens.
    try:
        os.chmod(CREDENTIALS_PATH.parent, 0o700)
    except OSError:
        pass


def load_credentials(path: Path | None = None) -> CodexCredentials | None:
    """Load cached credentials from disk. None if the file is missing
    or unreadable / malformed (caller decides whether to re-login)."""
    p = path or CREDENTIALS_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return CodexCredentials.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("codex credentials at %s unreadable: %s", p, e)
        return None


def save_credentials(creds: CodexCredentials, path: Path | None = None) -> None:
    """Write credentials atomically with 0600 perms.

    Atomic via `os.replace` of a sibling tempfile so a crash mid-write
    can't truncate the existing file.
    """
    p = path or CREDENTIALS_PATH
    _ensure_credentials_dir()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(creds.to_dict(), indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ---- file-lock helper ---------------------------------------------------


@contextmanager
def _lock_credentials():
    """Best-effort exclusive lock around credential read-modify-write.

    Two SiliconPantheon clients running on the same machine could both
    decide to refresh the same expired token simultaneously — without
    a lock they'd both POST to /token, the second would 400 with
    "refresh_token already used", and one would lose the auth.

    Uses `fcntl.flock` on the credentials file's directory. Falls
    back to no-op on Windows (which doesn't have flock); the rare
    double-refresh race is benign there since one of the two attempts
    will simply re-read the file and find the fresh token.
    """
    _ensure_credentials_dir()
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return

    lock_path = CREDENTIALS_PATH.parent / ".codex-oauth.lock"
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


# ---- PKCE helpers -------------------------------------------------------


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636.

    code_verifier: 43-128 char URL-safe random string
    code_challenge: BASE64URL(SHA256(verifier)), no padding
    """
    verifier = secrets.token_urlsafe(64)[:128]
    sha = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(sha).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_authorize_url(
    code_challenge: str, state: str, redirect_uri: str
) -> str:
    """Compose the URL we open in the user's browser."""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        # Codex CLI passes these — see codex-rs/login/src/server.rs.
        # `id_token_add_organizations` asks the auth server to embed
        # org membership in the id_token so downstream API calls can
        # route to the right account.
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ---- callback HTTP server ----------------------------------------------


class _CallbackResult:
    """Holds the auth code (or error) the redirect handler captured.

    Used as a one-element mailbox between the asyncio task running
    the HTTP server and the caller awaiting the OAuth completion.
    """

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self.event = asyncio.Event()


async def _wait_for_callback(
    expected_state: str,
    port: int,
    timeout_s: float,
) -> _CallbackResult:
    """Run a tiny HTTP server on `port` that captures the OAuth
    redirect and returns the auth code.

    The redirect URL is `http://127.0.0.1:<port>/auth/callback?code=...`.
    Once we've captured the code we send a friendly HTML page so the
    user sees "All good, you can close this tab".
    """
    result = _CallbackResult()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            line = request_line.decode("ascii", errors="replace").strip()
            # Drain remaining headers (we don't need them, just don't
            # leave bytes in the kernel buffer).
            while True:
                hl = await reader.readline()
                if not hl or hl in (b"\r\n", b"\n"):
                    break
            if not line.startswith("GET"):
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                await writer.drain()
                return
            # GET <path> HTTP/1.1
            try:
                _, path, _ = line.split(" ", 2)
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                return
            parsed = urllib.parse.urlparse(path)
            if parsed.path != REDIRECT_PATH:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
                await writer.drain()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            result.code = (qs.get("code") or [None])[0]
            result.state = (qs.get("state") or [None])[0]
            result.error = (qs.get("error") or [None])[0]

            # Friendly response page so the user knows it worked.
            ok = (result.error is None and result.code is not None
                  and result.state == expected_state)
            html = (
                "<html><body style='font-family:sans-serif;text-align:center;"
                "padding:4em'>"
                f"<h2>{'✓ Logged in to SiliconPantheon' if ok else '✗ Login failed'}</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
            body = html.encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:
            log.exception("OAuth callback handler raised")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            result.event.set()

    server = await asyncio.start_server(handle, host=REDIRECT_HOST, port=port)
    try:
        try:
            await asyncio.wait_for(result.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            result.error = "timeout"
        return result
    finally:
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass


# ---- public flow --------------------------------------------------------


class CodexAuthError(RuntimeError):
    """Raised when login or refresh fails. Caller surfaces to user."""


async def login_interactive(
    *, port: int = REDIRECT_PORT_DEFAULT, timeout_s: float = 300.0,
    open_browser: bool = True,
) -> CodexCredentials:
    """Run the full PKCE flow: browser → callback → token exchange →
    persist. Blocks until done; intended to be awaited from the TUI's
    "log in to OpenAI" affordance.

    Raises CodexAuthError on any step failing.
    """
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://{REDIRECT_HOST}:{port}{REDIRECT_PATH}"
    url = _build_authorize_url(challenge, state, redirect_uri)

    log.info(
        "starting Codex OAuth login: callback=%s timeout=%ds",
        redirect_uri, int(timeout_s),
    )

    if open_browser:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            log.warning("could not open browser; user must open URL manually")

    # Always print the URL too — headless boxes won't have a browser
    # and the user can copy-paste from there.
    print(f"\nIf no browser opened, visit:\n  {url}\n", flush=True)

    result = await _wait_for_callback(
        expected_state=state, port=port, timeout_s=timeout_s
    )
    if result.error:
        raise CodexAuthError(f"OAuth callback error: {result.error}")
    if result.code is None:
        raise CodexAuthError("OAuth callback delivered no code")
    if result.state != state:
        raise CodexAuthError(
            "OAuth state mismatch — possible CSRF; aborting"
        )

    # Exchange code → tokens.
    creds = await _exchange_code_for_tokens(
        code=result.code, code_verifier=verifier, redirect_uri=redirect_uri,
    )
    save_credentials(creds)
    log.info(
        "Codex login complete (account_id=%s, expires_in=%ds)",
        creds.account_id or "?",
        int(creds.expires_at - time.time()),
    )
    return creds


async def _exchange_code_for_tokens(
    *, code: str, code_verifier: str, redirect_uri: str
) -> CodexCredentials:
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise CodexAuthError(
            f"token exchange failed (HTTP {resp.status_code}): {resp.text[:400]}"
        )
    body = resp.json()
    expires_in = float(body.get("expires_in", 3600))
    # OpenAI tucks account_id inside the access_token JWT in some
    # flows; if `account_id` is on the body we use it, else None.
    return CodexCredentials(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", ""),
        expires_at=time.time() + expires_in,
        account_id=body.get("account_id"),
        id_token=body.get("id_token"),
    )


async def refresh_access_token(creds: CodexCredentials) -> CodexCredentials:
    """Use the refresh_token to mint a fresh access_token.

    Returns the new credentials and writes them to disk inside the
    file lock so concurrent callers see the same fresh token. The
    refresh_token MAY rotate (server returns a new one); we honor
    that — the old refresh_token is single-use after rotation.
    """
    if not creds.refresh_token:
        raise CodexAuthError(
            "no refresh_token on stored credentials — re-login required"
        )
    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": creds.refresh_token,
        "scope": SCOPES,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise CodexAuthError(
            f"refresh failed (HTTP {resp.status_code}): {resp.text[:400]}"
        )
    body = resp.json()
    expires_in = float(body.get("expires_in", 3600))
    new = CodexCredentials(
        access_token=body["access_token"],
        # Server may rotate the refresh_token; keep the old one as
        # fallback if not.
        refresh_token=body.get("refresh_token") or creds.refresh_token,
        expires_at=time.time() + expires_in,
        account_id=body.get("account_id") or creds.account_id,
        id_token=body.get("id_token") or creds.id_token,
    )
    save_credentials(new)
    return new


async def ensure_fresh_access_token(
    creds: CodexCredentials | None = None,
) -> str:
    """Top-level helper. Pass nothing to load + maybe-refresh from
    the on-disk credentials. Returns a usable bearer token. Raises
    CodexAuthError if no credentials exist or refresh fails."""
    with _lock_credentials():
        if creds is None:
            creds = load_credentials()
        if creds is None:
            raise CodexAuthError(
                "no Codex credentials — run login_interactive() first"
            )
        if not creds.is_expired():
            return creds.access_token
        log.info("Codex access_token expired; refreshing")
        new = await refresh_access_token(creds)
        return new.access_token

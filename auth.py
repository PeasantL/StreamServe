"""Optional shared-password authentication.

The IP allowlist in ``middleware`` is a network filter, not authentication:
anyone who can reach the port from an allowed address has full control. That
is fine on a LAN and useless the moment the server is reachable over a VPN or
a tunnel where every peer shares one source address.

This adds a second, opt-in layer *behind* the allowlist -- not instead of it.
Leave ``auth_password`` empty and nothing here does anything, which is the
default and the previous behaviour exactly.

The session is a signed cookie rather than server-side state, so it survives a
restart and costs no memory. It carries an expiry and an HMAC over that
expiry; there is nothing else to carry, because there is only one account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from config import settings

log = logging.getLogger(__name__)

COOKIE_NAME = "streamserve_session"

# Reachable without a session: the login form itself, the endpoint that grants
# one, the metadata browsers need to install it, and the health check the
# container polls from inside its own network.
EXEMPT_PATHS = frozenset({"/login", "/healthz", "/manifest.webmanifest"})

# The login page carries the site icon, which the browser fetches before
# there is any session to check.
EXEMPT_PREFIXES = ("/static/",)

# A shared password with no throttle is a password anyone patient can guess.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def is_enabled() -> bool:
    return bool(settings.auth_password)


def _secret() -> bytes:
    """The signing key, generated per-process when none is configured.

    An unconfigured secret means sessions do not survive a restart, which is a
    minor annoyance; a hardcoded default would mean anyone could forge a
    session against any deployment, which is not.
    """
    if settings.session_secret:
        return settings.session_secret.encode("utf-8")
    return _EPHEMERAL_SECRET


_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _sign(expiry: int) -> str:
    signature = hmac.new(_secret(), str(expiry).encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def issue_token(now: float | None = None) -> str:
    expiry = int((time.time() if now is None else now) + settings.session_ttl)
    return f"{expiry}.{_sign(expiry)}"


def verify_token(token: str | None, now: float | None = None) -> bool:
    """True when *token* carries an unexpired, correctly signed expiry."""
    if not token or "." not in token:
        return False

    raw_expiry, _, signature = token.partition(".")
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return False

    # Constant-time: a timing-distinguishable comparison here is what makes a
    # signature forgeable one byte at a time.
    if not hmac.compare_digest(signature, _sign(expiry)):
        return False

    return expiry > (time.time() if now is None else now)


def check_password(candidate: str) -> bool:
    if not is_enabled():
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), settings.auth_password.encode("utf-8"))


@dataclass
class Throttle:
    """Per-address failure counter with a fixed lockout.

    In-process and unbounded only by the number of addresses that have failed
    recently; entries are dropped once their window has passed.
    """

    max_failures: int = MAX_FAILURES
    lockout_seconds: int = LOCKOUT_SECONDS
    _failures: dict[str, tuple[int, float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def locked_for(self, address: str, now: float | None = None) -> int:
        """Seconds remaining before *address* may try again; 0 when it may."""
        now = time.time() if now is None else now
        with self._lock:
            count, last = self._failures.get(address, (0, 0.0))
            if count < self.max_failures:
                return 0
            remaining = int(last + self.lockout_seconds - now)
            if remaining <= 0:
                del self._failures[address]
                return 0
            return remaining

    def record_failure(self, address: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            count, _ = self._failures.get(address, (0, 0.0))
            self._failures[address] = (count + 1, now)

    def reset(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


throttle = Throttle()


def _wants_json(request: Request) -> bool:
    """True for the fetch() calls the UI makes, false for a browser navigation.

    An unauthenticated API call should get a 401 it can report, not an HTML
    login page it will try to parse as JSON.
    """
    if request.url.path.startswith("/api/"):
        return True
    return "application/json" in request.headers.get("accept", "")


async def auth_middleware(request: Request, call_next):
    """Require a valid session cookie once a password is configured."""
    path = request.url.path
    if not is_enabled() or path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
        return await call_next(request)

    if verify_token(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)

    if _wants_json(request):
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})

    # Carry the requested path so logging in lands where the user was going.
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={_quote(target)}", status_code=303)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def safe_next(target: str | None) -> str:
    """A same-site redirect target, or "/".

    Anything absolute, scheme-relative or backslash-prefixed is discarded: a
    login form that will redirect anywhere is an open redirect, and one on a
    page people type a password into is a phishing primitive.
    """
    if not target or not target.startswith("/"):
        return "/"
    if target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")

"""authentication helpers: password hashing, login_required decorator, rate limiting."""

from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import threading
import time
from collections import deque
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import (
    Response,
    current_app,
    g,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import abort
from werkzeug.wrappers import Response as WerkzeugResponse

# argon2id parameters are locked per design spec section 3
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# minimum password length; shared by the first-run wizard and the api password
# change endpoint so policy stays consistent across surfaces.
MIN_PASSWORD_LENGTH = 12

# rate-limit sliding window: 10 failures within 10 minutes locks the IP
_FAILURE_WINDOW_SECONDS = 600
_FAILURE_THRESHOLD = 10
_LOCKOUT_SECONDS = 900
# caps the number of tracked ips so a flood of distinct addresses cannot grow
# the rate-limit tables without bound.
_MAX_TRACKED_IPS = 10000

# session key holding the password-hash fingerprint the session was issued for
SESSION_VERSION_KEY = "pwv"

# maps ip -> deque of monotonic failure timestamps
_failure_timestamps: dict[str, deque[float]] = {}
# maps ip -> monotonic timestamp when the lockout expires
_locked_until: dict[str, float] = {}
_rate_limit_lock = threading.Lock()


def hash_password(plaintext: str) -> str:
    """hashes plaintext with argon2id using locked parameters; returns the encoded hash."""
    result: str = _HASHER.hash(plaintext)
    return result


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """verifies plaintext against stored_hash; returns False on mismatch, True on success."""
    try:
        result: bool = _HASHER.verify(stored_hash, plaintext)
        return result
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """returns True if the stored hash was produced with different parameters."""
    result: bool = _HASHER.check_needs_rehash(stored_hash)
    return result


# ---------------------------------------------------------------------------
# rate-limit helpers
# ---------------------------------------------------------------------------


def is_login_locked_out(ip: str) -> bool:
    """returns True if ip is currently locked out."""
    with _rate_limit_lock:
        now = time.monotonic()
        # check explicit lockout-until timestamp first
        until = _locked_until.get(ip, 0.0)
        if now < until:
            return True
        # prune expired lockout entries to bound memory
        if ip in _locked_until and _locked_until[ip] <= now:
            del _locked_until[ip]
        # also check sliding window in case a lockout was never set
        window_start = now - _FAILURE_WINDOW_SECONDS
        dq = _failure_timestamps.get(ip)
        if dq is None:
            return False
        while dq and dq[0] < window_start:
            dq.popleft()
        return len(dq) >= _FAILURE_THRESHOLD


def _prune_rate_limit_state(now: float) -> None:
    """drops stale entries and evicts the oldest ones beyond _MAX_TRACKED_IPS.

    caller must hold _rate_limit_lock.
    """
    window_start = now - _FAILURE_WINDOW_SECONDS
    for key in [
        k for k, dq in _failure_timestamps.items() if not dq or dq[-1] < window_start
    ]:
        del _failure_timestamps[key]
    for key in [k for k, until in _locked_until.items() if until <= now]:
        del _locked_until[key]
    # dicts preserve insertion order, so the first keys are the oldest entries
    for table in (_failure_timestamps, _locked_until):
        excess = len(table) - _MAX_TRACKED_IPS + 1
        for key in list(table)[: max(0, excess)]:
            del table[key]


def record_login_failure(ip: str) -> None:
    """records a failed login attempt for ip; sets a lockout if threshold is reached."""
    with _rate_limit_lock:
        now = time.monotonic()
        if ip not in _failure_timestamps and (
            len(_failure_timestamps) >= _MAX_TRACKED_IPS
            or len(_locked_until) >= _MAX_TRACKED_IPS
        ):
            _prune_rate_limit_state(now)
        if ip not in _failure_timestamps:
            _failure_timestamps[ip] = deque()
        dq = _failure_timestamps[ip]
        dq.append(now)
        # drops timestamps outside the window so a deque cannot grow unbounded
        window_start = now - _FAILURE_WINDOW_SECONDS
        while dq and dq[0] < window_start:
            dq.popleft()
        recent = len(dq)
        if recent >= _FAILURE_THRESHOLD:
            _locked_until[ip] = now + _LOCKOUT_SECONDS


def clear_login_failures(ip: str) -> None:
    """clears all recorded failures and any lockout for ip (called on successful login)."""
    with _rate_limit_lock:
        _failure_timestamps.pop(ip, None)
        _locked_until.pop(ip, None)


# ---------------------------------------------------------------------------
# login_required decorator
# ---------------------------------------------------------------------------

_ViewFunc = Callable[..., Any]


def session_version(password_hash: str) -> str:
    """returns a short fingerprint of password_hash stored in the session.

    a session whose fingerprint no longer matches the current hash was issued
    before a password change and is rejected by login_required.
    """
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def peer_address() -> str:
    """returns the socket peer address, ignoring any X-Forwarded-For rewrite.

    ProxyFix (enabled by BLACKVUESYNC_TRUST_PROXY) replaces REMOTE_ADDR with a
    client-supplied value and keeps the original under werkzeug.proxy_fix.orig.
    """
    orig = request.environ.get("werkzeug.proxy_fix.orig")
    if isinstance(orig, dict) and orig.get("REMOTE_ADDR"):
        return str(orig["REMOTE_ADDR"])
    return str(request.environ.get("REMOTE_ADDR") or "")


def is_trusted_proxy(address: str, trusted: object) -> bool:
    """returns True if address matches any IP or CIDR entry in trusted."""
    if not isinstance(trusted, (tuple, list)) or not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # dual-stack sockets report ipv4 peers as ::ffff:a.b.c.d
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    for entry in trusted:
        if not isinstance(entry, str):
            continue
        try:
            network = ipaddress.ip_network(entry.strip(), strict=False)
        except ValueError:
            continue
        if ip.version == network.version and ip in network:
            return True
    return False


def _unauthenticated() -> WerkzeugResponse:
    """returns the unauthenticated response suited to the request type.

    api paths get 401 JSON (fetch would otherwise follow a redirect to html),
    htmx requests get 401 with HX-Redirect, and pages redirect to /login.
    """
    login_url = url_for("auth_bp.login", next=request.path)
    if request.path.startswith("/api/"):
        body = json.dumps(
            {"error": "authentication required", "code": "AUTH_REQUIRED", "details": {}}
        )
        return Response(body, status=401, mimetype="application/json")
    if request.headers.get("HX-Request") == "true":
        resp = Response("", status=401)
        resp.headers["HX-Redirect"] = login_url
        return resp
    return redirect(login_url)


def login_required(view: _ViewFunc) -> _ViewFunc:
    """decorator enforcing authentication per the current auth.mode setting.

    reads auth.mode fresh on every request so a settings change takes
    effect without restarting the server.

    - "none": passes through; sets g.current_user = "anonymous".
    - "proxy": reads the proxy_user_header when the socket peer is a trusted
      proxy (IP or CIDR); aborts 401 otherwise or if the header is absent.
    - "login": checks session["user"] and the session's password version;
      answers unauthenticated requests per _unauthenticated().
    """

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        settings = current_app.settings_store.get()  # type: ignore[attr-defined]
        auth = settings.auth
        mode = auth.mode

        if mode == "none":
            g.current_user = "anonymous"
            return view(*args, **kwargs)

        if mode == "proxy":
            if not is_trusted_proxy(peer_address(), auth.trusted_proxies):
                abort(401)
            header_value = request.headers.get(auth.proxy_user_header, "")
            if not header_value:
                abort(401)
            g.current_user = header_value
            return view(*args, **kwargs)

        # mode == "login"
        user = session.get("user")
        if not user:
            return _unauthenticated()
        if session.get(SESSION_VERSION_KEY) != session_version(auth.password_hash):
            # issued before the latest password change; revokes it
            session.clear()
            return _unauthenticated()
        g.current_user = user
        return view(*args, **kwargs)

    return wrapped

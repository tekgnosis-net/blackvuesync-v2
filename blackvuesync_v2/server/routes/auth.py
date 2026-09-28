"""authentication routes: /login, /logout, /first-run."""

from __future__ import annotations

import dataclasses
import time
from urllib.parse import urlparse

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.wrappers import Response

from blackvuesync_v2.server.auth import (
    MIN_PASSWORD_LENGTH,
    SESSION_VERSION_KEY,
    clear_login_failures,
    hash_password,
    is_login_locked_out,
    login_required,
    record_login_failure,
    session_version,
    verify_password,
)
from blackvuesync_v2.settings import Settings

bp = Blueprint("auth_bp", __name__)

# template paths and endpoint names extracted as constants per python:S1192
_LOGIN_TEMPLATE = "login.html"
_FIRST_RUN_TEMPLATE = "first_run.html"
_LOGIN_ENDPOINT = "auth_bp.login"
_FIRST_RUN_ENDPOINT = "auth_bp.first_run"

# minimum response time for a failed login in seconds; guards against timing attacks
_MIN_FAILURE_SECONDS = 1.5


@bp.before_app_request
def redirect_to_first_run() -> Response | None:
    """redirects any request to /first-run when no password has been set.

    skips the redirect for the first-run page itself, static assets, and
    health-check endpoints so the wizard and infrastructure routes remain
    accessible.
    """
    exempt_prefixes = ("/first-run", "/static", "/healthz", "/readyz")
    if any(request.path.startswith(p) for p in exempt_prefixes):
        return None
    settings = current_app.settings_store.get()  # type: ignore[attr-defined]
    if not settings.auth.password_hash:
        return redirect(url_for(_FIRST_RUN_ENDPOINT))
    return None


@bp.route("/login", methods=["GET"])
def login() -> str | Response:
    """renders the login form; redirects to /first-run if no password is set.

    the optional `next` query parameter is read here once and passed into the
    template as a hidden form field; the POST handler then reads it from the
    form body. this avoids query-params-on-POST (python:S8370).
    """
    settings = current_app.settings_store.get()  # type: ignore[attr-defined]
    if not settings.auth.password_hash:
        return redirect(url_for(_FIRST_RUN_ENDPOINT))
    return render_template(_LOGIN_TEMPLATE, next=request.args.get("next", ""))


@bp.route("/login", methods=["POST"])
def login_post() -> tuple[str, int] | Response:
    """validates credentials; sets session on success; pads timing on failure."""
    start = time.perf_counter()

    settings = current_app.settings_store.get()  # type: ignore[attr-defined]
    auth = settings.auth
    ip = request.remote_addr or "unknown"

    next_url = request.form.get("next", "")

    if is_login_locked_out(ip):
        _pad_response_time(start)
        return (
            render_template(
                _LOGIN_TEMPLATE,
                error="too many failed attempts; try again later",
                next=next_url,
            ),
            429,
        )

    username = request.form.get("username", "")
    password = request.form.get("password", "")

    # always verify (even for wrong usernames) to produce uniform timing
    stored_hash = (
        auth.password_hash
        if auth.password_hash
        else "$argon2id$v=19$m=65536,t=3,p=4$" + "a" * 16 + "$" + "b" * 32
    )
    username_ok = username == auth.username
    password_ok = verify_password(stored_hash, password)

    if not (username_ok and password_ok):
        record_login_failure(ip)
        _pad_response_time(start)
        return (
            render_template(
                _LOGIN_TEMPLATE,
                error="invalid username or password",
                next=next_url,
            ),
            401,
        )

    clear_login_failures(ip)
    session.clear()
    session["user"] = auth.username
    session[SESSION_VERSION_KEY] = session_version(auth.password_hash)
    session.permanent = True

    if not next_url:
        next_url = url_for("ui_bp.dashboard")
    # prevent open-redirect: reject any url with a scheme or netloc (e.g.
    # //evil.com, http://evil.com, javascript:...) and any non-path value.
    _parsed = urlparse(next_url)
    if _parsed.scheme or _parsed.netloc or not next_url.startswith("/"):
        next_url = "/"

    return redirect(next_url)


def _pad_response_time(start: float) -> None:
    """sleeps long enough to make the total response time at least _MIN_FAILURE_SECONDS."""
    elapsed = time.perf_counter() - start
    remaining = _MIN_FAILURE_SECONDS - elapsed
    if remaining > 0:
        time.sleep(remaining)


@bp.route("/logout", methods=["POST"])
@login_required
def logout() -> Response:
    """clears the session and redirects to /login."""
    session.clear()
    return redirect(url_for(_LOGIN_ENDPOINT))


@bp.route("/first-run", methods=["GET"])
def first_run() -> str | Response:
    """renders the first-run wizard; redirects to /login if a password is already set."""
    settings = current_app.settings_store.get()  # type: ignore[attr-defined]
    if settings.auth.password_hash:
        return redirect(url_for(_LOGIN_ENDPOINT))
    return render_template(_FIRST_RUN_TEMPLATE)


@bp.route("/first-run", methods=["POST"])
def first_run_post() -> tuple[str, int] | Response:
    """processes the first-run form: validates and stores the initial password."""
    settings = current_app.settings_store.get()  # type: ignore[attr-defined]
    if settings.auth.password_hash:
        return redirect(url_for(_LOGIN_ENDPOINT))

    username = request.form.get("username", "admin").strip() or "admin"
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            render_template(
                _FIRST_RUN_TEMPLATE,
                error=f"password must be at least {MIN_PASSWORD_LENGTH} characters",
                username=username,
            ),
            400,
        )

    if password != confirm:
        return (
            render_template(
                _FIRST_RUN_TEMPLATE,
                error="passwords do not match",
                username=username,
            ),
            400,
        )

    pw_hash = hash_password(password)

    def _set_initial_password(s: Settings) -> Settings:
        """stores the first password unless a concurrent request already did."""
        if s.auth.password_hash:
            # returning the settings unchanged keeps the winner's password
            return s
        return dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username=username, password_hash=pw_hash),
        )

    # the check runs inside update(), which serializes mutations, so two
    # concurrent first-run posts cannot both set the password.
    current_app.settings_store.update(  # type: ignore[attr-defined]
        _set_initial_password
    )
    return redirect(url_for(_LOGIN_ENDPOINT))

"""flask application factory for the blackvuesync-v2 web server."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from typing import Optional

from flask import Flask, Response, request
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from blackvuesync_v2 import __version__
from blackvuesync_v2.server.log_buffer import BOOT_ID, LogBuffer
from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.server.stats_store import StatsStore
from blackvuesync_v2.settings import Settings, SettingsStore

logger = logging.getLogger(__name__)


def create_app(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments,too-many-statements
    settings_store: SettingsStore,
    testing: bool = False,
    progress_publisher: Optional[ProgressPublisher] = None,
    log_buffer: Optional[LogBuffer] = None,
    log_file_path: Optional[str] = None,
    stats_store: Optional[StatsStore] = None,
) -> Flask:
    """constructs and configures the Flask app with auth, routes, and middleware.

    attaches settings_store to the app instance so routes can access it
    via current_app.settings_store without importing a global singleton.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.settings_store = settings_store  # type: ignore[attr-defined]
    # attaches or creates the progress publisher; defaults to a new instance so
    # all routes have a live publisher even in tests that don't supply one.
    app.progress_publisher = progress_publisher or ProgressPublisher()  # type: ignore[attr-defined]
    # attaches or creates the log buffer; defaults to a new instance so route
    # tests have a live buffer even when serve mode did not supply one.
    app.log_buffer = log_buffer or LogBuffer()  # type: ignore[attr-defined]
    # absolute path of the rotating log file for the viewer to display; None
    # (rendered as "") when no file handler is configured (e.g. in tests).
    app.log_file_path = log_file_path  # type: ignore[attr-defined]
    # lets the logs page detect a server restart (seq restarts per process).
    app.jinja_env.globals["log_boot_id"] = BOOT_ID
    app.jinja_env.globals["app_version"] = __version__
    # attaches the stats store, or an empty in-memory store so route/page
    # handlers always have one even when serve mode did not supply it.
    app.stats_store = stats_store or StatsStore(":memory:")  # type: ignore[attr-defined]

    settings = settings_store.get()
    secret = settings.auth.session_secret
    if not secret:
        if testing:
            secret = "test-only-placeholder"
        else:
            # never signs sessions with a public constant; a per-process key
            # means sessions do not survive a restart until a secret is stored.
            logger.warning(
                "auth.session_secret is empty; using a random per-process key"
            )
            secret = secrets.token_hex(32)

    # when deployed behind an https reverse proxy, set BLACKVUESYNC_TRUST_PROXY=1
    # so the session cookie is only sent over https connections and the
    # X-Forwarded-* headers from that one proxy are honored.
    trust_proxy = os.environ.get("BLACKVUESYNC_TRUST_PROXY", "").lower() in (
        "1",
        "true",
        "yes",
    )

    app.config.update(
        TESTING=testing,
        SECRET_KEY=secret.encode(),
        SESSION_COOKIE_NAME="bvs_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=trust_proxy,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.web.session_lifetime_hours),
        WTF_CSRF_HEADERS=["X-CSRFToken"],
        WTF_CSRF_ENABLED=not testing,
        # tokens stay bound to the session but never expire, so a page left
        # open for hours can still submit.
        WTF_CSRF_TIME_LIMIT=None,
    )

    CSRFProtect(app)

    def _apply_session_secret(old: Settings, new: Settings) -> None:
        """swaps the signing key when the session secret rotates.

        flask reads app.secret_key per request, so every existing session
        cookie becomes invalid immediately.
        """
        rotated = new.auth.session_secret
        if rotated and rotated != old.auth.session_secret:
            app.secret_key = rotated.encode()

    settings_store.on_change(_apply_session_secret)

    # X-Forwarded-* headers are client-controlled unless a proxy overwrites
    # them, so they are honored only when the operator opts in.
    if trust_proxy:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
        )

    # blueprints are imported inside create_app to avoid circular imports at
    # module level; flask routes depend on the app context. pylint C0415 is
    # suppressed below because the deferred import is intentional.

    # pylint: disable=import-outside-toplevel
    from blackvuesync_v2.server.routes.api_auth import api_auth_bp
    from blackvuesync_v2.server.routes.api_camera import api_camera_bp
    from blackvuesync_v2.server.routes.api_dashcam import api_dashcam_bp
    from blackvuesync_v2.server.routes.api_health import api_health_bp
    from blackvuesync_v2.server.routes.api_logs import api_logs_bp
    from blackvuesync_v2.server.routes.api_recordings import api_recordings_bp
    from blackvuesync_v2.server.routes.api_schedule import api_schedule_bp
    from blackvuesync_v2.server.routes.api_settings import api_settings_bp
    from blackvuesync_v2.server.routes.api_stats import api_stats_bp
    from blackvuesync_v2.server.routes.api_sync import api_sync_bp
    from blackvuesync_v2.server.routes.api_viewer import api_viewer_bp
    from blackvuesync_v2.server.routes.auth import bp as auth_bp
    from blackvuesync_v2.server.routes.health import bp as health_bp
    from blackvuesync_v2.server.routes.hx_camera import hx_camera_bp
    from blackvuesync_v2.server.routes.hx_dashboard import hx_dashboard_bp
    from blackvuesync_v2.server.routes.hx_sync import hx_sync_bp
    from blackvuesync_v2.server.routes.media import media_bp
    from blackvuesync_v2.server.routes.ui import bp as ui_bp

    # pylint: enable=import-outside-toplevel

    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(api_auth_bp)
    app.register_blueprint(api_camera_bp)
    app.register_blueprint(api_dashcam_bp)
    app.register_blueprint(api_health_bp)
    app.register_blueprint(api_recordings_bp)
    app.register_blueprint(api_schedule_bp)
    app.register_blueprint(api_settings_bp)
    app.register_blueprint(api_sync_bp)
    app.register_blueprint(api_logs_bp)
    app.register_blueprint(api_stats_bp)
    app.register_blueprint(api_viewer_bp)
    app.register_blueprint(hx_dashboard_bp)
    app.register_blueprint(hx_camera_bp)
    app.register_blueprint(hx_sync_bp)
    app.register_blueprint(media_bp)

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        """sets security-related HTTP response headers on every response."""
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.tile.openstreetmap.org; "
            "media-src 'self' blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        return response

    return app


__all__: list[str] = ["create_app"]

import os
from flask import Flask
from app.config import config_by_name
from app.extensions import (
    db,
    migrate,
    jwt,
    cors,
    ma,
    limiter,
    socketio,
    cache,
)
from app.utils.errors import register_error_handlers
from app.api import register_api_blueprints
from app.api.v1.auth_callbacks import register_jwt_callbacks


def _validate_secrets(flask_app):
    """Refuse to run with missing/placeholder signing secrets (tokens would be forgeable)."""
    if flask_app.config.get("TESTING"):
        return
    from app.config import INSECURE_SECRET_VALUES
    for name in ("SECRET_KEY", "JWT_SECRET_KEY"):
        value = flask_app.config.get(name) or ""
        if value.strip() in INSECURE_SECRET_VALUES:
            raise RuntimeError(
                f"{name} is not set (or is a placeholder). Set a long random value in the "
                "environment, e.g. python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if len(value) < 32:
            flask_app.logger.warning("%s is shorter than 32 characters; use a longer random value.", name)

    samesite = flask_app.config.get("JWT_COOKIE_SAMESITE")
    if samesite not in ("Lax", "Strict", "None"):
        raise RuntimeError("REFRESH_COOKIE_SAMESITE must be Lax, Strict or None.")
    if samesite == "None":
        # Browsers drop SameSite=None cookies that aren't Secure.
        flask_app.config["JWT_COOKIE_SECURE"] = True


def _register_security_headers(flask_app):
    """Defensive HTTP headers on every response."""
    from flask import request

    @flask_app.after_request
    def _security_headers(response):
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if response.mimetype == "application/json":
            # The API only ever returns data: nothing may be executed or framed.
            headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if request.path.startswith("/api/"):
            # Responses carry personal and financial data: never cache them.
            headers.setdefault("Cache-Control", "no-store")
        if request.is_secure:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def _validate_runtime_modes(flask_app, config_name):
    """Fail fast on unsafe or incomplete database/authentication configuration."""
    if flask_app.config.get("DATABASE_ENABLED"):
        if not flask_app.config.get("SQLALCHEMY_DATABASE_URI"):
            raise RuntimeError(
                "DATABASE_ENABLED=true but no database URL is configured "
                "(set DATABASE_URL, or TEST_DATABASE_URL for the testing config)."
            )
        if flask_app.config.get("DEV_AUTH_ENABLED"):
            flask_app.logger.warning(
                "DEV_AUTH_ENABLED is ignored because the database is enabled; "
                "database-backed authentication is in use."
            )

    if flask_app.config.get("DEV_AUTH_ENABLED") and config_name == "production":
        raise RuntimeError("DEV_AUTH_ENABLED must never be set in production.")


def create_app(config_name=None, config_overrides=None):
    """Application factory for the School ERP backend."""
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    flask_app = Flask(__name__)
    config_obj = config_by_name.get(config_name, config_by_name["development"])
    flask_app.config.from_object(config_obj)
    if config_overrides:
        flask_app.config.update(config_overrides)

    _validate_runtime_modes(flask_app, config_name)
    _validate_secrets(flask_app)

    proxies = flask_app.config.get("TRUSTED_PROXY_COUNT") or 0
    if proxies > 0:
        # Real client IP/scheme from the proxy's X-Forwarded-* headers (rate
        # limits, audit logs, HSTS). Only as many hops as are actually trusted.
        from werkzeug.middleware.proxy_fix import ProxyFix
        flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)
    _register_security_headers(flask_app)

    # Initialize extensions with app instance. SQLAlchemy and Alembic are only
    # bound when the database is enabled, so no engine or connection pool is
    # created while it is disconnected.
    if flask_app.config.get("DATABASE_ENABLED"):
        db.init_app(flask_app)
        migrate.init_app(flask_app, db)
    else:
        flask_app.logger.warning(
            "Database integration is disabled (DATABASE_ENABLED=false). "
            "Database-dependent endpoints will return 503 DATABASE_UNAVAILABLE."
        )
        # init_app normally registers this; keep scoped sessions from accumulating.
        flask_app.teardown_appcontext(lambda exc: db.session.remove())

    jwt.init_app(flask_app)
    register_jwt_callbacks(flask_app)
    cors.init_app(
        flask_app,
        origins=flask_app.config.get("CORS_ALLOWED_ORIGINS", "*"),
        supports_credentials=True,
    )
    ma.init_app(flask_app)
    limiter.init_app(flask_app)
    cache.init_app(flask_app)

    # Register the SocketIO event handlers (connect auth + institution rooms)
    # BEFORE init_app: flask-socketio queues handlers declared while it has no
    # server and replays them on every init_app, so each app gets them.
    import app.sockets  # noqa: F401

    # Use Redis as the SocketIO message queue when configured, so realtime events
    # fan out across multiple workers/servers; falls back to single-process.
    redis_url = flask_app.config.get("REDIS_URL")
    if redis_url:
        socketio.init_app(flask_app, message_queue=redis_url)
        flask_app.logger.info("Redis connected: rate-limiting, caching, and SocketIO message queue are using Redis.")
    else:
        socketio.init_app(flask_app)

    # Register standardized error handlers
    register_error_handlers(flask_app)

    # Register API blueprints
    register_api_blueprints(flask_app)

    # Root endpoint: a lightweight liveness check so GET / returns 200 instead of
    # the standard 404 envelope. Kept outside the /api/v1 blueprint so it does not
    # affect versioned API routes or the custom error handlers.
    @flask_app.route("/")
    def index():
        from flask import jsonify
        return jsonify({
            "success": True,
            "message": "School Management API is running",
        }), 200

    # Load all models so Alembic and SQLAlchemy discover entity metadata
    with flask_app.app_context():
        import app.models  # noqa: F401

    return flask_app

import os
from datetime import timedelta
from dotenv import load_dotenv

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Load environment variables from backend/.env file reliably regardless of CWD
load_dotenv(os.path.join(BACKEND_DIR, ".env"), override=True)
load_dotenv()  # also load CWD .env if present


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable ("true", "1", "yes", "on")."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Placeholder values that must never sign real tokens or sessions.
INSECURE_SECRET_VALUES = {
    "", "sms-erp-default-secret-key-change-me", "sms-erp-default-jwt-secret-key",
    "change-me", "changeme", "secret",
}


class BaseConfig:
    """Base configuration with shared defaults."""
    # Secrets come only from the environment. create_app() refuses to start
    # (outside tests) when they are missing or a known placeholder, because
    # anyone could then forge access tokens.
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")

    # Database integration switch. Disabled by default: the backend runs without
    # any database connection until DATABASE_ENABLED=true and DATABASE_URL are set
    # (e.g. Google Cloud SQL for PostgreSQL). There is no hardcoded fallback URL.
    DATABASE_ENABLED = env_flag("DATABASE_ENABLED", False)

    # SQLAlchemy
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL") if DATABASE_ENABLED else None
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Connection pool. Opening a fresh TLS connection to a managed/remote Postgres
    # costs several seconds, so connections are kept warm for much longer and TCP
    # keepalives stop idle ones being dropped by the network in between.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,          # 30 min (was 5 min → reconnect storms)
        "pool_size": 3,
        "max_overflow": 5,
        "pool_timeout": 30,
        "connect_args": {
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
            "connect_timeout": 15,
            "application_name": "school-erp",
        },
    }

    # Razorpay payment gateway (fee payments). The Key ID is public and is also
    # exposed to the browser via VITE_RAZORPAY_KEY_ID; the Key Secret is used only
    # server-side for order creation and signature verification — never sent to the
    # client. Leave unset to keep the frontend's demo/test checkout behaviour.
    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    # Secret configured on the Razorpay dashboard webhook (HMAC of the raw body).
    RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    # auto (default): live when both keys are set, otherwise disabled.
    # live: real Razorpay Checkout (startup fails without both keys).
    # mock: simulated gateway for development/demo — must be chosen explicitly;
    #       every payment/receipt is flagged as a TEST payment.
    # disabled: fees are visible but online payment is switched off.
    PAYMENT_MODE = (os.getenv("PAYMENT_MODE", "auto") or "auto").strip().lower()
    # Optional local PNG/JPEG printed on PDF receipts (never fetched over the network).
    RECEIPT_LOGO_PATH = os.getenv("RECEIPT_LOGO_PATH", "").strip()

    # Development/test authentication (database-free). Only honoured when
    # DATABASE_ENABLED is false and never in production.
    DEV_AUTH_ENABLED = env_flag("DEV_AUTH_ENABLED", False)
    DEV_AUTH_ACCOUNTS_FILE = os.getenv(
        "DEV_AUTH_ACCOUNTS_FILE",
        os.path.join(BACKEND_DIR, "dev_auth_accounts.json"),
    )

    # JWT Lifecycles (15 minutes access, 7 days refresh)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    # Access tokens are accepted only from the Authorization header, so no API
    # call can be triggered cross-site with ambient cookies (CSRF). The refresh
    # token lives in an HttpOnly cookie scoped to /api/v1/auth; /auth/refresh
    # validates it manually and requires a custom header when it comes from the
    # cookie (see auth.py).
    JWT_TOKEN_LOCATION = ["headers"]
    JWT_COOKIE_CSRF_PROTECT = False
    JWT_REFRESH_COOKIE_PATH = "/api/v1/auth"
    # Refresh-cookie attributes. When the frontend and API are on different
    # sites (e.g. Vercel + Render) set REFRESH_COOKIE_SAMESITE=None so browsers
    # send the cookie on the cross-site refresh call; it is then always Secure.
    JWT_COOKIE_SAMESITE = os.getenv("REFRESH_COOKIE_SAMESITE", "Lax").strip().capitalize() or "Lax"
    JWT_COOKIE_SECURE = env_flag("REFRESH_COOKIE_SECURE", True)

    # Number of reverse proxies in front of the app (Render/Heroku/Nginx: 1).
    # Needed so rate limits and audit logs see the real client IP instead of
    # the proxy's; keep 0 when the app is reached directly (otherwise clients
    # could spoof X-Forwarded-For).
    TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", "0") or 0)

    # Account protection: lock an account after repeated failed logins.
    LOGIN_MAX_FAILED_ATTEMPTS = int(os.getenv("LOGIN_MAX_FAILED_ATTEMPTS", "5") or 5)
    LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15") or 15)

    # Reject request bodies over 10 MB (synced records are capped at 2 MB each).
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024

    # Redis (optional). When REDIS_URL is unset, everything falls back to
    # in-process memory so the app runs unchanged without Redis.
    REDIS_URL = os.getenv("REDIS_URL", "").strip()

    # Rate Limiter — shared Redis store when REDIS_URL is set, else in-memory.
    RATELIMIT_DEFAULT = "120/minute"
    RATELIMIT_STORAGE_URI = REDIS_URL or "memory://"

    # Caching — Redis-backed when available, else a simple in-process cache.
    CACHE_TYPE = "RedisCache" if REDIS_URL else "SimpleCache"
    CACHE_REDIS_URL = REDIS_URL or None
    CACHE_DEFAULT_TIMEOUT = 300

    # CORS
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ALLOWED_ORIGINS",
            "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173",
        ).split(",")
    ]


class DevelopmentConfig(BaseConfig):
    """Configuration for local development."""
    DEBUG = True
    TESTING = False
    # Local http:// development can't use Secure cookies.
    JWT_COOKIE_SECURE = env_flag("REFRESH_COOKIE_SECURE", False)


class TestingConfig(BaseConfig):
    """Configuration for automated test execution."""
    DEBUG = False
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL") if BaseConfig.DATABASE_ENABLED else None
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }
    RATELIMIT_ENABLED = False
    JWT_COOKIE_SECURE = False
    # Fixed test-only secrets (never used outside the testing config).
    # Tests never see the real gateway keys from .env; payment tests set their
    # own fake keys/mode per test and mock every Razorpay HTTP call.
    RAZORPAY_KEY_ID = os.getenv("TEST_RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = os.getenv("TEST_RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET = os.getenv("TEST_RAZORPAY_WEBHOOK_SECRET", "")
    PAYMENT_MODE = (os.getenv("TEST_PAYMENT_MODE", "auto") or "auto").strip().lower()
    SECRET_KEY = os.getenv("SECRET_KEY") or "test-only-secret-key-not-for-production-use"
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or "test-only-jwt-secret-key-not-for-production-use"


class ProductionConfig(BaseConfig):
    """Configuration for production deployment."""
    DEBUG = False
    TESTING = False
    JWT_COOKIE_SECURE = True


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}

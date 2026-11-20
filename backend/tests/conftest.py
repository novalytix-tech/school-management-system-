import pytest
import os
import sys

# Ensure backend root is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app
from app.config import config_by_name
from app.extensions import db
from seed import seed_database


@pytest.fixture(scope="session")
def app():
    """Create application configured for testing and initialize test seed data."""
    testing_cfg = config_by_name["testing"]
    if not testing_cfg.DATABASE_ENABLED or not testing_cfg.SQLALCHEMY_DATABASE_URI:
        pytest.skip(
            "BLOCKED BY DATABASE DISCONNECTION: set DATABASE_ENABLED=true and "
            "TEST_DATABASE_URL to run database-backed tests."
        )
    _app = create_app("testing")

    with _app.app_context():
        # The disposable test database starts empty, so build the schema from the
        # current models before seeding. create_all is idempotent and only ever
        # runs against TEST_DATABASE_URL (never the production DATABASE_URL).
        db.create_all()
        # Seed test database once per session
        seed_database(_app)

    # Yield outside the setup context: holding one app context open for the whole
    # session made every test request share a single SQLAlchemy session, so
    # requests read stale identity-map rows instead of fresh DB state like they
    # do in production (one app context per request).
    yield _app


@pytest.fixture(scope="function")
def client(app):
    """Test HTTP client."""
    return app.test_client()


@pytest.fixture(scope="function")
def session(app):
    """Transactional database session fixture with automatic rollback."""
    with app.app_context():
        connection = db.engine.connect()
        transaction = connection.begin()
        
        yield db.session

        transaction.rollback()
        connection.close()


@pytest.fixture(scope="function")
def auth_headers(client):
    """Helper fixture to obtain JWT Authorization header for a given persona."""
    def _login(email: str, password: str = "SchoolERP@Demo2026!"):
        res = client.post("/api/v1/auth/login", json={
            "email": email,
            "password": password
        })
        data = res.get_json()
        assert res.status_code == 200, f"Login failed for {email}: {data}"
        token = data["data"]["access_token"]
        return {"Authorization": f"Bearer {token}"}
    return _login

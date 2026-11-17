import hashlib
import re
import secrets
from typing import Tuple, Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

# Argon2id password hasher with RFC 9106 recommended parameters
ph = PasswordHasher(
    time_cost=3,          # 3 iterations
    memory_cost=65536,    # 64 MiB
    parallelism=4,        # 4 threads
    hash_len=32,          # 32-byte hash
    salt_len=16,          # 16-byte salt
)


def hash_password(plain_password: str) -> str:
    """Generate an Argon2id salted hash for a plain password."""
    return ph.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against an Argon2id hash."""
    try:
        return ph.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_needs_rehash(hashed_password: str) -> bool:
    """Check if the password hash needs upgrading."""
    try:
        return ph.check_needs_rehash(hashed_password)
    except Exception:
        return True


def validate_password_policy(password: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that password meets enterprise security requirements:
    - At least 8 characters
    - At least 1 uppercase letter
    - At least 1 lowercase letter
    - At least 1 numeric digit
    - At least 1 special character
    """
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter."
    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit."
    if not re.search(r"[!@#$%^&*()_\-+=\[\]{}|;:,.<>?/~`]", password):
        return False, "Password must contain at least one special character."
    return True, None


def generate_secure_token(nbytes: int = 32) -> str:
    """Generate a cryptographically secure URL-safe random string."""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw_token: str) -> str:
    """Compute a SHA-256 hash for database token storage."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


"""
Tenant-aware response caching helpers.

These wrap Flask-Caching so that read-heavy GET endpoints can be served from
Redis (when ``REDIS_URL`` is set) or the in-process fallback cache, without ever
leaking one institution's data to another and without serving stale data after a
write.

Strategy — generational / versioned keys
-----------------------------------------
Each (namespace, tenant) pair owns a small integer "version" stored in the cache.
Every cached response key embeds that version. A write simply bumps the version,
so every previously cached key for that namespace+tenant becomes unreachable and
expires on its own. This needs no pattern-based deletes, so it behaves identically
on RedisCache and SimpleCache.
"""

import functools
import hashlib
from typing import Callable, Optional

from flask import request

from app.extensions import cache
from app.utils.tenant import get_current_tenant_id

# Version counters must not expire on their own, or bumps would be lost and stale
# responses could reappear. 0 == "never expire" in Flask-Caching.
_VERSION_TIMEOUT = 0


def _tenant_key() -> str:
    """Stable per-institution component of a cache key (never raises)."""
    try:
        tid = get_current_tenant_id()
    except Exception:
        tid = None
    return str(tid) if tid else "global"


def _version_key(namespace: str, tenant: str) -> str:
    return f"cachever:{namespace}:{tenant}"


def _current_version(namespace: str, tenant: str) -> int:
    """Read (and lazily initialise) the generation counter for namespace+tenant."""
    vkey = _version_key(namespace, tenant)
    try:
        version = cache.get(vkey)
        if version is None:
            version = 1
            cache.set(vkey, version, timeout=_VERSION_TIMEOUT)
        return int(version)
    except Exception:
        # Any cache backend hiccup must not break the request.
        return 1


def bump_version(namespace: str, tenant: Optional[str] = None) -> None:
    """Invalidate every cached response in a namespace for the current tenant."""
    tenant = tenant or _tenant_key()
    vkey = _version_key(namespace, tenant)
    try:
        version = cache.get(vkey) or 1
        cache.set(vkey, int(version) + 1, timeout=_VERSION_TIMEOUT)
    except Exception:
        pass


def invalidate_user_display_caches(institution_id) -> None:
    """
    Drop cached responses that embed user display names for one institution.

    The "academics" namespace caches class lists that include the class
    teacher's name, so any user name/profile edit must call this (e.g. from the
    users/auth profile-update endpoints) or the old name is served until expiry.
    """
    if institution_id:
        bump_version("academics", str(institution_id))


def _make_key(namespace: str) -> str:
    tenant = _tenant_key()
    version = _current_version(namespace, tenant)
    # full_path captures path + query string, so different filters cache separately.
    raw = f"{namespace}:{tenant}:v{version}:{request.full_path}"
    return "resp:" + hashlib.md5(raw.encode("utf-8")).hexdigest()


def _is_cacheable(result) -> bool:
    """Only cache clean 200 OK responses — never errors or 503s.

    Views may return a Response or a ``(body, status[, headers])`` tuple; the
    tuple's explicit status wins over the body Response's default 200.
    """
    if isinstance(result, tuple):
        if len(result) >= 2 and isinstance(result[1], int) and not isinstance(result[1], bool):
            status = result[1]
        else:
            status = getattr(result[0], "status_code", 200) if result else 200
    else:
        status = getattr(result, "status_code", 200)
    return status == 200


def cached_response(namespace: str, timeout: int = 120) -> Callable:
    """
    Decorator: cache a GET view's response per institution + query string.

    ``namespace`` groups related endpoints so a single write can invalidate them
    all via :func:`bump_version`. Caching is skipped for non-GET requests and for
    any response that is not a 200, so write paths and error envelopes are never
    served from the cache.
    """

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if request.method != "GET":
                return view(*args, **kwargs)

            try:
                key = _make_key(namespace)
                cached = cache.get(key)
                if cached is not None:
                    return cached
            except Exception:
                key = None

            result = view(*args, **kwargs)

            try:
                if key is not None:
                    if _is_cacheable(result):
                        cache.set(key, result, timeout=timeout)
            except Exception:
                pass

            return result

        return wrapper

    return decorator


def invalidate_on_write(*namespaces: str) -> Callable:
    """
    Blueprint ``after_request`` hook factory.

    Register the returned function with ``@bp.after_request`` so that any
    successful non-GET request to that blueprint bumps the given namespaces'
    versions, dropping all their cached responses for the current tenant.
    """

    def hook(response):
        try:
            if request.method not in ("GET", "HEAD", "OPTIONS") and 200 <= response.status_code < 400:
                for ns in namespaces:
                    bump_version(ns)
        except Exception:
            pass
        return response

    return hook

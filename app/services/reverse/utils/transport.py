"""
Transport compatibility helpers for reverse requests.
"""

from typing import Any, Dict, Optional

from app.core.logger import logger


def normalize_proxies(proxies: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not isinstance(proxies, dict):
        return None
    out: Dict[str, str] = {}
    for key in ("http", "https"):
        value = proxies.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out or None


def _proxy_enabled(proxies: Optional[Dict[str, str]]) -> bool:
    return normalize_proxies(proxies) is not None


def is_tls_proxy_impersonation_error(error: Exception) -> bool:
    """Detect curl_cffi TLS failures caused by proxy+impersonate incompatibility."""
    msg = str(error).lower()
    return (
        "curl: (35)" in msg
        and "tls connect error" in msg
        and "invalid library" in msg
    )


def should_retry_without_impersonate(
    error: Exception, browser: Optional[str], proxies: Optional[Dict[str, str]]
) -> bool:
    if not browser:
        return False
    if not _proxy_enabled(proxies):
        return False
    return is_tls_proxy_impersonation_error(error)


async def request_with_impersonation_fallback(
    session: Any,
    method: str,
    url: str,
    *,
    browser: Optional[str],
    proxies: Optional[Dict[str, str]],
    log_prefix: str,
    **kwargs: Any,
) -> Any:
    """
    Execute request with impersonation first, then fallback to non-impersonated request
    when a known proxy TLS compatibility error is hit.
    """
    request_fn = getattr(session, method)
    proxies = normalize_proxies(proxies)
    try:
        if browser:
            return await request_fn(
                url,
                proxies=proxies,
                impersonate=browser,
                **kwargs,
            )
        return await request_fn(
            url,
            proxies=proxies,
            **kwargs,
        )
    except Exception as e:
        if should_retry_without_impersonate(e, browser, proxies):
            logger.warning(
                f"{log_prefix}: proxy TLS incompatibility with impersonate='{browser}', "
                "retrying once without impersonate"
            )
            return await request_fn(
                url,
                proxies=proxies,
                **kwargs,
            )
        raise


__all__ = [
    "is_tls_proxy_impersonation_error",
    "should_retry_without_impersonate",
    "normalize_proxies",
    "request_with_impersonation_fallback",
]

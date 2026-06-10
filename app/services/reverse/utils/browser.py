"""Browser impersonation helpers for Grok reverse requests."""

import re
from typing import Optional

from app.core.config import get_config
from app.core.logger import logger

DEFAULT_BROWSER_PROFILE = "chrome142"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

_FALLBACK_TARGET_MAP = {
    "chrome": "chrome142",
    "edge": "edge101",
    "firefox": "firefox144",
    "safari": "safari2601",
    "safari_ios": "safari260_ios",
    "chrome_android": "chrome131_android",
}

_FALLBACK_SUPPORTED = {
    "edge99",
    "edge101",
    "chrome99",
    "chrome100",
    "chrome101",
    "chrome104",
    "chrome107",
    "chrome110",
    "chrome116",
    "chrome119",
    "chrome120",
    "chrome123",
    "chrome124",
    "chrome131",
    "chrome133a",
    "chrome136",
    "chrome142",
    "chrome99_android",
    "chrome131_android",
    "firefox133",
    "firefox135",
    "firefox144",
}


def _supported_profiles() -> set[str]:
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        values = getattr(BrowserTypeLiteral, "__args__", None)
        if values:
            return {str(value) for value in values}
    except Exception:
        pass
    return set(_FALLBACK_SUPPORTED)


def _target_map() -> dict[str, str]:
    try:
        from curl_cffi.requests.impersonate import REAL_TARGET_MAP

        if isinstance(REAL_TARGET_MAP, dict):
            return {str(k): str(v) for k, v in REAL_TARGET_MAP.items()}
    except Exception:
        pass
    return dict(_FALLBACK_TARGET_MAP)


def chromium_major_from_browser(browser: Optional[str]) -> Optional[str]:
    if not browser:
        return None
    match = re.search(r"(?:chrome|chromium)(\d{2,3})", str(browser).lower())
    if match:
        return match.group(1)
    return None


def chromium_major_from_user_agent(user_agent: Optional[str]) -> Optional[str]:
    if not user_agent:
        return None
    for pattern in (r"Chrome/(\d+)", r"Chromium/(\d+)", r"Edg/(\d+)"):
        match = re.search(pattern, str(user_agent))
        if match:
            return match.group(1)
    return None


def resolve_browser_profile(browser: Optional[str] = None) -> str:
    """Return a curl-cffi supported browser profile, resolving aliases safely."""
    raw = str(browser or "").strip().lower() or DEFAULT_BROWSER_PROFILE
    targets = _target_map()
    supported = _supported_profiles()

    if raw in targets:
        return targets[raw]
    if raw in supported:
        return raw
    if raw.startswith(("chrome", "chromium")):
        return targets.get("chrome", DEFAULT_BROWSER_PROFILE)
    if raw.startswith("edge"):
        return targets.get("edge", "edge101")
    if raw.startswith("firefox"):
        return targets.get("firefox", "firefox144")
    if raw.startswith("safari"):
        return targets.get("safari", "safari2601")
    return targets.get("chrome", DEFAULT_BROWSER_PROFILE)


def coerce_user_agent_to_browser(
    user_agent: Optional[str],
    browser: Optional[str],
) -> str:
    """Keep User-Agent major version aligned with the curl-cffi profile."""
    profile = resolve_browser_profile(browser)
    target_major = chromium_major_from_browser(profile)
    raw = str(user_agent or "").strip()

    if not target_major:
        return raw or DEFAULT_USER_AGENT

    if raw and re.search(r"(Chrome|Chromium)/\d+(?:\.\d+){0,3}", raw):
        return re.sub(
            r"(Chrome|Chromium)/\d+(?:\.\d+){0,3}",
            rf"\1/{target_major}.0.0.0",
            raw,
            count=1,
        )

    return re.sub(
        r"Chrome/\d+(?:\.\d+){0,3}",
        f"Chrome/{target_major}.0.0.0",
        DEFAULT_USER_AGENT,
        count=1,
    )


def get_effective_browser() -> str:
    configured = get_config("proxy.browser") or DEFAULT_BROWSER_PROFILE
    effective = resolve_browser_profile(configured)
    if str(configured).strip().lower() != effective:
        logger.debug(
            f"Resolved browser profile {configured!r} -> {effective!r}"
        )
    return effective


def get_effective_user_agent() -> str:
    configured_browser = get_config("proxy.browser") or DEFAULT_BROWSER_PROFILE
    effective_browser = resolve_browser_profile(configured_browser)
    configured_ua = get_config("proxy.user_agent") or DEFAULT_USER_AGENT
    effective_ua = coerce_user_agent_to_browser(configured_ua, effective_browser)
    configured_major = chromium_major_from_user_agent(configured_ua)
    effective_major = chromium_major_from_browser(effective_browser)
    if configured_major and effective_major and configured_major != effective_major:
        logger.debug(
            "Adjusted User-Agent Chrome major "
            f"{configured_major} -> {effective_major} for {effective_browser}"
        )
    return effective_ua


__all__ = [
    "chromium_major_from_browser",
    "chromium_major_from_user_agent",
    "coerce_user_agent_to_browser",
    "get_effective_browser",
    "get_effective_user_agent",
    "resolve_browser_profile",
]

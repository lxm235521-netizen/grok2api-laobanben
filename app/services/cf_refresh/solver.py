"""
通过 FlareSolverr 自动获取 cf_clearance

FlareSolverr 是一个 Docker 服务，内部运行 Chrome 浏览器，
自动处理 Cloudflare 挑战（包括 Turnstile），无需 GUI。
"""

import asyncio
import json
from typing import Optional, Dict
from urllib.parse import unquote, urlparse, urlunparse
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from loguru import logger

from app.services.reverse.utils.browser import (
    coerce_user_agent_to_browser,
    resolve_browser_profile,
)

from .config import GROK_URL, get_timeout, get_proxy, get_flaresolverr_url


def _extract_all_cookies(cookies: list[dict]) -> str:
    """将 FlareSolverr 返回 of cookie 列表转换为字符串格式"""
    return "; ".join([f"{c.get('name')}={c.get('value')}" for c in cookies])


def _extract_cookie_value(cookies: list[dict], name: str) -> str:
    for cookie in cookies:
        if cookie.get("name") == name:
            return cookie.get("value") or ""
    return ""


def _extract_user_agent(solution: dict) -> str:
    """从 FlareSolverr 的 solution 中提取 User-Agent"""
    return solution.get("userAgent", "")


def _extract_browser_profile(user_agent: str) -> str:
    """从 User-Agent 提取 chromeXXX 格式的指纹识别号"""
    import re
    match = re.search(r"Chrome/(\d+)", user_agent)
    if match:
        candidate = f"chrome{match.group(1)}"
        supported_profile = resolve_browser_profile(candidate)
        if supported_profile == candidate:
            return supported_profile
        logger.warning(
            f"FlareSolverr returned unsupported browser profile {candidate}; "
            f"falling back to {supported_profile}"
        )
        return supported_profile
    return resolve_browser_profile("chrome")


def _build_proxy_payloads(proxy_url: str) -> list[Dict[str, str]]:
    """Build FlareSolverr proxy objects, preserving URL-auth SOCKS support."""
    if not proxy_url:
        return []

    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return [{"url": proxy_url}]

    clean_netloc = parsed.hostname
    if parsed.port:
        clean_netloc = f"{clean_netloc}:{parsed.port}"

    scheme = parsed.scheme.lower()
    if scheme == "socks":
        candidate_schemes = ("socks5", "socks5h")
    elif scheme == "socks5":
        candidate_schemes = ("socks5", "socks5h")
    elif scheme == "socks5h":
        candidate_schemes = ("socks5h", "socks5")
    elif scheme == "socks4":
        candidate_schemes = ("socks4", "socks4a")
    elif scheme == "socks4a":
        candidate_schemes = ("socks4a", "socks4")
    else:
        candidate_schemes = (scheme,)

    payloads: list[Dict[str, str]] = []
    seen: set[str] = set()

    def add_payload(payload: Dict[str, str]) -> None:
        key = json.dumps(payload, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        payloads.append(payload)

    for candidate in candidate_schemes:
        embedded_url = urlunparse((candidate, parsed.netloc, "", "", "", ""))
        add_payload({"url": embedded_url})

    return payloads


def _build_proxy_payload(proxy_url: str) -> Optional[Dict[str, str]]:
    payloads = _build_proxy_payloads(proxy_url)
    return payloads[0] if payloads else None


def _redact_proxy(proxy_payload: Optional[Dict[str, str]]) -> str:
    if not proxy_payload:
        return "direct"
    redacted = {
        key: ("<redacted>" if key in {"username", "password"} else _redact_url(value))
        for key, value in proxy_payload.items()
    }
    return json.dumps(redacted, ensure_ascii=False)


def _redact_url(value: str) -> str:
    parsed = urlparse(str(value))
    if not parsed.scheme or "@" not in parsed.netloc:
        return str(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, f"<redacted>@{host}", parsed.path, "", "", ""))


def _browser_error_summary(solution: dict) -> str:
    """Return a short Chromium network error summary if FlareSolverr hit one."""
    import re

    text = solution.get("response") or ""
    if not isinstance(text, str) or not text:
        return ""
    compact = re.sub(r"\s+", " ", text)
    plain = re.sub(r"<[^>]+>", " ", compact)
    plain = re.sub(r"\s+", " ", plain).strip()
    match = re.search(
        r"(ERR_[A-Z0-9_]+|This site can(?:'|’)t be reached|No internet|proxy)",
        plain,
        re.IGNORECASE,
    )
    if not match:
        return ""
    start = max(0, match.start() - 80)
    return plain[start : match.start() + 240]


async def _post_flaresolverr(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib_request.Request(url, data=body, method="POST", headers=headers)

    def _post():
        with urllib_request.urlopen(req, timeout=timeout + 30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.to_thread(_post)


async def solve_cf_challenge() -> Optional[Dict[str, str]]:
    """
    通过 FlareSolverr 访问 grok.com，自动过 CF 挑战，提取 cf_clearance。

    Returns:
        成功时返回 {"cookies": "...", "user_agent": "..."}，失败返回 None
    """
    flaresolverr_url = get_flaresolverr_url()
    cf_timeout = get_timeout()
    proxy = get_proxy()

    if not flaresolverr_url:
        logger.error("FlareSolverr 地址未配置，无法刷新 cf_clearance")
        return None

    url = f"{flaresolverr_url.rstrip('/')}/v1"

    base_payload = {
        "cmd": "request.get",
        "url": GROK_URL,
        "maxTimeout": cf_timeout * 1000,
    }

    attempts: list[tuple[str, dict]] = []
    if proxy:
        for proxy_payload in _build_proxy_payloads(proxy):
            proxied_payload = dict(base_payload)
            proxied_payload["proxy"] = proxy_payload
            scheme = urlparse(proxy_payload.get("url", "")).scheme or "proxy"
            attempts.append((f"proxy:{scheme}", proxied_payload))
    attempts.append(("direct", dict(base_payload)))

    logger.info(f"正在通过 FlareSolverr 访问 {GROK_URL} ...")
    logger.debug(f"FlareSolverr 地址: {url}")
    last_error = ""

    try:
        for label, payload in attempts:
            proxy_payload = payload.get("proxy") if isinstance(payload, dict) else None
            logger.debug(
                f"FlareSolverr attempt={label}, proxy={_redact_proxy(proxy_payload)}"
            )
            try:
                result = await _post_flaresolverr(url, payload, cf_timeout)
            except HTTPError as e:
                body_text = e.read().decode("utf-8", "replace")[:500]
                last_error = f"HTTP {e.code}: {body_text}"
                logger.warning(
                    f"FlareSolverr attempt={label} failed: {last_error}"
                )
                continue
            except URLError as e:
                last_error = str(e.reason)
                logger.warning(
                    f"FlareSolverr attempt={label} connection failed: {last_error}"
                )
                continue

            status = result.get("status", "")
            if status != "ok":
                message = result.get("message", "unknown error")
                last_error = f"{status} - {message}"
                logger.warning(
                    f"FlareSolverr attempt={label} returned: {last_error}"
                )
                continue

            solution = result.get("solution", {})
            cookies = solution.get("cookies", [])

            if not cookies:
                browser_error = _browser_error_summary(solution)
                if browser_error:
                    last_error = f"browser network error: {browser_error}"
                else:
                    last_error = "no cookies returned"
                logger.warning(
                    f"FlareSolverr attempt={label} produced no cookies: {last_error}"
                )
                continue

            cookie_str = _extract_all_cookies(cookies)
            clearance = _extract_cookie_value(cookies, "cf_clearance")
            ua = _extract_user_agent(solution)
            browser = _extract_browser_profile(ua)
            ua = coerce_user_agent_to_browser(ua, browser)
            logger.info(
                f"成功获取 cookies (数量: {len(cookies)}), 指纹: {browser}, attempt={label}"
            )

            return {
                "cookies": cookie_str,
                "cf_clearance": clearance,
                "user_agent": ua,
                "browser": browser,
            }

        logger.error(f"FlareSolverr 未能获取 cookies: {last_error or 'unknown error'}")
        return None

    except HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")[:300]
        logger.error(f"FlareSolverr 请求失败: {e.code} - {body_text}")
        return None
    except URLError as e:
        logger.error(f"无法连接 FlareSolverr ({flaresolverr_url}): {e.reason}")
        logger.info("请确认 FlareSolverr 服务已启动: docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
        return None
    except Exception as e:
        logger.error(f"请求异常: {e}")
        return None

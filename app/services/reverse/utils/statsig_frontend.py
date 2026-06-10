"""Auto-discover Grok frontend statsig animation indices."""

import asyncio
import re
import time
from collections import OrderedDict
from typing import Optional

from curl_cffi import requests

from app.core.config import config, get_config
from app.core.logger import logger
from app.core.proxy_pool import normalize_proxy_url
from app.services.reverse.utils.browser import (
    get_effective_browser,
    get_effective_user_agent,
)

GROK_C_URL = "https://grok.com/c"
CDN_BASE_URL = "https://cdn.grok.com/_next/"
DEFAULT_BROWSER = "chrome142"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
SCRIPT_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
BOT_SIGN_MODULE_RE = re.compile(
    r"(?:\.A\((\d+)\).{0,500}?botoxSign|botoxSign.{0,500}?\.A\((\d+)\))",
    re.S,
)
LET_ARRAY_RE = re.compile(r"let\s*\[[^\]]{1,80}\]\s*=\s*\[")
INDEX_REF_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\[(\d{1,3})\]")

_dynamic_indices: Optional[tuple[int, int, int, int]] = None
_last_attempt_at = 0.0
_last_success_at = 0.0
_lock: asyncio.Lock | None = None
_task: asyncio.Task | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def format_statsig_indices(indices: tuple[int, int, int, int]) -> str:
    return ",".join(str(index) for index in indices)


def get_dynamic_statsig_indices() -> Optional[tuple[int, int, int, int]]:
    return _dynamic_indices


def _proxy_kwargs(proxy_url: Optional[str]) -> dict:
    if not proxy_url:
        return {}
    normalized = normalize_proxy_url(proxy_url)
    scheme = normalized.split(":", 1)[0].lower()
    if scheme.startswith("socks"):
        return {"proxy": normalized}
    return {"proxies": {"http": normalized, "https": normalized}}


def _script_urls(html: str) -> list[str]:
    urls: list[str] = []
    for src in SCRIPT_RE.findall(html or ""):
        if "/_next/static/chunks/" not in src or not src.endswith(".js"):
            continue
        if src.startswith("https://"):
            url = src
        elif src.startswith("//"):
            url = f"https:{src}"
        elif src.startswith("/"):
            url = f"https://cdn.grok.com{src}"
        else:
            url = f"https://cdn.grok.com/{src}"
        if url not in urls:
            urls.append(url)
    return urls


def _chunk_url(path: str) -> str:
    if path.startswith("https://"):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    if path.startswith("/_next/"):
        return f"https://cdn.grok.com{path}"
    if path.startswith("static/chunks/"):
        return f"{CDN_BASE_URL}{path}"
    if path.startswith("/"):
        return f"https://cdn.grok.com{path}"
    return f"{CDN_BASE_URL}{path}"


def extract_signer_module_id(script_text: str) -> Optional[str]:
    match = BOT_SIGN_MODULE_RE.search(script_text or "")
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


def extract_dynamic_chunk_paths(script_text: str, module_id: str) -> list[str]:
    paths: list[str] = []
    marker_re = re.compile(rf"(?<![\w$]){re.escape(module_id)}\s*,\s*[\w$]+\s*=>")
    for match in marker_re.finditer(script_text or ""):
        snippet = script_text[match.start() : match.start() + 1500]
        for path in re.findall(r"[\"']([^\"']*static/chunks/[^\"']+\.js)[\"']", snippet):
            if path not in paths:
                paths.append(path)
    return paths


def _extract_bracket_content(text: str, open_index: int) -> Optional[str]:
    depth = 0
    quote = ""
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    return None


def _index_tuple_from_expression(expression: str) -> Optional[tuple[int, int, int, int]]:
    refs_by_name: OrderedDict[str, list[int]] = OrderedDict()
    for name, raw_index in INDEX_REF_RE.findall(expression or ""):
        index = int(raw_index)
        if index >= 64:
            continue
        refs_by_name.setdefault(name, [])
        if index not in refs_by_name[name]:
            refs_by_name[name].append(index)

    for refs in refs_by_name.values():
        if len(refs) >= 4 and all(0 <= item < 48 for item in refs[:4]):
            return tuple(refs[:4])  # type: ignore[return-value]
    return None


def parse_statsig_indices_from_signer_chunk(
    chunk_text: str,
) -> Optional[tuple[int, int, int, int]]:
    """Extract byte indices from Grok's obfuscated client:botoxSign chunk."""
    text = chunk_text or ""
    epoch_positions = [match.start() for match in re.finditer("0x644f6370", text)]
    windows: list[tuple[int, int]] = []
    if epoch_positions:
        for pos in epoch_positions:
            windows.append((max(0, pos - 1200), min(len(text), pos + 14000)))
    else:
        windows.append((0, len(text)))

    checked_starts: set[int] = set()
    for start, end in windows:
        window = text[start:end]
        for match in LET_ARRAY_RE.finditer(window):
            open_index = start + match.end() - 1
            if open_index in checked_starts:
                continue
            checked_starts.add(open_index)
            expression = _extract_bracket_content(text, open_index)
            indices = _index_tuple_from_expression(expression or "")
            if indices:
                return indices
    return None


def _fetch_text(
    session: requests.Session,
    url: str,
    *,
    proxy_url: Optional[str],
    headers: dict,
    timeout: float = 25,
) -> str:
    response = session.get(
        url,
        headers=headers,
        timeout=timeout,
        **_proxy_kwargs(proxy_url),
    )
    if response.status_code != 200:
        raise RuntimeError(f"GET {url} failed with status {response.status_code}")
    return response.text or ""


def _discover_statsig_indices_sync() -> tuple[int, int, int, int]:
    browser = str(get_effective_browser() or DEFAULT_BROWSER)
    user_agent = str(get_effective_user_agent() or DEFAULT_USER_AGENT)
    cookie = str(get_config("proxy.cf_cookies") or "")
    configured_proxy = get_config("proxy.base_proxy_url") or None
    proxy_attempts: list[Optional[str]] = [None]
    if configured_proxy:
        normalized = normalize_proxy_url(str(configured_proxy))
        if normalized not in proxy_attempts:
            proxy_attempts.append(normalized)

    headers = {
        "user-agent": user_agent,
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,image/apng,*/*;q=0.8"
        ),
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["cookie"] = cookie

    last_error: Optional[Exception] = None
    for proxy_url in proxy_attempts:
        try:
            session = requests.Session(impersonate=browser, default_headers=False)
            html = _fetch_text(
                session,
                GROK_C_URL,
                proxy_url=proxy_url,
                headers=headers,
                timeout=35,
            )
            script_urls = _script_urls(html)
            module_id: Optional[str] = None
            dynamic_paths: list[str] = []
            fetched_scripts: list[str] = []
            script_headers = {"user-agent": user_agent, "accept": "*/*"}

            for script_url in script_urls:
                try:
                    text = _fetch_text(
                        session,
                        script_url,
                        proxy_url=proxy_url,
                        headers=script_headers,
                    )
                except Exception:
                    continue
                fetched_scripts.append(text)
                module_id = module_id or extract_signer_module_id(text)
                if not module_id:
                    continue
                for candidate in fetched_scripts:
                    for path in extract_dynamic_chunk_paths(candidate, module_id):
                        if path not in dynamic_paths:
                            dynamic_paths.append(path)
                if dynamic_paths:
                    break

            if not module_id:
                raise RuntimeError("client:botoxSign module id not found")
            if not dynamic_paths:
                raise RuntimeError(f"dynamic chunk path for module {module_id} not found")

            for path in dynamic_paths:
                chunk_url = _chunk_url(path)
                try:
                    chunk_text = _fetch_text(
                        session,
                        chunk_url,
                        proxy_url=proxy_url,
                        headers=script_headers,
                    )
                except Exception as exc:
                    last_error = exc
                    continue
                indices = parse_statsig_indices_from_signer_chunk(chunk_text)
                if indices:
                    logger.info(
                        "Grok statsig frontend indices discovered: "
                        f"indices={format_statsig_indices(indices)}, "
                        f"module={module_id}, chunk={path}, "
                        f"proxy={'direct' if not proxy_url else 'configured'}"
                    )
                    return indices
            raise RuntimeError("statsig indices not found in signer chunk")
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Grok statsig frontend discovery failed "
                f"via {'direct' if not proxy_url else 'configured proxy'}: {exc}"
            )

    raise RuntimeError(f"Grok statsig frontend discovery failed: {last_error}")


async def refresh_statsig_indices(
    *,
    force: bool = False,
    reason: str = "scheduler",
    persist: bool = True,
) -> Optional[tuple[int, int, int, int]]:
    """Refresh cached/configured statsig indices from the current Grok frontend."""
    global _dynamic_indices, _last_attempt_at, _last_success_at

    if not force and not bool(get_config("app.statsig_auto_refresh", True)):
        return _dynamic_indices

    cooldown = float(get_config("app.statsig_refresh_cooldown", 300) or 300)
    now = time.time()
    if not force and _last_attempt_at and now - _last_attempt_at < cooldown:
        return _dynamic_indices

    async with _get_lock():
        now = time.time()
        if not force and _last_attempt_at and now - _last_attempt_at < cooldown:
            return _dynamic_indices
        _last_attempt_at = now

        try:
            indices = await asyncio.to_thread(_discover_statsig_indices_sync)
        except Exception as exc:
            logger.warning(f"Grok statsig frontend refresh failed ({reason}): {exc}")
            return _dynamic_indices

        old_indices = _dynamic_indices
        _dynamic_indices = indices
        _last_success_at = time.time()
        formatted = format_statsig_indices(indices)

        if persist and formatted != str(get_config("app.statsig_indices") or ""):
            try:
                await config.update({"app": {"statsig_indices": formatted}})
                logger.info(
                    f"Grok statsig config updated from frontend: statsig_indices={formatted}"
                )
            except Exception as exc:
                logger.warning(f"Grok statsig config update failed: {exc}")

        if old_indices != indices:
            logger.info(
                "Grok statsig indices refreshed: "
                f"{format_statsig_indices(old_indices) if old_indices else 'none'} -> "
                f"{formatted} ({reason})"
            )
        return indices


async def _scheduler_loop():
    logger.info(
        "Grok statsig frontend scheduler started "
        f"(interval={get_config('app.statsig_refresh_interval', 1800)}s)"
    )
    while True:
        try:
            if bool(get_config("app.statsig_auto_refresh", True)):
                await refresh_statsig_indices(force=True, reason="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Grok statsig scheduler error: {exc}")

        interval = float(get_config("app.statsig_refresh_interval", 1800) or 1800)
        await asyncio.sleep(max(60.0, interval))


def start():
    global _task
    if _task is not None:
        return
    _task = asyncio.create_task(_scheduler_loop())
    logger.info("Grok statsig frontend background task started")


def stop():
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
        logger.info("Grok statsig frontend background task stopped")


__all__ = [
    "extract_dynamic_chunk_paths",
    "extract_signer_module_id",
    "format_statsig_indices",
    "get_dynamic_statsig_indices",
    "parse_statsig_indices_from_signer_chunk",
    "refresh_statsig_indices",
    "start",
    "stop",
]

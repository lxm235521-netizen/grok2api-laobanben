"""
Reverse interface: app chat conversations.
"""

import inspect
import orjson
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse
from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.proxy_pool import (
    get_current_proxy_from,
    normalize_proxy_url,
    rotate_proxy,
    should_rotate_proxy,
)
from app.core.exceptions import UpstreamException
from app.services.token.service import TokenService
from app.services.reverse.utils.grok_challenge import (
    build_app_chat_challenge,
    format_statsig_indices,
    statsig_index_candidates,
)
from app.services.reverse.utils.statsig_frontend import refresh_statsig_indices
from app.services.reverse.utils.browser import get_effective_browser
from app.services.reverse.utils.headers import build_headers
from app.services.reverse.utils.retry import extract_status_for_retry, retry_on_status

CHAT_API = "https://grok.com/rest/app-chat/conversations/new"
_LAST_PROXY_LOG_STATE: tuple[str, str] | None = None
_ANTIBOT_RETRY_AFTER_SECONDS = 2.5


def _normalize_chat_proxy(proxy_url: str) -> str:
    """Normalize proxy URL for curl-cffi app-chat requests."""
    return normalize_proxy_url(proxy_url)


def _chat_proxy_args(proxy_url: str | None) -> tuple[str | None, dict | None, str, str]:
    if not proxy_url:
        return None, None, "", ""
    normalized_proxy = _normalize_chat_proxy(proxy_url)
    scheme = urlparse(normalized_proxy).scheme.lower()
    if scheme.startswith("socks"):
        return normalized_proxy, None, normalized_proxy, scheme
    return None, {"http": normalized_proxy, "https": normalized_proxy}, normalized_proxy, scheme


def _redact_proxy_url(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    try:
        parsed = urlparse(proxy_url)
        if not parsed.username and not parsed.password:
            return proxy_url
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        return "<redacted>"


def _log_proxy_state_once(base_proxy: str, normalized_proxy: str = "", scheme: str = ""):
    """仅在代理状态变化时记录一次代理配置日志。"""
    global _LAST_PROXY_LOG_STATE

    state = ("enabled", normalized_proxy) if base_proxy else ("direct", "")
    if state == _LAST_PROXY_LOG_STATE:
        return

    _LAST_PROXY_LOG_STATE = state
    if base_proxy:
        logger.info(
            f"AppChatReverse proxy enabled: scheme={scheme}, target={_redact_proxy_url(normalized_proxy)}"
        )
    else:
        logger.info("AppChatReverse proxy is empty, requests will use direct network")


def _retry_after_for_response(status_code: int, content: str) -> float | None:
    """Slow down Grok anti-bot retries so fresh challenges are not burned immediately."""
    if status_code != 403:
        return None
    lowered = (content or "").lower()
    if "anti-bot" in lowered or "request rejected" in lowered:
        return _ANTIBOT_RETRY_AFTER_SECONDS
    return None


class AppChatReverse:
    """/rest/app-chat/conversations/new reverse interface."""

    @staticmethod
    async def _read_error_body(response: Any) -> str:
        """Best-effort read for non-200 upstream responses."""
        readers = (
            "text",
            "atext",
            "read",
            "aread",
        )
        for attr_name in readers:
            attr = getattr(response, attr_name, None)
            if attr is None:
                continue
            try:
                value = attr() if callable(attr) else attr
                if inspect.isawaitable(value):
                    value = await value
                if value is None:
                    continue
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="ignore")
                value = str(value)
                if value:
                    return value
            except Exception:
                continue

        content = getattr(response, "content", None)
        if content:
            try:
                if isinstance(content, bytes):
                    return content.decode("utf-8", errors="ignore")
                return str(content)
            except Exception:
                pass
        return ""

    @staticmethod
    def _resolve_custom_personality() -> Optional[str]:
        """Resolve optional custom personality from app config."""
        value = get_config("app.custom_instruction", "")
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        if not value.strip():
            return None
        return value

    # modelMode → modeId 映射（Grok Web 新 API 格式）
    # 基于浏览器前端 JS 逆向和 API 全量测试验证：
    #   付费 SuperGrok 的多智能体模式需要 modeId 字段才能正常响应
    #   有 modeId 时不发 modelName/modelMode（浏览器前端逻辑）
    _MODE_ID_MAP = {
        "MODEL_MODE_FAST": "fast",
        "MODEL_MODE_EXPERT": "expert",
        "MODEL_MODE_HEAVY": "heavy",
        "MODEL_MODE_GROK_420": "expert",
        "MODEL_MODE_GROK_4_1_THINKING": "expert",
        "MODEL_MODE_GROK_4_1_MINI_THINKING": "expert",
    }

    @staticmethod
    def build_payload(
        message: str,
        model: str,
        mode: str = None,
        file_attachments: List[str] = None,
        tool_overrides: Dict[str, Any] = None,
        model_config_override: Dict[str, Any] = None,
        request_overrides: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Build chat payload for Grok app-chat API."""

        attachments = file_attachments or []

        payload = {
            "deviceEnvInfo": {
                "darkModeEnabled": False,
                "devicePixelRatio": 2,
                "screenHeight": 1329,
                "screenWidth": 2056,
                "viewportHeight": 1083,
                "viewportWidth": 2056,
            },
            "disableMemory": get_config("app.disable_memory"),
            "disableSearch": False,
            "disableSelfHarmShortCircuit": False,
            "disableTextFollowUps": False,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "enableSideBySide": True,
            "fileAttachments": attachments,
            "forceConcise": False,
            "forceSideBySide": False,
            "imageAttachments": [],
            "imageGenerationCount": 2,
            "isAsyncChat": False,
            "isReasoning": False,
            "message": message,
            "modelMode": mode,
            "modelName": model,
            "responseMetadata": {
                "requestModelDetails": {"modelId": model},
            },
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "sendFinalMetadata": True,
            "temporary": get_config("app.temporary"),
            "toolOverrides": tool_overrides or {},
        }

        # 优先使用 modeId（Grok 新 API 格式，付费号多智能体模式必需）
        # 有 modeId 时移除 modelName/modelMode（浏览器前端逻辑）
        mode_id = AppChatReverse._MODE_ID_MAP.get(mode)
        if mode_id:
            payload["modeId"] = mode_id
            payload.pop("modelName", None)
            payload.pop("modelMode", None)

        custom_personality = AppChatReverse._resolve_custom_personality()
        if custom_personality is not None:
            payload["customPersonality"] = custom_personality

        if model_config_override:
            payload["responseMetadata"]["modelConfigOverride"] = model_config_override

        if request_overrides:
            payload.update({k: v for k, v in request_overrides.items() if v is not None})

        import json
        logger.debug(f"AppChatReverse payload: {json.dumps(payload, indent=4, ensure_ascii=False)}")

        return payload

    @staticmethod
    async def request(
        session: AsyncSession,
        token: str,
        message: str,
        model: str,
        mode: str = None,
        file_attachments: List[str] = None,
        tool_overrides: Dict[str, Any] = None,
        model_config_override: Dict[str, Any] = None,
        request_overrides: Dict[str, Any] = None,
    ) -> Any:
        """Send app chat request to Grok.
        
        Args:
            session: AsyncSession, the session to use for the request.
            token: str, the SSO token.
            message: str, the message to send.
            model: str, the model to use.
            mode: str, the mode to use.
            file_attachments: List[str], the file attachments to send.
            tool_overrides: Dict[str, Any], the tool overrides to use.
            model_config_override: Dict[str, Any], the model config override to use.

        Returns:
            Any: The response from the request.
        """
        try:
            # Build headers
            headers = build_headers(
                cookie_token=token,
                content_type="application/json",
                origin="https://grok.com",
                referer="https://grok.com/",
            )

            # Build payload
            payload = AppChatReverse.build_payload(
                message=message,
                model=model,
                mode=mode,
                file_attachments=file_attachments,
                tool_overrides=tool_overrides,
                model_config_override=model_config_override,
                request_overrides=request_overrides,
            )
            payload_summary = {
                "model": payload.get("modelName"),
                "mode": payload.get("modelMode"),
                "message_len": payload.get("message") or "",
                "file_attachments": len(payload.get("fileAttachments") or []),
                "custom_personality_len": len(payload.get("customPersonality") or ""),
            }
            logger.bind(grok_payload=payload_summary).debug(
                "AppChatReverse final Grok params (redacted)",
            )

            # Curl Config
            timeout = float(get_config("chat.timeout") or 0)
            if timeout <= 0:
                timeout = max(
                    float(get_config("video.timeout") or 0),
                    float(get_config("image.timeout") or 0),
                )
            browser = get_effective_browser()
            active_proxy_key = None

            async def _do_request():
                nonlocal active_proxy_key
                active_proxy_key, base_proxy = get_current_proxy_from("proxy.base_proxy_url")
                if base_proxy:
                    normalized_proxy = _normalize_chat_proxy(base_proxy)
                    scheme = urlparse(normalized_proxy).scheme.lower()
                    _log_proxy_state_once(base_proxy, normalized_proxy, scheme)
                else:
                    _log_proxy_state_once("")

                async def _post_once(
                    *,
                    direct_first: bool,
                    statsig_indices: tuple[int, int, int, int],
                    strip_proxy_clearance: bool = False,
                    include_verification_cookie: bool = False,
                    label: str = "",
                ):
                    nonlocal active_proxy_key
                    challenge = await build_app_chat_challenge(
                        cookie_token=token,
                        path="/rest/app-chat/conversations/new",
                        method="POST",
                        proxy_url=base_proxy,
                        statsig_indices=statsig_indices,
                        direct_first=direct_first,
                        strip_proxy_clearance=strip_proxy_clearance,
                        include_verification_cookie=include_verification_cookie,
                    )
                    request_headers = dict(headers)
                    request_headers.update(challenge.headers)
                    request_headers["Cookie"] = challenge.cookie

                    proxy, proxies, normalized_proxy, scheme = _chat_proxy_args(
                        challenge.proxy_url
                    )
                    if challenge.proxy_url:
                        _log_proxy_state_once(challenge.proxy_url, normalized_proxy, scheme)
                    else:
                        active_proxy_key = None
                        _log_proxy_state_once("")
                    response = await session.post(
                        CHAT_API,
                        headers=request_headers,
                        data=orjson.dumps(payload),
                        timeout=timeout,
                        stream=True,
                        proxy=proxy,
                        proxies=proxies,
                        impersonate=browser,
                    )
                    return response, challenge.proxy_url, label

                attempts = [
                    {
                        "label": "legacy-direct-first",
                        "direct_first": True,
                        "strip_proxy_clearance": False,
                        "include_verification_cookie": False,
                    }
                ]
                if base_proxy:
                    attempts.extend(
                        [
                            {
                                "label": "configured-proxy",
                                "direct_first": False,
                                "strip_proxy_clearance": False,
                                "include_verification_cookie": False,
                            },
                            {
                                "label": "configured-proxy-account-cookies",
                                "direct_first": False,
                                "strip_proxy_clearance": True,
                                "include_verification_cookie": False,
                            },
                            {
                                "label": "configured-proxy-verification-cookie",
                                "direct_first": False,
                                "strip_proxy_clearance": True,
                                "include_verification_cookie": True,
                            },
                        ]
                    )

                response = None
                used_label = ""
                failed_statuses: list[str] = []
                stop_attempting = False
                attempted_indices: set[tuple[int, int, int, int]] = set()

                async def _try_statsig_indices(
                    statsig_indices: tuple[int, int, int, int],
                ) -> bool:
                    nonlocal response, used_label, stop_attempting
                    attempted_indices.add(statsig_indices)
                    indices_label = format_statsig_indices(statsig_indices)
                    for attempt in attempts:
                        attempt_label = f"{attempt['label']}@statsig={indices_label}"
                        try:
                            response, used_proxy, used_label = await _post_once(
                                **attempt,
                                statsig_indices=statsig_indices,
                            )
                        except Exception as attempt_exc:
                            failed_statuses.append(f"{attempt_label}:error")
                            logger.warning(
                                f"AppChatReverse attempt {attempt_label} failed: {attempt_exc}"
                            )
                            continue
                        if response.status_code == 200:
                            stop_attempting = True
                            break
                        failed_statuses.append(
                            f"{attempt_label}:{response.status_code}"
                        )
                        if response.status_code != 403:
                            stop_attempting = True
                            break
                        logger.warning(
                            f"AppChatReverse attempt {attempt_label} returned 403; "
                            "trying next app-chat route"
                        )
                    if stop_attempting:
                        return True
                    return False

                for statsig_indices in statsig_index_candidates():
                    if await _try_statsig_indices(statsig_indices):
                        break

                if response is not None and response.status_code == 403:
                    refreshed_indices = await refresh_statsig_indices(
                        force=True,
                        reason="app-chat-403",
                    )
                    if (
                        refreshed_indices
                        and refreshed_indices not in attempted_indices
                    ):
                        logger.info(
                            "AppChatReverse retrying with refreshed statsig indices: "
                            f"{format_statsig_indices(refreshed_indices)}"
                        )
                        await _try_statsig_indices(refreshed_indices)

                if response is None:
                    raise UpstreamException(
                        message="AppChatReverse: Chat failed before request",
                        details={"attempts": failed_statuses},
                    )

                if response.status_code != 200:
                    content = await AppChatReverse._read_error_body(response)
                    if failed_statuses:
                        content = (
                            content + f"\nattempts={','.join(failed_statuses)}"
                        ).strip()
                    content_type = str(response.headers.get("content-type", ""))

                    logger.bind(error_type="UpstreamException").error(
                        f"AppChatReverse: Chat failed, {response.status_code}, "
                        f"content_type={content_type}, body={content[:500]}",
                    )
                    details = {"status": response.status_code, "body": content}
                    retry_after = _retry_after_for_response(
                        response.status_code, content
                    )
                    if retry_after is not None:
                        details["retry_after"] = retry_after
                    raise UpstreamException(
                        message=f"AppChatReverse: Chat failed, {response.status_code}",
                        details=details,
                    )

                return response

            def extract_status(e: Exception) -> Optional[int]:
                status = extract_status_for_retry(e)
                if status == 429:
                    return None
                return status

            async def _on_retry(attempt: int, status_code: int, error: Exception, delay: float):
                if active_proxy_key and should_rotate_proxy(status_code):
                    rotate_proxy(active_proxy_key)

            response = await retry_on_status(
                _do_request,
                extract_status=extract_status,
                on_retry=_on_retry,
            )

            # Stream response
            async def stream_response():
                try:
                    async for line in response.aiter_lines():
                        yield line
                finally:
                    await session.close()

            return stream_response()

        except Exception as e:
            # Handle upstream exception
            if isinstance(e, UpstreamException):
                status = None
                if e.details and "status" in e.details:
                    status = e.details["status"]
                else:
                    status = getattr(e, "status_code", None)
                if status == 401:
                    try:
                        await TokenService.record_fail(
                            token, status, "app_chat_auth_failed"
                        )
                    except Exception:
                        pass
                raise

            # Handle other non-upstream exceptions
            logger.bind(error_type=type(e).__name__).error(
                f"AppChatReverse: Chat failed, {str(e)}",
            )
            raise UpstreamException(
                message=f"AppChatReverse: Chat failed, {str(e)}",
                details={"status": 502, "error": str(e)},
            )


__all__ = ["AppChatReverse"]

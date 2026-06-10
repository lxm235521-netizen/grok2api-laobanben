"""Grok /c challenge helpers for app-chat requests."""

import asyncio
import base64
import hashlib
import math
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import orjson
from curl_cffi import CurlMime, requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from app.core.config import get_config
from app.core.logger import logger
from app.core.proxy_pool import normalize_proxy_url
from app.services.reverse.utils.browser import (
    get_effective_browser,
    get_effective_user_agent,
)
from app.services.reverse.utils.headers import _build_client_hints, build_sso_cookie

GROK_C_URL = "https://grok.com/c"
DEFAULT_BROWSER = "chrome142"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
DEFAULT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
ROUTER_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22c%22%2C%7B%22children%22%3A"
    "%5B%5B%22slug%22%2C%22%22%2C%22oc%22%5D%2C%7B%22children%22%3A"
    "%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D"
    "%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
SECP256K1_ORDER = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16
)
ACTION_NAMES = ("createAnonUser", "createChallenge", "setAnonCookies")
# Current Grok frontend chunk derives the animation fingerprint from these
# verification-token byte indices. Keep older known tuples as fallbacks because
# Grok changes this obfuscated client module without changing the /c protocol.
STATSIG_INDICES = (12, 8, 14, 47)
STATSIG_FALLBACK_INDICES = (
    STATSIG_INDICES,
    (41, 4, 23, 10),
    (12, 39, 5, 2),
    (4, 6, 44, 23),
)
STATSIG_EPOCH = 1682924400
STATSIG_KEYWORD = "obfiowerehiring"
ACTION_RE = re.compile(
    r'createServerReference\)\("([a-f0-9]{20,})",[^\)]*?"([^"]+)"\)'
)
SCRIPT_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
ANON_ID_RE = re.compile(r'"anonUserId"\s*:\s*"([^"]+)"')
META_DASH_RE = r"[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212]"
VERIFICATION_NAME_RE = rf"grok-site{META_DASH_RE}verification"
VERIFICATION_RE = re.compile(
    rf'"name"\s*:\s*"{VERIFICATION_NAME_RE}"\s*,\s*"content"\s*:\s*"([^"]+)"'
)
HEX_ROW_RE = re.compile(r"[^\d]+")


@dataclass(frozen=True)
class GrokChallengeResult:
    """Signed app-chat headers and cookies from the /c challenge."""

    headers: Dict[str, str]
    cookie: str
    proxy_url: Optional[str]


def _b64decode_padded(value: str) -> bytes:
    value = value.strip()
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _parse_statsig_indices(value: Any) -> Optional[tuple[int, int, int, int]]:
    if value in (None, "", []):
        return None
    try:
        if isinstance(value, str):
            parts = [
                item
                for item in re.split(r"[\s,]+", value.strip().strip("[]()"))
                if item
            ]
        elif isinstance(value, Iterable):
            parts = list(value)
        else:
            return None
        indices = tuple(int(item) for item in parts)
    except Exception:
        logger.warning(f"Invalid app.statsig_indices value: {value!r}")
        return None

    if len(indices) != 4 or any(index < 0 for index in indices):
        logger.warning(f"Invalid app.statsig_indices value: {value!r}")
        return None
    return indices  # type: ignore[return-value]


def statsig_index_candidates() -> tuple[tuple[int, int, int, int], ...]:
    """Return configured and known Grok frontend statsig index candidates."""
    candidates: list[tuple[int, int, int, int]] = []
    try:
        from app.services.reverse.utils.statsig_frontend import (
            get_dynamic_statsig_indices,
        )

        dynamic = get_dynamic_statsig_indices()
    except Exception:
        dynamic = None
    if dynamic:
        candidates.append(dynamic)
    configured = _parse_statsig_indices(get_config("app.statsig_indices"))
    if configured and configured not in candidates:
        candidates.append(configured)
    for indices in STATSIG_FALLBACK_INDICES:
        if indices not in candidates:
            candidates.append(indices)
    return tuple(candidates)


def format_statsig_indices(indices: tuple[int, int, int, int]) -> str:
    return ",".join(str(index) for index in indices)


def _cookie_dict_from_header(cookie: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in (cookie or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            result[name] = value
    return result


def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name)


def _status_error(stage: str, response: Any) -> RuntimeError:
    text = getattr(response, "text", "") or ""
    return RuntimeError(
        f"{stage} failed with status {response.status_code}: {text[:200]}"
    )


def _proxy_kwargs(proxy_url: Optional[str]) -> Dict[str, Any]:
    if not proxy_url:
        return {}
    normalized = normalize_proxy_url(proxy_url)
    scheme = normalized.split(":", 1)[0].lower()
    if scheme.startswith("socks"):
        return {"proxy": normalized}
    return {"proxies": {"http": normalized, "https": normalized}}


def _load_headers(cookie: str, user_agent: str) -> Dict[str, str]:
    headers = {
        "upgrade-insecure-requests": "1",
        "user-agent": user_agent,
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": DEFAULT_LANGUAGE,
        "priority": "u=0, i",
        "cookie": cookie,
    }
    for key, value in _build_client_hints(_browser(), user_agent).items():
        headers[key.lower()] = value
    return headers


def _action_headers(
    *,
    action_id: str,
    cookie: str,
    user_agent: str,
    baggage: str,
    sentry_trace_prefix: str,
    content_type: bool = True,
) -> Dict[str, str]:
    headers = {
        "next-action": action_id,
        "next-router-state-tree": ROUTER_STATE_TREE,
        "baggage": baggage,
        "sentry-trace": f"{sentry_trace_prefix}-{uuid.uuid4().hex[:16]}-0",
        "user-agent": user_agent,
        "accept": "text/x-component",
        "origin": "https://grok.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": GROK_C_URL,
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": DEFAULT_LANGUAGE,
        "priority": "u=1, i",
        "cookie": cookie,
    }
    for key, value in _build_client_hints(_browser(), user_agent).items():
        headers[key.lower()] = value
    if content_type:
        headers["content-type"] = "text/plain;charset=UTF-8"
    return headers


def _conversation_headers(
    *,
    baggage: str,
    sentry_trace_prefix: str,
    statsig_id: str,
) -> Dict[str, str]:
    return {
        "Baggage": baggage,
        "sentry-trace": f"{sentry_trace_prefix}-{uuid.uuid4().hex[:16]}-0",
        "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-00",
        "x-statsig-id": statsig_id,
        "x-xai-request-id": str(uuid.uuid4()),
    }


def _extract_meta(html: str, name: str) -> str:
    name_pattern = re.escape(name).replace(r"\-", META_DASH_RE)
    patterns = (
        rf'<meta\s+name="{name_pattern}"\s+content="([^"]*)"',
        rf'<meta\s+content="([^"]*)"\s+name="{name_pattern}"',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


def _script_urls(html: str) -> list[str]:
    urls: list[str] = []
    for src in SCRIPT_RE.findall(html):
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


def parse_action_ids(script_texts: Iterable[str]) -> Dict[str, str]:
    """Extract current Grok /c server action ids from Next.js chunks."""
    actions: Dict[str, str] = {}
    for text in script_texts:
        for action_id, name in ACTION_RE.findall(text or ""):
            if name in ACTION_NAMES:
                actions[name] = action_id
        if all(name in actions for name in ACTION_NAMES):
            break
    return actions


def _load_actions(session: requests.Session, html: str, proxy_url: Optional[str]) -> Dict[str, str]:
    script_texts: list[str] = []
    headers = {"user-agent": _user_agent(), "accept": "*/*"}
    for url in _script_urls(html):
        try:
            response = session.get(
                url,
                headers=headers,
                timeout=25,
                **_proxy_kwargs(proxy_url),
            )
        except Exception:
            continue
        if response.status_code != 200:
            continue
        text = response.text or ""
        if "createServerReference" not in text:
            continue
        script_texts.append(text)
        actions = parse_action_ids(script_texts)
        if all(name in actions for name in ACTION_NAMES):
            return actions
    return parse_action_ids(script_texts)


def _extract_anon_user_id(text: str) -> str:
    match = ANON_ID_RE.search(text or "")
    if match:
        return match.group(1)
    raise RuntimeError("createAnonUser response did not include anonUserId")


def _extract_challenge(content: bytes) -> bytes:
    marker = b":o86,"
    start = content.find(marker)
    if start < 0:
        raise RuntimeError("createChallenge response did not include challenge marker")
    start += len(marker)
    end = content.find(b"1:", start)
    if end < 0:
        raise RuntimeError("createChallenge response did not include challenge terminator")
    challenge = content[start:end]
    if not challenge:
        raise RuntimeError("createChallenge response included an empty challenge")
    return challenge


def _find_verification_token(text: str) -> str:
    match = VERIFICATION_RE.search(text or "")
    if match:
        return match.group(1)
    return _extract_meta(text or "", "grok-site-verification")


def _extract_verification_token(text: str) -> str:
    token = _find_verification_token(text)
    if token:
        return token
    raise RuntimeError("setAnonCookies response did not include grok-site-verification")


def _extract_json_array_after(text: str, marker: str) -> Any:
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"marker not found: {marker}")
    start = text.find("[", start)
    if start < 0:
        raise RuntimeError(f"array start not found after marker: {marker}")

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return orjson.loads(text[start : index + 1])

    raise RuntimeError(f"array end not found after marker: {marker}")


def _json_text_candidates(text: str) -> list[str]:
    candidates = [text or ""]
    if '\\"' in (text or ""):
        candidates.append((text or "").replace('\\"', '"'))
    return candidates


def _extract_curves_array(text: str) -> Any:
    last_error: Optional[Exception] = None
    for candidate in _json_text_candidates(text):
        try:
            return _extract_json_array_after(candidate, '"curves":')
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"curves array not found: {last_error}")


def build_svg_path(curves: list[dict[str, Any]]) -> str:
    """Build the legacy SVG path string from current Grok curve props."""
    parts: list[str] = []
    for entry in curves:
        color = entry["color"]
        bezier = entry["bezier"]
        parts.append(
            f" {color[0]},{color[1]} {color[2]},{color[3]} {color[4]},{color[5]} "
            f"h {entry['deg']} s {bezier[0]},{bezier[1]} {bezier[2]},{bezier[3]}"
        )
    return "M 10,30 C" + " C".join(parts)


def _path_rows(svg_path: str) -> list[list[int]]:
    rows: list[list[int]] = []
    for item in svg_path[9:].split("C"):
        values = [
            int(value)
            for value in HEX_ROW_RE.sub(" ", item).strip().split(" ")
            if value
        ]
        if values:
            rows.append(values)
    return rows


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def _solve(value: float, min_value: float, max_value: float, rounding: bool) -> float:
    result = (value * (max_value - min_value)) / 255 + min_value
    if rounding:
        return math.floor(result)
    return _js_round(result * 100) / 100


def _is_odd(index: int) -> float:
    return -1.0 if index % 2 else 0.0


class _Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    def calculate(self, a: float, b: float, m: float) -> float:
        return (
            3.0 * a * (1 - m) * (1 - m) * m
            + 3.0 * b * (1 - m) * m * m
            + m * m * m
        )

    def get_value(self, point: float) -> float:
        start_gradient = 0.0
        end_gradient = 0.0
        start = 0.0
        mid = 0.0
        end = 1.0

        if point <= 0.0:
            if self.curves[0] > 0.0:
                start_gradient = self.curves[1] / self.curves[0]
            elif self.curves[1] == 0.0 and self.curves[2] > 0.0:
                start_gradient = self.curves[3] / self.curves[2]
            return start_gradient * point

        if point >= 1.0:
            if self.curves[2] < 1.0:
                end_gradient = (self.curves[3] - 1.0) / (self.curves[2] - 1.0)
            elif self.curves[2] == 1.0 and self.curves[0] < 1.0:
                end_gradient = (self.curves[1] - 1.0) / (self.curves[0] - 1.0)
            return 1.0 + end_gradient * (point - 1.0)

        while start < end:
            mid = (start + end) / 2
            x_est = self.calculate(self.curves[0], self.curves[2], mid)
            if abs(point - x_est) < 0.00001:
                return self.calculate(self.curves[1], self.curves[3], mid)
            if x_est < point:
                start = mid
            else:
                end = mid

        return self.calculate(self.curves[1], self.curves[3], mid)


def _interpolate(from_values: list[float], to_values: list[float], factor: float) -> list[float]:
    return [
        from_value * (1 - factor) + to_value * factor
        for from_value, to_value in zip(from_values, to_values)
    ]


def _rotation_matrix(degrees: float) -> list[float]:
    radians = (degrees * math.pi) / 180
    cos = math.cos(radians)
    sin = math.sin(radians)
    return [cos, sin, -sin, cos, 0.0, 0.0]


def _float_to_hex(value: float) -> str:
    result: list[str] = []
    quotient = math.floor(value)
    fraction = value - quotient
    x = value

    while quotient > 0:
        quotient = math.floor(x / 16)
        remainder = math.floor(x - quotient * 16)
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = quotient

    if fraction == 0:
        return "".join(result)

    result.append(".")
    guard = 0
    while fraction > 0 and guard < 64:
        guard += 1
        fraction *= 16
        integer = math.floor(fraction)
        fraction -= integer
        result.append(chr(integer + 55) if integer > 9 else str(integer))

    return "".join(result)


def _animation_key(frame: list[int], target_time: float) -> str:
    from_color = [float(value) for value in frame[:3]] + [1.0]
    to_color = [float(value) for value in frame[3:6]] + [1.0]
    from_rotation = [0.0]
    to_rotation = [_solve(frame[6], 60.0, 360.0, True)]
    curves = [
        _solve(value, _is_odd(index), 1.0, False)
        for index, value in enumerate(frame[7:])
    ]
    value = _Cubic(curves).get_value(target_time)
    color = [max(item, 0.0) for item in _interpolate(from_color, to_color, value)]
    rotation = _interpolate(from_rotation, to_rotation, value)
    matrix = _rotation_matrix(rotation[0])

    parts = [format(_js_round(item), "x") for item in color[:-1]]
    for item in matrix:
        rounded = _js_round(item * 100) / 100
        if rounded < 0:
            rounded = -rounded
        hex_value = _float_to_hex(rounded)
        if hex_value.startswith("."):
            parts.append(f"0{hex_value}".lower())
        else:
            parts.append(hex_value or "0")

    return "".join(parts).replace(".", "").replace("-", "")


def build_animation_key(
    verification_token: str,
    svg_path: str,
    indices: tuple[int, int, int, int] = STATSIG_INDICES,
) -> str:
    """Build the animation fingerprint used by the x-statsig-id hash."""
    key_bytes = _b64decode_padded(verification_token)
    row_index = key_bytes[indices[0]] % 16
    frame_time = 1
    for index in indices[1:]:
        frame_time *= key_bytes[index] % 16
    rows = _path_rows(svg_path)
    if row_index >= len(rows):
        raise RuntimeError(f"animation row index out of range: {row_index}")
    return _animation_key(rows[row_index], frame_time / 4096)


def generate_x_statsig_id(
    path: str,
    method: str,
    verification_token: str,
    animation_key: str,
    *,
    now: Optional[float] = None,
) -> str:
    """Generate Grok's current x-statsig-id value."""
    key_bytes = _b64decode_padded(verification_token)
    timestamp = int((time.time() if now is None else now) - STATSIG_EPOCH)
    timestamp_bytes = timestamp.to_bytes(4, "little", signed=False)
    input_string = f"{method.upper()}!{path}!{timestamp}{STATSIG_KEYWORD}{animation_key}"
    digest = hashlib.sha256(input_string.encode()).digest()
    xor_key = secrets.randbelow(256)
    payload = key_bytes + timestamp_bytes + digest[:16] + b"\x03"
    output = bytes([xor_key]) + bytes(item ^ xor_key for item in payload)
    return base64.b64encode(output).decode().rstrip("=")


def _sign_challenge(challenge: bytes, private_key: ec.EllipticCurvePrivateKey) -> Dict[str, str]:
    signature_der = private_key.sign(challenge, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = utils.decode_dss_signature(signature_der)
    if s_value > SECP256K1_ORDER // 2:
        s_value = SECP256K1_ORDER - s_value
    signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    return {
        "challenge": base64.b64encode(challenge).decode(),
        "signature": base64.b64encode(signature).decode(),
    }


def _public_key_bytes(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )


def _user_agent() -> str:
    return str(get_effective_user_agent() or DEFAULT_USER_AGENT)


def _browser() -> str:
    return str(get_effective_browser() or DEFAULT_BROWSER)


def _build_with_proxy(
    *,
    cookie_token: str,
    path: str,
    method: str,
    proxy_url: Optional[str],
    statsig_indices: Optional[tuple[int, int, int, int]] = None,
    strip_proxy_clearance: bool = False,
    include_verification_cookie: bool = False,
) -> GrokChallengeResult:
    user_agent = _user_agent()
    initial_cookie = build_sso_cookie(cookie_token)
    if proxy_url and strip_proxy_clearance:
        # Clearance cookies are bound to the IP that obtained them. The
        # scheduler may have acquired them through a different egress route,
        # so this fallback can solve /c with only the account cookies.
        cookie_values = _cookie_dict_from_header(initial_cookie)
        initial_cookie = _cookie_header(
            {
                name: value
                for name, value in cookie_values.items()
                if name in {"sso", "sso-rw"}
            }
        )
    session = requests.Session(impersonate=_browser(), default_headers=False)
    session.cookies.update(_cookie_dict_from_header(initial_cookie))
    proxy_args = _proxy_kwargs(proxy_url)

    load_response = session.get(
        GROK_C_URL,
        headers=_load_headers(initial_cookie, user_agent),
        timeout=35,
        **proxy_args,
    )
    if load_response.status_code != 200:
        raise _status_error("load /c", load_response)
    html = load_response.text or ""
    page_verification_token = _find_verification_token(html)

    actions = _load_actions(session, html, proxy_url)
    missing = [name for name in ACTION_NAMES if not actions.get(name)]
    if missing:
        raise RuntimeError(f"failed to parse /c server actions: {missing}")

    baggage = _extract_meta(html, "baggage")
    sentry_trace_prefix = (_extract_meta(html, "sentry-trace").split("-", 1)[0]).strip()
    if not baggage or not sentry_trace_prefix:
        raise RuntimeError("failed to parse /c baggage or sentry trace")

    private_key = ec.generate_private_key(ec.SECP256K1())
    current_cookie = _cookie_header(
        {**_cookie_dict_from_header(initial_cookie), **session.cookies.get_dict()}
    )
    mime = CurlMime()
    try:
        mime.addpart(
            name="1",
            data=_public_key_bytes(private_key),
            filename="blob",
            content_type="application/octet-stream",
        )
        mime.addpart(name="0", filename=None, data='[{"userPublicKey":"$o1"}]')
        anon_response = session.post(
            GROK_C_URL,
            multipart=mime,
            headers=_action_headers(
                action_id=actions["createAnonUser"],
                cookie=current_cookie,
                user_agent=user_agent,
                baggage=baggage,
                sentry_trace_prefix=sentry_trace_prefix,
                content_type=False,
            ),
            timeout=35,
            **proxy_args,
        )
    finally:
        mime.close()
    if anon_response.status_code != 200:
        raise _status_error("createAnonUser", anon_response)

    anon_user_id = _extract_anon_user_id(anon_response.text or "")
    current_cookie = _cookie_header(
        {**_cookie_dict_from_header(initial_cookie), **session.cookies.get_dict()}
    )
    challenge_response = session.post(
        GROK_C_URL,
        data=orjson.dumps([{"anonUserId": anon_user_id}]),
        headers=_action_headers(
            action_id=actions["createChallenge"],
            cookie=current_cookie,
            user_agent=user_agent,
            baggage=baggage,
            sentry_trace_prefix=sentry_trace_prefix,
        ),
        timeout=35,
        **proxy_args,
    )
    if challenge_response.status_code != 200:
        raise _status_error("createChallenge", challenge_response)

    challenge = _extract_challenge(challenge_response.content)
    signed_challenge = _sign_challenge(challenge, private_key)
    current_cookie = _cookie_header(
        {**_cookie_dict_from_header(initial_cookie), **session.cookies.get_dict()}
    )
    verify_response = session.post(
        GROK_C_URL,
        data=orjson.dumps([{"anonUserId": anon_user_id, **signed_challenge}]),
        headers=_action_headers(
            action_id=actions["setAnonCookies"],
            cookie=current_cookie,
            user_agent=user_agent,
            baggage=baggage,
            sentry_trace_prefix=sentry_trace_prefix,
        ),
        timeout=35,
        **proxy_args,
    )
    if verify_response.status_code != 200:
        raise _status_error("setAnonCookies", verify_response)

    verify_text = verify_response.text or ""
    verification_token = _find_verification_token(verify_text)
    verification_source = "setAnonCookies"
    if not verification_token:
        verification_token = page_verification_token
        verification_source = "page"
    if not verification_token:
        raise RuntimeError(
            "setAnonCookies response and /c page did not include grok-site-verification"
        )

    verification_bytes = _b64decode_padded(verification_token)
    curves_source = "setAnonCookies"
    try:
        curves = _extract_curves_array(verify_text)
    except Exception:
        curves = _extract_curves_array(html)
        curves_source = "page"

    anim_index = verification_bytes[5] % 4
    if anim_index >= len(curves):
        raise RuntimeError(f"curves animation index out of range: {anim_index}")
    svg_path = build_svg_path(curves[anim_index])
    active_statsig_indices = statsig_indices or statsig_index_candidates()[0]
    animation_key = build_animation_key(
        verification_token,
        svg_path,
        indices=active_statsig_indices,
    )
    statsig_id = generate_x_statsig_id(path, method, verification_token, animation_key)
    final_cookie_values = {
        **_cookie_dict_from_header(initial_cookie),
        **session.cookies.get_dict(),
    }
    if include_verification_cookie:
        final_cookie_values.setdefault("grok-site-verification", verification_token)
    final_cookie = _cookie_header(final_cookie_values)

    logger.info(
        "Grok /c challenge solved: "
        f"actions=3, challenge_len={len(challenge)}, "
        f"verification_len={len(verification_token)}, "
        f"verification_source={verification_source}, curves_source={curves_source}, "
        f"statsig_indices={format_statsig_indices(active_statsig_indices)}, "
        f"proxy={'direct' if not proxy_url else 'configured'}"
    )
    return GrokChallengeResult(
        headers=_conversation_headers(
            baggage=baggage,
            sentry_trace_prefix=sentry_trace_prefix,
            statsig_id=statsig_id,
        ),
        cookie=final_cookie,
        proxy_url=proxy_url,
    )


async def build_app_chat_challenge(
    *,
    cookie_token: str,
    path: str,
    method: str = "POST",
    proxy_url: Optional[str] = None,
    statsig_indices: Optional[tuple[int, int, int, int]] = None,
    direct_first: bool = True,
    strip_proxy_clearance: bool = False,
    include_verification_cookie: bool = False,
) -> GrokChallengeResult:
    """Solve Grok's current app-chat challenge in a worker thread."""
    normalized = normalize_proxy_url(proxy_url) if proxy_url else None
    if direct_first:
        attempts: list[Optional[str]] = [None]
        if normalized and normalized not in attempts:
            attempts.append(normalized)
    else:
        attempts = [normalized] if normalized else [None]

    last_error: Optional[Exception] = None
    for attempt_proxy in attempts:
        try:
            return await asyncio.to_thread(
                _build_with_proxy,
                cookie_token=cookie_token,
                path=path,
                method=method,
                proxy_url=attempt_proxy,
                statsig_indices=statsig_indices,
                strip_proxy_clearance=strip_proxy_clearance,
                include_verification_cookie=include_verification_cookie,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Grok /c challenge attempt failed "
                f"via {'direct' if not attempt_proxy else 'configured proxy'}: {exc}"
            )

    raise RuntimeError(f"Grok /c challenge failed: {last_error}")


__all__ = [
    "GrokChallengeResult",
    "build_animation_key",
    "build_app_chat_challenge",
    "build_svg_path",
    "format_statsig_indices",
    "generate_x_statsig_id",
    "parse_action_ids",
    "statsig_index_candidates",
]

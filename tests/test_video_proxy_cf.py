from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.v1.function.video import _verify_sse_function_key
from app.core.proxy_pool import build_http_proxies, normalize_proxy_url
from app.services.cf_refresh.solver import (
    _browser_error_summary,
    _build_proxy_payload,
    _extract_browser_profile,
)
from app.services.reverse.utils.browser import coerce_user_agent_to_browser
from app.services.reverse.utils.grok_challenge import (
    _extract_curves_array,
    _extract_json_array_after,
    _find_verification_token,
    build_app_chat_challenge,
    build_animation_key,
    build_svg_path,
    generate_x_statsig_id,
    parse_action_ids,
)
from app.services.grok.services.model import ModelService


class _FakeRequest:
    def __init__(self, query_params):
        self.query_params = query_params


def test_build_http_proxies_normalizes_socks_dns_mode():
    proxy = "socks5://user:pass@example.test:1000"

    assert normalize_proxy_url(proxy) == "socks5h://user:pass@example.test:1000"
    assert build_http_proxies(proxy) == {
        "http": "socks5h://user:pass@example.test:1000",
        "https": "socks5h://user:pass@example.test:1000",
    }


def test_build_http_proxies_normalizes_socks4_dns_mode():
    proxy = "socks4://example.test:1000"

    assert normalize_proxy_url(proxy) == "socks4a://example.test:1000"
    assert build_http_proxies(proxy)["https"] == "socks4a://example.test:1000"


def test_flaresolverr_proxy_payload_preserves_url_auth():
    payload = _build_proxy_payload(
        "socks5://user%40tenant:pa%3Ass@example.test:1000"
    )

    assert payload == {
        "url": "socks5://user%40tenant:pa%3Ass@example.test:1000",
    }


def test_browser_error_summary_detects_chromium_network_error():
    summary = _browser_error_summary(
        {
            "response": (
                "<html><body>grok.com This site cannot be reached "
                "ERR_NO_SUPPORTED_PROXIES</body></html>"
            )
        }
    )

    assert "ERR_NO_SUPPORTED_PROXIES" in summary


def test_unsupported_flaresolverr_chrome_profile_falls_back_to_supported_profile():
    assert _extract_browser_profile("Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36") == "chrome142"


def test_user_agent_is_coerced_to_effective_browser_major():
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

    coerced = coerce_user_agent_to_browser(user_agent, "chrome142")

    assert "Chrome/142.0.0.0" in coerced
    assert "Chrome/148" not in coerced


@pytest.mark.asyncio
async def test_grok_challenge_tries_direct_before_configured_proxy():
    attempts = []

    def fake_build_with_proxy(**kwargs):
        attempts.append(kwargs.get("proxy_url"))
        raise RuntimeError("stop")

    with patch(
        "app.services.reverse.utils.grok_challenge._build_with_proxy",
        side_effect=fake_build_with_proxy,
    ):
        with pytest.raises(RuntimeError, match="Grok /c challenge failed"):
            await build_app_chat_challenge(
                cookie_token="token",
                path="/rest/app-chat/conversations/new",
                proxy_url="socks5://example.test:1000",
            )

    assert attempts == [None, "socks5h://example.test:1000"]


def test_grok_imagine_15_aliases_are_video_models():
    model_ids = [
        "grok-imagine-1.5",
        "grok-imagine-video-1.5",
        "grok-imagine-video-1.5-preview",
        "grok-imagine-video-1.5-2026-05-30",
    ]

    for model_id in model_ids:
        model = ModelService.get(model_id)
        assert model is not None
        assert model.is_video
        assert not model.is_image
        assert ModelService.pool_for_model(model_id) == "ssoSuper"


def test_grok_imagine_15_candidates_do_not_fallback_to_basic_pool():
    assert ModelService.pool_candidates_for_model("grok-imagine-1.5") == ["ssoSuper"]
    assert ModelService.pool_candidates_for_model("grok-imagine-1.0-video") == [
        "ssoBasic",
        "ssoSuper",
    ]


def test_video_sse_accepts_function_key_query_param():
    with patch("app.api.v1.function.video.get_function_api_key", return_value="fn-secret"):
        _verify_sse_function_key(_FakeRequest({"function_key": "fn-secret"}))
        _verify_sse_function_key(_FakeRequest({"function_key": "Bearer fn-secret"}))


def test_video_sse_rejects_missing_function_key_when_configured():
    with patch("app.api.v1.function.video.get_function_api_key", return_value="fn-secret"):
        with pytest.raises(HTTPException) as exc_info:
            _verify_sse_function_key(_FakeRequest({}))

    assert exc_info.value.status_code == 401


def test_video_sse_allows_public_function_when_enabled_without_key():
    with patch("app.api.v1.function.video.get_function_api_key", return_value=""):
        with patch("app.api.v1.function.video.is_function_enabled", return_value=True):
            _verify_sse_function_key(_FakeRequest({}))


def test_grok_challenge_parses_current_server_action_ids():
    script = (
        'createServerReference)("7f22e2455769948cde5e465c314e80203dcb5382ee",'
        'null,null,"createAnonUser");'
        'createServerReference)("7f281f8b311f9a4022578659cd33d6ada54cb0e01a",'
        'null,null,"createChallenge");'
        'createServerReference)("40130b23bcb85aafaeafb75db6530f418d99508e6f",'
        'null,null,"setAnonCookies");'
    )

    actions = parse_action_ids([script])

    assert actions["createAnonUser"].startswith("7f22")
    assert actions["createChallenge"].startswith("7f28")
    assert actions["setAnonCookies"].startswith("4013")


def test_grok_challenge_extracts_curves_array():
    text = '0:{"props":{"curves":[[{"color":[1,2,3,4,5,6],"deg":7,"bezier":[8,9,10,11]}]]},"x":1}'

    curves = _extract_json_array_after(text, '"curves":')

    assert curves[0][0]["color"] == [1, 2, 3, 4, 5, 6]
    assert curves[0][0]["bezier"] == [8, 9, 10, 11]


def test_grok_challenge_extracts_escaped_page_curves_array():
    text = (
        'self.__next_f.push([1,"{\\"curves\\":[[{\\"color\\":[1,2,3,4,5,6],'
        '\\"deg\\":7,\\"bezier\\":[8,9,10,11]}]],\\"css_class\\":\\"r-d5yoi\\"}"])'
    )

    curves = _extract_curves_array(text)

    assert curves[0][0]["color"] == [1, 2, 3, 4, 5, 6]
    assert curves[0][0]["deg"] == 7


def test_grok_challenge_finds_page_verification_meta():
    html = '<meta name="grok-site-verification" content="abc123"/>'

    assert _find_verification_token(html) == "abc123"


def test_grok_challenge_finds_page_verification_meta_with_dash_variant():
    html = '<meta name="grok-site\u2015verification" content="abc123"/>'

    assert _find_verification_token(html) == "abc123"


def test_grok_challenge_generates_statsig_shape_from_curves():
    curves = [
        {"color": [10, 20, 30, 40, 50, 60], "deg": 70, "bezier": [80, 90, 100, 110]}
        for _ in range(16)
    ]
    verification = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
    svg_path = build_svg_path(curves)
    animation_key = build_animation_key(verification, svg_path)

    statsig_id = generate_x_statsig_id(
        "/rest/app-chat/conversations/new",
        "POST",
        verification,
        animation_key,
        now=1682924400 + 123,
    )
    decoded = __import__("base64").b64decode(statsig_id + "=" * (-len(statsig_id) % 4))

    assert svg_path.startswith("M 10,30 C")
    assert animation_key
    assert "=" not in statsig_id
    assert len(decoded) == 70


def test_grok_challenge_uses_current_frontend_statsig_indices():
    curves = [
        {
            "color": [index + 1, index + 2, index + 3, index + 40, index + 50, index + 60],
            "deg": 70 + index,
            "bezier": [80 + index, 90 + index, 100 + index, 110 + index],
        }
        for index in range(16)
    ]
    verification = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
    svg_path = build_svg_path(curves)

    current_key = build_animation_key(verification, svg_path)
    previous_key = build_animation_key(
        verification,
        svg_path,
        indices=(12, 39, 5, 2),
    )

    assert current_key == build_animation_key(
        verification,
        svg_path,
        indices=(41, 4, 23, 10),
    )
    assert current_key != previous_key

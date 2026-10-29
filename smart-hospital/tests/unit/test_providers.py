"""Provider adapter compatibility, fallback and error-handling tests."""
from __future__ import annotations

import pytest

from backend.ai.providers import (
    PROVIDERS,
    ChatProvider,
    CompletionResult,
    GeminiProvider,
    GroqProvider,
    ProviderRouter,
    build_provider,
    provider_status,
)
from backend.core.config import Settings
from backend.core.errors import ProviderError

MESSAGES = [{"role": "user", "content": "What are the visiting hours?"}]


def _settings(**overrides) -> Settings:
    base = dict(
        gemini_api_key="", groq_api_key="",
        provider_order=["groq", "gemini"],
    )
    base.update(overrides)
    return Settings(**base)


def test_both_providers_are_registered():
    assert set(PROVIDERS) == {"gemini", "groq"}


def test_unknown_provider_rejected():
    with pytest.raises(ProviderError, match="Unknown provider"):
        build_provider("openai")


def test_provider_status_reports_capabilities():
    rows = provider_status(_settings())
    assert len(rows) == 2
    for row in rows:
        assert set(row) >= {"provider", "model", "configured", "tool_calling", "streaming"}
        assert row["configured"] is False


def test_configuration_detection():
    assert not GroqProvider(_settings()).is_configured()
    assert not GeminiProvider(_settings()).is_configured()
    assert GeminiProvider(_settings(gemini_api_key="k")).is_configured()
    assert GroqProvider(_settings(groq_api_key="k")).is_configured()


def test_unconfigured_provider_raises_clear_error():
    with pytest.raises(ProviderError, match="not configured"):
        GroqProvider(_settings()).complete(MESSAGES)


def test_router_without_providers_raises():
    with pytest.raises(ProviderError, match="No AI provider configured"):
        ProviderRouter(settings=_settings()).complete(MESSAGES)


class _StubProvider(ChatProvider):
    """Test double that can be made to fail deterministically."""

    def __init__(self, name: str, settings: Settings, fail: bool = False) -> None:
        super().__init__(settings)
        self.name = name
        self.fail = fail
        self.calls = 0

    @property
    def model(self) -> str:
        return f"{self.name}-model"

    def is_configured(self) -> bool:
        return True

    def complete(self, messages, *, max_tokens=None) -> CompletionResult:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} unavailable")
        return CompletionResult(text=f"answer from {self.name}", provider=self.name, model=self.model)


def test_router_falls_back_to_next_provider(monkeypatch):
    settings = _settings(max_retries=0)
    primary = _StubProvider("primary", settings, fail=True)
    secondary = _StubProvider("gemini", settings)
    router = ProviderRouter(["groq", "gemini"], settings)
    monkeypatch.setattr(router, "available", lambda: [primary, secondary])

    result = router.complete(MESSAGES)
    assert result.provider == "gemini"
    assert result.fallback_used
    assert primary.calls == 1 and secondary.calls == 1


def test_router_retries_before_failing_over(monkeypatch):
    settings = _settings(max_retries=2)
    primary = _StubProvider("primary", settings, fail=True)
    secondary = _StubProvider("groq", settings)
    router = ProviderRouter(["groq"], settings)
    monkeypatch.setattr(router, "available", lambda: [primary, secondary])

    router.complete(MESSAGES)
    assert primary.calls == 3  # 1 initial + 2 bounded retries


def test_router_raises_when_every_provider_fails(monkeypatch):
    settings = _settings(max_retries=0)
    providers = [_StubProvider(n, settings, fail=True) for n in ("primary", "gemini", "groq")]
    router = ProviderRouter(settings=settings)
    monkeypatch.setattr(router, "available", lambda: providers)

    with pytest.raises(ProviderError, match="All providers failed"):
        router.complete(MESSAGES)
    assert "primary" in router.last_error and "groq" in router.last_error


def test_non_streaming_provider_falls_back_to_single_chunk():
    settings = _settings()
    provider = _StubProvider("primary", settings)
    provider.capabilities.streaming = False
    chunks = list(provider.stream(MESSAGES))
    assert chunks == ["answer from primary"]


def test_token_accounting_is_populated():
    settings = _settings()
    provider = _StubProvider("primary", settings)
    result = provider.complete(MESSAGES)
    assert result.total_tokens == result.prompt_tokens + result.completion_tokens


def test_provider_order_is_configurable():
    router = ProviderRouter(["gemini", "groq"], _settings())
    assert router.order == ["gemini", "groq"]


def test_unknown_providers_in_order_are_ignored():
    router = ProviderRouter(["retired_gateway", "groq"], _settings())
    assert router.order == ["groq"]
    assert ProviderRouter(["retired_gateway"], _settings()).order == ["groq", "gemini"]


def test_unconfigured_router_explains_why():
    router = ProviderRouter(["groq", "gemini"], _settings())
    assert router.diagnose() == {
        "groq": "GROQ_API_KEY is not set in .env",
        "gemini": "GEMINI_API_KEY is not set in .env",
    }


class _FakeResponse:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": "Visiting hours are 11:00-13:00 [S1]."}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        }


def test_groq_sends_bearer_token_to_openai_compatible_endpoint(monkeypatch):
    import httpx

    captured = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None):
            captured.update(url=url, json=json, headers=headers)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _Client)
    provider = GroqProvider(_settings(groq_api_key="gsk_test", groq_model="llama-3.1-8b-instant"))
    result = provider.complete(MESSAGES)

    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer gsk_test"
    assert captured["json"]["model"] == "llama-3.1-8b-instant"
    assert captured["json"]["messages"] == MESSAGES
    assert result.text.startswith("Visiting hours")
    assert result.provider == "groq" and result.total_tokens == 20


def test_groq_http_error_is_reported_with_status(monkeypatch):
    import httpx

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None):
            request = httpx.Request("POST", url)
            return httpx.Response(401, text='{"error":"Invalid API Key"}', request=request)

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(ProviderError, match="HTTP 401"):
        GroqProvider(_settings(groq_api_key="bad")).complete(MESSAGES)




def test_proxy_block_page_is_reported_clearly(monkeypatch):
    import httpx

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None):
            return httpx.Response(
                403,
                text="<html>blocked</html>",
                headers={"server": "Zscaler/6.2", "content-type": "text/html"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(ProviderError, match="blocked by network proxy"):
        GroqProvider(_settings(groq_api_key="k")).complete(MESSAGES)

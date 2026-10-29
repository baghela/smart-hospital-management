"""Pluggable chat providers: Google Gemini and Groq.

Business logic only depends on :class:`ChatProvider`, so a provider can be
swapped through configuration without touching services. Capabilities are
declared explicitly instead of assuming every model supports tool calling,
structured output or streaming.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from backend.core.config import Settings, get_settings
from backend.core.errors import ProviderError
from backend.core.logging import get_logger
from backend.guardrails.policy import estimate_tokens

logger = get_logger("ai.providers")

Message = Dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}


def _ssl_verify(settings: Settings):
    """TLS verification for outbound AI calls.

    Order: explicit ``AI_CA_BUNDLE`` file -> OS trust store via ``truststore``
    (trusts corporate TLS-inspection CAs on Windows/macOS) -> httpx default.
    """
    if not settings.ai_verify_ssl:
        logger.warning("ai_ssl_verification_disabled", extra={"event": "config"})
        return False
    if settings.ai_ca_bundle:
        return settings.ai_ca_bundle
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


@dataclass
class Capabilities:
    tool_calling: bool = False
    structured_output: bool = False
    streaming: bool = False


@dataclass
class CompletionResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    fallback_used: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatProvider:
    """Interface implemented by every adapter."""

    name: str = "base"
    capabilities = Capabilities()

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def model(self) -> str:  # pragma: no cover - overridden
        return ""

    def is_configured(self) -> bool:  # pragma: no cover - overridden
        return False

    def unavailable_reason(self) -> str:
        return "" if self.is_configured() else f"{self.name} is not configured"

    def complete(self, messages: Sequence[Message], *, max_tokens: int | None = None) -> CompletionResult:
        raise NotImplementedError

    def stream(self, messages: Sequence[Message], *, max_tokens: int | None = None) -> Iterator[str]:
        """Default streaming: yield the full completion once (non-streaming models)."""
        yield self.complete(messages, max_tokens=max_tokens).text

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _join(messages: Sequence[Message]) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in messages)

    def _result(self, text: str, messages: Sequence[Message], started: float) -> CompletionResult:
        return CompletionResult(
            text=text.strip(),
            provider=self.name,
            model=self.model,
            prompt_tokens=estimate_tokens(self._join(messages)),
            completion_tokens=estimate_tokens(text),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


# --------------------------------------------------------------------------
# Gemini / Groq via their OpenAI-compatible REST endpoints (Bearer auth)
# --------------------------------------------------------------------------
class _OpenAICompatibleProvider(ChatProvider):
    """Calls ``POST {base_url}/chat/completions`` with ``Authorization: Bearer <key>``.

    Only ``httpx`` is required, so no vendor SDK has to be installed.
    """

    key_env: str = ""
    capabilities = Capabilities(tool_calling=True, structured_output=True, streaming=True)

    @property
    def api_key(self) -> str:  # pragma: no cover - overridden
        return ""

    @property
    def base_url(self) -> str:  # pragma: no cover - overridden
        return ""

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    def unavailable_reason(self) -> str:
        if not self.api_key:
            return f"{self.key_env} is not set in .env"
        if not self.base_url:
            return f"{self.name} base URL is empty"
        return ""

    def _endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def _client(self):
        import httpx

        return httpx.Client(timeout=self.settings.request_timeout_seconds, verify=_ssl_verify(self.settings))

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, messages: Sequence[Message], max_tokens: int | None, stream: bool = False) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": 0.1,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
        }
        if stream:
            payload["stream"] = True
        return payload

    def _fail(self, what: str, exc: Exception) -> ProviderError:
        import httpx

        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            server = response.headers.get("server", "")
            if "zscaler" in server.lower() or (
                response.status_code == 403 and "text/html" in response.headers.get("content-type", "")
            ):
                detail = (
                    f"HTTP {response.status_code}: blocked by network proxy ({server or 'unknown'}) - "
                    f"{self.base_url} is not allowed on this network. Ask IT to allow-list it or use an "
                    "approved network."
                )
            else:
                body = response.text[:300]
                if self.api_key:
                    body = body.replace(self.api_key, "***")
                detail = f"HTTP {response.status_code}: {body}"
        detail = detail.splitlines()[0][:300] if detail else type(exc).__name__
        logger.error(f"{self.name}_{what}_failed: {detail}", extra={"event": self.name})
        return ProviderError(f"{self.name} {what} failed: {detail}")

    def complete(self, messages: Sequence[Message], *, max_tokens: int | None = None) -> CompletionResult:
        reason = self.unavailable_reason()
        if reason:
            raise ProviderError(f"{self.name} is not configured: {reason}.")

        started = time.perf_counter()
        try:
            with self._client() as client:
                response = client.post(self._endpoint(), json=self._payload(messages, max_tokens), headers=self._headers())
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # network, auth, rate limit, bad JSON
            raise self._fail("call", exc) from exc

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise self._fail("response_parse", ValueError(f"unexpected payload: {str(data)[:200]}")) from exc

        result = self._result(text, messages, started)
        usage = data.get("usage") or {}
        result.prompt_tokens = int(usage.get("prompt_tokens") or result.prompt_tokens)
        result.completion_tokens = int(usage.get("completion_tokens") or result.completion_tokens)
        return result

    def stream(self, messages: Sequence[Message], *, max_tokens: int | None = None) -> Iterator[str]:
        reason = self.unavailable_reason()
        if reason:
            raise ProviderError(f"{self.name} is not configured: {reason}.")

        try:
            with self._client() as client:
                with client.stream(
                    "POST", self._endpoint(), json=self._payload(messages, max_tokens, stream=True), headers=self._headers()
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                        if delta:
                            yield delta
        except ProviderError:
            raise
        except Exception as exc:
            raise self._fail("stream", exc) from exc


class GroqProvider(_OpenAICompatibleProvider):
    name = "groq"
    key_env = "GROQ_API_KEY"

    @property
    def model(self) -> str:
        return self.settings.groq_model

    @property
    def api_key(self) -> str:
        return self.settings.groq_api_key

    @property
    def base_url(self) -> str:
        return self.settings.groq_base_url


class GeminiProvider(_OpenAICompatibleProvider):
    name = "gemini"
    key_env = "GEMINI_API_KEY"

    @property
    def model(self) -> str:
        return self.settings.gemini_model

    @property
    def api_key(self) -> str:
        return self.settings.gemini_api_key

    @property
    def base_url(self) -> str:
        return self.settings.gemini_base_url


# --------------------------------------------------------------------------
# Registry + resilient router
# --------------------------------------------------------------------------
PROVIDERS: Dict[str, type[ChatProvider]] = {
    "groq": GroqProvider,
    "gemini": GeminiProvider,
}


def build_provider(name: str, settings: Settings | None = None) -> ChatProvider:
    key = (name or "").strip().lower()
    if key not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{name}'. Available: {', '.join(PROVIDERS)}")
    return PROVIDERS[key](settings)


def provider_status(settings: Settings | None = None) -> List[dict]:
    settings = settings or get_settings()
    rows = []
    for key, cls in PROVIDERS.items():
        provider = cls(settings)
        rows.append(
            {
                "provider": key,
                "model": provider.model,
                "configured": provider.is_configured(),
                "tool_calling": provider.capabilities.tool_calling,
                "structured_output": provider.capabilities.structured_output,
                "streaming": provider.capabilities.streaming,
            }
        )
    return rows


class ProviderRouter:
    """Tries providers in order with bounded retries and records fallbacks."""

    def __init__(self, order: Optional[Iterable[str]] = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        requested = [p.strip().lower() for p in (order or self.settings.provider_order) if p.strip()]
        self.ignored = [p for p in requested if p not in PROVIDERS]
        if self.ignored:
            logger.warning(f"ignoring_unknown_providers: {', '.join(self.ignored)}", extra={"event": "config"})
        # Unknown names (e.g. a stale AI_PROVIDER_ORDER) never leave the chain empty.
        self.order = [p for p in requested if p in PROVIDERS] or list(PROVIDERS)
        self.last_error: str = ""

    def available(self) -> List[ChatProvider]:
        providers = []
        for name in self.order:
            if name in PROVIDERS:
                provider = PROVIDERS[name](self.settings)
                if not provider.unavailable_reason():
                    providers.append(provider)
        return providers

    def diagnose(self) -> Dict[str, str]:
        """Why each provider in the configured order is (un)usable."""
        report: Dict[str, str] = {}
        for name in self.order:
            if name not in PROVIDERS:
                report[name] = f"unknown provider (supported: {', '.join(PROVIDERS)})"
                continue
            report[name] = PROVIDERS[name](self.settings).unavailable_reason() or "ready"
        return report

    def complete(self, messages: Sequence[Message], *, max_tokens: int | None = None) -> CompletionResult:
        providers = self.available()
        if not providers:
            reasons = "; ".join(f"{k}: {v}" for k, v in self.diagnose().items())
            raise ProviderError(f"No AI provider configured. {reasons}")
        errors: List[str] = []
        for index, provider in enumerate(providers):
            for attempt in range(self.settings.max_retries + 1):
                try:
                    result = provider.complete(messages, max_tokens=max_tokens)
                    result.fallback_used = index > 0
                    if errors:
                        result.notes.append(f"fallback after: {'; '.join(errors)}")
                    return result
                except ProviderError as exc:
                    errors.append(f"{provider.name}:{exc.message}")
                    logger.warning(
                        f"provider_attempt_failed: {exc.message}",
                        extra={"event": provider.name, "resource": f"attempt{attempt}"},
                    )
                    time.sleep(min(0.5 * (attempt + 1), 2.0))
        self.last_error = "; ".join(errors)
        raise ProviderError(f"All providers failed: {self.last_error}")

    def stream(self, messages: Sequence[Message], *, max_tokens: int | None = None) -> Iterator[str]:
        providers = self.available()
        if not providers:
            raise ProviderError("No AI provider configured.")
        provider = providers[0]
        if not provider.capabilities.streaming:
            yield provider.complete(messages, max_tokens=max_tokens).text
            return
        try:
            yield from provider.stream(messages, max_tokens=max_tokens)
        except ProviderError:
            # Fall back to a non-streaming completion through the full chain.
            yield self.complete(messages, max_tokens=max_tokens).text


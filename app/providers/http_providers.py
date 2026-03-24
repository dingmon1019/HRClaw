from __future__ import annotations

from app.core.errors import ProviderError
from app.providers.base import BaseProvider
from app.providers.http_client import post_json
from app.schemas.providers import ProviderRequest, ProviderResponse
from app.schemas.settings import EffectiveSettings


class OpenAIProvider(BaseProvider):
    name = "openai"
    description = "OpenAI Chat Completions provider using an API key from the environment."
    profiles = ["strong"]
    supports_local = False
    supports_remote = True
    capabilities = ["text", "planning", "review"]

    def is_configured(self, settings: EffectiveSettings) -> bool:
        try:
            self.env_value(settings.api_key_env)
            return True
        except ProviderError:
            return False

    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        api_key = self.resolve_secret(request, settings.api_key_env)
        base_url = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
        response = self._post_chat_completion(
            url=f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            request=request,
            settings=settings,
        )
        return ProviderResponse(
            provider_name=self.name,
            model_name=request.model_name or settings.model,
            content=self._extract_openai_text(response),
            raw_response=response,
        )

    @staticmethod
    def _post_chat_completion(
        url: str,
        headers: dict[str, str],
        request: ProviderRequest,
        settings: EffectiveSettings,
    ) -> dict:
        payload = {
            "model": request.model_name or settings.model,
            "messages": [
                {"role": "system", "content": request.system_prompt or "You are a concise local agent planner."},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": 0.1,
        }
        merged_headers = {"Content-Type": "application/json", **headers}
        return post_json(url=url, headers=merged_headers, payload=payload, settings=settings)

    @staticmethod
    def _extract_openai_text(response: dict) -> str:
        choices = response.get("choices") or []
        if not choices:
            raise ProviderError("Provider returned no choices.")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "".join(text_parts).strip()
        raise ProviderError("Provider response did not include text content.")


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai-compatible"
    description = "OpenAI-compatible HTTP provider for local or self-hosted APIs."
    profiles = ["fast", "strong", "local-only"]
    supports_local = True
    supports_remote = True
    capabilities = ["text", "planning", "review"]

    def is_configured(self, settings: EffectiveSettings) -> bool:
        return bool(settings.base_url)

    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        base_url = (settings.base_url or "").rstrip("/")
        if not base_url:
            raise ProviderError("base_url is required for the openai-compatible provider.")
        headers: dict[str, str] = {}
        try:
            headers["Authorization"] = f"Bearer {self.resolve_secret(request, settings.api_key_env)}"
        except ProviderError:
            headers = {}
        response = self._post_chat_completion(
            url=f"{base_url}/chat/completions",
            headers=headers,
            request=request,
            settings=settings,
        )
        return ProviderResponse(
            provider_name=self.name,
            model_name=request.model_name or settings.model,
            content=self._extract_openai_text(response),
            raw_response=response,
        )


class GenericHTTPProvider(BaseProvider):
    name = "generic-http"
    description = "Generic JSON-over-HTTP provider for custom model gateways."
    profiles = ["fast", "cheap", "local-only"]
    supports_local = True
    supports_remote = True
    capabilities = ["text", "planning"]

    def is_configured(self, settings: EffectiveSettings) -> bool:
        return bool(settings.generic_http_endpoint or settings.base_url)

    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        endpoint = settings.generic_http_endpoint or settings.base_url
        if not endpoint:
            raise ProviderError("generic_http_endpoint or base_url must be configured.")
        payload = {
            "model": request.model_name or settings.model,
            "prompt": request.prompt,
            "system_prompt": request.system_prompt,
            "response_format": request.response_format,
        }
        headers = {"Content-Type": "application/json"}
        try:
            headers["Authorization"] = f"Bearer {self.resolve_secret(request, settings.api_key_env)}"
        except ProviderError:
            pass
        data = post_json(url=endpoint, headers=headers, payload=payload, settings=settings)
        content = (
            data.get("output_text")
            or data.get("text")
            or data.get("response")
            or self._extract_openai_like(data)
        )
        if not content:
            raise ProviderError("Generic HTTP provider response could not be parsed.")
        return ProviderResponse(
            provider_name=self.name,
            model_name=request.model_name or settings.model,
            content=content,
            raw_response=data,
        )

    @staticmethod
    def _extract_openai_like(response: dict) -> str:
        try:
            return OpenAIProvider._extract_openai_text(response)
        except ProviderError:
            return ""


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    description = "Anthropic Messages API provider."
    profiles = ["strong"]
    supports_local = False
    supports_remote = True
    capabilities = ["text", "planning", "review"]

    def is_configured(self, settings: EffectiveSettings) -> bool:
        try:
            self.env_value(self.base_settings.anthropic_api_key_env)
            return True
        except ProviderError:
            return False

    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        api_key = self.resolve_secret(request, self.base_settings.anthropic_api_key_env)
        payload = {
            "model": request.model_name or settings.model,
            "max_tokens": 512,
            "system": request.system_prompt or "You are a concise local agent planner.",
            "messages": [{"role": "user", "content": request.prompt}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        data = post_json(
            url="https://api.anthropic.com/v1/messages",
            headers=headers,
            payload=payload,
            settings=settings,
        )
        blocks = data.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict)).strip()
        if not text:
            raise ProviderError("Anthropic response did not include text.")
        return ProviderResponse(
            provider_name=self.name,
            model_name=request.model_name or settings.model,
            content=text,
            raw_response=data,
        )


class GeminiProvider(BaseProvider):
    name = "gemini"
    description = "Gemini generateContent provider."
    profiles = ["strong"]
    supports_local = False
    supports_remote = True
    capabilities = ["text", "planning", "review"]

    def is_configured(self, settings: EffectiveSettings) -> bool:
        try:
            self.env_value(self.base_settings.gemini_api_key_env)
            return True
        except ProviderError:
            return False

    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        api_key = self.resolve_secret(request, self.base_settings.gemini_api_key_env)
        model_name = request.model_name or settings.model
        payload = {
            "system_instruction": {
                "parts": [{"text": request.system_prompt or "You are a concise local agent planner."}]
            },
            "contents": [{"parts": [{"text": request.prompt}]}],
        }
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        )
        data = post_json(url=endpoint, headers={"content-type": "application/json"}, payload=payload, settings=settings)
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError("Gemini response did not include candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            raise ProviderError("Gemini response did not include text.")
        return ProviderResponse(
            provider_name=self.name,
            model_name=model_name,
            content=text,
            raw_response=data,
        )

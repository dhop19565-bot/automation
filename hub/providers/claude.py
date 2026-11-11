"""ClaudeProvider - real AI execution through the official Anthropic SDK.

The API key is read by the SDK from the server's environment. It is never
written to the database, the audit log, an error message or an API response.
"""
from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from .base import (
    MODE_REAL_AI,
    AIProvider,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderInfo,
    ProviderNotConfigured,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRateLimited,
    ProviderRefused,
    ProviderResult,
    ProviderTask,
    ProviderUnavailable,
)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
MAX_TOKENS = 16000
# Lets the API retry a safety-classifier decline on Anthropic's recommended
# fallback model inside the same call. Remove both lines to opt out.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeProvider(AIProvider):
    provider_id = "claude"

    def __init__(self, *, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
                 timeout_seconds: float = 300.0, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._model = model
        self._effort = effort
        self._timeout = timeout_seconds
        self._client = client

    def _configured(self) -> bool:
        return self._client is not None or any(os.environ.get(name) for name in _CREDENTIAL_ENV_VARS)

    def info(self) -> ProviderInfo:
        configured = self._configured()
        return ProviderInfo(
            provider_id=self.provider_id,
            label="Claude (Anthropic API)",
            execution_mode=MODE_REAL_AI,
            available=configured,
            description="Real AI execution. The fictional documents are sent to the Anthropic API from the server.",
            unavailable_reason=None if configured else "ANTHROPIC_API_KEY is not set on the server.",
            model=self._model,
        )

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._configured():
                raise ProviderNotConfigured("ANTHROPIC_API_KEY is not set on the server.")
            self._client = anthropic.AsyncAnthropic(timeout=self._timeout)
        return self._client

    async def run(self, task: ProviderTask) -> ProviderResult:
        client = self._get_client()
        user_content = (
            "Here is the data for this run as JSON.\n\n"
            f"<run_data>\n{json.dumps(task.payload, indent=2, sort_keys=True)}\n</run_data>\n\n"
            f"{task.request}"
        )
        try:
            response = await client.beta.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=task.instructions,
                output_config={
                    "effort": task.options.get("effort") or self._effort,
                    "format": {"type": "json_schema", "schema": task.output_schema},
                },
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.AuthenticationError as exc:
            raise ProviderAuthError("The Anthropic API rejected the server's credential.",
                                    provider_request_id=exc.request_id) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderAuthError("The server's Anthropic credential lacks permission for this request.",
                                    provider_request_id=exc.request_id) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimited("The Anthropic API rate limit was reached. Try again shortly.",
                                      provider_request_id=exc.request_id) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderBadRequest(f"The Anthropic API rejected the request: {exc.message}",
                                     provider_request_id=exc.request_id) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailable(f"The Anthropic API returned a server error ({exc.status_code}).",
                                          provider_request_id=exc.request_id) from exc
            raise ProviderBadRequest(f"The Anthropic API returned an error ({exc.status_code}): {exc.message}",
                                     provider_request_id=exc.request_id) from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderUnavailable("The Anthropic API did not respond in time.") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable("Could not reach the Anthropic API.") from exc

        request_id = getattr(response, "_request_id", None)
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise ProviderRefused(f"Claude declined the request (category: {category or 'unspecified'}).",
                                  provider_request_id=request_id)
        if response.stop_reason == "max_tokens":
            raise ProviderOutputTruncated("Claude's answer was cut off at the output token limit.",
                                          provider_request_id=request_id)

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise ProviderOutputInvalid("Claude returned no text content.", provider_request_id=request_id)
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderOutputInvalid("Claude's answer was not valid JSON.", provider_request_id=request_id) from exc
        if not isinstance(output, dict):
            raise ProviderOutputInvalid("Claude's answer was not a JSON object.", provider_request_id=request_id)

        notes: list[str] = []
        if response.model != self._model:
            notes.append(f"Served by {response.model} rather than the requested {self._model}.")
        return ProviderResult(
            output=output,
            execution_mode=MODE_REAL_AI,
            model=response.model,
            provider_request_id=request_id,
            usage=_usage(response.usage),
            notes=notes,
        )


def _usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {name: getattr(usage, name) for name in fields if getattr(usage, name, None) is not None}

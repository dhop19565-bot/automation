"""Provider contract.

A provider receives a ``ProviderTask`` - instructions, a JSON payload and the
JSON Schema its answer must satisfy - and returns a ``ProviderResult``. That is
the whole surface. The engine, the validators, the API and the frontend know
nothing else about where an answer came from, which is what lets a provider be
added or swapped without touching any of them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# How an answer was produced. Shown verbatim in the UI and the audit log.
MODE_REAL_AI = "real_ai"
MODE_SIMULATED = "simulated"
MODE_NOT_CONNECTED = "not_connected"


@dataclass(frozen=True)
class ProviderTask:
    run_id: str
    workflow: str
    instructions: str
    payload: dict[str, Any]
    output_schema: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict)
    # Closing sentence of the user message: what the provider is asked to return.
    request: str = "Return the journal proposal."


@dataclass(frozen=True)
class ProviderResult:
    output: dict[str, Any]
    execution_mode: str
    model: str | None = None
    provider_request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderInfo:
    provider_id: str
    label: str
    execution_mode: str
    available: bool
    description: str
    unavailable_reason: str | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "label": self.label,
            "execution_mode": self.execution_mode,
            "available": self.available,
            "description": self.description,
            "unavailable_reason": self.unavailable_reason,
            "model": self.model,
        }


class ProviderError(Exception):
    """A provider failure with a stable code. ``message`` is safe to store and show."""

    code = "PROVIDER_ERROR"
    retryable = False

    def __init__(self, message: str, *, provider_request_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.provider_request_id = provider_request_id


class ProviderNotConfigured(ProviderError):
    code = "PROVIDER_NOT_CONFIGURED"


class ProviderAuthError(ProviderError):
    code = "PROVIDER_AUTH_FAILED"


class ProviderBadRequest(ProviderError):
    code = "PROVIDER_BAD_REQUEST"


class ProviderRateLimited(ProviderError):
    code = "PROVIDER_RATE_LIMITED"
    retryable = True


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"
    retryable = True


class ProviderRefused(ProviderError):
    code = "PROVIDER_REFUSED"


class ProviderOutputTruncated(ProviderError):
    code = "PROVIDER_OUTPUT_TRUNCATED"


class ProviderOutputInvalid(ProviderError):
    code = "PROVIDER_OUTPUT_INVALID"


class AIProvider(ABC):
    provider_id: str

    @abstractmethod
    def info(self) -> ProviderInfo: ...

    @abstractmethod
    async def run(self, task: ProviderTask) -> ProviderResult: ...


class ProviderRegistry:
    def __init__(self, providers: list[AIProvider]) -> None:
        self._providers = {p.provider_id: p for p in providers}

    def get(self, provider_id: str) -> AIProvider | None:
        return self._providers.get(provider_id)

    def list(self) -> list[ProviderInfo]:
        return [p.info() for p in self._providers.values()]

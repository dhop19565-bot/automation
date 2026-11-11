"""CoworkProvider - placeholder for a future Microsoft Copilot integration.

Nothing here talks to Microsoft. There is no endpoint, no URL and no request
shape for Cowork in this file, because none has been confirmed against a
supported, documented Microsoft interface - and guessing one would be worse
than having none.

What exists is the seam. A real integration supplies a ``CoworkTransport``
(for example a thin wrapper around a published Copilot Studio agent) and the
provider turns on. See docs/cowork-integration.md.
"""
from __future__ import annotations

from typing import Any, Protocol

from .base import (
    MODE_NOT_CONNECTED,
    MODE_REAL_AI,
    AIProvider,
    ProviderInfo,
    ProviderNotConfigured,
    ProviderOutputInvalid,
    ProviderResult,
    ProviderTask,
)


class CoworkTransport(Protocol):
    """The only thing a real integration has to implement.

    ``invoke`` hands the task to the Microsoft-side workflow and returns its
    answer as a JSON object that satisfies ``task.output_schema``. How it gets
    there - SDK, connector, polling, callback - is the transport's business.
    """

    label: str

    async def invoke(self, task: ProviderTask) -> dict[str, Any]: ...


class CoworkProvider(AIProvider):
    provider_id = "cowork"

    def __init__(self, transport: CoworkTransport | None = None) -> None:
        self._transport = transport

    def info(self) -> ProviderInfo:
        connected = self._transport is not None
        return ProviderInfo(
            provider_id=self.provider_id,
            label="Cowork (Microsoft Copilot Studio)",
            execution_mode=MODE_REAL_AI if connected else MODE_NOT_CONNECTED,
            available=connected,
            description=(
                f"Connected through {self._transport.label}." if connected else
                "Placeholder. Reserved for a supported Copilot Studio workflow; not connected to any Microsoft service."
            ),
            unavailable_reason=None if connected else "No Cowork transport is configured on the server.",
        )

    async def run(self, task: ProviderTask) -> ProviderResult:
        if self._transport is None:
            raise ProviderNotConfigured(
                "The Cowork provider is a placeholder and is not connected to any Microsoft service."
            )
        output = await self._transport.invoke(task)
        if not isinstance(output, dict):
            raise ProviderOutputInvalid("The Cowork transport did not return a JSON object.")
        return ProviderResult(output=output, execution_mode=MODE_REAL_AI, model=self._transport.label)

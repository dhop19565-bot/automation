"""What the engine needs from a workflow. Add a workflow by implementing this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class InvalidInput(Exception):
    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []


class Workflow(ABC):
    name: str
    title: str
    checklist_task_key: str

    @abstractmethod
    def parse_input(self, raw: dict[str, Any]) -> Any:
        """Validate raw input. Raises ``InvalidInput``."""

    @abstractmethod
    def scope(self, data: Any) -> tuple[str, str]:
        """(entity, period) the run belongs to."""

    @abstractmethod
    def dump_input(self, data: Any) -> dict[str, Any]: ...

    @abstractmethod
    def load_input(self, stored: dict[str, Any]) -> Any: ...

    @abstractmethod
    def provider_task(self, data: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """(instructions, payload, output JSON schema) for the provider."""

    @abstractmethod
    def review(self, run_id: str, data: Any, provider_output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Turn raw provider output into (proposal for review, validation report).
        Raises ``ProviderOutputInvalid`` if the output does not fit the schema."""

    @abstractmethod
    def posting_batch(self, run: dict[str, Any]) -> dict[str, Any]:
        """Ledger batch built from an approved run."""

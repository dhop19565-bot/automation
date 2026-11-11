"""DemoProvider - offline, simulated workflows. No AI and no network.

Each workflow registers a simulator: a plain function that fabricates an answer
in the same shape a real model would return. Results are always labelled
``simulated`` so nobody mistakes a canned answer for AI output.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .base import (
    MODE_SIMULATED,
    AIProvider,
    ProviderBadRequest,
    ProviderInfo,
    ProviderResult,
    ProviderTask,
)

Simulator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class DemoProvider(AIProvider):
    provider_id = "demo"

    def __init__(self, *, latency_seconds: float = 0.6) -> None:
        self._simulators: dict[str, Simulator] = {}
        self._scenarios: dict[str, list[str]] = {}
        self._latency = latency_seconds

    def register(self, workflow: str, simulator: Simulator, scenarios: list[str]) -> None:
        self._simulators[workflow] = simulator
        self._scenarios[workflow] = scenarios

    def scenarios(self, workflow: str) -> list[str]:
        return list(self._scenarios.get(workflow, []))

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            provider_id=self.provider_id,
            label="Demo (offline simulation)",
            execution_mode=MODE_SIMULATED,
            available=True,
            description="Simulated. A scripted answer generated on the server; no AI model is called.",
            model=None,
        )

    async def run(self, task: ProviderTask) -> ProviderResult:
        simulator = self._simulators.get(task.workflow)
        if simulator is None:
            raise ProviderBadRequest(f"The demo provider has no simulation for workflow '{task.workflow}'.")
        scenario = task.options.get("demo_scenario") or "clean"
        if scenario not in self._scenarios[task.workflow]:
            raise ProviderBadRequest(f"Unknown demo scenario '{scenario}'.")
        if self._latency:
            await asyncio.sleep(self._latency)
        return ProviderResult(
            output=simulator(task.payload, {"demo_scenario": scenario}),
            execution_mode=MODE_SIMULATED,
            model=f"demo-script/{scenario}",
            provider_request_id=f"demo-{task.run_id}",
            notes=[f"Simulated answer (scenario: {scenario}). No AI model was called."],
        )

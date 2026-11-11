from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from hub.db import Database
from hub.engine import WorkflowEngine, utc_now
from hub.identity import DEMO_USERS
from hub.integrations.d365 import SimulatedD365Client
from hub.providers.base import MODE_REAL_AI, AIProvider, ProviderInfo, ProviderRegistry, ProviderResult, ProviderTask
from hub.providers.cowork import CoworkProvider
from hub.providers.demo import DemoProvider
from hub.workflows.derivatives import demo as derivatives_demo
from hub.workflows.derivatives.workflow import DerivativesWorkflow

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
WORKFLOW = DerivativesWorkflow.name

ALEX = DEMO_USERS["alex.preparer"]  # preparer only
SAM = DEMO_USERS["sam.controller"]  # preparer + approver
JO = DEMO_USERS["jo.reviewer"]  # approver only


def load_input() -> dict[str, Any]:
    return {
        "report": json.loads((FIXTURES / "derivatives_report_2026-10.json").read_text()),
        "trial_balance": json.loads((FIXTURES / "trial_balance_2026-10.json").read_text()),
    }


def clean_output(raw_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """A correct provider answer for the sample documents."""
    wf = DerivativesWorkflow()
    _, payload, _ = wf.provider_task(wf.parse_input(raw_input or load_input()))
    return derivatives_demo.simulate(payload, {"demo_scenario": "clean"})


class ScriptedProvider(AIProvider):
    """Test double: returns a fixed answer, raises a fixed error, or blocks until released."""

    provider_id = "scripted"

    def __init__(self, output: Any = None, error: Exception | None = None, hold: bool = False) -> None:
        self.output, self.error = output, error
        self.calls: list[ProviderTask] = []
        self.gate = asyncio.Event()
        if not hold:
            self.gate.set()

    def info(self) -> ProviderInfo:
        return ProviderInfo(self.provider_id, "Scripted", MODE_REAL_AI, True, "test double", model="scripted-1")

    async def run(self, task: ProviderTask) -> ProviderResult:
        self.calls.append(task)
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return ProviderResult(output=copy.deepcopy(self.output), execution_mode=MODE_REAL_AI, model="scripted-1",
                              provider_request_id="req_scripted", usage={"input_tokens": 10, "output_tokens": 20})


class SpyLedger:
    """Wraps the simulated ledger, counts calls and can fail on demand."""

    def __init__(self, inner: SimulatedD365Client) -> None:
        self.inner = inner
        self.integration_mode = inner.integration_mode
        self.system_label = inner.system_label
        self.calls: list[dict[str, Any]] = []
        self.fail_next = 0

    async def post_journal_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(batch)
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("simulated ledger outage")
        return await self.inner.post_journal_batch(batch)


class Harness:
    def __init__(self, db_path: str = ":memory:", scripted: ScriptedProvider | None = None,
                 extra_providers: list[AIProvider] | None = None, timeout: float = 5.0) -> None:
        self.db = Database(db_path)
        self.scripted = scripted or ScriptedProvider(output=clean_output())
        self.demo = DemoProvider(latency_seconds=0)
        self.demo.register(WORKFLOW, derivatives_demo.simulate, derivatives_demo.SCENARIOS)
        self.ledger = SpyLedger(SimulatedD365Client(self.db, utc_now))
        self.engine = WorkflowEngine(
            db=self.db,
            providers=ProviderRegistry([self.scripted, self.demo, CoworkProvider(), *(extra_providers or [])]),
            workflows=[DerivativesWorkflow()],
            ledger=self.ledger,
            provider_timeout_seconds=timeout,
        )

    async def submit(self, provider: str = "scripted", user=ALEX, raw_input: dict[str, Any] | None = None,
                     options: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.engine.submit(workflow=WORKFLOW, provider=provider, raw_input=raw_input or load_input(),
                                        user=user, options=options)

    async def submit_and_wait(self, **kwargs: Any) -> dict[str, Any]:
        run = await self.submit(**kwargs)
        await self.engine.wait_idle()
        return self.db.get_run(run["id"])

    def run(self, run_id: str) -> dict[str, Any]:
        return self.db.get_run(run_id)

    def events(self, run_id: str) -> list[str]:
        return [e["event"] for e in self.db.audit_for_run(run_id)]

    def checklist_item(self) -> dict[str, Any]:
        return next(i for i in self.db.get_checklist("FAH01", "2026-10") if i["task_key"] == "derivatives_revaluation")


@pytest.fixture
def raw_input() -> dict[str, Any]:
    return load_input()


@pytest.fixture
async def h() -> Harness:
    return Harness()

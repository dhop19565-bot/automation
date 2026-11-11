"""HTTP API and static frontend for the Finance Automation Hub.

The browser talks only to this API. It never sees a provider credential and
never calls a provider directly.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .engine import EngineError, WorkflowEngine, utc_now
from .identity import DEMO_USERS, User, resolve_user
from .integrations.d365 import LedgerPostingClient, SimulatedD365Client
from .noi import chat as noi_chat
from .noi.metrics import NoiError, Portfolio
from .providers.base import AIProvider, ProviderError, ProviderRegistry, ProviderTask
from .providers.claude import ClaudeProvider
from .providers.cowork import CoworkProvider
from .providers.demo import DemoProvider
from .workflows.derivatives import demo as derivatives_demo
from .workflows.derivatives.workflow import DerivativesWorkflow

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT.parent / "fixtures"

# Fields of a run that are safe and useful in a list view.
_SUMMARY_FIELDS = ("id", "workflow", "provider", "execution_mode", "status", "entity", "period",
                   "requested_by", "approved_by", "rejected_by", "validation_passed", "error_code",
                   "error_message", "created_at", "started_at", "completed_at", "updated_at")


class StartRunRequest(BaseModel):
    workflow: str
    provider: str
    input: dict[str, Any]
    options: dict[str, Any] = Field(default_factory=dict)


class ApproveRequest(BaseModel):
    proposal_hash: str
    comment: str = ""


class RejectRequest(BaseModel):
    reason: str


class NoiRenderRequest(BaseModel):
    view: dict[str, Any] = Field(default_factory=dict)
    widgets: list[dict[str, Any]] = Field(default_factory=list, max_length=60)


class NoiAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=noi_chat.MAX_QUESTION_CHARS)
    provider: str
    view: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


def build_providers(settings: Settings) -> list[AIProvider]:
    demo = DemoProvider(latency_seconds=settings.demo_latency_seconds)
    demo.register(DerivativesWorkflow.name, derivatives_demo.simulate, derivatives_demo.SCENARIOS)
    demo.register(noi_chat.WORKFLOW, noi_chat.simulate, ["clean"])
    return [
        ClaudeProvider(model=settings.claude_model, effort=settings.claude_effort,
                       timeout_seconds=settings.provider_timeout_seconds),
        demo,
        CoworkProvider(),
    ]


def create_app(settings: Settings | None = None, *, providers: list[AIProvider] | None = None,
               ledger: LedgerPostingClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    engine = WorkflowEngine(
        db=db,
        providers=ProviderRegistry(providers if providers is not None else build_providers(settings)),
        workflows=[DerivativesWorkflow()],
        ledger=ledger or SimulatedD365Client(db, utc_now),
        provider_timeout_seconds=settings.provider_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.recover()
        yield
        await engine.wait_idle()

    app = FastAPI(title="Finance Automation Hub", lifespan=lifespan)
    app.state.engine = engine
    app.state.db = db
    portfolio = Portfolio.load(FIXTURES / "noi_portfolio_2026-10.json")
    noi_schema = noi_chat.output_schema(portfolio)

    @app.exception_handler(NoiError)
    async def noi_error(_: Request, exc: NoiError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": {"code": "NOI_BAD_REQUEST", "message": str(exc)}})

    @app.exception_handler(EngineError)
    async def engine_error(_: Request, exc: EngineError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"error": {"code": exc.code, "message": exc.message, **exc.extra}})

    def current_user(x_hub_user: str | None = Header(default=None)) -> User:
        user = resolve_user(x_hub_user)
        if user is None:
            raise EngineError("UNAUTHENTICATED", "Unknown or missing user (X-Hub-User).", 401)
        return user

    def summary(run: dict[str, Any]) -> dict[str, Any]:
        return {key: run[key] for key in _SUMMARY_FIELDS}

    # ------------------------------------------------------------------ reference data

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        demo = engine.providers.get("demo")
        return {
            "identity_notice": "Demo identity only - users are selected, not authenticated.",
            "users": [u.to_dict() for u in DEMO_USERS.values()],
            "workflows": [{"name": w.name, "title": w.title} for w in engine.workflows.values()],
            "demo_scenarios": [
                {"id": s, "label": derivatives_demo.SCENARIO_LABELS[s]}
                for s in (demo.scenarios(DerivativesWorkflow.name) if isinstance(demo, DemoProvider) else [])
            ],
            "ledger": {"integration_mode": engine.ledger.integration_mode, "system": engine.ledger.system_label},
        }

    @app.get("/api/providers")
    async def providers_list() -> dict[str, Any]:
        return {"providers": [info.to_dict() for info in engine.providers.list()]}

    @app.get("/api/fixtures/derivatives")
    async def derivatives_fixture() -> dict[str, Any]:
        return {
            "report": json.loads((FIXTURES / "derivatives_report_2026-10.json").read_text()),
            "trial_balance": json.loads((FIXTURES / "trial_balance_2026-10.json").read_text()),
        }

    # ------------------------------------------------------------------ runs

    @app.post("/api/runs", status_code=202)
    async def start_run(body: StartRunRequest, user: User = Depends(current_user)) -> dict[str, Any]:
        run = await engine.submit(workflow=body.workflow, provider=body.provider, raw_input=body.input,
                                  user=user, options=body.options)
        return {"run": summary(run)}

    @app.get("/api/runs")
    async def list_runs() -> dict[str, Any]:
        return {"runs": [summary(r) for r in db.list_runs()]}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = db.get_run(run_id)
        if run is None:
            raise EngineError("RUN_NOT_FOUND", f"No run with id {run_id}.", 404)
        run.pop("dedupe_key", None)
        return {"run": run, "audit": db.audit_for_run(run_id)}

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str, body: ApproveRequest, user: User = Depends(current_user)) -> dict[str, Any]:
        run = await engine.approve(run_id, user, proposal_hash=body.proposal_hash, comment=body.comment)
        return {"run": summary(run)}

    @app.post("/api/runs/{run_id}/reject")
    async def reject(run_id: str, body: RejectRequest, user: User = Depends(current_user)) -> dict[str, Any]:
        run = await engine.reject(run_id, user, reason=body.reason)
        return {"run": summary(run)}

    @app.post("/api/runs/{run_id}/retry-posting", status_code=202)
    async def retry_posting(run_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        run = await engine.retry_posting(run_id, user)
        return {"run": summary(run)}

    # ------------------------------------------------------------------ checklist and audit

    @app.get("/api/checklist")
    async def checklist(entity: str | None = None, period: str | None = None) -> dict[str, Any]:
        scopes = db.checklist_scopes()
        if entity is None or period is None:
            if not scopes:
                fixture = json.loads((FIXTURES / "derivatives_report_2026-10.json").read_text())
                engine.ensure_checklist(fixture["entity"], fixture["period"])
                scopes = db.checklist_scopes()
            entity, period = scopes[0]["entity"], scopes[0]["period"]
        return {"entity": entity, "period": period, "scopes": scopes, "items": db.get_checklist(entity, period)}

    @app.get("/api/audit")
    async def audit(limit: int = 200) -> dict[str, Any]:
        return {"entries": db.recent_audit(max(1, min(limit, 1000))), "chain": db.verify_audit_chain()}

    # ------------------------------------------------------------------ NOI dashboard

    @app.get("/api/noi/meta")
    async def noi_meta() -> dict[str, Any]:
        return {**portfolio.meta(), "providers": [info.to_dict() for info in engine.providers.list()]}

    @app.post("/api/noi/render")
    async def noi_render(body: NoiRenderRequest) -> dict[str, Any]:
        """Figures for a set of widgets. One bad widget reports its own error; the rest still render."""
        view = portfolio.parse_view(body.view)
        results = []
        for raw in body.widgets:
            try:
                spec = portfolio.normalise_widget(raw)
                results.append({"spec": spec, "data": portfolio.resolve(spec, view)})
            except NoiError as exc:
                results.append({"error": str(exc)})
        return {"view": portfolio.view_dict(view), "widgets": results}

    @app.post("/api/noi/ask")
    async def noi_ask(body: NoiAskRequest, x_hub_user: str | None = Header(default=None)) -> dict[str, Any]:
        view = portfolio.parse_view(body.view)
        provider = engine.providers.get(body.provider)
        if provider is None:
            raise EngineError("UNKNOWN_PROVIDER", f"No provider '{body.provider}'.", 400)
        info = provider.info()
        if not info.available:
            raise EngineError("PROVIDER_NOT_AVAILABLE", info.unavailable_reason or f"{info.label} is not available.", 409)
        question = body.question.strip()
        ask_id = f"ASK-{uuid.uuid4().hex[:12].upper()}"
        actor = (resolve_user(x_hub_user) or User("dashboard", "Dashboard user", frozenset())).user_id
        task = ProviderTask(
            run_id=ask_id, workflow=noi_chat.WORKFLOW, instructions=noi_chat.SYSTEM_PROMPT,
            payload=noi_chat.build_payload(portfolio, view, question, noi_chat.clean_history(body.history)),
            output_schema=noi_schema, options={"effort": settings.noi_chat_effort},
            request=noi_chat.CLOSING_REQUEST)

        def audit(event: str, **detail: Any) -> None:
            db.append_audit(run_id=None, ts=utc_now(), actor=actor, event=event,
                            detail={"ask_id": ask_id, "provider": body.provider, **detail})

        audit("noi.question_asked", question=question, view=portfolio.view_dict(view))
        try:
            result = await asyncio.wait_for(provider.run(task), settings.noi_chat_timeout_seconds)
            answer, suggestions, notes = noi_chat.normalise_answer(portfolio, view, result.output)
        except asyncio.TimeoutError:
            audit("noi.question_failed", error_code="PROVIDER_TIMEOUT")
            raise EngineError("PROVIDER_TIMEOUT", "The assistant took too long to answer. Try again.", 504) from None
        except ProviderError as exc:
            audit("noi.question_failed", error_code=exc.code)
            raise EngineError(exc.code, exc.message, 502) from exc
        except NoiError as exc:
            audit("noi.question_failed", error_code="PROVIDER_OUTPUT_INVALID")
            raise EngineError("PROVIDER_OUTPUT_INVALID", str(exc), 502) from exc
        audit("noi.question_answered", execution_mode=result.execution_mode, model=result.model,
              provider_request_id=result.provider_request_id, usage=result.usage,
              suggestions=[s["kind"] for s in suggestions])
        return {"ask_id": ask_id, "question": question, "answer": answer, "suggestions": suggestions,
                "notes": [*result.notes, *notes], "answered_at": utc_now(),
                "provenance": {"provider": body.provider, "label": info.label,
                               "execution_mode": result.execution_mode, "model": result.model}}

    # ------------------------------------------------------------------ frontend

    @app.get("/noi")
    async def noi_page() -> FileResponse:
        return FileResponse(ROOT / "static" / "noi.html")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn hub.main:app_factory --factory``."""
    return create_app()

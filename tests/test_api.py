"""HTTP API: what the frontend sees, and what it must never see."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from hub.config import Settings
from hub.main import create_app

from .conftest import load_input

SECRET = "sk-ant-test-SECRET-VALUE-9f3a1c"
ALEX = {"X-Hub-User": "alex.preparer"}
SAM = {"X-Hub-User": "sam.controller"}
JO = {"X-Hub-User": "jo.reviewer"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = create_app(Settings(db_path=str(tmp_path / "api.sqlite3"), demo_latency_seconds=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.test") as http:
        http.app = app
        yield http
    await app.state.engine.wait_idle()


def body(provider: str = "demo", **options) -> dict:
    return {"workflow": "derivatives_revaluation", "provider": provider, "input": load_input(), "options": options}


async def settle(client: httpx.AsyncClient, run_id: str) -> dict:
    await client.app.state.engine.wait_idle()
    return (await client.get(f"/api/runs/{run_id}", headers=ALEX)).json()


async def test_providers_are_described_by_the_server(client):
    providers = {p["id"]: p for p in (await client.get("/api/providers")).json()["providers"]}
    assert set(providers) == {"claude", "demo", "cowork"}
    assert providers["claude"]["execution_mode"] == "real_ai" and providers["claude"]["available"] is False
    assert providers["demo"]["execution_mode"] == "simulated" and providers["demo"]["available"] is True
    assert providers["cowork"]["execution_mode"] == "not_connected" and providers["cowork"]["available"] is False


async def test_full_flow_over_http(client):
    started = await client.post("/api/runs", json=body(), headers=ALEX)
    assert started.status_code == 202
    run_id = started.json()["run"]["id"]
    assert started.json()["run"]["status"] == "queued"

    detail = await settle(client, run_id)
    run = detail["run"]
    assert run["status"] == "awaiting_approval" and run["execution_mode"] == "simulated"
    assert run["validation"]["passed"] is True and len(run["proposal"]["journals"]) == 5
    assert "dedupe_key" not in run

    refused = await client.post(f"/api/runs/{run_id}/approve", json={"proposal_hash": run["proposal_hash"]}, headers=ALEX)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "NOT_AN_APPROVER"

    approved = await client.post(f"/api/runs/{run_id}/approve",
                                 json={"proposal_hash": run["proposal_hash"], "comment": "ok"}, headers=JO)
    assert approved.status_code == 200

    done = await settle(client, run_id)
    assert done["run"]["status"] == "posted"
    assert done["run"]["posting"]["simulated"] is True and "SIMULATED" in done["run"]["posting"]["system"]
    assert [e["event"] for e in done["audit"]][-2:] == ["posting.completed", "checklist.updated"]

    checklist = (await client.get("/api/checklist")).json()
    line = next(i for i in checklist["items"] if i["task_key"] == "derivatives_revaluation")
    assert line["status"] == "complete" and line["run_id"] == run_id
    assert all(i["status"] == "not_started" for i in checklist["items"] if i["kind"] == "manual")
    assert (await client.get("/api/audit")).json()["chain"]["intact"] is True


async def test_duplicate_submission_returns_409_with_the_existing_run(client):
    first = (await client.post("/api/runs", json=body(), headers=ALEX)).json()["run"]["id"]
    again = await client.post("/api/runs", json=body(), headers=SAM)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "DUPLICATE_RUN" and again.json()["error"]["existing_run_id"] == first
    assert len((await client.get("/api/runs")).json()["runs"]) == 1


async def test_segregation_of_duties_over_http(client):
    run_id = (await client.post("/api/runs", json=body(), headers=SAM)).json()["run"]["id"]
    run = (await settle(client, run_id))["run"]
    own = await client.post(f"/api/runs/{run_id}/approve", json={"proposal_hash": run["proposal_hash"]}, headers=SAM)
    assert own.status_code == 403 and own.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"


async def test_failed_validation_cannot_be_approved_over_http(client):
    run_id = (await client.post("/api/runs", json=body(demo_scenario="calc_error"), headers=ALEX)).json()["run"]["id"]
    run = (await settle(client, run_id))["run"]
    assert run["status"] == "validation_failed"
    response = await client.post(f"/api/runs/{run_id}/approve", json={"proposal_hash": run["proposal_hash"]}, headers=JO)
    assert response.status_code == 409 and response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_unconfigured_and_placeholder_providers_cannot_be_launched(client):
    for provider in ("claude", "cowork"):
        response = await client.post("/api/runs", json=body(provider), headers=ALEX)
        assert response.status_code == 409 and response.json()["error"]["code"] == "PROVIDER_NOT_AVAILABLE"
    assert (await client.get("/api/runs")).json()["runs"] == []


async def test_requests_without_a_known_user_are_refused(client):
    assert (await client.post("/api/runs", json=body())).status_code == 401
    assert (await client.post("/api/runs", json=body(), headers={"X-Hub-User": "mallory"})).status_code == 401


async def test_invalid_documents_return_422_with_details(client):
    payload = body()
    payload["input"]["trial_balance"]["period"] = "2026-09"
    response = await client.post("/api/runs", json=payload, headers=ALEX)
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_INPUT"
    assert any("disagree on period" in d for d in response.json()["error"]["details"])


async def test_unknown_run_is_a_404(client):
    assert (await client.get("/api/runs/RUN-NOPE", headers=ALEX)).status_code == 404


async def test_api_key_never_reaches_the_browser(tmp_path, monkeypatch):
    """With a key configured, no endpoint and no static asset contains it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    app = create_app(Settings(db_path=str(tmp_path / "secret.sqlite3"), demo_latency_seconds=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.test") as http:
        run_id = (await http.post("/api/runs", json=body(), headers=ALEX)).json()["run"]["id"]
        await app.state.engine.wait_idle()
        providers = (await http.get("/api/providers")).json()["providers"]
        assert next(p for p in providers if p["id"] == "claude")["available"] is True
        paths = ["/", "/static/app.js", "/static/styles.css", "/api/meta", "/api/providers", "/api/runs",
                 f"/api/runs/{run_id}", "/api/checklist", "/api/audit", "/api/fixtures/derivatives", "/healthz"]
        for path in paths:
            response = await http.get(path, headers=ALEX)
            assert response.status_code == 200, path
            assert SECRET not in response.text, path
    database = Path(tmp_path / "secret.sqlite3").read_bytes()
    assert SECRET.encode() not in database


def test_frontend_calls_only_the_hub_api():
    """The browser must never call a provider or hold a credential."""
    static = Path(__file__).resolve().parent.parent / "hub" / "static"
    source = "".join(p.read_text() for p in static.iterdir())
    for forbidden in ("api.anthropic.com", "x-api-key", "Bearer", "sk-ant", "ANTHROPIC_API_KEY", "microsoft.com",
                      "dynamics.com"):
        assert forbidden not in source, forbidden
